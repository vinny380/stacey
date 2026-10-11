import asyncio
import json
import socket
from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from stacy.cli import backend_http_app


def test_voice_tool_handler_survives_backend_session_state_replacement():
    """A backend restart replaces its MCP sessions while the voice client stays alive."""
    pytest.importorskip("pipecat.services.mcp_service")
    import uvicorn
    from mcp.client.session_group import StreamableHttpParameters
    from pipecat.services.mcp_service import MCPClient

    class SwitchableBackend:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            await self.app(scope, receive, send)

    async def scenario():
        calls = []

        def new_backend():
            host = FastMCP("Stacy test backend")

            @host.tool
            def perform_action(value: str):
                calls.append(value)
                return {"value": value}

            return backend_http_app(host)

        async with AsyncExitStack() as stack:
            first = new_backend()
            await stack.enter_async_context(first.router.lifespan_context(first))
            application = SwitchableBackend(first)
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            server = uvicorn.Server(
                uvicorn.Config(
                    application, lifespan="off", log_level="critical", access_log=False,
                    timeout_graceful_shutdown=0.1,
                )
            )
            running = asyncio.create_task(server.serve(sockets=[sock]))
            while not server.started:
                if running.done():
                    await running
                await asyncio.sleep(0.01)
            try:
                async with MCPClient(
                    StreamableHttpParameters(url=f"http://127.0.0.1:{port}/mcp", timeout=5)
                ) as voice:
                    tools = await voice.tools()
                    handler = next(
                        t.handler for t in tools.standard_tools if t.name == "perform_action"
                    )
                    outputs = []

                    async def result_callback(result, **kwargs):
                        outputs.append(result)

                    async def invoke(value):
                        await handler(SimpleNamespace(
                            function_name="perform_action", tool_call_id=value,
                            arguments={"value": value}, result_callback=result_callback,
                        ))

                    await invoke("before-restart")
                    assert json.loads(outputs[-1]) == {"value": "before-restart"}
                    # New process means a fresh session registry. Keeping HTTP alive
                    # isolates the exact 404 failure rather than a connection refusal.
                    replacement = new_backend()
                    await stack.enter_async_context(
                        replacement.router.lifespan_context(replacement)
                    )
                    application.app = replacement
                    await invoke("after-restart")
                    assert not outputs[-1].startswith("Error"), outputs[-1]
                    assert json.loads(outputs[-1]) == {"value": "after-restart"}
                    assert calls == ["before-restart", "after-restart"]
            finally:
                server.should_exit = True
                await running
                sock.close()

    asyncio.run(scenario())
