# SPDX-FileCopyrightText: © 2024 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0
"""
Tenstorrent Burnin (TT-Burnin) is a command line utility
to run a high power consumption workload on TT devices.
"""
from __future__ import annotations

import os
import sys
import time
import argparse
import gc
import tt_burnin
from rich.live import Live
from rich.text import Text
from rich.console import Group
from importlib.resources import path
from tt_burnin.chip import WhChip, RemoteWhChip, BhChip
from tt_burnin.load_ttx import load_ttx_file, read_bin_image_chunks, TtxFile, CoreId
from tt_burnin.ramp import (
    BoardPowerLimitExceeded,
    check_power_limits,
    ordered_tensix_cores,
    release_tensix_cores,
)
from tt_tools_common.ui_common.themes import CMD_LINE_COLOR
from tt_burnin.utils import (
    parse_reset_input,
    mobo_reset_from_json,
    pci_indices_from_json,
    pci_board_reset,
    print_all_available_devices,
    generate_table,
    get_board_type,
    reset_6u_glx,
)
from tt_tools_common.utils_common.system_utils import (
    get_driver_version,
    is_driver_version_at_least,
)
from tt_tools_common.utils_common.tools_utils import (
    detect_chips_with_callback,
)


def reset_all_devices(devices, reset_filename=None):
    """Reset all devices"""
    print(CMD_LINE_COLOR.BLUE, "Resetting devices on host...", CMD_LINE_COLOR.ENDC)
    LOG_FOLDER = os.path.expanduser("~/.config/tenstorrent")
    log_filename = f"{LOG_FOLDER}/reset_config.json"
    if not devices:
        print(
            CMD_LINE_COLOR.RED,
            "No devices detected. Exiting...",
            CMD_LINE_COLOR.ENDC,
        )
        sys.exit(1)
    # Check board type and reset accordingly
    board_id = hex(devices[0].board_id()).replace("0x", "")
    board_type = get_board_type(board_id)
    if board_type == "tt-galaxy-wh" or board_type == "tt-galaxy-bh":
        # Perform a full galaxy reset and detect chips post reset
        reset_6u_glx()
        return

    # If input is just reset board
    if not reset_filename:
        log_filename = reset_filename
    data = parse_reset_input(log_filename)
    if data:
        # reset using the json file
        parsed_dict = mobo_reset_from_json(data)
        pci_indices, reinit = pci_indices_from_json(parsed_dict)
        if pci_indices:
            pci_board_reset(pci_indices, reinit)
    else:
        # reset all boards
        dev_ids = []
        for device in devices:
            if not device.is_remote():
                dev_ids.append(device.get_pci_interface_id())
        pci_board_reset(dev_ids, reinit=True)


def start_burnin_wh(
    device,
    keep_trisc_under_reset: bool = False,
    stagger_start: bool = False,
    no_check: bool = False,
    idle: bool = False,
    ramp_step: int = 0,
    max_cores: int | None = None,
    after_batch=None,
):
    BRISC_SOFT_RESET = 1 << 11
    TRISC_SOFT_RESETS = (1 << 12) | (1 << 13) | (1 << 14)
    NCRISC_SOFT_RESET = 1 << 18
    STAGGERED_START_ENABLE = (1 << 31) if stagger_start else 0

    # Put tensix under soft reset
    device.noc_broadcast32(
        0, 0xFFB121B0, BRISC_SOFT_RESET | TRISC_SOFT_RESETS | NCRISC_SOFT_RESET
    )

    # Deassert riscv reset
    device.arc_msg(0xBA)

    # Go busy
    device.arc_msg(0x52)

    all_cores = {CoreId(*core) for core in device.get_tensix_locations()}
    selected_cores = ordered_tensix_cores(all_cores, max_cores)

    if not idle:
        with path("tt_burnin", "") as data_path:
            load_ttx_file(
                device,
                TtxFile(str(data_path.joinpath("ttx/whpv.ttx"))),
                {CoreId(0, 0): set(selected_cores)},
                no_check,
            )

    if keep_trisc_under_reset:
        soft_reset_value = (
            NCRISC_SOFT_RESET | TRISC_SOFT_RESETS | STAGGERED_START_ENABLE
        )
    else:
        soft_reset_value = NCRISC_SOFT_RESET | STAGGERED_START_ENABLE

    # Preserve the legacy broadcast for API callers that do not request staging.
    if ramp_step == 0 and len(selected_cores) == len(all_cores):
        device.noc_broadcast32(0, 0xFFB121B0, soft_reset_value)
        if after_batch is not None:
            after_batch(len(selected_cores), len(selected_cores))
    else:
        release_tensix_cores(
            device,
            selected_cores,
            soft_reset_value,
            ramp_step,
            after_batch,
        )


