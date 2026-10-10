import asyncio
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from stacy.integrations import add_integrations


def test_unconfigured_services_are_not_advertised(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("E2B_API_KEY", raising=False)

    async def scenario():
        server = FastMCP("test")
        add_integrations(server)
        async with Client(server) as client:
            assert await client.list_tools() == []

    asyncio.run(scenario())


def test_search_returns_sources(monkeypatch):
    exa = pytest.importorskip("exa_py")
    monkeypatch.setenv("EXA_API_KEY", "offline-test")
    monkeypatch.delenv("E2B_API_KEY", raising=False)

    async def search(self, query, **kwargs):
        return SimpleNamespace(
            results=[SimpleNamespace(title="Source", url="https://example.com", highlights=[query])]
        )

    monkeypatch.setattr(exa.AsyncExa, "search", search)

    async def scenario():
        server = FastMCP("test")
        add_integrations(server)
        async with Client(server) as client:
            result = await client.call_tool("web_search", {"query": "test question"})
            assert result.data[0]["url"] == "https://example.com"
            assert result.data[0]["highlights"] == ["test question"]

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "code_error", "transport_error"])
def test_sandbox_is_cleaned_up_and_sdk_errors_are_serializable(monkeypatch, outcome):
    sdk = pytest.importorskip("e2b_code_interpreter")
    from e2b_code_interpreter.models import Execution, ExecutionError, Logs

    monkeypatch.setenv("E2B_API_KEY", "offline-test")
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    killed = []

    class Sandbox:
        async def run_code(self, code, **kwargs):
            if outcome == "transport_error":
                raise RuntimeError("Connection lost")
            return Execution(
                logs=Logs(stdout=["hello"], stderr=[]),
                error=(
                    ExecutionError("ValueError", "bad input", "trace")
                    if outcome == "code_error"
                    else None
                ),
            )

        async def kill(self):
            killed.append(True)

    async def create(**kwargs):
        return Sandbox()

    monkeypatch.setattr(sdk.AsyncSandbox, "create", create)

    async def scenario():
        server = FastMCP("test")
        add_integrations(server)
        async with Client(server) as client:
            if outcome == "transport_error":
                with pytest.raises(ToolError, match="Connection lost"):
                    await client.call_tool("run_python", {"code": "print('hello')"})
            else:
                result = await client.call_tool("run_python", {"code": "print('hello')"})
                assert result.data["stdout"] == ["hello"]
                if outcome == "code_error":
                    assert result.data["error"]["name"] == "ValueError"
                else:
                    assert result.data["error"] is None
        assert killed == [True]

    asyncio.run(scenario())
