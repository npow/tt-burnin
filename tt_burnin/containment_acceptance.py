# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed Blackhole runtime-power containment acceptance test."""

from __future__ import annotations

import argparse
import gc
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from pyluwen import PciChip

from tt_burnin.main import (
    _BH_RUNTIME_POWER_FAULT_LATCHED,
    _BH_RUNTIME_POWER_POLICY_READY,
    _BH_RUNTIME_POWER_REQUIRED,
    _BH_TAG_BOARD_POWER_LIMIT,
    _BH_TAG_INPUT_POWER,
    _BH_TAG_RUNTIME_POWER_FAULT,
    check_runtime_power_status,
    read_bh_telemetry_tag,
)
from tt_burnin.utils import _tt_interface_ids, pci_board_reset, tenstorrent_pci_bdfs


DEFAULT_STATE_FILE = Path("/var/tmp/tt-burnin-containment-acceptance.json")
DEFAULT_LOG_FILE = Path("/var/tmp/tt-burnin-containment-acceptance.log")


def read_boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as state_file:
        state_file.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        state_file.flush()
        os.fsync(state_file.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def capture_devices() -> tuple[list[dict], list[PciChip]]:
    interface_ids = _tt_interface_ids()
    if not interface_ids:
        raise RuntimeError("No local Tenstorrent devices are visible")

    snapshots = []
    chips = []
    for interface_id in interface_ids:
        chip = PciChip(pci_interface=interface_id)
        if chip.as_bh() is None:
            raise RuntimeError(
                "Containment acceptance requires every local target to be Blackhole; "
                f"interface {interface_id} is not"
            )
        snapshots.append(
            {
                "interface_id": interface_id,
                "bdf": chip.get_pci_bdf(),
                "board_id": chip.board_id(),
                "board_power_limit": read_bh_telemetry_tag(
                    chip, _BH_TAG_BOARD_POWER_LIMIT
                ),
                "input_power": read_bh_telemetry_tag(chip, _BH_TAG_INPUT_POWER),
                "runtime_power_status": read_bh_telemetry_tag(
                    chip, _BH_TAG_RUNTIME_POWER_FAULT
                ),
            }
        )
        chips.append(chip)
    captured_bdfs = sorted(entry["bdf"] for entry in snapshots)
    enumerated_bdfs = tenstorrent_pci_bdfs()
    if captured_bdfs != enumerated_bdfs:
        raise RuntimeError(
            "Luwen detection did not cover every Tenstorrent PCI function: "
            f"sysfs={enumerated_bdfs}, detected={captured_bdfs}"
        )
    return snapshots, chips


def stable_identities(snapshots: list[dict]) -> list[tuple[str, int]]:
    return sorted((entry["bdf"], entry["board_id"]) for entry in snapshots)


def require_healthy_policy(
    snapshots: list[dict], chips: list[PciChip], expected_board_limit: int
) -> None:
    deadline = time.monotonic() + 5.0
    last_error = None
    while time.monotonic() < deadline:
        for entry, chip in zip(snapshots, chips):
            entry["board_power_limit"] = read_bh_telemetry_tag(
                chip, _BH_TAG_BOARD_POWER_LIMIT
            )
            entry["runtime_power_status"] = read_bh_telemetry_tag(
                chip, _BH_TAG_RUNTIME_POWER_FAULT
            )
        try:
            check_runtime_power_status(chips)
            wrong_limits = [
                f"{entry['bdf']}={entry['board_power_limit']} W"
                for entry in snapshots
                if entry["board_power_limit"] != expected_board_limit
            ]
            if wrong_limits:
                raise RuntimeError(
                    f"Expected a {expected_board_limit} W policy on every target; "
                    "found " + ", ".join(wrong_limits)
                )
            return
        except RuntimeError as error:
            last_error = error
            time.sleep(0.05)
    raise RuntimeError(f"Runtime power policy did not become ready: {last_error}")


def require_idle_headroom(
    snapshots: list[dict],
    chips: list[PciChip],
    low_limit: int,
    minimum_headroom: int,
    observation_seconds: float = 0.5,
) -> None:
    """Prove the low cap starts above steady idle, before changing policy."""
    deadline = time.monotonic() + observation_seconds
    maxima = [0] * len(chips)
    while True:
        for index, (entry, chip) in enumerate(zip(snapshots, chips)):
            power = read_bh_telemetry_tag(chip, _BH_TAG_INPUT_POWER)
            entry["input_power"] = power
            maxima[index] = max(maxima[index], power)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    unsafe = [
        f"{entry['bdf']}={maximum} W"
        for entry, maximum in zip(snapshots, maxima)
        if maximum + minimum_headroom > low_limit
    ]
    if unsafe:
        raise RuntimeError(
            f"The {low_limit} W deliberate trip limit lacks "
            f"{minimum_headroom} W of measured idle headroom: " + ", ".join(unsafe)
        )
    for entry, maximum in zip(snapshots, maxima):
        entry["idle_input_power_max"] = maximum


def require_containment_observed(
    snapshots: list[dict], expected_trip_floor: int | None = None
) -> None:
    contained = []
    required_while_latched = (
        _BH_RUNTIME_POWER_REQUIRED & ~_BH_RUNTIME_POWER_POLICY_READY
    )
    for entry in snapshots:
        status = entry["runtime_power_status"]
        if status & required_while_latched != required_while_latched:
            raise RuntimeError(
                f"Runtime policy stopped reporting strict/fresh on {entry['bdf']}: "
                f"status={status:#010x}"
            )
        if status & _BH_RUNTIME_POWER_FAULT_LATCHED:
            trip_watts = status >> 16
            if expected_trip_floor is not None and trip_watts < expected_trip_floor:
                raise RuntimeError(
                    f"Containment on {entry['bdf']} reported an invalid "
                    f"{trip_watts} W trip below the {expected_trip_floor} W limit"
                )
            contained.append(entry)
    if not contained:
        raise RuntimeError(
            "The low-limit workload exited without a latched firmware containment trip"
        )


def require_controlled_workload_crossing(log_text: str) -> None:
    """Reject a cap that tripped at policy setup rather than under workload."""
    if re.search(r"Device \d+: active Tensix cores [1-9]\d*/\d+", log_text) is None:
        raise RuntimeError(
            "Containment tripped before TT-Burnin released its first Tensix core; "
            "this does not prove a controlled workload crossing"
        )


def release_chips(chips: list[PciChip]) -> None:
    chips.clear()
    gc.collect()


def check_previous(state_file: Path) -> int:
    if not state_file.exists():
        print(f"No prior state file exists at {state_file}", file=sys.stderr)
        return 2
    state = json.loads(state_file.read_text())
    previous_boot = state.get("boot_id")
    current_boot = read_boot_id()
    if state.get("stage") == "passed":
        print(
            "PASS: the prior containment test completed on one unchanged boot "
            f"({previous_boot})."
        )
        return 0
    if previous_boot != current_boot:
        print(
            "FAIL: the host boot ID changed during or after the prior containment "
            f"test ({previous_boot} -> {current_boot}).",
            file=sys.stderr,
        )
        return 1
    print(
        f"FAIL: the prior test stopped at stage {state.get('stage')!r}; "
        "it did not produce an acceptance result.",
        file=sys.stderr,
    )
    return 1


def run_acceptance(args) -> None:
    if args.state_file.exists():
        prior = json.loads(args.state_file.read_text())
        if prior.get("stage") != "passed" and not args.replace_state:
            prior_boot = prior.get("boot_id")
            current_boot = read_boot_id()
            if prior_boot != current_boot:
                raise RuntimeError(
                    "A prior test did reboot the host: "
                    f"boot ID changed from {prior_boot} to {current_boot}"
                )
            raise RuntimeError(
                f"A prior test is incomplete at stage {prior.get('stage')!r}; "
                "inspect it or pass --replace-state to start a new run"
            )

    initial_boot_id = read_boot_id()
    initial, chips = capture_devices()
    try:
        require_healthy_policy(initial, chips, args.expected_board_limit)
        require_idle_headroom(
            initial,
            chips,
            args.low_limit,
            args.minimum_idle_headroom,
        )
    finally:
        release_chips(chips)

    state = {
        "stage": "preflight-complete",
        "boot_id": initial_boot_id,
        "devices": initial,
        "low_limit_watts": args.low_limit,
        "aiclk_mhz": args.aiclk,
        "log_file": str(args.log_file),
    }
    write_state(args.state_file, state)

    command = [
        args.tt_burnin,
        "--no-reset",
        "--board-power-limit",
        str(args.low_limit),
        "--aiclk-limit",
        str(args.aiclk),
        "--ramp-step",
        "1",
        "--ramp-interval",
        str(args.ramp_interval),
        "--duration",
        str(args.duration),
    ]
    state["stage"] = "low-limit-workload-started"
    state["command"] = command
    write_state(args.state_file, state)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log_file.open("w") as log_file:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )

    log_text = args.log_file.read_text(errors="replace")
    if result.returncode == 0:
        raise RuntimeError(
            "The deliberately low-cap workload did not abort; refusing any "
            "high-load acceptance phase"
        )
    if "firmware board-power containment tripped" not in log_text:
        raise RuntimeError(
            f"TT-Burnin failed for an unexpected reason; inspect {args.log_file}"
        )
    require_controlled_workload_crossing(log_text)
    state["stage"] = "workload-aborted-by-containment"
    state["workload_returncode"] = result.returncode
    write_state(args.state_file, state)

    if read_boot_id() != initial_boot_id:
        raise RuntimeError("Host boot ID changed during the containment workload")
    after_trip, chips = capture_devices()
    try:
        if stable_identities(after_trip) != stable_identities(initial):
            raise RuntimeError(
                "PCIe device identity changed during containment: "
                f"expected {stable_identities(initial)}, "
                f"observed {stable_identities(after_trip)}"
            )
        require_containment_observed(after_trip, args.low_limit)
    finally:
        interface_ids = [entry["interface_id"] for entry in after_trip]
        release_chips(chips)
    state["stage"] = "host-and-devices-live-after-trip"
    state["after_trip"] = after_trip
    write_state(args.state_file, state)

    pci_board_reset(interface_ids, reinit=False)
    after_reset, chips = capture_devices()
    try:
        if stable_identities(after_reset) != stable_identities(initial):
            raise RuntimeError(
                "Reset did not redetect the same device set: "
                f"expected {stable_identities(initial)}, "
                f"observed {stable_identities(after_reset)}"
            )
        require_healthy_policy(after_reset, chips, args.expected_board_limit)
    finally:
        release_chips(chips)

    if read_boot_id() != initial_boot_id:
        raise RuntimeError("Host boot ID changed during containment reset validation")
    state["stage"] = "passed"
    state["after_reset"] = after_reset
    write_state(args.state_file, state)
    print(
        "PASS: workload aborted on the deliberate low-cap trip, host boot ID "
        "remained unchanged, every BDF/board identity survived, and every "
        f"device redetected policy-ready/strict/fresh at {args.expected_board_limit} W."
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--run",
        action="store_true",
        help="Run the low-cap workload and reset acceptance sequence",
    )
    action.add_argument(
        "--check-previous",
        action="store_true",
        help="Check whether a prior persisted run crossed a host reboot",
    )
    parser.add_argument("--low-limit", type=int, default=100, metavar="WATTS")
    parser.add_argument("--aiclk", type=int, default=850, metavar="MHZ")
    parser.add_argument("--duration", type=float, default=30.0, metavar="SECONDS")
    parser.add_argument("--ramp-interval", type=float, default=0.1)
    parser.add_argument("--expected-board-limit", type=int, default=300)
    parser.add_argument(
        "--minimum-idle-headroom",
        type=int,
        default=10,
        metavar="WATTS",
        help="Required measured gap between steady idle and the deliberate trip cap",
    )
    parser.add_argument(
        "--tt-burnin",
        default=str(Path(sys.executable).with_name("tt-burnin")),
        metavar="PATH",
    )
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--log-file", type=Path, default=DEFAULT_LOG_FILE)
    parser.add_argument(
        "--replace-state",
        action="store_true",
        help="Replace an incomplete state file after it has been inspected",
    )
    args = parser.parse_args(argv)
    if not 50 <= args.low_limit < args.expected_board_limit:
        parser.error("--low-limit must be at least 50 W and below the expected limit")
    if args.duration <= 0 or args.ramp_interval < 0:
        parser.error("duration must be positive and ramp interval non-negative")
    if args.minimum_idle_headroom < 1:
        parser.error("minimum idle headroom must be positive")
    return args


def main() -> int:
    args = parse_args()
    if args.check_previous:
        return check_previous(args.state_file)
    try:
        run_acceptance(args)
    except Exception as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
