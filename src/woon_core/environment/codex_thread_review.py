"""Read-only preparation and requery for the documented Codex thread/delete API.

Deletion is deliberately not dispatched here. A reviewed ancestor closure, content
retention decision and fresh Desktop runtime state are required before that action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from woon_core.errors import WoonError

SOURCE_KINDS = [
    "cli",
    "vscode",
    "exec",
    "appServer",
    "subAgent",
    "subAgentReview",
    "subAgentCompact",
    "subAgentThreadSpawn",
    "subAgentOther",
    "unknown",
]
READ_METHODS = {"initialize", "thread/read", "thread/list"}
Rpc = Callable[[str, dict[str, Any]], dict[str, Any]]


class ReadOnlyAppServer:
    """Use official stdio transport without starting/resuming a task or a daemon."""

    def __init__(self, codex: str = "codex") -> None:
        self.process = subprocess.Popen(
            [codex, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.sequence = 0
        self.buffer = b""

    def __enter__(self) -> Self:
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "woon_thread_review", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self._send({"method": "initialized", "params": {}})
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()

    def _send(self, request: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        self.process.stdin.flush()

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method not in READ_METHODS:
            raise WoonError("thread review adapter does not dispatch mutations")
        return self._request(method, params)

    def _notification(self, value: dict[str, Any]) -> None:
        """Read-only review ignores notifications; a reviewed writer may record them."""

    def _receive(self, deadline: float) -> dict[str, Any]:
        assert self.process.stdout is not None
        while time.monotonic() < deadline:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    value = json.loads(line)
                except ValueError as error:
                    raise WoonError("official App Server returned invalid JSON") from error
                if not isinstance(value, dict):
                    raise WoonError("official App Server returned an invalid message")
                return value
            ready, _, _ = select.select(
                [self.process.stdout], [], [], max(0, deadline - time.monotonic())
            )
            if not ready:
                break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                break
            self.buffer += chunk
        raise WoonError("official App Server timed out; result is unverified")

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        self._send({"id": self.sequence, "method": method, "params": params})
        assert self.process.stdout is not None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            value = self._receive(deadline)
            self._notification(value)
            if value.get("id") != self.sequence:
                continue
            if "error" in value:
                raise WoonError(f"official App Server {method} failed; result is unverified")
            result = value.get("result")
            if not isinstance(result, dict):
                raise WoonError(f"official App Server {method} returned an invalid result")
            return result
        raise WoonError(f"official App Server {method} timed out; absence is unverified")


def _listed(rpc: Rpc, filters: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for archived in (False, True):
        cursor = None
        seen = set()
        while True:
            response = rpc(
                "thread/list",
                {
                    **filters,
                    "archived": archived,
                    "cursor": cursor,
                    "limit": 100,
                    "modelProviders": [],
                    "sourceKinds": SOURCE_KINDS,
                },
            )
            data = response.get("data")
            if (
                "nextCursor" not in response
                or not isinstance(data, list)
                or not all(
                    isinstance(row, dict) and isinstance(row.get("id"), str) and bool(row["id"])
                    for row in data
                )
            ):
                raise WoonError("thread/list did not return a complete page")
            rows.extend(data)
            cursor = response.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise WoonError("thread/list returned an invalid or repeated cursor")
            seen.add(cursor)
    return rows


def plan_thread_deletion(rpc: Rpc, root_ids: list[str], protected_ids: list[str]) -> dict[str, Any]:
    if not root_ids or len(root_ids) != len(set(root_ids)) or any(not x for x in root_ids):
        raise WoonError("explicit distinct root thread IDs are required")
    affected: dict[str, dict[str, Any]] = {}
    for root_id in root_ids:
        if root_id in protected_ids:
            raise WoonError("a protected thread is a requested deletion root")
        root = rpc("thread/read", {"threadId": root_id, "includeTurns": False}).get("thread")
        if not isinstance(root, dict) or root.get("id") != root_id:
            raise WoonError("thread/read identity does not match the requested root")
        descendants = _listed(rpc, {"ancestorThreadId": root_id})
        if any(row["id"] in root_ids for row in descendants):
            raise WoonError("requested deletion roots overlap; review the ancestor closure once")
        for row in [root, *descendants]:
            thread_id = row.get("id")
            if not isinstance(thread_id, str) or not isinstance(row.get("cwd"), str):
                raise WoonError("thread inventory is missing identity or cwd")
            if thread_id in protected_ids:
                raise WoonError("a protected thread is in the deletion ancestor closure")
            if row.get("ephemeral") is not False:
                raise WoonError("ephemeral or unknown persistence requires separate review")
            summary = {
                key: row.get(key)
                for key in (
                    "id",
                    "name",
                    "cwd",
                    "parentThreadId",
                    "updatedAt",
                    "ephemeral",
                    "status",
                )
            }
            if thread_id in affected and affected[thread_id] != summary:
                raise WoonError("thread changed during inventory; read again")
            affected[thread_id] = summary
    inventory = [affected[key] for key in sorted(affected)]
    digest = hashlib.sha256(json.dumps(inventory, sort_keys=True).encode()).hexdigest()
    return {
        "action": "thread-delete-review",
        "root_ids": root_ids,
        "affected": inventory,
        "metadata_sha256": digest,
        "content_reviewed": False,
        "desktop_idle_verified": False,
        "deletion_requests": [
            {"method": "thread/delete", "params": {"threadId": x}} for x in root_ids
        ],
        "delete_calls": 0,
        "project_registration_removal_supported": False,
    }


def requery_thread_absence(rpc: Rpc, plan: dict[str, Any]) -> dict[str, Any]:
    affected = plan.get("affected")
    if not isinstance(affected, list) or not affected:
        raise WoonError("requery requires the exact prior affected-thread inventory")
    if not all(
        isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("cwd"), str)
        for row in affected
    ):
        raise WoonError("requery inventory is missing identity or cwd")
    expected = {row["id"] for row in affected}
    rows = _listed(rpc, {"cwd": sorted({row["cwd"] for row in affected})})
    remaining = sorted(expected.intersection(row.get("id") for row in rows))
    # This is a read observation, not proof that a delete command was executed successfully.
    return {
        "action": "thread-delete-requery",
        "remaining_ids": remaining,
        "all_absent": not remaining,
        "delete_calls": 0,
        "delete_success_receipt": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "requery"))
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--thread-id", action="append", default=[])
    parser.add_argument("--protected-thread-id", action="append", default=[])
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.action == "plan" and (not args.thread_id or args.plan):
        parser.error("plan requires explicit --thread-id values and no --plan")
    if args.action == "requery" and (args.plan is None or args.thread_id):
        parser.error("requery requires --plan and no --thread-id")
    with ReadOnlyAppServer(args.codex) as client:
        result = (
            plan_thread_deletion(client.request, args.thread_id, args.protected_thread_id)
            if args.action == "plan"
            else requery_thread_absence(client.request, json.loads(args.plan.read_bytes()))
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
