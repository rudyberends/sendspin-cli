"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import traceback
from collections.abc import Sequence
from importlib.metadata import version
from typing import TYPE_CHECKING, Any, cast

from sendspin.settings import ClientSettings, get_client_settings, get_serve_settings
from sendspin.utils import create_task, get_device_info

if TYPE_CHECKING:
    from aiosendspin.models.source import SourceControl
    from sendspin.audio import AudioDevice

LOGGER = logging.getLogger(__name__)

PORTAUDIO_NOT_FOUND_MESSAGE = """Error: PortAudio library not found.

Please install PortAudio for your system:
  • Debian/Ubuntu/Raspberry Pi: sudo apt-get install libportaudio2
  • macOS: brew install portaudio
  • Other systems: https://www.portaudio.com/"""


def list_audio_devices() -> None:
    """List all available audio output devices."""
    try:
        from sendspin.audio import query_devices
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            sys.exit(1)
        raise

    try:
        devices = query_devices()

        print("Available audio output devices:")
        print()
        for device in devices:
            default_marker = " (default)" if device.is_default else ""
            print(
                f"  [{device.index}] {device.name}{default_marker}\n"
                f"       Channels: {device.output_channels}, "
                f"Sample rate: {device.sample_rate} Hz"
            )
        if devices:
            print("\nTo select an audio device:\n  sendspin --audio-device 0")

    except Exception as e:  # noqa: BLE001
        print(f"Error listing audio devices: {e}")
        sys.exit(1)


def list_input_devices() -> None:
    """List all available audio input devices."""
    try:
        import sounddevice as sd
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            sys.exit(1)
        raise

    try:
        devices = sd.query_devices()
        default_input = sd.default.device[0]

        print("Available audio input devices:")
        print()
        listed = 0
        for i, d in enumerate(devices):
            max_in = int(d.get("max_input_channels", 0))
            if max_in <= 0:
                continue
            default_marker = " (default)" if i == default_input else ""
            print(
                f"  [{i}] {d['name']}{default_marker}\n"
                f"       Channels: {max_in}, Sample rate: {d['default_samplerate']} Hz"
            )
            listed += 1
        print("\nTo select an input device:\n  sendspin source run --source-device 0")
        if listed == 0:
            print("  (No input devices found)")

    except Exception as e:  # noqa: BLE001
        print(f"Error listing input devices: {e}")
        sys.exit(1)


def _add_source_control_hook_args(parser: argparse.ArgumentParser) -> None:
    """Add optional source control hook arguments to a parser."""
    parser.add_argument(
        "--source-hook-play",
        type=str,
        default=None,
        help="Hook to run when source receives play control",
    )
    parser.add_argument(
        "--source-hook-pause",
        type=str,
        default=None,
        help="Hook to run when source receives pause control",
    )
    parser.add_argument(
        "--source-hook-next",
        type=str,
        default=None,
        help="Hook to run when source receives next control",
    )
    parser.add_argument(
        "--source-hook-previous",
        type=str,
        default=None,
        help="Hook to run when source receives previous control",
    )
    parser.add_argument(
        "--source-hook-activate",
        type=str,
        default=None,
        help="Hook to run when source receives activate control",
    )
    parser.add_argument(
        "--source-hook-deactivate",
        type=str,
        default=None,
        help="Hook to run when source receives deactivate control",
    )


def _resolve_source_control_hooks(args: argparse.Namespace) -> dict[SourceControl, str]:
    """Build source control -> hook command map from CLI args."""
    from aiosendspin.models.source import SourceControl

    mapping = {
        SourceControl.PLAY: args.source_hook_play,
        SourceControl.PAUSE: args.source_hook_pause,
        SourceControl.NEXT: args.source_hook_next,
        SourceControl.PREVIOUS: args.source_hook_previous,
        SourceControl.ACTIVATE: args.source_hook_activate,
        SourceControl.DEACTIVATE: args.source_hook_deactivate,
    }
    return {control: hook for control, hook in mapping.items() if hook}


