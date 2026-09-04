# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import re
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from tt_burnin.load_ttx import CoreId
from tt_burnin.main import (
    BurninStopped,
    check_runtime_power_status,
    cleanup_can_relax_protection,
    parse_args,
    set_board_power_limit,
    set_burnin_power_state,
    set_host_aiclk_limit,
    set_tdp_limit,
    preflight_run_support,
    reset_all_devices,
    start_burnin_bh,
    stop_burnin_bh,
    wait_with_power_checks,
)


class FakeChip:
    def __init__(self):
        self.broadcasts = []
        self.writes = []
        self.block_writes = []
        self.block_broadcasts = []
        self.block_reads = []
        self.axi_writes = []
        self.messages = []

    def get_tensix_locations(self):
        return {(10, 2), (1, 2), (2, 3)}

    def noc_broadcast32(self, *args):
        self.broadcasts.append(args)

    def noc_write32(self, *args):
        self.writes.append(args)

    def noc_write(self, *args):
        self.block_writes.append(args)

    def noc_broadcast(self, *args):
        self.block_broadcasts.append(args)

    def noc_read(self, *args):
        self.block_reads.append(args[:-1])

    def axi_write32(self, *args):
        self.axi_writes.append(args)

    def arc_msg(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        return (0, 0)


class FakeRawBlackhole:
    def __init__(self):
        self.power = None
        self.messages = []
        self.telemetry_address = 0x1000
        self.telemetry = {
            53: 300,
            63: 1350,
            64: 150,
            70: 0,
            80: 0xE,
        }

    def as_bh(self):
        return self

    def is_remote(self):
        return False

    def get_pci_bdf(self):
        return "0000:41:00.0"

    def set_power(self, **kwargs):
        self.power = kwargs

    def arc_msg_buf(self, message):
        self.messages.append(message)
        if message[0] == 0x23:
            self.telemetry[70] = 0 if message[2] else message[1]
        elif message[0] == 0x24:
            self.telemetry[53] = 300 if message[2] else message[1]
        elif message[0] == 0x22:
            self.telemetry[64] = message[1]
        return [0] * 8

    def axi_read32(self, address):
        if address == 0x80030430:
            return self.telemetry_address
        return self.telemetry[(address - self.telemetry_address) // 4]

    def get_telemetry(self):
        telemetry = MagicMock()
        telemetry.board_power_limit = self.telemetry[53]
        telemetry.tdp_limit_max = self.telemetry[64]
        return telemetry


class MainTests(unittest.TestCase):
    def test_cli_defaults_to_one_core_per_second(self):
        with patch.object(sys, "argv", ["tt-burnin"]):
            args = parse_args()
        self.assertEqual(args.ramp_step, 1)
        self.assertEqual(args.ramp_interval, 1.0)
        self.assertIsNone(args.max_cores)

    def test_enter_can_stop_during_the_ramp_wait(self):
        with patch("tt_burnin.main.sys.stdin.read", return_value="\n"):
            with self.assertRaises(BurninStopped):
                wait_with_power_checks([], 10, check_stdin=True)

    def test_blackhole_power_profile_controls_optional_domains(self):
        device = FakeRawBlackhole()
        set_burnin_power_state(device)
        self.assertEqual(
            device.power,
            {
                "aiclk": True,
                "mrisc": False,
                "tensix": True,
                "l2cpu": False,
                "pcie": True,
            },
        )
        set_burnin_power_state(device, mrisc=True, l2cpu=True)
        self.assertEqual(device.power["mrisc"], True)
        self.assertEqual(device.power["l2cpu"], True)

    def test_host_aiclk_limit_is_set_and_restored(self):
        device = FakeRawBlackhole()
        set_host_aiclk_limit(device, 900)
        set_host_aiclk_limit(device)
        self.assertEqual(
            device.messages,
            [
                [0x23, 900, 0, 0, 0, 0, 0, 0],
                [0x23, 0, 1, 0, 0, 0, 0, 0],
            ],
        )

    def test_tdp_limit_uses_blackhole_runtime_message(self):
        device = FakeRawBlackhole()
        set_tdp_limit(device, 75)
        self.assertEqual(device.messages, [[0x22, 75, 0, 0, 0, 0, 0, 0]])

    def test_board_power_limit_is_set_and_verified(self):
        device = FakeRawBlackhole()
        set_board_power_limit(device, 300)
        self.assertEqual(device.messages, [[0x24, 300, 0, 0, 0, 0, 0, 0]])

    def test_aiclk_limit_fails_when_firmware_readback_does_not_change(self):
        device = FakeRawBlackhole()
        device.arc_msg_buf = MagicMock(return_value=[0] * 8)
        with patch("tt_burnin.main.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "telemetry reports"):
                set_host_aiclk_limit(device, 900)

    def test_preflight_rejects_bh_only_options_on_any_wormhole(self):
        args = MagicMock(
            aiclk_limit=900,
            tdp_limit=None,
            board_power_limit=300,
            enable_gddr=False,
            enable_l2cpu=False,
        )
        wormhole = MagicMock()
        wormhole.as_bh.return_value = None
        with self.assertRaisesRegex(RuntimeError, "every target"):
            preflight_run_support(args, [FakeRawBlackhole(), wormhole], "2.11.0")

    @patch(
        "tt_burnin.main.tenstorrent_pci_bdfs",
        return_value=["0000:41:00.0", "0000:42:00.0"],
    )
    def test_preflight_rejects_a_tenstorrent_function_detection_skipped(self, _bdfs):
        args = MagicMock(
            aiclk_limit=None,
            tdp_limit=None,
            board_power_limit=None,
            enable_gddr=False,
            enable_l2cpu=False,
        )
        with self.assertRaisesRegex(RuntimeError, "did not cover every"):
            preflight_run_support(args, [FakeRawBlackhole()], "2.11.0")

    def test_runtime_power_fault_aborts_the_workload(self):
        device = FakeRawBlackhole()
        device.telemetry[80] = (312 << 16) | 0xF
        with self.assertRaisesRegex(RuntimeError, "tripped at 312 W"):
            check_runtime_power_status([device])

    def test_runtime_power_policy_must_be_strict_fresh_and_ready(self):
        device = FakeRawBlackhole()
        device.telemetry[80] = 0xA
        with self.assertRaisesRegex(RuntimeError, "missing bits 0x4"):
            check_runtime_power_status([device])

    def test_cleanup_never_relaxes_protection_after_workload_failure(self):
        self.assertTrue(cleanup_can_relax_protection(None, []))
        self.assertFalse(
            cleanup_can_relax_protection(RuntimeError("containment tripped"), [])
        )
        self.assertFalse(cleanup_can_relax_protection(None, ["stop failed"]))

    @patch("tt_burnin.main.pci_board_reset")
    @patch("tt_burnin.main.get_board_type", return_value="p150a")
    def test_reset_configuration_cannot_silently_skip_a_device(
        self, _board_type, pci_board_reset
    ):
        devices = []
        for interface_id in (0, 1):
            device = MagicMock()
            device.board_id.return_value = 1
            device.is_remote.return_value = False
            device.get_pci_interface_id.return_value = interface_id
            devices.append(device)

        reset_config = {
            "wh_link_reset": {"pci_index": [0]},
            "re_init_devices": True,
        }
        with self.assertRaisesRegex(RuntimeError, "include every local"):
            reset_all_devices(devices, reset_config)
        pci_board_reset.assert_not_called()

    @patch("tt_burnin.main.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.main.get_driver_version", return_value="2.11.0")
    @patch("tt_burnin.main.load_ttx_file")
    @patch("tt_burnin.main.TtxFile")
    @patch("tt_burnin.main.path")
    def test_blackhole_loads_and_releases_only_selected_cores(
        self,
        resource_path,
        ttx_file,
        load_ttx_file,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        data_path = MagicMock()
        data_path.joinpath.return_value = "/tmp/bhpv.ttx"
        resource_path.return_value.__enter__.return_value = data_path
        ttx = object()
        ttx_file.return_value = ttx
        device = FakeChip()
        progress = []

        loaded_cores = start_burnin_bh(
            device,
            no_check=True,
            ramp_step=1,
            max_cores=2,
            after_batch=lambda released, total: progress.append((released, total)),
        )

        self.assertEqual(loaded_cores, {CoreId(1, 2), CoreId(10, 2)})

        load_ttx_file.assert_called_once_with(
            device,
            ttx,
            {CoreId(0, 0): {CoreId(1, 2), CoreId(10, 2)}},
            True,
        )
        self.assertEqual(
            device.writes,
            [
                (0, 1, 2, 0xFFB121B0, 1 << 18),
                (0, 10, 2, 0xFFB121B0, 1 << 18),
            ],
        )
        self.assertEqual(progress, [(1, 2), (2, 2)])
        self.assertEqual(len(device.broadcasts), 1)

    @patch("tt_burnin.main.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.main.get_driver_version", return_value="2.11.0")
    @patch("tt_burnin.main.load_ttx_file")
    def test_idle_does_not_load_the_power_virus(
        self,
        load_ttx_file,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        device = FakeChip()
        start_burnin_bh(device, idle=True, ramp_step=1, max_cores=1)
        load_ttx_file.assert_not_called()
        self.assertEqual(device.writes, [(0, 1, 2, 0xFFB121B0, 1 << 18)])

    @patch("tt_burnin.main.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.main.get_driver_version", return_value="2.11.0")
    @patch("tt_burnin.main.load_ttx_file")
    def test_blackhole_revalidates_policy_after_load_before_releasing_a_core(
        self,
        _load_ttx_file,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        device = FakeChip()

        def fail_policy_check():
            raise RuntimeError("policy stale")

        with self.assertRaisesRegex(RuntimeError, "policy stale"):
            start_burnin_bh(
                device,
                no_check=True,
                ramp_step=1,
                max_cores=1,
                before_release=fail_policy_check,
            )

        self.assertEqual(device.writes, [])

    @patch("tt_burnin.main.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.main.get_driver_version", return_value="2.11.0")
    def test_blackhole_stop_cooperatively_quiesces_without_reset_messages(
        self,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        device = FakeChip()
        device.get_tensix_locations = MagicMock(
            side_effect=AssertionError("cleanup must not discover or read endpoints")
        )

        stop_burnin_bh(device, {CoreId(1, 2)})

        all_compute_soft_reset = 0x7FFFF
        stream_reset_broadcasts = []
        for noc in (1, 0):
            for stream_id in (4, 5, 6):
                stream_base = 0xFFB40000 + stream_id * 0x1000
                stream_reset_broadcasts.append((noc, stream_base + 8, 0))
                stream_reset_broadcasts.append((noc, stream_base + 271 * 4, 1))
        self.assertEqual(
            device.broadcasts,
            [
                (1, 0xFFB121B0, all_compute_soft_reset),
                (0, 0xFFB121B0, all_compute_soft_reset),
            ]
            + stream_reset_broadcasts
            + [
                (1, 0xFFB121B0, all_compute_soft_reset),
                (0, 0xFFB121B0, all_compute_soft_reset),
            ],
        )
        device.get_tensix_locations.assert_not_called()
        self.assertEqual(device.block_broadcasts, [])
        self.assertEqual(device.block_reads, [])
        self.assertEqual(device.axi_writes, [])
        self.assertEqual(device.messages, [])

    def test_packaged_blackhole_power_virus_uses_only_quiesced_streams(self):
        ttx_path = Path(__file__).parents[1] / "tt_burnin" / "ttx" / "bhpv.ttx"
        with zipfile.ZipFile(ttx_path) as ttx:
            manifest = ttx.read("test.yaml")

        stream_ids = [
            int(stream_id)
            for stream_id in re.findall(rb"(?m)^\s+stream_id:\s*(\d+)\s*$", manifest)
        ]
        self.assertEqual(stream_ids, [4, 5, 6])

    @patch("tt_burnin.main.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.main.get_driver_version", return_value="2.11.0")
    def test_blackhole_stop_does_not_depend_on_firmware_reset_commands(
        self,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        device = FakeChip()

        device.arc_msg = MagicMock(
            side_effect=RuntimeError("firmware rejected message")
        )

        stop_burnin_bh(device, {CoreId(1, 2)})

        device.arc_msg.assert_not_called()
        self.assertEqual(device.axi_writes, [])

    @patch("tt_burnin.main.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.main.get_driver_version", return_value="2.11.0")
    def test_blackhole_stop_reasserts_soft_reset_after_a_quiesce_write_fails(
        self,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        device = FakeChip()
        real_broadcast = device.noc_broadcast32

        def fail_one_stream_write(*args):
            real_broadcast(*args)
            if args == (1, 0xFFB40000 + 4 * 0x1000 + 8, 0):
                raise RuntimeError("NOC1 stream write failed")

        device.noc_broadcast32 = fail_one_stream_write

        with self.assertRaisesRegex(RuntimeError, "NOC1 stream write failed"):
            stop_burnin_bh(device, {CoreId(1, 2)})

        self.assertEqual(device.block_broadcasts, [])
        self.assertEqual(device.block_reads, [])
        self.assertEqual(device.axi_writes, [])
        self.assertEqual(device.messages, [])
        self.assertEqual(
            device.broadcasts[-2:],
            [(1, 0xFFB121B0, 0x7FFFF), (0, 0xFFB121B0, 0x7FFFF)],
        )


if __name__ == "__main__":
    unittest.main()