def stop_burnin_wh(device):
    BRISC_SOFT_RESET = 1 << 11
    TRISC_SOFT_RESETS = (1 << 12) | (1 << 13) | (1 << 14)
    NCRISC_SOFT_RESET = 1 << 18

    # Go idle
    device.arc_msg(0x54)

    # Put tensix back under soft reset
    device.noc_broadcast32(
        0, 0xFFB121B0, BRISC_SOFT_RESET | TRISC_SOFT_RESETS | NCRISC_SOFT_RESET
    )


def start_burnin_bh(
    device,
    keep_trisc_under_reset: bool = False,
    stagger_start: bool = False,
    no_check: bool = False,
    idle: bool = False,
    ramp_step: int = 0,
    max_cores: int | None = None,
    after_batch=None,
):
    BRISC_SOFT_RESET = 1 << 11
    TRISC_SOFT_RESETS = (1 << 12) | (1 << 13) | (1 << 14)
    NCRISC_SOFT_RESET = 1 << 18
    STAGGERED_START_ENABLE = (1 << 31) if stagger_start else 0

    # Put tensix under soft reset
    device.noc_broadcast32(
        0, 0xFFB121B0, BRISC_SOFT_RESET | TRISC_SOFT_RESETS | NCRISC_SOFT_RESET
    )

    # We only send GO_BUSY/GO_IDLE on BH if kmd < 2.6.0
    driver = get_driver_version()
    if not is_driver_version_at_least(driver, "2.6.0"):
        # GO_BUSY message (power management interface prior to KMD v2.6.0, FW v18.12.0)
        device.arc_msg(0x52)

    all_cores = {CoreId(*core) for core in device.get_tensix_locations()}
    selected_cores = ordered_tensix_cores(all_cores, max_cores)

    if not idle:
        with path("tt_burnin", "") as data_path:
            load_ttx_file(
                device,
                TtxFile(str(data_path.joinpath("ttx/bhpv.ttx"))),
                {CoreId(0, 0): set(selected_cores)},
                no_check,
            )

    if keep_trisc_under_reset:
        soft_reset_value = (
            NCRISC_SOFT_RESET | TRISC_SOFT_RESETS | STAGGERED_START_ENABLE
        )
    else:
        soft_reset_value = NCRISC_SOFT_RESET | STAGGERED_START_ENABLE

    # Preserve the legacy broadcast for API callers that do not request staging.
    if ramp_step == 0 and len(selected_cores) == len(all_cores):
        device.noc_broadcast32(0, 0xFFB121B0, soft_reset_value)
        if after_batch is not None:
            after_batch(len(selected_cores), len(selected_cores))
    else:
        release_tensix_cores(
            device,
            selected_cores,
            soft_reset_value,
            ramp_step,
            after_batch,
        )

    return set(selected_cores)