def _resolve_source_controls(args: argparse.Namespace) -> list[SourceControl] | None:
    """Build advertised supported source controls from configured hooks."""
    controls = list(_resolve_source_control_hooks(args).keys())
    return controls or None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Sendspin client."""
    parser = argparse.ArgumentParser(description="Sendspin CLI")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('sendspin')}",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Start a Sendspin server")
    serve_parser.add_argument(
        "source",
        nargs="?",
        default=None,
        help="Audio source: local file path or URL (http/https)",
    )
    serve_parser.add_argument(
        "--source-format",
        default=None,
        help="ffmpeg container format for source audio",
    )
    serve_parser.add_argument(
        "--demo",
        action="store_true",
        help="Use a demo audio stream (retro dance music)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (default: 8927)",
    )
    serve_parser.add_argument(
        "--name",
        default=None,
        help="Server name for mDNS discovery (default: Sendspin Server)",
    )
    serve_parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use (default: INFO)",
    )
    serve_parser.add_argument(
        "--client",
        action="append",
        dest="clients",
        default=[],
        help="Client URL to connect to (can be specified multiple times)",
    )

    # Daemon subcommand
    daemon_parser = subparsers.add_parser(
        "daemon",
        help="Run Sendspin client in daemon mode (no UI)",
        description=(
            "Run as a headless audio player. By default, listens for incoming server "
            "connections and advertises via mDNS (_sendspin._tcp.local.). "
            "Use --url to connect to a specific server instead."
        ),
    )
    daemon_parser.add_argument(
        "--url",
        default=None,
        help=(
            "WebSocket URL of the Sendspin server to connect to. "
            "If omitted, listen for incoming server connections via mDNS."
        ),
    )
    daemon_parser.add_argument(
        "--port",
        type=int,
        default=None,
        dest="listen_port",
        help="Port to listen on for incoming server connections (default: 8928)",
    )
    daemon_parser.add_argument(
        "--name",
        default=None,
        help="Friendly name for this client (defaults to hostname)",
    )
    daemon_parser.add_argument(
        "--id",
        default=None,
        help="Unique identifier for this client (defaults to sendspin-cli-<hostname>)",
    )
    daemon_parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use (default: INFO)",
    )
    daemon_parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=None,
        help="Extra playback delay in milliseconds applied after clock sync",
    )
    daemon_parser.add_argument(
        "--audio-device",
        type=str,
        default=None,
        help=(
            "Audio output device by index (e.g., 0, 1, 2) or name prefix (e.g., 'MacBook'). "
            "Use --list-audio-devices to see available devices."
        ),
    )
    daemon_parser.add_argument(
        "--settings-dir",
        type=str,
        default=None,
        help="Directory to store settings (default: ~/.config/sendspin)",
    )
    daemon_parser.add_argument(
        "--disable-mpris",
        action="store_true",
        help="Disable MPRIS integration",
    )
    daemon_parser.add_argument(
        "--hook-start",
        type=str,
        default=None,
        help="Command to run when audio stream starts (receives SENDSPIN_* env vars)",
    )
    daemon_parser.add_argument(
        "--hook-stop",
        type=str,
        default=None,
        help="Command to run when audio stream stops (receives SENDSPIN_* env vars)",
    )
    daemon_parser.add_argument(
        "--source",
        action="store_true",
        default=None,
        help="Enable source@v1 input on this daemon",
    )
    daemon_parser.add_argument(
        "--no-source",
        action="store_false",
        dest="source",
        default=None,
        help="Disable source@v1 input on this daemon",
    )
    daemon_parser.add_argument(
        "--source-input",
        choices=["sine", "linein"],
        default=None,
        help="Source input type",
    )
    daemon_parser.add_argument(
        "--source-device",
        type=str,
        default=None,
        help="Input device name or index for line-in capture",
    )
    daemon_parser.add_argument(
        "--source-codec",
        choices=["pcm", "opus", "flac"],
        default=None,
        help="Audio codec to advertise",
    )
    daemon_parser.add_argument(
        "--source-sample-rate",
        type=int,
        default=None,
        help="Source sample rate in Hz",
    )
    daemon_parser.add_argument(
        "--source-channels",
        type=int,
        default=None,
        help="Source channel count",
    )
    daemon_parser.add_argument(
        "--source-bit-depth",
        type=int,
        default=None,
        help="Source bit depth",
    )
    daemon_parser.add_argument(
        "--source-frame-ms",
        type=int,
        default=None,
        help="Source frame size in milliseconds",
    )
    daemon_parser.add_argument(
        "--source-sine-hz",
        type=float,
        default=None,
        help="Sine wave frequency for synthetic source",
    )
    daemon_parser.add_argument(
        "--signal-threshold-db",
        type=float,
        default=None,
        help="Signal threshold in dB",
    )
    daemon_parser.add_argument(
        "--signal-hold",
        type=float,
        default=None,
        help="Signal hold in milliseconds",
    )
    _add_source_control_hook_args(daemon_parser)

    source_parser = subparsers.add_parser("source", help="Run a source role client")
    source_subparsers = source_parser.add_subparsers(dest="source_command")
    source_run_parser = source_subparsers.add_parser("run", help="Start a source client")
    source_run_parser.add_argument("--url", required=True, help="WebSocket URL of Sendspin server")
    source_run_parser.add_argument("--name", default=None, help="Friendly source name")
    source_run_parser.add_argument("--id", default=None, help="Source client id")
    source_run_parser.add_argument(
        "--source-input",
        choices=["sine", "linein"],
        default="sine",
        help="Audio input source (default: sine)",
    )
    source_run_parser.add_argument(
        "--source-device",
        type=str,
        default=None,
        help="Input device name or index for line-in capture",
    )
    source_run_parser.add_argument(
        "--source-codec",
        choices=["pcm", "opus", "flac"],
        default="pcm",
        help="Audio codec to advertise (default: pcm)",
    )
    source_run_parser.add_argument("--source-sample-rate", type=int, default=48000)
    source_run_parser.add_argument("--source-channels", type=int, default=1)
    source_run_parser.add_argument("--source-bit-depth", type=int, default=16)
    source_run_parser.add_argument("--source-frame-ms", type=int, default=20)
    source_run_parser.add_argument("--source-sine-hz", type=float, default=440.0)
    source_run_parser.add_argument("--signal-threshold-db", type=float, default=-45.0)
    source_run_parser.add_argument("--signal-hold", type=float, default=300.0)
    _add_source_control_hook_args(source_run_parser)

    # Default behavior (client mode) - existing arguments
    parser.add_argument(
        "--url",
        default=None,
        help=("WebSocket URL of the Sendspin server. If omitted, discover via mDNS."),
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Friendly name for this client (defaults to hostname)",
    )
    parser.add_argument(
        "--id",
        default=None,
        help="Unique identifier for this client (defaults to sendspin-cli-<hostname>)",
    )
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use (default: INFO)",
    )
    parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=None,
        help="Extra playback delay in milliseconds applied after clock sync",
    )
    parser.add_argument(
        "--audio-device",
        type=str,
        default=None,
        help=(
            "Audio output device by index (e.g., 0, 1, 2) or name prefix (e.g., 'MacBook'). "
            "Use --list-audio-devices to see available devices."
        ),
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="List available audio output devices and exit",
    )
    parser.add_argument(
        "--list-input-devices",
        action="store_true",
        help="List available audio input devices and exit",
    )
    parser.add_argument(
        "--list-servers",
        action="store_true",
        help="Discover and list available Sendspin servers on the network",
    )
    parser.add_argument(
        "--list-clients",
        action="store_true",
        help="Discover and list available Sendspin clients on the network",
    )
    parser.add_argument(
        "--disable-mpris",
        action="store_true",
        help="Disable MPRIS integration",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="(DEPRECATED: use 'sendspin daemon' instead) Run without the interactive terminal UI",
    )
    parser.add_argument(
        "--hook-start",
        type=str,
        default=None,
        help="Command to run when audio stream starts (receives SENDSPIN_* env vars)",
    )
    parser.add_argument(
        "--hook-stop",
        type=str,
        default=None,
        help="Command to run when audio stream stops (receives SENDSPIN_* env vars)",
    )
    return parser.parse_args(argv)


async def list_servers() -> None:
    """Discover and list all Sendspin servers on the network."""
    from sendspin.discovery import discover_servers

    try:
        servers = await discover_servers(discovery_time=3.0)
        if not servers:
            print("No Sendspin servers found.")
            return

        print(f"\nFound {len(servers)} server(s):")
        print()
        for server in servers:
            print(f"  {server.name}")
            print(f"    URL:  {server.url}")
            print(f"    Host: {server.host}:{server.port}")
    except Exception as e:  # noqa: BLE001
        print(f"Error discovering servers: {e}")
        sys.exit(1)


async def list_clients() -> None:
    """Discover and list all Sendspin clients on the network."""
    from sendspin.discovery import discover_clients

    try:
        clients = await discover_clients(discovery_time=3.0)
        if not clients:
            print("No Sendspin clients found.")
            return

        print(f"\nFound {len(clients)} client(s):")
        print()
        for client in clients:
            print(f"  {client.name}")
            print(f"    URL:  {client.url}")
            print(f"    Host: {client.host}:{client.port}")
    except Exception as e:  # noqa: BLE001
        print(f"Error discovering clients: {e}")
        sys.exit(1)


class CLIError(Exception):
    """CLI error with exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _resolve_audio_device(device_arg: str | None) -> AudioDevice:
    """Resolve audio device from CLI argument.

    Args:
        device_arg: Device specifier (index number, name prefix, or None for default).

    Returns:
        The resolved AudioDevice.

    Raises:
        CLIError: If the device cannot be found.
    """
    from sendspin.audio import query_devices

    devices = query_devices()

    # Find device by: default, index, or name prefix
    if device_arg is None:
        device = next((d for d in devices if d.is_default), None)
    elif device_arg.isnumeric():
        device_id = int(device_arg)
        device = next((d for d in devices if d.index == device_id), None)
    else:
        device = next((d for d in devices if d.name.startswith(device_arg)), None)

    if device is None:
        kind = "Default" if device_arg is None else "Specified"
        raise CLIError(f"{kind} audio device not found.")

    LOGGER.info("Using audio device %d: %s", device.index, device.name)
    return device


