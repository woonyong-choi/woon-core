"""Local stdio MCP server for Woon's Obsidian task workflow."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import ToolAnnotations

from woon_core.tasks.factory import build_task_service
from woon_core.tasks.service import TaskService

FastMCPSettings.model_rebuild()

mcp = FastMCP(
    "Woon Obsidian Tasks",
    instructions=(
        "Manage the user's private Markdown task sources and daily task blocks. "
        "Use a stated purpose before creating a routine, materialize before completion, "
        "and never operate a graphical task application or an external task database."
    ),
    json_response=True,
)


@lru_cache(maxsize=1)
def _service() -> TaskService:
    return build_task_service()


@mcp.tool(
    name="woon_tasks_find",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def find_tasks(query: str, day: str | None = None) -> dict[str, object]:
    """Find matching routines and today's completion state without changing Markdown."""

    target_day = _parse_day(day) if day else None
    tasks = _service().find(query, on_date=target_day)
    return {"query": query, "count": len(tasks), "tasks": [asdict(task) for task in tasks]}


@mcp.tool(
    name="woon_tasks_upsert_recurring_todo",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def upsert_recurring_todo(
    task_id: str,
    title: str,
    purpose: str,
    area: str,
    start_date: str | None = None,
    goal_id: str | None = None,
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Create or update a daily Markdown routine. purpose is required, never inferred."""

    result = _service().upsert_recurring_todo(
        task_id=task_id,
        title=title,
        purpose=purpose,
        area=area,
        start_date=_parse_day(start_date) if start_date else None,
        goal_id=goal_id,
        expected_revision=expected_revision,
    )
    return {"created": result.created, "changed": result.changed, "routine": asdict(result.routine)}


@mcp.tool(
    name="woon_tasks_upsert_goal",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def upsert_goal(
    goal_id: str,
    title: str,
    purpose: str,
    completion_condition: str,
    end_date: str | None = None,
    current_value: float | None = None,
    target_value: float | None = None,
    target_operator: str | None = None,
    unit: str | None = None,
    measurement_confirmed: bool = False,
    status: str = "active",
    expected_revision: str | None = None,
) -> dict[str, object]:
    """Create or update a user-editable daily-routine goal and its stop condition."""

    result = _service().upsert_goal(
        goal_id=goal_id,
        title=title,
        purpose=purpose,
        completion_condition=completion_condition,
        end_date=_parse_day(end_date) if end_date else None,
        current_value=current_value,
        target_value=target_value,
        target_operator=target_operator,
        unit=unit,
        measurement_confirmed=measurement_confirmed,
        status=status,
        expected_revision=expected_revision,
    )
    return {"created": result.created, "changed": result.changed, "goal": asdict(result.goal)}


@mcp.tool(
    name="woon_tasks_materialize_due",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def materialize_due(day: str | None = None) -> dict[str, object]:
    """Create the requested day's note once and refresh only its managed task block."""

    result = _service().materialize_due(on_date=_parse_day(day) if day else None)
    return asdict(result)


@mcp.tool(
    name="woon_tasks_complete",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def complete_task(task_id: str, day: str | None = None) -> dict[str, object]:
    """Mark exactly one already-materialized task complete in its daily Markdown note."""

    result = _service().complete(task_id=task_id, on_date=_parse_day(day) if day else None)
    return asdict(result)


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("day must use YYYY-MM-DD") from error


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