def scrub_burnin_bh(device, loaded_cores):
    """Erase and verify the packaged BHPV image while all RISCs are reset."""
    with path("tt_burnin", "") as data_path:
        with TtxFile(str(data_path.joinpath("ttx/bhpv.ttx"))) as ttx:
            for image_name in ("0-0/image.bin", "0-0/ckernels.bin"):
                for address, data in read_bin_image_chunks(ttx.open(image_name)):
                    cleared = bytes(len(data))
                    device.noc_broadcast(0, address, cleared)
                    for core in sorted(loaded_cores):
                        # Check both ends of every cleared region on every
                        # functional core. A zeroed entry point prevents the
                        # retained image from executing; checking the tail also
                        # catches truncated broadcast writes.
                        for offset in (0, len(data) - 4):
                            observed = bytearray(4)
                            device.noc_read(0, *core, address + offset, observed)
                            if observed != bytes(4):
                                raise RuntimeError(
                                    f"Failed to scrub BHPV from core {core} "
                                    f"at 0x{address + offset:x}"
                                )


def stop_burnin_bh(device, loaded_cores=None):
    """Stop BHPV and clear all state that could restart the workload.

    Merely asserting the per-RISC soft-reset register is insufficient for an
    infinite-loop power virus: stream and math-engine state lives outside the
    RISC cores and can survive a later power-state transition.  Follow the
    full-Tensix reset sequence used by the Blackhole firmware end-to-end reset
    test so opening the device again cannot resume the old workload.
    """
    TT_SMC_MSG_REINIT_TENSIX = 0x20
    TT_SMC_MSG_FORCE_AICLK = 0x33
    TT_SMC_MSG_TOGGLE_TENSIX_RESET = 0xAF
    TENSIX_RISC_RESET_ADDRS = tuple(0x80030040 + index * 4 for index in range(8))
    SOFT_RESET_ADDR = 0xFFB121B0
    SOFT_RESET_DATA = (1 << 11) | (1 << 12) | (1 << 13) | (1 << 14) | (1 << 18)

    def checked_arc_msg(message, operation, **kwargs):
        response = device.arc_msg(message, **kwargs)
        if len(response) < 2 or response[1] != 0:
            raise RuntimeError(f"Blackhole firmware failed to {operation}: {response}")
        return response

    # We only send GO_BUSY/GO_IDLE on BH if kmd < 2.6.0
    driver = get_driver_version()
    if not is_driver_version_at_least(driver, "2.6.0"):
        device.arc_msg(0x54)

    # Stop the RISC loops before resetting complete Tensix tiles.
    device.noc_broadcast32(0, SOFT_RESET_ADDR, SOFT_RESET_DATA)

    # Always scrub every functional core, including cores carrying an image
    # left by an earlier interrupted run. The current run's set is retained for
    # defensive compatibility with callers whose location list changes.
    scrub_cores = {CoreId(*core) for core in device.get_tensix_locations()}
    if loaded_cores is not None:
        scrub_cores.update(loaded_cores)

    # This sequence mirrors tensix_reset_sequence() in the Blackhole firmware
    # e2e tests. Keep AICLK at the firmware-qualified reset frequency while the
    # tile and NOC state are rebuilt, and always release that temporary force.
    checked_arc_msg(
        TT_SMC_MSG_FORCE_AICLK,
        "force the reset-safe AICLK",
        arg0=250,
        arg1=0,
    )
    try:
        for address in TENSIX_RISC_RESET_ADDRS:
            device.axi_write32(address, 0)

        checked_arc_msg(TT_SMC_MSG_TOGGLE_TENSIX_RESET, "reset the Tensix tiles")
        checked_arc_msg(TT_SMC_MSG_REINIT_TENSIX, "reinitialize the Tensix tiles")

        # The tile reset clears this register. Reassert every RISC's soft reset
        # before touching L1 or releasing the ASIC-level RISC reset signals.
        device.noc_broadcast32(1, SOFT_RESET_ADDR, SOFT_RESET_DATA)

        # A complete tile reset clears engine state but preserves Tensix L1
        # SRAM. Erase the retained image after reset/reinit, verify the erase,
        # and only then allow the ASIC-level RISC reset signals to be released.
        scrub_burnin_bh(device, scrub_cores)
        for address in TENSIX_RISC_RESET_ADDRS:
            device.axi_write32(address, 0xFFFFFFFF)
    finally:
        checked_arc_msg(
            TT_SMC_MSG_FORCE_AICLK,
            "release the reset-safe AICLK",
            arg0=0,
            arg1=0,
        )


