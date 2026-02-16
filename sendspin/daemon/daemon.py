"""Daemon mode for running a Sendspin client without UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from aiohttp import ClientError, web
from aiosendspin.client import ClientListener, SendspinClient
from aiosendspin.models.core import (
    ClientGoodbyeMessage,
    ClientGoodbyePayload,
    ServerCommandPayload,
)
from aiosendspin.models.player import ClientHelloPlayerSupport
from aiosendspin.models.source import (
    ClientHelloSourceSupport,
    SourceControl,
    SourceFeatures,
    SourceFormat,
)
from aiosendspin_mpris import MPRIS_AVAILABLE, SendspinMpris
from aiosendspin.models.types import (
    AudioCodec,
    GoodbyeReason,
    PlayerCommand,
    PlayerStateType,
    Roles,
)

from sendspin.audio import AudioDevice, detect_supported_audio_formats
from sendspin.audio_connector import AudioStreamHandler
from sendspin.hooks import run_hook
from sendspin.settings import ClientSettings
from sendspin.source_stream import SourceStreamConfig, SourceStreamer
from sendspin.utils import create_task, get_device_info

logger = logging.getLogger(__name__)


@dataclass
class DaemonArgs:
    """Configuration for the Sendspin daemon."""

    audio_device: AudioDevice
    client_id: str
    client_name: str
    settings: ClientSettings
    url: str | None = None
    static_delay_ms: float | None = None
    listen_port: int = 8928
    use_mpris: bool = True
    hook_start: str | None = None
    hook_stop: str | None = None
    source_enabled: bool = False
    source_input: str = "linein"
    source_device: str | None = None
    source_codec: str = "pcm"
    source_sample_rate: int = 48000
    source_channels: int = 2
    source_bit_depth: int = 16
    source_frame_ms: int = 20
    source_sine_hz: float = 440.0
    source_signal_threshold_db: float = -45.0
    source_signal_hold_ms: float = 300.0
    source_hook_play: str | None = None
    source_hook_pause: str | None = None
    source_hook_next: str | None = None
    source_hook_previous: str | None = None
    source_hook_activate: str | None = None
    source_hook_deactivate: str | None = None


class SendspinDaemon:
    """Sendspin daemon - headless audio player mode.

    When a URL is provided, the daemon connects to that server (client-initiated).
    When no URL is provided, the daemon listens for incoming server connections
    and advertises itself via mDNS (server-initiated connections).
    """

    def __init__(self, args: DaemonArgs) -> None:
        """Initialize the daemon."""
        self._args = args
        self._client: SendspinClient | None = None
        self._listener: ClientListener | None = None
        self._audio_handler: AudioStreamHandler | None = None
        self._settings: ClientSettings | None = None
        self._mpris: SendspinMpris | None = None
        self._static_delay_ms: float = 0.0
        self._connection_lock: asyncio.Lock | None = None
        self._server_url: str | None = None
        self._source_task: asyncio.Task[None] | None = None
        self._source_streaming: asyncio.Event | None = None
        self._source_unsubscribe: Callable[[], None] | None = None
        self._source_format_unsubscribe: Callable[[], None] | None = None
        self._source_streamer: SourceStreamer | None = None

    def _create_client(self, static_delay_ms: float = 0.0) -> SendspinClient:
        """Create a new SendspinClient instance."""
        client_roles = [Roles.PLAYER]
        if MPRIS_AVAILABLE and self._args.use_mpris:
            client_roles.extend([Roles.METADATA, Roles.CONTROLLER])
        source_support = None
        if self._args.source_enabled:
            client_roles.append(Roles("source@v1"))
            controls = list(self._source_control_hooks().keys()) or None
            source_support = ClientHelloSourceSupport(
                supported_formats=[
                    SourceFormat(
                        codec=AudioCodec(self._args.source_codec),
                        channels=self._args.source_channels,
                        sample_rate=self._args.source_sample_rate,
                        bit_depth=self._args.source_bit_depth,
                    )
                ],
                controls=controls,
                features=SourceFeatures(level=True, line_sense=True),
            )

        supported_formats = detect_supported_audio_formats(self._args.audio_device.index)

        client_kwargs: dict[str, Any] = {
            "client_id": self._args.client_id,
            "client_name": self._args.client_name,
            "roles": client_roles,
            "device_info": get_device_info(),
            "player_support": ClientHelloPlayerSupport(
                supported_formats=supported_formats,
                buffer_capacity=32_000_000,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
            "static_delay_ms": static_delay_ms,
        }
        if source_support is not None:
            client_kwargs["source_support"] = source_support
        return SendspinClient(**client_kwargs)

    async def run(self) -> int:
        """Run the daemon."""
        logger.info("Starting Sendspin daemon: %s", self._args.client_id)
        loop = asyncio.get_running_loop()

        # Store reference to current task so it can be cancelled on shutdown
        main_task = asyncio.current_task()
        assert main_task is not None

        def signal_handler() -> None:
            logger.debug("Received interrupt signal, shutting down...")
            main_task.cancel()

        # Register signal handlers
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signal.SIGINT, signal_handler)
            loop.add_signal_handler(signal.SIGTERM, signal_handler)

        self._settings = self._args.settings

        # CLI arg overrides settings for static delay
        delay = (
            self._args.static_delay_ms
            if self._args.static_delay_ms is not None
            else self._settings.static_delay_ms
        )

        self._audio_handler = AudioStreamHandler(
            audio_device=self._args.audio_device,
            volume=self._settings.player_volume,
            muted=self._settings.player_muted,
            on_event=self._on_stream_event,
            on_format_change=self._handle_format_change,
        )

        try:
            if self._args.url is not None:
                # Client-initiated connection mode
                await self._run_client_initiated(delay)
            else:
                # Server-initiated connection mode (listen for incoming connections)
                await self._run_server_initiated(delay)
        except asyncio.CancelledError:
            logger.debug("Daemon cancelled")
        finally:
            await self._stop_source_streaming()
            await self._stop_mpris_and_audio()
            if self._client is not None:
                await self._client.disconnect()
                self._client = None
            if self._listener is not None:
                await self._listener.stop()
                self._listener = None
            if self._settings:
                await self._settings.flush()
            logger.info("Daemon stopped")

        return 0

    async def _run_client_initiated(self, static_delay_ms: float) -> None:
        """Run in client-initiated mode, connecting to a specific URL."""
        assert self._args.url is not None
        assert self._audio_handler is not None
        self._client = self._create_client(static_delay_ms)
        if MPRIS_AVAILABLE and self._args.use_mpris:
            self._mpris = SendspinMpris(self._client)
            self._mpris.start()
        self._audio_handler.attach_client(self._client)
        self._server_url = self._args.url
        self._client.add_server_command_listener(self._handle_server_command)
        self._attach_source_command_listener()
        await self._start_source_streaming()
        await self._connection_loop(self._args.url)

    async def _run_server_initiated(self, static_delay_ms: float) -> None:
        """Run in server-initiated mode, listening for incoming connections."""
        logger.info(
            "Listening for server connections on port %d (mDNS: _sendspin._tcp.local.)",
            self._args.listen_port,
        )

        self._static_delay_ms = static_delay_ms  # Store for use in connection handler
        self._connection_lock = asyncio.Lock()

        self._listener = ClientListener(
            client_id=self._args.client_id,
            on_connection=self._handle_server_connection,
            port=self._args.listen_port,
        )
        await self._listener.start()

        # Keep running until cancelled
        while True:
            await asyncio.sleep(3600)

    async def _stop_mpris_and_audio(self) -> None:
        """Stop MPRIS and cleanup audio handler."""
        if self._mpris is not None:
            self._mpris.stop()
            self._mpris = None
        if self._audio_handler is not None:
            await self._audio_handler.cleanup()

    async def _handle_server_connection(self, ws: web.WebSocketResponse) -> None:
        """Handle an incoming server connection."""
        logger.info("Server connected")
        assert self._audio_handler is not None
        assert self._connection_lock is not None

        # Lock ensures we wait for any in-progress handshake to complete
        # before disconnecting the previous server
        async with self._connection_lock:
            # Clean up any previous client
            if self._client is not None:
                logger.info("Disconnecting from previous server")
                await self._stop_mpris_and_audio()
                if self._client.connected:
                    try:
                        await self._client._send_message(  # noqa: SLF001
                            ClientGoodbyeMessage(
                                payload=ClientGoodbyePayload(reason=GoodbyeReason.ANOTHER_SERVER)
                            ).to_json()
                        )
                    except Exception:
                        logger.debug("Failed to send goodbye message", exc_info=True)
                await self._client.disconnect()

            # Create a new client for this connection
            client = self._create_client(self._static_delay_ms)
            self._client = client
            self._audio_handler.attach_client(client)
            client.add_server_command_listener(self._handle_server_command)
            self._attach_source_command_listener()
            await self._start_source_streaming()
            if MPRIS_AVAILABLE and self._args.use_mpris:
                self._mpris = SendspinMpris(client)
                self._mpris.start()

            try:
                await client.attach_websocket(ws)
            except TimeoutError:
                logger.warning("Handshake with server timed out")
                await self._stop_mpris_and_audio()
                if self._client is client:
                    self._client = None
                return
            except Exception:
                logger.exception("Error during server handshake")
                await self._stop_mpris_and_audio()
                if self._client is client:
                    self._client = None
                return

        # Handshake complete, release lock so new connections can proceed
        # Now wait for disconnect (outside the lock)
        try:
            disconnect_event = asyncio.Event()
            unsubscribe = client.add_disconnect_listener(disconnect_event.set)
            await disconnect_event.wait()
            unsubscribe()
            logger.info("Server disconnected")
        except Exception:
            logger.exception("Error waiting for server disconnect")
        finally:
            # Only cleanup if we're still the active client (not replaced by new connection)
            if self._client is client:
                await self._stop_mpris_and_audio()
                await self._stop_source_streaming()

    async def _connection_loop(self, url: str) -> None:
        """Run the connection loop with automatic reconnection (client-initiated mode)."""
        assert self._client is not None
        assert self._audio_handler is not None
        error_backoff = 1.0
        max_backoff = 300.0

        while True:
            try:
                await self._client.connect(url)
                error_backoff = 1.0

                # Wait for disconnect
                disconnect_event: asyncio.Event = asyncio.Event()
                unsubscribe = self._client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsubscribe()

                # Connection dropped
                logger.info("Disconnected from server")
                await self._audio_handler.cleanup()

                logger.info("Reconnecting to %s", url)

            except (TimeoutError, OSError, ClientError) as e:
                logger.warning(
                    "Connection error (%s), retrying in %.0fs",
                    type(e).__name__,
                    error_backoff,
                )

                await asyncio.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, max_backoff)

            except Exception:
                logger.exception("Unexpected error during connection")
                break

    async def _start_source_streaming(self) -> None:
        """Start source streaming worker if source mode is enabled."""
        if not self._args.source_enabled or self._client is None:
            return
        if self._source_task is not None and not self._source_task.done():
            return

        device: str | int | None = self._args.source_device
        if isinstance(device, str) and device.isdigit():
            device = int(device)

        self._source_streaming = asyncio.Event()
        self._source_streamer = SourceStreamer(
            self._client,
            SourceStreamConfig(
                codec=AudioCodec(self._args.source_codec),
                input=self._args.source_input,
                device=device,
                sample_rate=self._args.source_sample_rate,
                channels=self._args.source_channels,
                bit_depth=self._args.source_bit_depth,
                frame_ms=self._args.source_frame_ms,
                signal_threshold_db=self._args.source_signal_threshold_db,
                signal_hold_ms=self._args.source_signal_hold_ms,
                sine_hz=self._args.source_sine_hz,
                control_hooks=self._source_control_hooks(),
                hook_client_id=self._args.client_id,
                hook_client_name=self._args.client_name,
                hook_server_url=self._server_url,
            ),
            logger=logger,
        )

        async def _run_stream() -> None:
            assert self._source_streaming is not None
            assert self._source_streamer is not None
            await self._source_streamer.run(self._source_streaming)

        self._source_task = create_task(_run_stream())

    async def _stop_source_streaming(self) -> None:
        """Stop source streaming worker and listeners."""
        if self._source_task is not None:
            if self._source_streaming is not None:
                self._source_streaming.clear()
            self._source_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._source_task
            self._source_task = None
        self._source_streaming = None
        self._source_streamer = None
        if self._source_unsubscribe is not None:
            self._source_unsubscribe()
            self._source_unsubscribe = None
        if self._source_format_unsubscribe is not None:
            self._source_format_unsubscribe()
            self._source_format_unsubscribe = None

    def _attach_source_command_listener(self) -> None:
        """Attach source command listener when source role is enabled."""
        if not self._args.source_enabled or self._client is None:
            return
        if self._source_unsubscribe is not None:
            return

        def _on_source_command(payload: Any) -> None:
            streamer = self._source_streamer
            if self._source_streaming is None or streamer is None:
                return
            create_task(streamer.handle_source_command(payload, self._source_streaming))

        def _on_format_request(payload: Any) -> None:
            streamer = self._source_streamer
            if streamer is None:
                return
            create_task(streamer.handle_format_request(payload))

        client_any = cast("Any", self._client)
        self._source_unsubscribe = client_any.add_source_command_listener(_on_source_command)
        self._source_format_unsubscribe = client_any.add_input_stream_request_format_listener(
            _on_format_request
        )

    def _handle_server_command(self, payload: ServerCommandPayload) -> None:
        """Handle server commands for player volume/mute control and save to settings."""
        if payload.player is None or self._settings is None or self._client is None:
            return

        assert self._audio_handler is not None
        player_cmd = payload.player

        if player_cmd.command == PlayerCommand.VOLUME and player_cmd.volume is not None:
            self._settings.update(player_volume=player_cmd.volume)
            self._audio_handler.set_volume(
                self._settings.player_volume, muted=self._settings.player_muted
            )
            logger.info("Server set player volume: %d%%", player_cmd.volume)
        elif player_cmd.command == PlayerCommand.MUTE and player_cmd.mute is not None:
            self._settings.update(player_muted=player_cmd.mute)
            self._audio_handler.set_volume(
                self._settings.player_volume, muted=self._settings.player_muted
            )
            logger.info("Server %s player", "muted" if player_cmd.mute else "unmuted")

        # Send state update back to server per spec
        create_task(
            self._client.send_player_state(
                state=PlayerStateType.SYNCHRONIZED,
                volume=self._settings.player_volume,
                muted=self._settings.player_muted,
            )
        )

    def _handle_format_change(
        self, codec: str | None, sample_rate: int, bit_depth: int, channels: int
    ) -> None:
        """Log audio format changes."""
        logger.info(
            "Audio format: %s %dHz/%d-bit/%dch",
            codec or "PCM",
            sample_rate,
            bit_depth,
            channels,
        )

    def _on_stream_event(self, event: str) -> None:
        """Handle stream lifecycle events by running hooks."""
        hook = self._args.hook_start if event == "start" else self._args.hook_stop
        if not hook:
            return
        server_info = self._client.server_info if self._client else None
        create_task(
            run_hook(
                hook,
                event=event,
                server_id=server_info.server_id if server_info else None,
                server_name=server_info.name if server_info else None,
                server_url=self._server_url,
                client_id=self._args.client_id,
                client_name=self._args.client_name,
            )
        )

    def _source_control_hooks(self) -> dict[SourceControl, str]:
        """Return configured source control hook mapping."""
        mapping = {
            SourceControl.PLAY: self._args.source_hook_play,
            SourceControl.PAUSE: self._args.source_hook_pause,
            SourceControl.NEXT: self._args.source_hook_next,
            SourceControl.PREVIOUS: self._args.source_hook_previous,
            SourceControl.ACTIVATE: self._args.source_hook_activate,
            SourceControl.DEACTIVATE: self._args.source_hook_deactivate,
        }
        return {control: hook for control, hook in mapping.items() if hook}
