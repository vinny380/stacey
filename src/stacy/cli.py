from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError

from stacy.alarms import AlarmService
from stacy.config import Settings
from stacy.memory import MemoryRepo

logger = logging.getLogger(__name__)


async def existing_backend(port: int) -> bool:
    """Recognize Stacy before constructing a second scheduler or audio bridge."""
    import httpx

    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=2) as client:
        try:
            result = await client.get(f"http://127.0.0.1:{port}/health")
        except httpx.ConnectError:
            return False
        except httpx.HTTPError:
            raise RuntimeError(f"Port {port} is busy; its service could not be verified") from None
        if result.status_code == 200 and result.json().get("service") == "stacy":
            return True
        # Older Stacy versions have no health endpoint. MCP initialization can
        # recognize them without creating alarm state or calling any tools.
        from fastmcp import Client

        try:
            async with Client(f"http://127.0.0.1:{port}/mcp", timeout=2) as mcp:
                if mcp.initialize_result.serverInfo.name == "Stacy":
                    return True
        except Exception:
            pass
        raise RuntimeError(f"Port {port} is already used by another service")


async def serve(settings: Settings, port: int, with_voice: bool) -> None:
    import uvicorn

    from stacy.sound import Ringer
    from stacy.tools import create_tools

    ringer = Ringer(settings.data_dir / "alarm.wav")
    alarms = AlarmService(
        settings.data_dir / "state.sqlite3",
        settings.timezone,
        ringer.start,
        ringer.stop,
    )
    voice_task = None
    music_task = None
    text_agent = None
    notifications = None
    try:
        memory = MemoryRepo(settings.memory_dir)
        tools = create_tools(
            alarms,
            memory,
            mcp_config=settings.mcp_config,
        )
        spotify_backend = os.environ.get("STACY_SPOTIFY_BACKEND", "auto")
        if spotify_backend not in {"auto", "webapi", "soloist"}:
            raise ValueError("STACY_SPOTIFY_BACKEND must be auto, webapi or soloist")
        use_soloist = spotify_backend == "soloist" or (
            spotify_backend == "auto"
            and (os.environ.get("SOLOIST_API_KEY") or os.environ.get("SOLOIST_WS_URL"))
        )
        if use_soloist:
            from stacy.soloist import Soloist

            Soloist(os.environ.get("SOLOIST_WS_URL", "ws://127.0.0.1:9090")).register(tools)
        elif spotify_backend == "webapi" or os.environ.get("SPOTIFY_CLIENT_ID"):
            from stacy.spotify import Spotify

            if not os.environ.get("SPOTIFY_CLIENT_ID"):
                raise ValueError("Set SPOTIFY_CLIENT_ID for the Spotify Web API backend")
            redirect_uri = os.environ.get(
                "SPOTIFY_REDIRECT_URI", f"http://127.0.0.1:{port}/callback"
            )
            if urlparse(redirect_uri).port != port:
                raise ValueError("SPOTIFY_REDIRECT_URI must use the same port as Stacy")
            Spotify(
                os.environ["SPOTIFY_CLIENT_ID"],
                redirect_uri,
                settings.data_dir / "spotify-token.json",
            ).register(tools)
        await alarms.start()
        if (
            os.environ.get("STACY_AGENT_TOKEN")
            or os.environ.get("STACY_MESSAGING_ENABLED") == "true"
            or Path(os.environ.get("STACY_AGENT_TOKEN_FILE", ".cache/agent-token")).exists()
        ):
            from stacy.agent import TextAgent
            from stacy.messaging import bridge_token, register_messaging
            from stacy.notifications import Notifications

            token = bridge_token()
            notifications = Notifications(settings.data_dir, token)
            notifications.register(tools)
            text_agent = TextAgent(settings, tools)
            register_messaging(tools, text_agent, token)
        from starlette.responses import JSONResponse

        @tools.custom_route("/health", methods=["GET"])
        async def health(request):
            return JSONResponse({"service": "stacy", "ready": True})

        server = uvicorn.Server(
            uvicorn.Config(
                tools.http_app(path="/mcp"),
                host="127.0.0.1",
                port=port,
                log_level="warning",
            )
        )

        async def readiness():
            while not server.started:
                await asyncio.sleep(0.05)
            logger.warning("Stacy's local tools and alarms are running on 127.0.0.1:%s", port)

        readiness_task = asyncio.create_task(readiness())

        async def voice_after_start():
            while not server.started:
                await asyncio.sleep(0.05)
            try:
                from stacy.voice import run_voice

                await run_voice(settings, f"http://127.0.0.1:{port}/mcp")
            except Exception as error:
                logger.error("Voice unavailable; local alarms remain active: %s", error)

        if with_voice:
            if (
                use_soloist
                and sys.platform == "darwin"
                and os.environ.get("SOLOIST_HOST_BRIDGE", "true").lower()
                not in {"false", "0", "off"}
            ):

                async def music_bridge():
                    from stacy.soloist_host import run_host_bridge

                    try:
                        await run_host_bridge()
                    except (OSError, RuntimeError, ValueError) as error:
                        logger.error("Soloist audio bridge unavailable: %s", error)

                music_task = asyncio.create_task(music_bridge())
            voice_task = asyncio.create_task(voice_after_start())
        try:
            await server.serve()
        finally:
            readiness_task.cancel()
            try:
                await readiness_task
            except asyncio.CancelledError:
                pass
    finally:
        if music_task:
            music_task.cancel()
            try:
                await music_task
            except asyncio.CancelledError:
                pass
        if voice_task:
            voice_task.cancel()
            try:
                await voice_task
            except asyncio.CancelledError:
                pass
        await ringer.close()
        if text_agent:
            await text_agent.close()
        if notifications:
            notifications.close()
        alarms.close()