def positive_int(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def non_negative_int(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value


def positive_float(value):
    value = float(value)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def non_negative_float(value):
    value = float(value)
    if value < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return value


def parse_args():
    # Parse arguments
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=tt_burnin.__version__,
    )
    parser.add_argument(
        "--reset_file",
        type=parse_reset_input,
        metavar="reset_config.json",
        default=None,
        help=(
            "Provide a custom reset json file for the host."
            "Generate a default reset json file with the -g option with tt-smi."
        ),
        dest="reset",
    )
    parser.add_argument(
        "--no-reset",
        action="store_true",
        default=False,
        help="Don't issue a reset before or after burning (WARNING: This may cause burnin or your next workload to no longer function)",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        default=False,
        help="Don't check tensix fw after loading (WARNING: if the workload was loaded incorrectly burnin may not run at maximum load)",
    )
    parser.add_argument(
        "--idle",
        action="store_true",
        default=False,
        help="Don't load the power virus workload, just run the tensix idle",
    )
    parser.add_argument(
        "--ramp-step",
        type=non_negative_int,
        default=1,
        metavar="CORES",
        help=(
            "Release this many Tensix cores per ramp step (default: 1). "
            "Use 0 for the legacy simultaneous start."
        ),
    )
    parser.add_argument(
        "--ramp-interval",
        type=non_negative_float,
        default=1.0,
        metavar="SECONDS",
        help="Wait this long after each ramp step (default: 1.0)",
    )
    parser.add_argument(
        "--max-cores",
        type=positive_int,
        default=None,
        metavar="CORES",
        help="Run on at most this many Tensix cores per device",
    )
    parser.add_argument(
        "--duration",
        type=positive_float,
        default=None,
        metavar="SECONDS",
        help="Stop automatically this long after the ramp completes",
    )
    parser.add_argument(
        "--aiclk-limit",
        type=positive_int,
        default=None,
        metavar="MHZ",
        help=(
            "Set a temporary Blackhole host AICLK ceiling. The firmware "
            "validates the device-specific value and TT-Burnin restores the "
            "default on exit."
        ),
    )
    parser.add_argument(
        "--tdp-limit",
        type=positive_int,
        default=None,
        metavar="WATTS",
        help=(
            "Set a temporary Blackhole ASIC TDP limit. The firmware validates "
            "the device-specific value and TT-Burnin restores the previous "
            "runtime limit on exit."
        ),
    )
    parser.add_argument(
        "--enable-gddr",
        action="store_true",
        help=(
            "Wake Blackhole GDDR/MRISC after the core ramp. BHPV does not use "
            "GDDR; this option adds board-power coverage."
        ),
    )
    parser.add_argument(
        "--enable-l2cpu",
        action="store_true",
        help=(
            "Enable Blackhole L2CPU clocks after the core ramp. BHPV does not "
            "use L2CPU; this option adds board-power coverage."
        ),
    )
    parser.add_argument(
        "--max-board-power",
        type=positive_float,
        default=None,
        metavar="WATTS",
        help=(
            "Stop if any local board reaches this measured input power. "
            "This is a reactive cutoff, not a hard electrical limit."
        ),
    )
    parser.add_argument(
        "--max-total-board-power",
        type=positive_float,
        default=None,
        metavar="WATTS",
        help=(
            "Stop if the measured input power summed across local boards "
            "reaches this value"
        ),
    )
    # subparsers = parser.add_subparsers(title="command", dest="command", required=True)
    return parser.parse_args()


def detect_and_group_devices():
    all_devices = detect_chips_with_callback()
    devs = []
    devices = []
    for device in all_devices:
        if device.as_wh() is not None:
            if device.is_remote():
                devs.append(RemoteWhChip(device.as_wh()))
            else:
                devs.append(WhChip(device.as_wh()))
        elif device.as_bh() is not None:
            devs.append(BhChip(device.as_bh()))
        else:
            raise ValueError("Did not recognize board")
        devices.append(device)

    return devs, devices


def set_device_power_state(device, state):
    try:
        device.set_power_state(state)
    except Exception as error:
        raise RuntimeError(
            f"Failed to set device power state to {state}. Firmware v18.12.0 "
            "or newer is required; otherwise try power cycling the host."
        ) from error


def set_burnin_power_state(device, mrisc=False, l2cpu=False):
    """Enable only the power domains required by the burnin workload."""
    try:
        if device.as_bh() is not None:
            # BHPV runs entirely on Tensix using local L1/NOC traffic. Waking
            # GDDR/MRISC and L2CPU adds a large board-power step without helping
            # the workload.
            device.set_power(
                aiclk=True,
                mrisc=mrisc,
                tensix=True,
                l2cpu=l2cpu,
                pcie=True,
            )
        else:
            device.set_power_state("high")
    except Exception as error:
        raise RuntimeError(
            "Failed to enable the device power domains needed by TT-Burnin. "
            "Firmware v18.12.0 or newer is required; otherwise try power "
            "cycling the host."
        ) from error


def set_host_aiclk_limit(device, frequency_mhz=None):
    """Set or restore Blackhole's temporary host AICLK ceiling."""
    blackhole = device.as_bh()
    if blackhole is None:
        raise RuntimeError("--aiclk-limit is only supported on Blackhole")

    restore_default = frequency_mhz is None
    response = blackhole.arc_msg_buf(
        [
            0x23,
            0 if restore_default else frequency_mhz,
            1 if restore_default else 0,
            0,
            0,
            0,
            0,
            0,
        ]
    )
    if response[0] != 0:
        operation = (
            "restore the default" if restore_default else f"set {frequency_mhz} MHz"
        )
        raise RuntimeError(
            f"Blackhole firmware rejected the request to {operation} host AICLK limit"
        )


def set_tdp_limit(device, watts):
    """Set Blackhole's runtime ASIC TDP limit."""
    blackhole = device.as_bh()
    if blackhole is None:
        raise RuntimeError("--tdp-limit is only supported on Blackhole")
    response = blackhole.arc_msg_buf([0x22, watts, 0, 0, 0, 0, 0, 0])
    if response[0] != 0:
        raise RuntimeError(f"Blackhole firmware rejected the {watts} W ASIC TDP limit")


def local_devices(devices):
    return [device for device in devices if not device.is_remote()]


class BurninStopped(Exception):
    """Internal signal used when the operator stops during the startup ramp."""


def wait_with_power_checks(
    devices,
    seconds,
    max_board_power=None,
    max_total_board_power=None,
    check_stdin=False,
    power_peaks=None,
):
    deadline = time.monotonic() + seconds
    while True:
        try:
            powers = check_power_limits(devices, max_board_power, max_total_board_power)
        except BoardPowerLimitExceeded as error:
            if power_peaks is not None:
                for index, power in enumerate(error.powers):
                    power_peaks[index] = max(power_peaks[index], power)
            raise
        if power_peaks is not None:
            for index, power in enumerate(powers):
                power_peaks[index] = max(power_peaks[index], power)
        if check_stdin and len(sys.stdin.read(1)) > 0:
            raise BurninStopped
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.1, remaining))


