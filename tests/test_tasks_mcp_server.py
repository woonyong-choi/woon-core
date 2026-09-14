from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_tasks_mcp_server_exposes_only_the_markdown_task_tools(tmp_path: Path) -> None:
    async def exercise() -> None:
        environment = dict(os.environ)
        environment["WOON_KNOWLEDGE_ROOT"] = str(tmp_path)
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "woon_core.tasks.mcp_server"],
            env=environment,
            cwd=Path.cwd(),
        )
        async with (
            stdio_client(parameters) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert names == {
                "woon_tasks_find",
                "woon_tasks_upsert_recurring_todo",
                "woon_tasks_upsert_goal",
                "woon_tasks_materialize_due",
                "woon_tasks_complete",
            }

    anyio.run(exercise)
