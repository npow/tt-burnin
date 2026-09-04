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
from tt_burnin.load_ttx import load_ttx_file, TtxFile, CoreId
from tt_burnin.ramp import (
    BoardPowerLimitExceeded,
    check_power_limits,
    ordered_tensix_cores,
    release_tensix_cores,
)
from tt_tools_common.ui_common.themes import CMD_LINE_COLOR
from tt_burnin.utils import (
    parse_reset_input,
    pci_indices_from_json,
    pci_board_reset,
    print_all_available_devices,
    generate_table,
    get_board_type,
    reset_6u_glx,
    tenstorrent_pci_bdfs,
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
    if not devices:
        print(
            CMD_LINE_COLOR.RED,
            "No devices detected. Exiting...",
            CMD_LINE_COLOR.ENDC,
        )
        sys.exit(1)
    # Check every local board before selecting a reset mechanism. A mixed host
    # must never silently choose the first device's reset path.
    board_types = {
        get_board_type(hex(device.board_id()).replace("0x", ""))
        for device in devices
        if not device.is_remote()
    }
    galaxy_types = {"tt-galaxy-wh", "tt-galaxy-bh"}
    if board_types & galaxy_types and not board_types <= galaxy_types:
        raise RuntimeError(
            f"Mixed Galaxy and PCI-card reset is unsupported: {sorted(board_types)}"
        )
    if board_types and board_types <= galaxy_types:
        # Perform a full galaxy reset and detect chips post reset
        reset_6u_glx()
        return

    if isinstance(reset_filename, dict):
        data = reset_filename
    elif reset_filename:
        data = parse_reset_input(reset_filename)
    else:
        data = None
    if data:
        if data.get("wh_mobo_reset"):
            raise RuntimeError(
                "Custom motherboard resets are not supported by the fail-closed "
                "burn-in reset path"
            )
        pci_indices, reinit = pci_indices_from_json(data)
        requested = set(pci_indices)
        detected = {
            device.get_pci_interface_id()
            for device in devices
            if not device.is_remote()
        }
        if requested != detected:
            raise RuntimeError(
                "Reset configuration must include every local Tenstorrent device; "
                f"detected {sorted(detected)}, requested {sorted(requested)}"
            )
        return pci_board_reset(pci_indices, reinit)
    else:
        # reset all boards
        dev_ids = []
        for device in devices:
            if not device.is_remote():
                dev_ids.append(device.get_pci_interface_id())
        return pci_board_reset(dev_ids, reinit=True)


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
    before_release=None,
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

    # Loading can take long enough for firmware state to change. Revalidate
    # containment immediately before the first core is allowed to execute.
    if before_release is not None:
        before_release()

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


def stop_burnin_bh(device, loaded_cores=None):
    """Cooperatively stop BHPV without resetting Tensix tiles or firmware.

    The packaged workload uses streams 4, 5, and 6.  Hold every compute block
    in soft reset, reset those autonomous streams over both NOC rings, and
    leave soft reset asserted.  The retained L1 image cannot execute in that
    state; a later workload must overwrite its selected cores before releasing
    them.  Avoiding L1 erase and verification is intentional: reads from a
    stalled endpoint can cause a PCIe Completion Timeout, and broad block writes
    add NOC traffic without strengthening the asserted-reset boundary.

    ``loaded_cores`` is retained for API compatibility.  The stop operation is
    a broadcast because startup first resets every functional Tensix and can be
    interrupted before its selected-core set is returned.
    """
    del loaded_cores
    SOFT_RESET_ADDR = 0xFFB121B0
    ALL_COMPUTE_SOFT_RESET_DATA = 0x7FFFF
    STREAM_REG_SPACE = 0x1000
    BHPV_STREAM_IDS = (4, 5, 6)
    STREAM_ONETIME_MISC_CFG_OFFSET = 2 * 4
    STREAM_RESET_OFFSET = 271 * 4
    STREAM_OVERLAY_BASE = 0xFFB40000

    def host_quiesce_tensix_streams():
        errors = []

        def attempt(description, operation):
            try:
                operation()
            except Exception as error:
                errors.append(f"{description}: {error}")

        # NOC1 runs in the opposite direction around the mesh. Try it first so
        # a backpressured NOC0 cannot prevent the initial stop, then repeat on
        # NOC0. Every later operation is best-effort even if one write fails.
        for noc in (1, 0):
            attempt(
                f"assert all-compute soft reset on NOC{noc}",
                lambda noc=noc: device.noc_broadcast32(
                    noc, SOFT_RESET_ADDR, ALL_COMPUTE_SOFT_RESET_DATA
                ),
            )
        time.sleep(0.01)

        for noc in (1, 0):
            for stream_id in BHPV_STREAM_IDS:
                stream_base = STREAM_OVERLAY_BASE + stream_id * STREAM_REG_SPACE
                attempt(
                    f"disable BHPV stream {stream_id} on NOC{noc}",
                    lambda noc=noc, stream_base=stream_base: device.noc_broadcast32(
                        noc,
                        stream_base + STREAM_ONETIME_MISC_CFG_OFFSET,
                        0,
                    ),
                )
                attempt(
                    f"reset BHPV stream {stream_id} on NOC{noc}",
                    lambda noc=noc, stream_base=stream_base: device.noc_broadcast32(
                        noc,
                        stream_base + STREAM_RESET_OFFSET,
                        1,
                    ),
                )

        # Seal the final state after the stream writes. In particular, a failed
        # stream transaction cannot skip these last reset attempts.
        for noc in (1, 0):
            attempt(
                f"reassert all-compute soft reset on NOC{noc}",
                lambda noc=noc: device.noc_broadcast32(
                    noc, SOFT_RESET_ADDR, ALL_COMPUTE_SOFT_RESET_DATA
                ),
            )
        time.sleep(0.01)

        if errors:
            raise RuntimeError("Failed to quiesce BHPV: " + "; ".join(errors))

    # Stop execution without discovery, readback, or firmware interaction.
    # The sequence contains no 0xAF/0x20 hard-reset messages, AICLK force, or
    # writes to the ASIC-level Tensix RISC reset registers.
    host_quiesce_tensix_streams()

    # Only unsupported pre-2.6 KMDs need the legacy power-manager idle message.
    # Send it last so a firmware error cannot prevent the cooperative stop.
    driver = get_driver_version()
    if not is_driver_version_at_least(driver, "2.6.0"):
        device.arc_msg(0x54)


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
        "--board-power-limit",
        type=positive_int,
        default=None,
        metavar="WATTS",
        help=(
            "Apply and verify a temporary Blackhole firmware board-input power "
            "limit after the initial reset, before enabling any workload cores"
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
            "Compatibility option; Blackhole L2CPU management clocks always "
            "remain enabled for host safety"
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
            # GDDR/MRISC adds a board-power step without helping the workload.
            # L2CPU remains enabled because it hosts PCIe/management services;
            # runtime firmware deliberately rejects attempts to gate it.
            device.set_power(
                aiclk=True,
                mrisc=mrisc,
                tensix=True,
                l2cpu=True,
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


_BH_TELEMETRY_DATA_REG_ADDR = 0x80030430
_BH_TAG_BOARD_POWER_LIMIT = 53
_BH_TAG_INPUT_POWER = 54
_BH_TAG_AICLK_LIMIT_MAX = 63
_BH_TAG_TDP_LIMIT_MAX = 64
_BH_TAG_HOST_AICLK_LIMIT = 70
_BH_TAG_RUNTIME_POWER_FAULT = 80
_BH_RUNTIME_POWER_FAULT_LATCHED = 1 << 0
_BH_RUNTIME_POWER_STRICT = 1 << 1
_BH_RUNTIME_POWER_SAMPLE_FRESH = 1 << 2
_BH_RUNTIME_POWER_POLICY_READY = 1 << 3
_BH_RUNTIME_POWER_REQUIRED = (
    _BH_RUNTIME_POWER_STRICT
    | _BH_RUNTIME_POWER_SAMPLE_FRESH
    | _BH_RUNTIME_POWER_POLICY_READY
)


def read_bh_telemetry_tag(device, tag):
    """Read a Blackhole ARC telemetry tag without relying on Luwen's schema."""
    blackhole = device.as_bh()
    if blackhole is None:
        raise RuntimeError("Blackhole telemetry was requested on another architecture")
    table_address = blackhole.axi_read32(_BH_TELEMETRY_DATA_REG_ADDR)
    if table_address in (0, 0xFFFFFFFF) or table_address % 4:
        raise RuntimeError(
            f"Blackhole firmware published an invalid telemetry address {table_address:#x}"
        )
    return blackhole.axi_read32(table_address + tag * 4)


def _verify_bh_telemetry(device, tag, expected, description):
    observed = read_bh_telemetry_tag(device, tag)
    if observed != expected:
        time.sleep(0.05)
        observed = read_bh_telemetry_tag(device, tag)
    if observed != expected:
        raise RuntimeError(
            f"Blackhole firmware did not apply {description}; telemetry reports "
            f"{observed}, expected {expected}"
        )


def check_runtime_power_status(devices):
    """Fail if Blackhole's electrical policy is unavailable or has tripped."""
    for index, device in enumerate(devices):
        if device.as_bh() is None:
            continue
        status = read_bh_telemetry_tag(device, _BH_TAG_RUNTIME_POWER_FAULT)
        trip_watts = status >> 16
        if status & _BH_RUNTIME_POWER_FAULT_LATCHED:
            raise RuntimeError(
                f"Blackhole device {index} firmware board-power containment "
                f"tripped at {trip_watts} W"
            )
        missing = _BH_RUNTIME_POWER_REQUIRED & ~status
        if missing:
            raise RuntimeError(
                f"Blackhole device {index} runtime board-power policy is not "
                f"ready (status {status:#010x}, missing bits {missing:#x})"
            )


def wait_for_runtime_power_status(devices, timeout=2.0):
    """Allow firmware's first DMC sample to arrive, then require protection."""
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            check_runtime_power_status(devices)
            return
        except RuntimeError as error:
            if "containment tripped" in str(error):
                raise
            last_error = error
            time.sleep(0.05)
    raise RuntimeError(f"Runtime board-power policy did not become ready: {last_error}")


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
    if response is None or response[0] != 0:
        operation = (
            "restore the default" if restore_default else f"set {frequency_mhz} MHz"
        )
        raise RuntimeError(
            f"Blackhole firmware rejected the request to {operation} host AICLK limit"
        )
    expected_mhz = 0 if restore_default else frequency_mhz
    _verify_bh_telemetry(
        device,
        _BH_TAG_HOST_AICLK_LIMIT,
        expected_mhz,
        f"the {expected_mhz} MHz host AICLK limit",
    )


def set_board_power_limit(device, watts=None):
    """Set or restore Blackhole's firmware-enforced board-input power limit."""
    blackhole = device.as_bh()
    if blackhole is None:
        raise RuntimeError("--board-power-limit is only supported on Blackhole")

    restore_default = watts is None
    response = blackhole.arc_msg_buf(
        [
            0x24,
            0 if restore_default else watts,
            1 if restore_default else 0,
            0,
            0,
            0,
            0,
            0,
        ]
    )
    if response is None or response[0] != 0:
        operation = "restore the default" if restore_default else f"set {watts} W"
        raise RuntimeError(
            f"Blackhole firmware rejected the request to {operation} board power limit"
        )

    if restore_default:
        applied_watts = read_bh_telemetry_tag(device, _BH_TAG_BOARD_POWER_LIMIT)
        if applied_watts <= 0:
            raise RuntimeError(
                "Blackhole firmware restored an invalid zero board power limit"
            )
    else:
        _verify_bh_telemetry(
            device,
            _BH_TAG_BOARD_POWER_LIMIT,
            watts,
            f"the {watts} W board power limit",
        )


def set_tdp_limit(device, watts):
    """Set Blackhole's runtime ASIC TDP limit."""
    blackhole = device.as_bh()
    if blackhole is None:
        raise RuntimeError("--tdp-limit is only supported on Blackhole")
    response = blackhole.arc_msg_buf([0x22, watts, 0, 0, 0, 0, 0, 0])
    if response is None or response[0] != 0:
        raise RuntimeError(f"Blackhole firmware rejected the {watts} W ASIC TDP limit")
    _verify_bh_telemetry(
        device,
        _BH_TAG_TDP_LIMIT_MAX,
        watts,
        f"the {watts} W ASIC TDP limit",
    )


def local_devices(devices):
    return [device for device in devices if not device.is_remote()]


def local_device_identities(devices):
    """Capture stable local identities without relying on mutable interface ids."""
    identities = []
    for device in local_devices(devices):
        if device.as_bh() is not None:
            architecture = "blackhole"
        elif device.as_wh() is not None:
            architecture = "wormhole"
        else:
            architecture = "unknown"
        identities.append((device.get_pci_bdf(), device.board_id(), architecture))
    return sorted(identities)


def verify_redetected_devices(expected_identities, devices):
    observed_identities = local_device_identities(devices)
    if observed_identities != expected_identities:
        raise RuntimeError(
            "Tenstorrent device set changed across reset: "
            f"expected {expected_identities}, observed {observed_identities}"
        )


def preflight_run_support(args, devices, driver):
    """Validate every target and required control before changing any device."""
    if not devices:
        raise RuntimeError("No Tenstorrent devices were detected")

    bh_only_options = {
        "--aiclk-limit": args.aiclk_limit is not None,
        "--tdp-limit": args.tdp_limit is not None,
        "--board-power-limit": args.board_power_limit is not None,
        "--enable-gddr": args.enable_gddr,
        "--enable-l2cpu": args.enable_l2cpu,
    }
    requested_bh_options = [
        name for name, enabled in bh_only_options.items() if enabled
    ]
    unsupported = [
        index for index, device in enumerate(devices) if device.as_bh() is None
    ]
    if requested_bh_options and unsupported:
        raise RuntimeError(
            f"{', '.join(requested_bh_options)} require Blackhole on every target; "
            f"unsupported device indices: {unsupported}"
        )

    remote_blackholes = [
        index
        for index, device in enumerate(devices)
        if device.as_bh() is not None and device.is_remote()
    ]
    if remote_blackholes:
        raise RuntimeError(
            "Safe Blackhole burn-in requires direct local policy telemetry; "
            f"remote Blackhole device indices are unsupported: {remote_blackholes}"
        )

    detected_bdfs = {device.get_pci_bdf() for device in local_devices(devices)}
    enumerated_bdfs = set(tenstorrent_pci_bdfs())
    if detected_bdfs != enumerated_bdfs:
        raise RuntimeError(
            "Device detection did not cover every Tenstorrent PCI function: "
            f"sysfs={sorted(enumerated_bdfs)}, detected={sorted(detected_bdfs)}"
        )

    blackholes = [device for device in local_devices(devices) if device.as_bh()]
    if blackholes and (
        driver is None or not is_driver_version_at_least(driver, "2.6.0")
    ):
        raise RuntimeError(
            "Safe Blackhole burn-in requires tt-kmd 2.6.0 or newer for "
            "power-aware device handling"
        )

    # Do not discover missing firmware telemetry after another board has already
    # had a policy or clock changed. A nonzero board policy is mandatory for
    # Blackhole high-load operation, even when the caller accepts the default.
    for index, device in enumerate(blackholes):
        board_limit = read_bh_telemetry_tag(device, _BH_TAG_BOARD_POWER_LIMIT)
        if board_limit <= 0:
            raise RuntimeError(
                f"Blackhole device {index} has no active board power policy"
            )
        if args.board_power_limit is not None and not (
            50 <= args.board_power_limit <= board_limit
        ):
            raise RuntimeError(
                f"Blackhole device {index} cannot safely preflight the requested "
                f"{args.board_power_limit} W board limit; its currently active "
                f"ceiling is {board_limit} W"
            )
        if args.tdp_limit is not None:
            current_tdp_limit = read_bh_telemetry_tag(device, _BH_TAG_TDP_LIMIT_MAX)
            if args.tdp_limit > current_tdp_limit:
                raise RuntimeError(
                    f"Blackhole device {index} cannot safely preflight the requested "
                    f"{args.tdp_limit} W TDP limit; its active limit is "
                    f"{current_tdp_limit} W"
                )
        if args.aiclk_limit is not None:
            read_bh_telemetry_tag(device, _BH_TAG_HOST_AICLK_LIMIT)
            max_aiclk = read_bh_telemetry_tag(device, _BH_TAG_AICLK_LIMIT_MAX)
            if not (800 <= args.aiclk_limit <= max_aiclk):
                raise RuntimeError(
                    f"Blackhole device {index} cannot safely preflight the requested "
                    f"{args.aiclk_limit} MHz AICLK limit; supported range is "
                    f"800-{max_aiclk} MHz"
                )
    wait_for_runtime_power_status(blackholes)


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
        check_runtime_power_status(devices)
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


def cleanup_can_relax_protection(active_error, cleanup_errors):
    """Only a clean workload and proven cleanup may restore wider limits."""
    return active_error is None and not cleanup_errors


def main():
    args = parse_args()
    # Uncomment the below to display a full Rust backtrace on error
    # os.environ["RUST_BACKTRACE"] = "full"
    # Allow non blocking read for accepting user input before stopping burnin
    os.set_blocking(sys.stdin.fileno(), False)
    devs, devices = detect_and_group_devices()
    print_all_available_devices(devs)
    driver = get_driver_version()
    preflight_run_support(args, devices, driver)
    expected_identities = local_device_identities(devices)
    if not args.no_reset:
        reset_all_devices(devices, reset_filename=args.reset)
        # Pre-reset handles are invalid after an ASIC reset. Drop them before
        # detecting the new interface mapping, then preflight every new handle.
        garbage_collect_all_devices(devs, devices)
        devs, devices = detect_and_group_devices()
        verify_redetected_devices(expected_identities, devices)
        preflight_run_support(args, devices, driver)
    telemetry_devices = local_devices(devices)
    power_peaks = [0.0] * len(telemetry_devices)
    dwell_power_sums = [0.0] * len(telemetry_devices)
    dwell_power_mins = [float("inf")] * len(telemetry_devices)
    dwell_power_maxs = [0.0] * len(telemetry_devices)
    dwell_sample_count = 0
    limited_aiclk_devices = []
    limited_board_power_devices = []
    changed_tdp_devices = []
    loaded_bh_cores = {}
    started_devices = []
    kmd_power_management = is_driver_version_at_least(driver, "2.6.0")
    try:
        print()
        print(
            CMD_LINE_COLOR.BLUE,
            "Starting TT-Burnin workload on all boards with staged core activation.",
            CMD_LINE_COLOR.ENDC,
        )
        print()

        # Apply protection to every local device as a complete phase before
        # changing any AICLK or enabling any workload power domain. This avoids
        # a partial multi-device startup if a later target lacks firmware
        # support or rejects its requested limit.
        blackhole_devices = [
            device for device in local_devices(devices) if device.as_bh() is not None
        ]
        if args.board_power_limit is not None:
            for raw_device in blackhole_devices:
                previous_board_power_limit = read_bh_telemetry_tag(
                    raw_device, _BH_TAG_BOARD_POWER_LIMIT
                )
                set_board_power_limit(raw_device, args.board_power_limit)
                limited_board_power_devices.append(
                    (raw_device, previous_board_power_limit)
                )
        if args.tdp_limit is not None:
            for raw_device in blackhole_devices:
                previous_tdp_limit = read_bh_telemetry_tag(
                    raw_device, _BH_TAG_TDP_LIMIT_MAX
                )
                set_tdp_limit(raw_device, args.tdp_limit)
                changed_tdp_devices.append((raw_device, previous_tdp_limit))
        wait_for_runtime_power_status(blackhole_devices)
        if args.aiclk_limit is not None:
            for raw_device in blackhole_devices:
                previous_aiclk_limit = read_bh_telemetry_tag(
                    raw_device, _BH_TAG_HOST_AICLK_LIMIT
                )
                set_host_aiclk_limit(raw_device, args.aiclk_limit)
                limited_aiclk_devices.append((raw_device, previous_aiclk_limit))
            wait_for_runtime_power_status(blackhole_devices)

        def start_burnin(device, raw_device, idx, total):
            print(
                CMD_LINE_COLOR.PURPLE,
                f"Starting TT-Burnin workload on device {idx + 1}/{total}",
                CMD_LINE_COLOR.ENDC,
            )

            # Any partial power-up failure still needs the full stop/scrub path.
            started_devices.append(device)

            # Power devices and start their cores one at a time. The old path raised
            # every device to high power during both detection passes, then started
            # all chips concurrently.
            if kmd_power_management:
                check_runtime_power_status(telemetry_devices)
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
                loaded_bh_cores[id(device)] = start_burnin_bh(
                    device,
                    before_release=lambda: check_runtime_power_status(
                        telemetry_devices
                    ),
                    **kwargs,
                )

                # The workload does not require these domains. If explicitly
                # requested for board-power coverage, add them only after the
                # Tensix ramp so their power step cannot overlap core startup.
                if args.enable_l2cpu:
                    check_runtime_power_status(telemetry_devices)
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
                    check_runtime_power_status(telemetry_devices)
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
                check_runtime_power_status(telemetry_devices)
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
        active_error = sys.exc_info()[1]
        cleanup_errors = []
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

        # Stop every device whose loader was entered. Continue containment
        # attempts after one failure, but do not later relax any protection.
        for device in started_devices:
            try:
                stop_burnin(device)
            except Exception as error:
                cleanup_errors.append(f"failed to stop {device}: {error}")
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
                    cleanup_errors.append(
                        f"failed to set a device to low power: {error}"
                    )
                    print(
                        CMD_LINE_COLOR.RED,
                        str(error),
                        CMD_LINE_COLOR.ENDC,
                    )

        if cleanup_can_relax_protection(active_error, cleanup_errors):
            for device, previous_aiclk_limit in limited_aiclk_devices:
                try:
                    set_host_aiclk_limit(
                        device,
                        None if previous_aiclk_limit == 0 else previous_aiclk_limit,
                    )
                except Exception as error:
                    cleanup_errors.append(
                        f"failed to restore host AICLK limit: {error}"
                    )
                    print(
                        CMD_LINE_COLOR.RED,
                        f"Failed to restore host AICLK limit: {error}",
                        CMD_LINE_COLOR.ENDC,
                    )
                    break

        if cleanup_can_relax_protection(active_error, cleanup_errors):
            for device, previous_board_power_limit in limited_board_power_devices:
                try:
                    set_board_power_limit(device, previous_board_power_limit)
                except Exception as error:
                    cleanup_errors.append(
                        "failed to restore board power limit: " + str(error)
                    )
                    print(
                        CMD_LINE_COLOR.RED,
                        f"Failed to restore {previous_board_power_limit} W board power limit: {error}",
                        CMD_LINE_COLOR.ENDC,
                    )
                    break

        if cleanup_can_relax_protection(active_error, cleanup_errors):
            for device, previous_tdp_limit in changed_tdp_devices:
                try:
                    set_tdp_limit(device, previous_tdp_limit)
                except Exception as error:
                    cleanup_errors.append(f"failed to restore TDP limit: {error}")
                    print(
                        CMD_LINE_COLOR.RED,
                        f"Failed to restore {previous_tdp_limit} W TDP limit: {error}",
                        CMD_LINE_COLOR.ENDC,
                    )
                    break

        if not cleanup_can_relax_protection(active_error, cleanup_errors):
            print(
                CMD_LINE_COLOR.RED,
                "The workload or cleanup did not complete cleanly; retaining "
                "clock/power protection and skipping the final reset.",
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

        try:
            # Reset only after the workload was proven stopped and the device
            # accepted low power. A reset after failed cleanup could discard the
            # very protection containing a retained workload.
            if not args.no_reset and cleanup_can_relax_protection(
                active_error, cleanup_errors
            ):
                reset_all_devices(devices, reset_filename=args.reset)
        except Exception as error:
            cleanup_errors.append(f"final reset failed: {error}")
            print(CMD_LINE_COLOR.RED, str(error), CMD_LINE_COLOR.ENDC)
        finally:
            # Drop all KMD clients so their power-management references are released.
            garbage_collect_all_devices(devs, devices)

        if cleanup_errors and active_error is None:
            raise RuntimeError("; ".join(cleanup_errors))