def garbage_collect_all_devices(*device_groups):
    for devices in device_groups:
        devices.clear()
    gc.collect()


def main():
    args = parse_args()
    # Uncomment the below to display a full Rust backtrace on error
    # os.environ["RUST_BACKTRACE"] = "full"
    # Allow non blocking read for accepting user input before stopping burnin
    os.set_blocking(sys.stdin.fileno(), False)
    devs, devices = detect_and_group_devices()
    print_all_available_devices(devs)
    if not args.no_reset:
        reset_all_devices(devices, reset_filename=args.reset)

    # Force garbage collection on the old devices and start with new device objects after reset
    garbage_collect_all_devices(devs, devices)
    devs, devices = detect_and_group_devices()
    telemetry_devices = local_devices(devices)
    power_peaks = [0.0] * len(telemetry_devices)
    dwell_power_sums = [0.0] * len(telemetry_devices)
    dwell_power_mins = [float("inf")] * len(telemetry_devices)
    dwell_power_maxs = [0.0] * len(telemetry_devices)
    dwell_sample_count = 0
    limited_aiclk_devices = []
    changed_tdp_devices = []
    loaded_bh_cores = {}
    driver = get_driver_version()
    kmd_power_management = is_driver_version_at_least(driver, "2.6.0")
    try:
        print()
        print(
            CMD_LINE_COLOR.BLUE,
            "Starting TT-Burnin workload on all boards with staged core activation.",
            CMD_LINE_COLOR.ENDC,
        )
        print()

        def start_burnin(device, raw_device, idx, total):
            print(
                CMD_LINE_COLOR.PURPLE,
                f"Starting TT-Burnin workload on device {idx + 1}/{total}",
                CMD_LINE_COLOR.ENDC,
            )

            # Power devices and start their cores one at a time. The old path raised
            # every device to high power during both detection passes, then started
            # all chips concurrently.
            if kmd_power_management:
                if args.tdp_limit is not None and raw_device.as_bh() is not None:
                    previous_tdp_limit = raw_device.get_telemetry().tdp_limit_max
                    set_tdp_limit(raw_device, args.tdp_limit)
                    changed_tdp_devices.append((raw_device, previous_tdp_limit))
                if args.aiclk_limit is not None and raw_device.as_bh() is not None:
                    set_host_aiclk_limit(raw_device, args.aiclk_limit)
                    limited_aiclk_devices.append(raw_device)
                set_burnin_power_state(raw_device)
                wait_with_power_checks(
                    telemetry_devices,
                    args.ramp_interval,
                    args.max_board_power,
                    args.max_total_board_power,
                    check_stdin=True,
                    power_peaks=power_peaks,
                )

            def after_batch(released, total_cores):
                print(
                    CMD_LINE_COLOR.PURPLE,
                    f"Device {idx + 1}: active Tensix cores {released}/{total_cores}",
                    CMD_LINE_COLOR.ENDC,
                )
                wait_with_power_checks(
                    telemetry_devices,
                    args.ramp_interval,
                    args.max_board_power,
                    args.max_total_board_power,
                    check_stdin=True,
                    power_peaks=power_peaks,
                )

            kwargs = {
                "no_check": args.no_check,
                "idle": args.idle,
                "ramp_step": args.ramp_step,
                "max_cores": args.max_cores,
                "after_batch": after_batch,
            }
            if isinstance(device, WhChip):
                start_burnin_wh(device, **kwargs)
            elif isinstance(device, BhChip):
                loaded_bh_cores[id(device)] = start_burnin_bh(device, **kwargs)

                # The workload does not require these domains. If explicitly
                # requested for board-power coverage, add them only after the
                # Tensix ramp so their power step cannot overlap core startup.
                if args.enable_l2cpu:
                    set_burnin_power_state(raw_device, l2cpu=True)
                    wait_with_power_checks(
                        telemetry_devices,
                        args.ramp_interval,
                        args.max_board_power,
                        args.max_total_board_power,
                        check_stdin=True,
                        power_peaks=power_peaks,
                    )
                if args.enable_gddr:
                    set_burnin_power_state(
                        raw_device,
                        mrisc=True,
                        l2cpu=args.enable_l2cpu,
                    )
                    wait_with_power_checks(
                        telemetry_devices,
                        args.ramp_interval,
                        args.max_board_power,
                        args.max_total_board_power,
                        check_stdin=True,
                        power_peaks=power_peaks,
                    )
            else:
                raise NotImplementedError(f"Don't support {device}")

        # Sequential startup prevents ramps on multiple boards from overlapping.
        for i, (device, raw_device) in enumerate(zip(devs, devices)):
            start_burnin(device, raw_device, i, len(devs))

        text = Text(
            " Press Enter to STOP TT-Burnin on all boards...", style="bold yellow"
        )

        # Create a live update for telemetry widget
        burnin_started = time.monotonic()
        with Live(Group(generate_table(devices), text), refresh_per_second=10) as live:
            while True:
                # Break if there is any user keypress
                c = sys.stdin.read(1)
                if len(c) > 0:
                    break
                if (
                    args.duration is not None
                    and time.monotonic() - burnin_started >= args.duration
                ):
                    break
                try:
                    powers = check_power_limits(
                        telemetry_devices,
                        args.max_board_power,
                        args.max_total_board_power,
                    )
                except BoardPowerLimitExceeded as error:
                    for index, power in enumerate(error.powers):
                        power_peaks[index] = max(power_peaks[index], power)
                    raise
                dwell_sample_count += 1
                for index, power in enumerate(powers):
                    power_peaks[index] = max(power_peaks[index], power)
                    dwell_power_sums[index] += power
                    dwell_power_mins[index] = min(dwell_power_mins[index], power)
                    dwell_power_maxs[index] = max(dwell_power_maxs[index], power)
                live.update(Group(generate_table(devices), text))
                time.sleep(0.1)
    except BurninStopped:
        pass
    except Exception as error:
        import traceback

        traceback.print_exc()
        print(error)
        raise
    finally:
        print()
        print(
            CMD_LINE_COLOR.GREEN,
            "Stopping TT-Burnin workload on all boards.",
            CMD_LINE_COLOR.ENDC,
        )
        print()

        def stop_burnin(device):
            if isinstance(device, WhChip):
                stop_burnin_wh(device)
            elif isinstance(device, BhChip):
                stop_burnin_bh(device, loaded_bh_cores.get(id(device)))
            else:
                raise NotImplementedError(f"Don't support {device}")

        # Stop sequentially too, and continue cleanup if one device is unhealthy.
        for device in devs:
            try:
                stop_burnin(device)
            except Exception as error:
                print(
                    CMD_LINE_COLOR.RED,
                    f"Failed to stop {device}: {error}",
                    CMD_LINE_COLOR.ENDC,
                )

        if kmd_power_management:
            for device in devices:
                try:
                    set_device_power_state(device, "low")
                except Exception as error:
                    print(
                        CMD_LINE_COLOR.RED,
                        str(error),
                        CMD_LINE_COLOR.ENDC,
                    )

        for device in limited_aiclk_devices:
            try:
                set_host_aiclk_limit(device)
            except Exception as error:
                print(
                    CMD_LINE_COLOR.RED,
                    f"Failed to restore host AICLK limit: {error}",
                    CMD_LINE_COLOR.ENDC,
                )

        for device, previous_tdp_limit in changed_tdp_devices:
            try:
                set_tdp_limit(device, previous_tdp_limit)
            except Exception as error:
                print(
                    CMD_LINE_COLOR.RED,
                    f"Failed to restore {previous_tdp_limit} W TDP limit: {error}",
                    CMD_LINE_COLOR.ENDC,
                )

        if power_peaks:
            print(
                CMD_LINE_COLOR.PURPLE,
                "Peak sampled board input power: "
                + ", ".join(
                    f"device {index}: {power:.1f} W"
                    for index, power in enumerate(power_peaks)
                ),
                CMD_LINE_COLOR.ENDC,
            )
        if dwell_sample_count:
            print(
                CMD_LINE_COLOR.PURPLE,
                "Sustained dwell board input power: "
                + ", ".join(
                    (
                        f"device {index}: avg "
                        f"{dwell_power_sums[index] / dwell_sample_count:.1f} W, "
                        f"range {dwell_power_mins[index]:.1f}-"
                        f"{dwell_power_maxs[index]:.1f} W "
                        f"({dwell_sample_count} samples)"
                    )
                    for index in range(len(dwell_power_sums))
                ),
                CMD_LINE_COLOR.ENDC,
            )

        # Final reset to restore state
        if not args.no_reset:
            reset_all_devices(devices, reset_filename=args.reset)

        # Drop all KMD clients so their power-management references are released.
        garbage_collect_all_devices(devs, devices)