def doctor(settings: Settings) -> dict:
    from stacy.mcp_gateway import MCPGateway, load_servers

    return {
        "timezone": settings.timezone,
        "data_dir": str(settings.data_dir),
        "memory_dir": str(settings.memory_dir),
        "mcp_config": str(settings.mcp_config) if settings.mcp_config else None,
        "mcp_servers": MCPGateway(load_servers(settings.mcp_config)).metadata(),
        "git": bool(shutil.which("git")),
        "alarm_player": shutil.which("afplay") or shutil.which("aplay"),
        "voice_sdk": importlib.util.find_spec("pipecat") is not None,
        "audio_sdk": importlib.util.find_spec("pyaudio") is not None,
        "configured": {
            name: bool(os.environ.get(name))
            for name in (
                "OPENAI_API_KEY",
                "EXA_API_KEY",
                "E2B_API_KEY",
                "SPOTIFY_CLIENT_ID",
                "SOLOIST_API_KEY",
                "COMPOSIO_API_KEY",
            )
        },
        "spotify_backend": os.environ.get("STACY_SPOTIFY_BACKEND", "auto"),
    }


async def command(args, settings: Settings):
    if args.command == "doctor":
        return doctor(settings)
    if args.command in {"start", "serve"}:
        if await existing_backend(args.port):
            if args.command == "start":
                logger.info("Attaching voice to Stacy's running backend on port %s", args.port)
                from stacy.voice import run_voice

                await run_voice(settings, f"http://127.0.0.1:{args.port}/mcp")
            else:
                logger.info("Stacy's backend is already running on port %s", args.port)
            return None
        await serve(settings, args.port, args.command == "start")
        return None
    if args.command == "voice":
        from stacy.voice import run_voice

        await run_voice(settings, f"http://127.0.0.1:{args.port}/mcp")
        return None
    if args.command == "soloist-host":
        from stacy.soloist_host import run_host_bridge

        await run_host_bridge()
        return None

    # Development CLI: the same SQLite state is read by the live tool service.
    # A live scheduler must be notified through MCP when adding/cancelling jobs.
    from fastmcp import Client

    async with Client(f"http://127.0.0.1:{args.port}/mcp") as client:
        if args.command == "timer":
            result = await client.call_tool(
                "set_timer", {"seconds": args.seconds, "label": args.label}
            )
        elif args.command == "alarm":
            result = await client.call_tool("set_alarm", {"when": args.when, "label": args.label})
        elif args.command == "list":
            result = await client.call_tool("list_alarms", {"include_finished": args.all})
        elif args.command == "cancel":
            result = await client.call_tool("cancel_alarm", {"alarm_id": args.id})
        elif args.command == "stop":
            result = await client.call_tool("stop_ringing", {"alarm_id": args.id})
        elif args.command == "snooze":
            result = await client.call_tool(
                "snooze_alarm", {"alarm_id": args.id, "seconds": args.seconds}
            )
        elif args.command == "connect-spotify":
            result = await client.call_tool("spotify_connect", {})
        elif args.command in {"spotify-status", "spotify-devices"}:
            result = await client.call_tool(args.command.replace("-", "_"), {})
        return result.data


def main() -> None:
    parser = argparse.ArgumentParser(prog="stacy")
    parser.add_argument("--port", type=int, default=8765)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "doctor",
        "start",
        "serve",
        "voice",
        "connect-spotify",
        "spotify-status",
        "spotify-devices",
        "soloist-host",
    ):
        commands.add_parser(name)
    timer = commands.add_parser("timer")
    timer.add_argument("seconds", type=float)
    timer.add_argument("--label", default="Timer")
    alarm = commands.add_parser("alarm")
    alarm.add_argument("when", help="HH:MM or ISO datetime")
    alarm.add_argument("--label", default="Wake-up alarm")
    listing = commands.add_parser("list")
    listing.add_argument("--all", action="store_true")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("id")
    stop = commands.add_parser("stop")
    stop.add_argument("--id")
    snooze = commands.add_parser("snooze")
    snooze.add_argument("id")
    snooze.add_argument("--seconds", type=float, default=540)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("stacy").setLevel(logging.INFO)
    try:
        result = asyncio.run(command(args, Settings.load()))
        if result is not None:
            print(json.dumps(result, indent=2, ensure_ascii=False))
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError, OSError, ToolError) as error:
        parser.exit(1, f"stacy: {error}\n")