def _resolve_input_defaults(device_arg: str | int | None) -> tuple[int | None, int | None]:
    """Resolve sample-rate/channels defaults from selected input device."""
    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001
        return None, None

    try:
        if device_arg is None:
            device_index = sd.default.device[0]
        elif isinstance(device_arg, str) and device_arg.isdigit():
            device_index = int(device_arg)
        else:
            device_index = device_arg
        info = sd.query_devices(device_index, kind="input")
        rate = int(info.get("default_samplerate", 0)) or None
        channels = int(info.get("max_input_channels", 0)) or None
        return rate, channels
    except Exception:  # noqa: BLE001
        return None, None


def _resolve_client_info(client_id: str | None, client_name: str | None) -> tuple[str, str]:
    """Determine client ID and name, using hostname as fallback."""
    if client_id is not None and client_name is not None:
        return client_id, client_name

    hostname = socket.gethostname()
    if not hostname:
        raise CLIError("Unable to determine hostname. Please specify --id and/or --name", 1)

    return (
        client_id or f"sendspin-cli-{hostname}",
        client_name or hostname,
    )


def _resolve_role_client_info(
    client_id: str | None, client_name: str | None, *, prefix: str
) -> tuple[str, str]:
    """Resolve client id/name for non-default roles."""
    if client_id is not None and client_name is not None:
        return client_id, client_name
    hostname = socket.gethostname() or "unknown"
    return (
        client_id or f"{prefix}-{hostname}",
        client_name or hostname,
    )


