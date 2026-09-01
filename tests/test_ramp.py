# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import unittest

from tt_burnin.ramp import (
    BoardPowerLimitExceeded,
    check_power_limits,
    core_batches,
    ordered_tensix_cores,
    release_tensix_cores,
)


class FakeDevice:
    def __init__(self, input_power=0):
        self.input_power = input_power
        self.writes = []

    def noc_write32(self, *args):
        self.writes.append(args)

    def get_telemetry(self):
        return type("Telemetry", (), {"input_power": self.input_power})()


class RampTests(unittest.TestCase):
    def test_orders_by_row_to_spread_first_batch_across_columns(self):
        cores = {(10, 3), (2, 2), (1, 3), (11, 2)}
        self.assertEqual(
            ordered_tensix_cores(cores),
            [(2, 2), (11, 2), (1, 3), (10, 3)],
        )

    def test_max_cores_is_applied_after_ordering(self):
        self.assertEqual(
            ordered_tensix_cores({(3, 2), (1, 2), (2, 2)}, max_cores=2),
            [(1, 2), (2, 2)],
        )

    def test_batches_support_staged_and_legacy_modes(self):
        cores = [(1, 2), (2, 2), (3, 2)]
        self.assertEqual(core_batches(cores, 2), [[(1, 2), (2, 2)], [(3, 2)]])
        self.assertEqual(core_batches(cores, 0), [cores])

    def test_release_only_touches_selected_cores_and_reports_progress(self):
        device = FakeDevice()
        progress = []
        release_tensix_cores(
            device,
            [(1, 2), (2, 2), (3, 2)],
            soft_reset_value=0x40000,
            batch_size=2,
            after_batch=lambda released, total: progress.append((released, total)),
        )
        self.assertEqual(
            device.writes,
            [
                (0, 1, 2, 0xFFB121B0, 0x40000),
                (0, 2, 2, 0xFFB121B0, 0x40000),
                (0, 3, 2, 0xFFB121B0, 0x40000),
            ],
        )
        self.assertEqual(progress, [(2, 3), (3, 3)])

    def test_power_limit_checks_every_board(self):
        self.assertEqual(
            check_power_limits([FakeDevice(80), FakeDevice(120)], 150),
            [80.0, 120.0],
        )
        with self.assertRaisesRegex(
            BoardPowerLimitExceeded, "Device 1 reached 150.0 W"
        ):
            check_power_limits([FakeDevice(80), FakeDevice(150)], 150)

    def test_total_power_limit_checks_the_host_sum(self):
        with self.assertRaisesRegex(
            BoardPowerLimitExceeded, "reached 200.0 W total input power"
        ):
            check_power_limits(
                [FakeDevice(80), FakeDevice(120)],
                max_board_power=None,
                max_total_board_power=200,
            )


if __name__ == "__main__":
    unittest.main()
