# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import sys
import unittest
from unittest.mock import MagicMock, patch

from tt_burnin.load_ttx import CoreId
from tt_burnin.main import (
    BurninStopped,
    parse_args,
    start_burnin_bh,
    wait_with_power_checks,
)


class FakeChip:
    def __init__(self):
        self.broadcasts = []
        self.writes = []
        self.messages = []

    def get_tensix_locations(self):
        return {(10, 2), (1, 2), (2, 3)}

    def noc_broadcast32(self, *args):
        self.broadcasts.append(args)

    def noc_write32(self, *args):
        self.writes.append(args)

    def arc_msg(self, message):
        self.messages.append(message)


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


if __name__ == "__main__":
    unittest.main()