async def _run_source_mode(args: argparse.Namespace) -> int:
    """Run a source-only client."""
    from aiosendspin.client import SendspinClient
    from aiosendspin.models.source import (
        ClientHelloSourceSupport,
        InputStreamRequestFormatSource,
        SourceCommandPayload,
        SourceFeatures,
        SourceFormat,
        SourceStateType,
    )
    from aiosendspin.models.types import AudioCodec, Roles
    from sendspin.source_stream import SourceStreamConfig, SourceStreamer

    client_id, client_name = _resolve_role_client_info(args.id, args.name, prefix="sendspin-source")
    if args.source_device is not None and args.source_input == "sine":
        args.source_input = "linein"

    if args.source_device is not None and args.source_input == "linein":
        if args.source_sample_rate == 48000 and args.source_channels == 1:
            default_rate, default_channels = _resolve_input_defaults(args.source_device)
            if default_rate:
                args.source_sample_rate = default_rate
            if default_channels:
                args.source_channels = default_channels

    source_support = ClientHelloSourceSupport(
        supported_formats=[
            SourceFormat(
                codec=AudioCodec(args.source_codec),
                channels=args.source_channels,
                sample_rate=args.source_sample_rate,
                bit_depth=args.source_bit_depth,
            )
        ],
        controls=_resolve_source_controls(args),
        features=SourceFeatures(level=True, line_sense=True),
    )

    if args.source_codec != "pcm":
        raise CLIError("Source demo currently only supports PCM frame generation")

    client_kwargs: dict[str, Any] = {
        "client_id": client_id,
        "client_name": client_name,
        "roles": [Roles("source@v1")],
        "device_info": get_device_info(),
        "source_support": source_support,
    }
    client = SendspinClient(**client_kwargs)
    client_any = cast("Any", client)

    streaming = asyncio.Event()
    connected_event = asyncio.Event()
    device = args.source_device
    if isinstance(device, str) and device.isdigit():
        device = int(device)

    streamer = SourceStreamer(
        client,
        SourceStreamConfig(
            codec=AudioCodec(args.source_codec),
            input=args.source_input,
            device=device,
            sample_rate=args.source_sample_rate,
            channels=args.source_channels,
            bit_depth=args.source_bit_depth,
            frame_ms=args.source_frame_ms,
            signal_threshold_db=args.signal_threshold_db,
            signal_hold_ms=args.signal_hold,
            sine_hz=args.source_sine_hz,
            control_hooks=_resolve_source_control_hooks(args),
            hook_client_id=client_id,
            hook_client_name=client_name,
            hook_server_url=args.url,
        ),
        logger=LOGGER,
    )

    def _on_source_command(payload: SourceCommandPayload) -> None:
        create_task(streamer.handle_source_command(payload, streaming))

    def _on_format_request(payload: InputStreamRequestFormatSource) -> None:
        create_task(streamer.handle_format_request(payload))

    client_any.add_source_command_listener(_on_source_command)
    client_any.add_input_stream_request_format_listener(_on_format_request)

    def _on_disconnect() -> None:
        streaming.clear()
        connected_event.clear()

    client.add_disconnect_listener(_on_disconnect)

    async def _connect_loop() -> None:
        backoff = 1.0
        while True:
            if not client.connected:
                try:
                    LOGGER.info("Connecting to Sendspin server at %s", args.url)
                    await client.connect(args.url)
                    connected_event.set()
                    backoff = 1.0
                    initial_state = (
                        SourceStateType.STREAMING if streaming.is_set() else SourceStateType.IDLE
                    )
                    if streaming.is_set():
                        await streamer.send_input_stream_start()
                    await streamer.send_state(initial_state)
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning("Source connection failed: %s", err)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2.0, 30.0)
                    continue
            await asyncio.sleep(1.0)

    connect_task = create_task(_connect_loop())
    stream_task = create_task(streamer.run(streaming))
    try:
        await asyncio.gather(connect_task, stream_task)
    except asyncio.CancelledError:
        return 0
    finally:
        connect_task.cancel()
        stream_task.cancel()
        await client.disconnect()
    return 0


