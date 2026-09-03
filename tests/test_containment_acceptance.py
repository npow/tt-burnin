# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from tt_burnin.containment_acceptance import (
    check_previous,
    parse_args,
    require_containment_observed,
    require_controlled_workload_crossing,
    require_idle_headroom,
    stable_identities,
    write_state,
)


class ContainmentAcceptanceTests(unittest.TestCase):
    def test_containment_requires_a_latched_trip(self):
        snapshots = [
            {
                "bdf": "0000:41:00.0",
                "board_id": 1,
                "runtime_power_status": 0xE,
            }
        ]
        with self.assertRaisesRegex(RuntimeError, "without a latched"):
            require_containment_observed(snapshots)

        snapshots[0]["runtime_power_status"] = (117 << 16) | 0x7
        require_containment_observed(snapshots, 100)

    def test_identity_comparison_ignores_interface_reindexing(self):
        before = [{"interface_id": 0, "bdf": "0000:41:00.0", "board_id": 7}]
        after = [{"interface_id": 4, "bdf": "0000:41:00.0", "board_id": 7}]
        self.assertEqual(stable_identities(before), stable_identities(after))

    def test_low_limit_must_be_below_expected_limit(self):
        with self.assertRaises(SystemExit):
            parse_args(
                [
                    "--run",
                    "--low-limit",
                    "300",
                    "--expected-board-limit",
                    "300",
                ]
            )

    def test_low_limit_requires_measured_idle_headroom(self):
        snapshots = [{"bdf": "0000:41:00.0"}]
        chip = object()
        with (
            patch(
                "tt_burnin.containment_acceptance.read_bh_telemetry_tag",
                return_value=91,
            ),
            patch("tt_burnin.containment_acceptance.time.sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "idle headroom"):
                require_idle_headroom(
                    snapshots,
                    [chip],
                    low_limit=100,
                    minimum_headroom=10,
                    observation_seconds=0,
                )

        with (
            patch(
                "tt_burnin.containment_acceptance.read_bh_telemetry_tag",
                return_value=85,
            ),
            patch("tt_burnin.containment_acceptance.time.sleep"),
        ):
            require_idle_headroom(
                snapshots,
                [chip],
                low_limit=100,
                minimum_headroom=10,
                observation_seconds=0,
            )
        self.assertEqual(snapshots[0]["idle_input_power_max"], 85)

    def test_containment_must_follow_a_released_workload_core(self):
        with self.assertRaisesRegex(RuntimeError, "before TT-Burnin released"):
            require_controlled_workload_crossing(
                "Blackhole device 0 firmware board-power containment tripped"
            )
        require_controlled_workload_crossing(
            "Device 1: active Tensix cores 1/120\n"
            "Blackhole device 0 firmware board-power containment tripped"
        )

    def test_persisted_incomplete_marker_detects_a_reboot(self):
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            write_state(
                state_file,
                {"stage": "low-limit-workload-started", "boot_id": "old-boot"},
            )
            with patch(
                "tt_burnin.containment_acceptance.read_boot_id",
                return_value="new-boot",
            ):
                self.assertEqual(check_previous(state_file), 1)


if __name__ == "__main__":
    unittest.main()
