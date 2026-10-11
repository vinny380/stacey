from __future__ import annotations

import os

from fastmcp import FastMCP


def add_integrations(server: FastMCP) -> None:
    """Advertise only integrations with credentials, using their official async SDKs."""
    if os.environ.get("EXA_API_KEY"):
        from exa_py import AsyncExa

        @server.tool
        async def web_search(query: str) -> list[dict]:
            """Search the web and return source URLs with relevant excerpts."""
            response = await AsyncExa(api_key=os.environ["EXA_API_KEY"]).search(
                query, num_results=5, type="auto", contents={"highlights": True}
            )
            return [
                {
                    "title": result.title,
                    "url": result.url,
                    "highlights": result.highlights,
                }
                for result in response.results
            ]

    if os.environ.get("E2B_API_KEY"):
        from e2b_code_interpreter import AsyncSandbox

        @server.tool
        async def run_python(code: str) -> dict:
            """Run Python in a fresh agent-owned E2B sandbox, not on the user's computer.

            Each call is independent; sandbox files do not survive this call.
            Return stdout, stderr and textual results. Runtime limit is 60 seconds.
            """
            sandbox = await AsyncSandbox.create(api_key=os.environ["E2B_API_KEY"], timeout=120)
            try:
                execution = await sandbox.run_code(code, timeout=60)
                return {
                    "stdout": execution.logs.stdout,
                    "stderr": execution.logs.stderr,
                    "results": [result.text for result in execution.results if result.text],
                    "error": (
                        {
                            "name": execution.error.name,
                            "value": execution.error.value,
                            "traceback": execution.error.traceback,
                        }
                        if execution.error
                        else None
                    ),
                }
            finally:
                await sandbox.kill()