async def _run_serve_mode(args: argparse.Namespace) -> int:
    """Run the server mode."""
    from sendspin.serve import ServeConfig, run_server

    # Load settings for serve mode
    settings = await get_serve_settings()

    # Apply settings defaults
    if args.port is None:
        args.port = settings.listen_port or 8927
    if args.name is None:
        args.name = settings.name or "Sendspin Server"
    if args.log_level is None:
        args.log_level = settings.log_level or "INFO"

    # Set up logging
    logging.basicConfig(level=getattr(logging, args.log_level))

    # Determine audio source: CLI > --demo > settings
    if args.demo:
        source = "http://retro.dancewave.online/retrodance.mp3"
        print(f"Demo mode enabled, serving URL {source}")
    elif args.source:
        source = args.source
    elif settings.source:
        source = settings.source
        print(f"Using source from settings: {source}")
    else:
        print("Error: either provide a source or use --demo")
        return 1

    serve_config = ServeConfig(
        source=source,
        source_format=args.source_format or settings.source_format,
        port=args.port,
        name=args.name,
        clients=args.clients or settings.clients,
    )
    return await run_server(serve_config)


async def _run_daemon_mode(args: argparse.Namespace, settings: ClientSettings) -> int:
    """Run the client in daemon mode (no UI)."""
    from sendspin.daemon.daemon import DaemonArgs, SendspinDaemon

    client_id, client_name = _resolve_client_info(args.id, args.name)
    if args.source_device is not None and args.source_input == "sine":
        args.source_input = "linein"
    if args.source_device is not None and args.source_input == "linein":
        if args.source_sample_rate == 48000 and args.source_channels == 2:
            default_rate, default_channels = _resolve_input_defaults(args.source_device)
            if default_rate:
                args.source_sample_rate = default_rate
            if default_channels:
                args.source_channels = default_channels

    daemon_args = DaemonArgs(
        audio_device=_resolve_audio_device(args.audio_device),
        url=args.url,
        client_id=client_id,
        client_name=client_name,
        settings=settings,
        static_delay_ms=args.static_delay_ms,
        listen_port=args.listen_port,
        use_mpris=args.use_mpris,
        hook_start=args.hook_start,
        hook_stop=args.hook_stop,
        source_enabled=args.source,
        source_input=args.source_input,
        source_device=args.source_device,
        source_codec=args.source_codec,
        source_sample_rate=args.source_sample_rate,
        source_channels=args.source_channels,
        source_bit_depth=args.source_bit_depth,
        source_frame_ms=args.source_frame_ms,
        source_sine_hz=args.source_sine_hz,
        source_signal_threshold_db=args.signal_threshold_db,
        source_signal_hold_ms=args.signal_hold,
        source_hook_play=args.source_hook_play,
        source_hook_pause=args.source_hook_pause,
        source_hook_next=args.source_hook_next,
        source_hook_previous=args.source_hook_previous,
        source_hook_activate=args.source_hook_activate,
        source_hook_deactivate=args.source_hook_deactivate,
    )

    daemon = SendspinDaemon(daemon_args)
    return await daemon.run()


