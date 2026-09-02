# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import sys
import unittest
from unittest.mock import MagicMock, patch

from tt_burnin.load_ttx import CoreId
from tt_burnin.main import (
    BurninStopped,
    parse_args,
    set_burnin_power_state,
    set_host_aiclk_limit,
    set_tdp_limit,
    start_burnin_bh,
    stop_burnin_bh,
    wait_with_power_checks,
)


class FakeChip:
    def __init__(self):
        self.broadcasts = []
        self.writes = []
        self.axi_writes = []
        self.messages = []

    def get_tensix_locations(self):
        return {(10, 2), (1, 2), (2, 3)}

    def noc_broadcast32(self, *args):
        self.broadcasts.append(args)

    def noc_write32(self, *args):
        self.writes.append(args)

    def axi_write32(self, *args):
        self.axi_writes.append(args)

    def arc_msg(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class FakeRawBlackhole:
    def __init__(self):
        self.power = None
        self.messages = []

    def as_bh(self):
        return self

    def set_power(self, **kwargs):
        self.power = kwargs

    def arc_msg_buf(self, message):
        self.messages.append(message)
        return [0] * 8


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

        start_burnin_bh(
            device,
            no_check=True,
            ramp_step=1,
            max_cores=2,
            after_batch=lambda released, total: progress.append((released, total)),
        )

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
    def test_blackhole_stop_resets_complete_tensix_tiles(
        self,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        device = FakeChip()

        stop_burnin_bh(device)

        soft_reset = (1 << 11) | (1 << 12) | (1 << 13) | (1 << 14) | (1 << 18)
        self.assertEqual(
            device.broadcasts,
            [
                (0, 0xFFB121B0, soft_reset),
                (1, 0xFFB121B0, soft_reset),
            ],
        )
        reset_addresses = [0x80030040 + index * 4 for index in range(8)]
        self.assertEqual(
            device.axi_writes,
            [(address, 0) for address in reset_addresses]
            + [(address, 0xFFFFFFFF) for address in reset_addresses],
        )
        self.assertEqual(
            device.messages,
            [
                ((0x33,), {"arg0": 250, "arg1": 0}),
                ((0xAF,), {}),
                ((0x20,), {}),
                ((0x33,), {"arg0": 0, "arg1": 0}),
            ],
        )

    @patch("tt_burnin.main.is_driver_version_at_least", return_value=True)
    @patch("tt_burnin.main.get_driver_version", return_value="2.11.0")
    def test_blackhole_stop_releases_forced_aiclk_after_reset_failure(
        self,
        _get_driver_version,
        _is_driver_version_at_least,
    ):
        device = FakeChip()

        def fail_full_tensix_reset(*args, **kwargs):
            device.messages.append((args, kwargs))
            if args == (0xAF,):
                raise RuntimeError("reset failed")

        device.arc_msg = fail_full_tensix_reset

        with self.assertRaisesRegex(RuntimeError, "reset failed"):
            stop_burnin_bh(device)

        self.assertEqual(device.messages[-1], ((0x33,), {"arg0": 0, "arg1": 0}))


if __name__ == "__main__":
    unittest.main()
