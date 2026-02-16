"""Source streaming helpers for CLI and daemon."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from dataclasses import dataclass
from typing import Any, cast

from aiosendspin.client import SendspinClient
from aiosendspin.models.source import (
    InputStreamRequestFormatSource,
    InputStreamStartSource,
    SourceClientCommand,
    SourceCommand,
    SourceCommandPayload,
    SourceControl,
    SourceSignalType,
    SourceStatePayload,
    SourceStateType,
)
from aiosendspin.models.types import AudioCodec

from sendspin.hooks import run_hook
from sendspin.source_utils import SourceSignalReporter, calc_level
from sendspin.utils import create_task


@dataclass(slots=True)
class SourceStreamConfig:
    """Configuration for source streaming."""

    codec: AudioCodec
    input: str
    device: str | int | None
    sample_rate: int
    channels: int
    bit_depth: int
    frame_ms: int
    signal_threshold_db: float
    signal_hold_ms: float
    sine_hz: float
    control_hooks: dict[SourceControl, str] | None = None
    hook_client_id: str | None = None
    hook_client_name: str | None = None
    hook_server_url: str | None = None


class SourceStreamer:
    """Stream source audio with signal detection."""

    def __init__(
        self,
        client: SendspinClient,
        config: SourceStreamConfig,
        *,
        logger: logging.Logger,
    ) -> None:
        self._client = client
        self._config = config
        self._logger = logger
        self._reporter = SourceSignalReporter(
            threshold_db=config.signal_threshold_db,
            hold_ms=config.signal_hold_ms,
            send_state=self._send_state,
            send_command=self._send_command,
        )
        self._control_hooks = config.control_hooks or {}

    def update_vad(
        self, *, threshold_db: float | None = None, hold_ms: float | None = None
    ) -> None:
        """Apply updated VAD settings."""
        self._reporter.update_vad(threshold_db=threshold_db, hold_ms=hold_ms)

    async def send_input_stream_start(self) -> None:
        """Send input_stream/start with the current source format."""
        if not self._client.connected:
            return
        try:
            client_any = cast("Any", self._client)
            await client_any.send_input_stream_start(
                InputStreamStartSource(
                    codec=self._config.codec,
                    channels=self._config.channels,
                    sample_rate=self._config.sample_rate,
                    bit_depth=self._config.bit_depth,
                    codec_header=None,
                )
            )
        except RuntimeError:
            return

    async def send_input_stream_end(self) -> None:
        """Send input_stream/end to stop the input stream."""
        if not self._client.connected:
            return
        try:
            client_any = cast("Any", self._client)
            await client_any.send_input_stream_end()
        except RuntimeError:
            return

    async def handle_format_request(self, request: InputStreamRequestFormatSource) -> None:
        """Handle input_stream/request-format from the server."""
        requested = SourceStreamConfig(
            codec=request.codec or self._config.codec,
            input=self._config.input,
            device=self._config.device,
            sample_rate=request.sample_rate or self._config.sample_rate,
            channels=request.channels or self._config.channels,
            bit_depth=request.bit_depth or self._config.bit_depth,
            frame_ms=self._config.frame_ms,
            signal_threshold_db=self._config.signal_threshold_db,
            signal_hold_ms=self._config.signal_hold_ms,
            sine_hz=self._config.sine_hz,
        )
        if (
            requested.codec != self._config.codec
            or requested.sample_rate != self._config.sample_rate
            or requested.channels != self._config.channels
            or requested.bit_depth != self._config.bit_depth
        ):
            self._logger.warning("Input stream format change not supported; keeping current format")
        await self.send_input_stream_start()

    def reset_signal(self) -> None:
        """Reset signal detection state."""
        self._reporter.reset()

    async def send_state(
        self,
        state: SourceStateType,
        *,
        level: float | None = None,
        signal: SourceSignalType | None = None,
    ) -> None:
        """Send an explicit source state update."""
        if signal is None:
            signal = (
                SourceSignalType.PRESENT
                if state == SourceStateType.STREAMING
                else SourceSignalType.ABSENT
            )
        if level is None:
            level = 0.5 if state == SourceStateType.STREAMING else 0.0
        await self._send_state(state, level, signal)

    async def run(self, streaming: asyncio.Event) -> None:
        """Run the source streaming loop."""
        if self._config.input == "sine":
            await self._stream_sine(streaming)
        else:
            await self._stream_linein(streaming)

    async def handle_source_command(
        self,
        payload: SourceCommandPayload,
        streaming: asyncio.Event,
    ) -> None:
        """Apply server source commands to local streaming state."""
        if payload.vad is not None:
            self.update_vad(
                threshold_db=payload.vad.threshold_db,
                hold_ms=payload.vad.hold_ms,
            )
            self._logger.info(
                "Updated VAD settings: threshold_db=%s hold_ms=%s",
                payload.vad.threshold_db,
                payload.vad.hold_ms,
            )

        if payload.control is not None:
            self._handle_control(payload.control)

        if payload.command == SourceCommand.START:
            streaming.set()
            self.reset_signal()
            await self.send_input_stream_start()
            await self.send_state(
                SourceStateType.STREAMING,
                level=0.0,
                signal=SourceSignalType.UNKNOWN,
            )
            self._logger.info("Source start command received")
        elif payload.command == SourceCommand.STOP:
            streaming.clear()
            self.reset_signal()
            await self.send_input_stream_end()
            await self.send_state(
                SourceStateType.IDLE,
                level=0.0,
                signal=SourceSignalType.ABSENT,
            )
            self._logger.info("Source stop command received")

    def _handle_control(self, control: SourceControl) -> None:
        """Run hook for an incoming source control command."""
        hook = self._control_hooks.get(control)
        if hook is None:
            self._logger.debug("Source control received: %s (no hook configured)", control.value)
            return

        server_info = self._client.server_info
        create_task(
            run_hook(
                hook,
                event=f"source_{control.value}",
                server_id=server_info.server_id if server_info else None,
                server_name=server_info.name if server_info else None,
                server_url=self._config.hook_server_url,
                client_id=self._config.hook_client_id,
                client_name=self._config.hook_client_name,
            )
        )
        self._logger.info("Source control received: %s", control.value)

    async def _send_state(
        self, state: SourceStateType, level: float, signal: SourceSignalType
    ) -> None:
        if not self._client.connected:
            return
        try:
            client_any = cast("Any", self._client)
            await client_any.send_source_state(
                state=SourceStatePayload(state=state, level=level, signal=signal)
            )
        except RuntimeError:
            return

    async def _send_command(self, command: SourceClientCommand) -> None:
        if not self._client.connected:
            return
        try:
            client_any = cast("Any", self._client)
            await client_any.send_source_command(command)
        except RuntimeError:
            return

    async def _send_chunk(self, pcm: bytes) -> bool:
        if not self._client.connected:
            return False
        if not self._client.is_time_synchronized():
            return False
        capture_timestamp_us = int(asyncio.get_running_loop().time() * 1_000_000)
        try:
            client_any = cast("Any", self._client)
            await client_any.send_source_audio_chunk(pcm, capture_timestamp_us=capture_timestamp_us)
        except RuntimeError:
            return False
        return True

    async def _stream_sine(self, streaming: asyncio.Event) -> None:
        samples_per_frame = int(self._config.sample_rate * self._config.frame_ms / 1000)
        phase = 0.0
        phase_step = 2.0 * math.pi * self._config.sine_hz / self._config.sample_rate
        amplitude = 0.3
        frame_duration = self._config.frame_ms / 1000.0
        last_log = asyncio.get_running_loop().time()
        frame_count = 0

        sine_level = min(1.0, abs(amplitude) / math.sqrt(2))
        while True:
            if not streaming.is_set():
                await asyncio.sleep(0.05)
                continue

            pcm = bytearray()
            for _ in range(samples_per_frame):
                sample = int(
                    amplitude * math.sin(phase) * ((1 << (self._config.bit_depth - 1)) - 1)
                )
                phase += phase_step
                for _ in range(self._config.channels):
                    if self._config.bit_depth == 16:
                        pcm.extend(struct.pack("<h", sample))
                    elif self._config.bit_depth == 24:
                        pcm.extend(int(sample).to_bytes(3, "little", signed=True))
                    elif self._config.bit_depth == 32:
                        pcm.extend(struct.pack("<i", sample))
                    else:
                        raise ValueError("Unsupported bit depth for PCM synthesis")

            if await self._send_chunk(bytes(pcm)):
                await self._reporter.update(sine_level, state=SourceStateType.STREAMING)
                frame_count += 1
                now = asyncio.get_running_loop().time()
                if now - last_log >= 1.0:
                    self._logger.info("Sent %d source frames/sec", frame_count)
                    frame_count = 0
                    last_log = now
            await asyncio.sleep(frame_duration)

    async def _stream_linein(self, streaming: asyncio.Event) -> None:
        try:
            import sounddevice as sd
        except Exception as err:  # noqa: BLE001
            raise RuntimeError("sounddevice is required for line-in capture") from err

        if self._config.bit_depth == 24:
            raise RuntimeError("Line-in capture does not support 24-bit PCM")
        dtype = "int16" if self._config.bit_depth == 16 else "int32"
        samples_per_frame = int(self._config.sample_rate * self._config.frame_ms / 1000)
        last_log = asyncio.get_running_loop().time()
        frame_count = 0
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=20)
        loop = asyncio.get_running_loop()

        def _enqueue(data: bytes) -> None:
            if queue.full():
                return
            queue.put_nowait(data)

        def _callback(indata: Any, frames: int, time: Any, status: Any) -> None:  # noqa: ARG001
            if status:
                self._logger.debug("Line-in status: %s", status)
            data = indata.copy().tobytes()
            loop.call_soon_threadsafe(_enqueue, data)

        with sd.InputStream(
            samplerate=self._config.sample_rate,
            channels=self._config.channels,
            dtype=dtype,
            blocksize=samples_per_frame,
            device=self._config.device,
            callback=_callback,
        ):
            while True:
                pcm = await queue.get()
                level = calc_level(pcm, self._config.bit_depth)
                if streaming.is_set():
                    if await self._send_chunk(pcm):
                        await self._reporter.update(level, state=SourceStateType.STREAMING)
                        frame_count += 1
                        now = asyncio.get_running_loop().time()
                        if now - last_log >= 1.0:
                            self._logger.info("Sent %d source frames/sec", frame_count)
                            frame_count = 0
                            last_log = now
                        continue
                await self._reporter.update(level, state=SourceStateType.IDLE)