def main() -> int:
    """Run the CLI client."""
    args = parse_args(sys.argv[1:])

    # Handle serve subcommand
    if args.command == "serve":
        try:
            return asyncio.run(_run_serve_mode(args))
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"Server error: {e}")
            traceback.print_exc()
            return 1

    if args.command == "source":
        if args.source_command != "run":
            print("Error: source requires a subcommand (use: source run)")
            return 1
        try:
            return asyncio.run(_run_source_mode(args))
        except KeyboardInterrupt:
            return 0
        except CLIError as e:
            print(f"Error: {e}")
            return e.exit_code
        except Exception as e:
            print(f"Source error: {e}")
            traceback.print_exc()
            return 1

    # Handle --list-audio-devices before starting async runtime
    if args.list_audio_devices:
        list_audio_devices()
        return 0
    if args.list_input_devices:
        list_input_devices()
        return 0

    if args.list_servers:
        asyncio.run(list_servers())
        return 0

    if args.list_clients:
        asyncio.run(list_clients())
        return 0

    try:
        return asyncio.run(_run_client_mode(args))
    except CLIError as e:
        print(f"Error: {e}")
        return e.exit_code
    except OSError as e:
        if "PortAudio library not found" in str(e):
            print(PORTAUDIO_NOT_FOUND_MESSAGE)
            return 1
        raise


