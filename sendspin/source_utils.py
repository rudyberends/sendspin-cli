"""Helpers for source signal detection and reporting."""

from __future__ import annotations

import array
import asyncio
import math
import sys
from collections.abc import Awaitable, Callable

from aiosendspin.models.source import SourceClientCommand, SourceSignalType, SourceStateType

SendStateFunc = Callable[[SourceStateType, float, SourceSignalType], Awaitable[None]]
SendCommandFunc = Callable[[SourceClientCommand], Awaitable[None]]


def calc_level(pcm: bytes, bit_depth: int) -> float:
    """Calculate normalized RMS level for 16/32-bit PCM."""
    if bit_depth == 16:
        values = array.array("h")
        max_val = float((1 << 15) - 1)
    elif bit_depth == 32:
        values = array.array("i")
        max_val = float((1 << 31) - 1)
    else:
        raise ValueError("Unsupported bit depth for level calculation")

    values.frombytes(pcm)
    if sys.byteorder != "little":
        values.byteswap()
    if not values:
        return 0.0
    acc = 0.0
    for sample in values:
        acc += float(sample) * float(sample)
    rms = math.sqrt(acc / len(values))
    return min(1.0, rms / max_val)


class SourceSignalReporter:
    """Emit source state and started/stopped events based on signal level."""

    def __init__(
        self,
        *,
        threshold_db: float,
        send_state: SendStateFunc,
        send_command: SendCommandFunc,
        hold_ms: float = 300.0,
    ) -> None:
        if not math.isfinite(threshold_db):
            raise ValueError("threshold_db must be a finite number")
        if not math.isfinite(hold_ms):
            raise ValueError("hold_ms must be a finite number")
        self._threshold_db = threshold_db
        self._threshold = 10 ** (threshold_db / 20.0)
        self._send_state = send_state
        self._send_command = send_command
        self._hold_seconds = max(0.0, hold_ms / 1000.0)
        self._last_signal: SourceSignalType | None = None
        self._last_state: SourceStateType | None = None
        self._candidate_signal: SourceSignalType | None = None
        self._candidate_since = 0.0

    def update_vad(
        self, *, threshold_db: float | None = None, hold_ms: float | None = None
    ) -> None:
        """Update VAD thresholds without resetting state."""
        if threshold_db is not None:
            if not math.isfinite(threshold_db):
                return
            self._threshold_db = threshold_db
            self._threshold = 10 ** (threshold_db / 20.0)
        if hold_ms is not None:
            if not math.isfinite(hold_ms):
                return
            self._hold_seconds = max(0.0, hold_ms / 1000.0)
        self._candidate_signal = None
        self._candidate_since = 0.0

    def reset(self) -> None:
        """Reset signal tracking state."""
        self._last_signal = None
        self._last_state = None
        self._candidate_signal = None
        self._candidate_since = 0.0

    async def update(self, level: float, *, state: SourceStateType) -> None:
        """Update signal state and emit events if needed."""
        raw_signal = (
            SourceSignalType.PRESENT if level >= self._threshold else SourceSignalType.ABSENT
        )
        previous_signal = self._last_signal
        now = asyncio.get_running_loop().time()

        if raw_signal != previous_signal:
            if self._candidate_signal != raw_signal:
                self._candidate_signal = raw_signal
                self._candidate_since = now
            elif now - self._candidate_since >= self._hold_seconds:
                if previous_signal is not None:
                    command = (
                        SourceClientCommand.STARTED
                        if raw_signal == SourceSignalType.PRESENT
                        else SourceClientCommand.STOPPED
                    )
                    await self._send_command(command)
                self._last_signal = raw_signal
                self._candidate_signal = None
                self._candidate_since = 0.0

        if self._last_signal is None:
            self._last_signal = raw_signal
        state_changed = self._last_state != state
        signal_changed = self._last_signal != previous_signal
        if state_changed or signal_changed:
            await self._send_state(state, level, self._last_signal)
            self._last_state = state
