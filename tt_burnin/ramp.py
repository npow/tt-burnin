# SPDX-FileCopyrightText: © 2026 Tenstorrent AI ULC
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar


Core = TypeVar("Core", bound=tuple[int, int])


class BoardPowerLimitExceeded(RuntimeError):
    """Raised when a board reaches the user-provided input-power cutoff."""

    def __init__(self, message: str, powers: list[float]):
        super().__init__(message)
        self.powers = powers


def ordered_tensix_cores(
    cores: Iterable[Core], max_cores: int | None = None
) -> list[Core]:
    """Return cores in row-major order so early batches span columns."""
    ordered = sorted(cores, key=lambda core: (core[1], core[0]))
    if max_cores is not None:
        ordered = ordered[:max_cores]
    return ordered


def core_batches(cores: Sequence[Core], batch_size: int) -> list[list[Core]]:
    """Split cores into activation batches; zero means one legacy full batch."""
    if batch_size < 0:
        raise ValueError("batch_size must be non-negative")
    if not cores:
        return []
    if batch_size == 0:
        batch_size = len(cores)
    return [list(cores[i : i + batch_size]) for i in range(0, len(cores), batch_size)]


def release_tensix_cores(
    device,
    cores: Sequence[Core],
    soft_reset_value: int,
    batch_size: int,
    after_batch: Callable[[int, int], None] | None = None,
) -> None:
    """Release selected cores in batches, leaving every other core in reset."""
    released = 0
    for batch in core_batches(cores, batch_size):
        for core in batch:
            device.noc_write32(0, core[0], core[1], 0xFFB121B0, soft_reset_value)
        released += len(batch)
        if after_batch is not None:
            after_batch(released, len(cores))


def check_power_limits(
    devices,
    max_board_power: float | None,
    max_total_board_power: float | None = None,
) -> list[float]:
    """Read fresh telemetry and fail when a user-provided cutoff is reached."""
    powers = []
    for index, device in enumerate(devices):
        telemetry = device.get_telemetry()
        if not hasattr(telemetry, "input_power"):
            raise RuntimeError(
                f"Device {index} does not expose input-power telemetry; "
                "cannot enforce --max-board-power"
            )
        powers.append(float(telemetry.input_power))

    for index, power in enumerate(powers):
        if max_board_power is not None and power >= max_board_power:
            raise BoardPowerLimitExceeded(
                f"Device {index} reached {power:.1f} W board input power "
                f"(cutoff: {max_board_power:.1f} W)",
                powers,
            )

    total_power = sum(powers)
    if max_total_board_power is not None and total_power >= max_total_board_power:
        raise BoardPowerLimitExceeded(
            f"Detected boards reached {total_power:.1f} W total input power "
            f"(cutoff: {max_total_board_power:.1f} W)",
            powers,
        )
    return powers
