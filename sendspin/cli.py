"""Command-line interface for running a Sendspin client."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

import sounddevice

from sendspin.app import AppConfig, SendspinApp
from sendspin.discovery import discover_servers


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for the Sendspin client."""
    parser = argparse.ArgumentParser(description="Run a Sendspin CLI client")
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
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level to use",
    )
    parser.add_argument(
        "--static-delay-ms",
        type=float,
        default=0.0,
        help="Extra playback delay in milliseconds applied after clock sync",
    )
    parser.add_argument(
        "--audio-device",
        type=int,
        default=None,
        help=(
            "Audio output device ID (e.g., 0, 1, 2). "
            "Use --list-audio-devices to see available devices."
        ),
    )
    parser.add_argument(
        "--list-audio-devices",
        action="store_true",
        help="List available audio output devices and exit",
    )
    parser.add_argument(
        "--list-servers",
        action="store_true",
        help="Discover and list available Sendspin servers on the network",
    )
    return parser.parse_args(argv)


def list_audio_devices() -> None:
    """List all available audio output devices."""
    try:
        devices = sounddevice.query_devices()
        default_device = sounddevice.default.device[1]  # Output device index

        print("Available audio output devices:")  # noqa: T201
        print("-" * 80)  # noqa: T201
        for i, device in enumerate(devices):
            if device["max_output_channels"] > 0:
                default_marker = " (default)" if i == default_device else ""
                print(  # noqa: T201
                    f"  [{i}] {device['name']}{default_marker}\n"
                    f"       Channels: {device['max_output_channels']}, "
                    f"Sample rate: {device['default_samplerate']} Hz"
                )
    except Exception as e:  # noqa: BLE001
        print(f"Error listing audio devices: {e}")  # noqa: T201
        sys.exit(1)


async def list_servers() -> None:
    """Discover and list all Sendspin servers on the network."""
    print("Searching for Sendspin servers...")  # noqa: T201
    try:
        servers = await discover_servers(discovery_time=3.0)
        if not servers:
            print("No Sendspin servers found.")  # noqa: T201
            return

        print(f"\nFound {len(servers)} server(s):")  # noqa: T201
        print("-" * 80)  # noqa: T201
        for server in servers:
            print(f"  {server.name}")  # noqa: T201
            print(f"    URL:  {server.url}")  # noqa: T201
            print(f"    Host: {server.host}:{server.port}")  # noqa: T201
        if servers:
            print(f"\nTo connect to a server:\n  sendspin --url {servers[0].url}")  # noqa: T201
    except Exception as e:  # noqa: BLE001
        print(f"Error discovering servers: {e}")  # noqa: T201
        sys.exit(1)


def main() -> int:
    """Run the CLI client."""
    # Handle --list-audio-devices before starting async runtime
    args = parse_args(sys.argv[1:])
    if args.list_audio_devices:
        list_audio_devices()
        return 0

    if args.list_servers:
        asyncio.run(list_servers())
        return 0

    # Create config from CLI arguments
    config = AppConfig(
        url=args.url,
        client_id=args.id,
        client_name=args.name,
        static_delay_ms=args.static_delay_ms,
        audio_device=args.audio_device,
        log_level=args.log_level,
    )

    # Run the application
    app = SendspinApp(config)
    return asyncio.run(app.run())


if __name__ == "__main__":
    raise SystemExit(main())
