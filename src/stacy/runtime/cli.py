from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import shutil
import sys
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from urllib.parse import urlparse

from fastmcp.exceptions import ToolError

from stacy.alarms.service import AlarmService
from stacy.memory.repository import MemoryRepo
from stacy.runtime.settings import Settings

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


def backend_http_app(tools):
    """Construct the local transport shared by voice, messaging and developer clients."""
    # All durable state belongs to application services. Voice must not hold a
    # transport session that disappears when the local backend is restarted.
    return tools.http_app(path="/mcp", stateless_http=True)


async def serve(settings: Settings, port: int, with_voice: bool) -> None:
    import uvicorn

    from stacy.alarms.audio import Ringer
    from stacy.runtime.tools import create_tools

    async with AsyncExitStack() as resources:
        ringer = Ringer(settings.data_dir / "alarm.wav")
        resources.push_async_callback(ringer.close)
        alarms = AlarmService(
            settings.data_dir / "state.sqlite3", settings.timezone, ringer.start, ringer.stop
        )
        resources.callback(alarms.close)

        def background(coroutine) -> None:
            resources.push_async_callback(stop_task, asyncio.create_task(coroutine))

        memory = MemoryRepo(settings.memory_dir)
        tools = create_tools(
            alarms,
            memory,
            mcp_config=settings.mcp_config,
            prewarm_spotify=True,
        )
        spotify_backend = settings.spotify_backend
        use_soloist = spotify_backend == "soloist" or (
            spotify_backend == "auto"
            and (os.environ.get("SOLOIST_API_KEY") or os.environ.get("SOLOIST_WS_URL"))
        )
        if use_soloist:
            from stacy.integrations.spotify.soloist import Soloist

            Soloist(os.environ.get("SOLOIST_WS_URL", "ws://127.0.0.1:9090")).register(tools)
        elif spotify_backend == "webapi" or os.environ.get("SPOTIFY_CLIENT_ID"):
            from stacy.integrations.spotify.web_api import Spotify

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
            from stacy.harness.text import TextAgent
            from stacy.integrations.messaging.bridge import bridge_token, register_messaging
            from stacy.integrations.messaging.notifications import Notifications

            token = bridge_token()
            notifications = Notifications(settings.data_dir, token)
            resources.callback(notifications.close)
            notifications.register(tools)
            text_agent = TextAgent(settings, tools)
            resources.push_async_callback(text_agent.close)
            register_messaging(tools, text_agent, token)
        from starlette.responses import JSONResponse

        @tools.custom_route("/health", methods=["GET"])
        async def health(request):
            return JSONResponse({"service": "stacy", "ready": True})

        server = uvicorn.Server(
            uvicorn.Config(
                backend_http_app(tools),
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
            )
        )

        async def readiness():
            while not server.started:
                await asyncio.sleep(0.05)
            logger.warning("Stacy's local tools and alarms are running on 127.0.0.1:%s", port)

        background(readiness())

        async def voice_after_start():
            while not server.started:
                await asyncio.sleep(0.05)
            try:
                from stacy.harness.voice import run_voice

                await run_voice(settings, f"http://127.0.0.1:{port}/mcp")
            except Exception as error:
                logger.error("Voice unavailable; local alarms remain active: %s", error)

        if with_voice:
            if use_soloist and sys.platform == "darwin" and settings.soloist_host_bridge:

                async def music_bridge():
                    from stacy.integrations.spotify.host_audio import run_host_bridge

                    try:
                        await run_host_bridge()
                    except (OSError, RuntimeError, ValueError) as error:
                        logger.error("Soloist audio bridge unavailable: %s", error)

                background(music_bridge())
            background(voice_after_start())
        await server.serve()


async def stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def doctor(settings: Settings) -> dict:
    from stacy.connections.gateway import MCPGateway, load_servers

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
        "spotify_backend": settings.spotify_backend,
    }


async def command(args, settings: Settings):
    if args.command == "doctor":
        return doctor(settings)
    if args.command in {"start", "serve"}:
        if await existing_backend(args.port):
            if args.command == "start":
                logger.info("Attaching voice to Stacy's running backend on port %s", args.port)
                from stacy.harness.voice import run_voice

                await run_voice(settings, f"http://127.0.0.1:{args.port}/mcp")
            else:
                logger.info("Stacy's backend is already running on port %s", args.port)
            return None
        await serve(settings, args.port, args.command == "start")
        return None
    if args.command == "voice":
        from stacy.harness.voice import run_voice

        await run_voice(settings, f"http://127.0.0.1:{args.port}/mcp")
        return None
    if args.command == "soloist-host":
        from stacy.integrations.spotify.host_audio import run_host_bridge

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