async def _run_client_mode(args: argparse.Namespace) -> int:
    """Run the client in TUI or daemon mode."""
    # Handle deprecated --headless flag early so all downstream logic
    # can simply check args.command == "daemon".
    if getattr(args, "headless", False):
        print("Warning: --headless is deprecated. Use 'sendspin daemon' instead.")
        print("Routing to daemon mode...\n")
        args.command = "daemon"

    is_daemon = args.command == "daemon"
    settings_dir = getattr(args, "settings_dir", None)
    settings = await get_client_settings("daemon" if is_daemon else "tui", settings_dir)

    # Apply settings as defaults for CLI arguments (CLI > settings > hard-coded)
    url_from_settings = False
    if args.url is None and settings.last_server_url:
        args.url = settings.last_server_url
        url_from_settings = True
    if args.name is None:
        args.name = settings.name
    if args.id is None:
        args.id = settings.client_id
    if args.audio_device is None:
        args.audio_device = settings.audio_device
    if args.static_delay_ms is None and settings.static_delay_ms != 0.0:
        args.static_delay_ms = settings.static_delay_ms
    if args.log_level is None:
        args.log_level = settings.log_level or "INFO"
    if is_daemon and getattr(args, "listen_port", None) is None:
        args.listen_port = settings.listen_port or 8928
    args.use_mpris = not args.disable_mpris and settings.use_mpris

    # Apply hook settings (CLI > settings)
    if args.hook_start is None:
        args.hook_start = settings.hook_start
    if args.hook_stop is None:
        args.hook_stop = settings.hook_stop

    if is_daemon:
        if args.source is None:
            args.source = settings.source_enabled
        if args.source_input is None:
            args.source_input = settings.source_input
        if args.source_device is None:
            args.source_device = settings.source_device
        if args.source_codec is None:
            args.source_codec = settings.source_codec
        if args.source_sample_rate is None:
            args.source_sample_rate = settings.source_sample_rate
        if args.source_channels is None:
            args.source_channels = settings.source_channels
        if args.source_bit_depth is None:
            args.source_bit_depth = settings.source_bit_depth
        if args.source_frame_ms is None:
            args.source_frame_ms = settings.source_frame_ms
        if args.source_sine_hz is None:
            args.source_sine_hz = settings.source_sine_hz
        if args.signal_threshold_db is None:
            args.signal_threshold_db = settings.source_signal_threshold_db
        if args.signal_hold is None:
            args.signal_hold = settings.source_signal_hold_ms
        if args.source_hook_play is None:
            args.source_hook_play = settings.source_hook_play
        if args.source_hook_pause is None:
            args.source_hook_pause = settings.source_hook_pause
        if args.source_hook_next is None:
            args.source_hook_next = settings.source_hook_next
        if args.source_hook_previous is None:
            args.source_hook_previous = settings.source_hook_previous
        if args.source_hook_activate is None:
            args.source_hook_activate = settings.source_hook_activate
        if args.source_hook_deactivate is None:
            args.source_hook_deactivate = settings.source_hook_deactivate

    # Set up logging with resolved log level
    logging.basicConfig(level=getattr(logging, args.log_level))

    # Handle daemon subcommand
    if args.command == "daemon":
        return await _run_daemon_mode(args, settings)

    from sendspin.tui.app import AppArgs, SendspinApp

    client_id, client_name = _resolve_client_info(args.id, args.name)

    app_args = AppArgs(
        audio_device=_resolve_audio_device(args.audio_device),
        url=args.url,
        url_from_settings=url_from_settings,
        client_id=client_id,
        client_name=client_name,
        settings=settings,
        static_delay_ms=args.static_delay_ms,
        use_mpris=args.use_mpris,
        hook_start=args.hook_start,
        hook_stop=args.hook_stop,
    )

    app = SendspinApp(app_args)
    return await app.run()


if __name__ == "__main__":
    raise SystemExit(main())
