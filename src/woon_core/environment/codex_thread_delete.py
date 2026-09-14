"""Execute an explicitly reviewed permanent root-and-descendant deletion through official RPC."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from woon_core.environment.codex_thread_review import (
    ReadOnlyAppServer,
    Rpc,
    plan_thread_deletion,
    requery_thread_absence,
)
from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _content_hash(value: object) -> str:
    # Match the preparation adapter and the full-turn review evidence contract.
    return _sha(json.dumps(value, sort_keys=True).encode())


def _bound_file(reference: dict[str, Any]) -> bytes:
    path = Path(reference.get("path", ""))
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise WoonError("review evidence must be an explicit existing regular file")
    raw = path.read_bytes()
    if _sha(raw) != reference.get("sha256"):
        raise WoonError("review evidence changed; refresh the reviewed input")
    return raw


class ReviewedDeleteAppServer(ReadOnlyAppServer):
    """The inherited request method stays read-only; only delete_reviewed can mutate."""

    def __init__(self, codex: str, allowed_roots: list[str]) -> None:
        self.allowed_roots = frozenset(allowed_roots)
        self.attempted: set[str] = set()
        self.delete_responses: dict[str, dict[str, Any]] = {}
        self.deleted_notifications: list[dict[str, Any]] = []
        super().__init__(codex)

    def _notification(self, value: dict[str, Any]) -> None:
        if value.get("method") != "thread/deleted":
            return
        params = value.get("params")
        if not isinstance(params, dict) or not isinstance(params.get("threadId"), str):
            raise WoonError("invalid official thread/deleted notification")
        self.deleted_notifications.append(
            {"method": "thread/deleted", "params": {"threadId": params["threadId"]}}
        )

    def delete_reviewed(self, root_id: str, affected_ids: set[str]) -> dict[str, Any]:
        if (
            root_id not in self.allowed_roots
            or root_id in self.attempted
            or root_id not in affected_ids
        ):
            raise WoonError("delete root is unreviewed or already attempted; no retry dispatched")
        self.attempted.add(root_id)
        offset = len(self.deleted_notifications)
        response = self._request("thread/delete", {"threadId": root_id})
        self.delete_responses[root_id] = response
        if response != {}:
            raise WoonError("unexpected thread/delete response; deletion is unverified")
        deadline = time.monotonic() + 30
        while True:
            notifications = self.deleted_notifications[offset:]
            seen = {event["params"]["threadId"] for event in notifications}
            if seen.difference(affected_ids):
                raise WoonError("unexpected deleted descendant; stop the remaining batch")
            if seen == affected_ids:
                return {"response": response, "notifications": notifications}
            self._notification(self._receive(deadline))


def _desktop_snapshot(
    entry: dict[str, Any],
    metadata: dict[str, Any],
    now: datetime,
    *,
    allow_pages: bool = True,
) -> tuple[datetime, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    try:
        observed = datetime.fromisoformat(entry["observed_at"].replace("Z", "+00:00"))
        age = (now - observed).total_seconds()
        if observed.tzinfo is None or not 0 <= age <= 300 or entry["thread_id"] != metadata["id"]:
            raise WoonError("Desktop evidence is stale or foreign")
        raw = json.loads(_bound_file(entry["evidence"]))
        if isinstance(raw, dict) and raw.get("kind") == "codex-desktop-read-thread-pages":
            if not allow_pages:
                raise WoonError("Desktop page evidence must be a raw read_thread response")
            return _desktop_history_pages(raw, metadata, now)
        if isinstance(raw, dict) and "content" in raw:
            if raw.get("isError") is True:
                raise WoonError("Desktop tool returned an error")
            texts = [item["text"] for item in raw["content"] if item.get("type") == "text"]
            if len(texts) != 1:
                raise WoonError("Desktop evidence must contain one raw read_thread response")
            raw = json.loads(texts[0])
        thread = raw["thread"]
        turns = raw["turns"]
        page = raw["page"]
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise WoonError("invalid raw Desktop read_thread evidence") from error
    if (
        not isinstance(thread, dict)
        or not isinstance(thread.get("status"), dict)
        or not isinstance(page, dict)
        or thread.get("id") != metadata["id"]
        or thread.get("hostId") != "local"
        or thread.get("kind") != "codex"
        or thread.get("cwd") != metadata["cwd"]
        or thread.get("updatedAt") != metadata["updatedAt"]
        or page.get("order") != "newest_first"
        or not isinstance(turns, list)
    ):
        raise WoonError("Desktop evidence is stale, foreign, or does not match reviewed metadata")
    return observed, thread, turns, page


def _desktop_history_pages(
    manifest: dict[str, Any],
    metadata: dict[str, Any],
    now: datetime,
) -> tuple[datetime, dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Join only a complete, fresh, hash-bound Desktop cursor chain in read order."""
    entries = manifest.get("pages")
    if (
        manifest.get("schemaVersion") != 1
        or manifest.get("threadId") != metadata["id"]
        or not isinstance(entries, list)
        or not entries
    ):
        raise WoonError("invalid Desktop page evidence manifest")
    cursor = None
    cursors: set[str] = set()
    turns: list[dict[str, Any]] = []
    turn_ids: set[str] = set()
    first_thread: dict[str, Any] = {}
    oldest_observation = now
    for index, entry in enumerate(entries):
        if (
            not isinstance(entry, dict)
            or "request_cursor" not in entry
            or entry["request_cursor"] != cursor
        ):
            raise WoonError("Desktop evidence page cursor chain is incomplete")
        observed, thread, page_turns, page = _desktop_snapshot(
            {**entry, "thread_id": metadata["id"]},
            metadata,
            now,
            allow_pages=False,
        )
        if index == 0:
            first_thread = thread
        if thread["status"] != first_thread["status"] or not page_turns:
            raise WoonError("Desktop evidence pages disagree on task state or contain no turns")
        oldest_observation = min(oldest_observation, observed)
        has_more = index < len(entries) - 1
        if (
            type(page.get("hasMore")) is not bool
            or page["hasMore"] != has_more
            or "nextCursor" not in page
        ):
            raise WoonError("Desktop evidence pages do not establish complete history")
        cursor = page["nextCursor"]
        if has_more:
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise WoonError("Desktop evidence page cursor is invalid or repeated")
            cursors.add(cursor)
        elif cursor is not None:
            raise WoonError("Desktop evidence final page must end the cursor chain")
        for turn in page_turns:
            if (
                not isinstance(turn, dict)
                or not isinstance(turn.get("id"), str)
                or not turn["id"]
                or turn["id"] in turn_ids
                or turn.get("status") not in {"completed", "interrupted"}
                or "error" not in turn
                or turn["error"] is not None
            ):
                raise WoonError("Desktop page has a duplicate, active, failed or unknown turn")
            turn_ids.add(turn["id"])
            turns.append(turn)
    # These aggregate flags are justified by every original page above. The
    # newest turn remains the first page's first turn, never the final page's.
    return (
        oldest_observation,
        first_thread,
        turns,
        {
            "order": "newest_first",
            "hasMore": False,
            "nextCursor": None,
        },
    )


def _desktop_idle(entry: dict[str, Any], metadata: dict[str, Any], now: datetime) -> str:
    observed, thread, turns, page = _desktop_snapshot(entry, metadata, now)
    if any(
        not isinstance(turn, dict)
        or turn.get("status")
        not in {
            "completed",
            "interrupted",
            "failed",
        }
        for turn in turns
    ):
        raise WoonError("Desktop evidence contains an active or unknown turn")
    status = thread.get("status", {}).get("type")
    if status == "idle":
        return "desktop-idle"
    if (
        status == "notLoaded"
        and turns
        and turns[0].get("status") in {"completed", "interrupted"}
        and turns[0].get("error") is None
    ):
        latest = turns[0]
        if latest.get("completedAt") is not None:
            return f"desktop-notLoaded-latest-{latest['status']}"
        # Legacy turns may have no timing at all. Accept explicit nulls only
        # with complete terminal Desktop history; never infer missing fields.
        started, updated = latest.get("startedAt"), metadata.get("updatedAt")
        if (
            "startedAt" in latest
            and "completedAt" in latest
            and "error" in latest
            and "nextCursor" in page
            and page.get("hasMore") is False
            and page.get("nextCursor") is None
            and type(updated) is int
            and 0 < updated < observed.timestamp()
            and (started is None or (type(started) is int and 0 < started <= updated))
            and all(
                turn.get("status") in {"completed", "interrupted"}
                and "error" in turn
                and turn["error"] is None
                for turn in turns
            )
        ):
            return f"desktop-notLoaded-legacy-{latest['status']}"
    raise WoonError("Desktop evidence does not establish a terminal inactive task")


def validate_review(
    plan: dict[str, Any],
    review: dict[str, Any],
    plan_sha256: str,
    *,
    now: datetime,
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    roots, affected = plan.get("root_ids"), plan.get("affected")
    if (
        plan.get("action") != "thread-delete-review"
        or not isinstance(roots, list)
        or not roots
        or not all(isinstance(value, str) and value for value in roots)
        or len(set(roots)) != len(roots)
        or not isinstance(affected, list)
        or not affected
        or not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in affected)
        or len({row["id"] for row in affected}) != len(affected)
        or plan.get("metadata_sha256") != _content_hash(affected)
    ):
        raise WoonError("invalid or modified deletion plan")
    if review.get("version") != 1 or review.get("plan_sha256") != plan_sha256:
        raise WoonError("review is not bound to this exact plan file")
    if review.get("root_ids") != roots:
        raise WoonError("review root allowlist differs from the plan")
    protected = review.get("protected_ids")
    operator = review.get("operator_thread_id")
    authorization = review.get("authorization", {})
    if (
        not isinstance(protected, list)
        or not protected
        or not all(isinstance(value, str) and value for value in protected)
        or not isinstance(operator, str)
        or not operator
        or authorization.get("effect") != "permanent-root-and-descendants"
        or not authorization.get("request_reference")
    ):
        raise WoonError(
            "explicit protected IDs, operator and permanent-deletion authorization required"
        )
    content = review.get("content_reviews", [])
    desktop = review.get("desktop_evidence", [])
    if not all(
        isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
        for rows in (content, desktop)
    ):
        raise WoonError("invalid content or Desktop review entries")
    content_map = {row.get("thread_id"): row for row in content}
    desktop_map = {row.get("thread_id"): row for row in desktop}
    ids = {row["id"] for row in affected}
    if (
        set(content_map) != ids
        or set(desktop_map) != ids
        or len(content_map) != len(content)
        or len(desktop_map) != len(desktop)
    ):
        raise WoonError(
            "every root and descendant needs one content review and Desktop observation"
        )
    protected = list({*protected, operator, *filter(None, [os.environ.get("CODEX_THREAD_ID")])})
    for row in affected:
        attestation = content_map[row["id"]]
        reviewer = attestation.get("reviewer_thread_id")
        if not isinstance(reviewer, str) or not reviewer:
            raise WoonError("each content review must identify its reviewer")
        protected.append(reviewer)
        if (
            attestation.get("all_history_reviewed") is not True
            or attestation.get("retention") not in {"no-unique-material", "preserved"}
            or not isinstance(attestation.get("content_sha256"), str)
            or len(attestation["content_sha256"]) != 64
        ):
            raise WoonError("complete content review and exact full-turn digest required")
        _bound_file(attestation.get("evidence", {}))
        if attestation["retention"] == "preserved":
            preserved = attestation.get("preserved_evidence", [])
            if not isinstance(preserved, list) or not preserved:
                raise WoonError("preserved content needs canonical file evidence")
            for reference in preserved:
                _bound_file(reference)
        _desktop_idle(desktop_map[row["id"]], row, now)
    if ids.intersection(protected):
        raise WoonError("a protected/operator/reviewer task is in the deletion closure")
    return sorted(set(protected)), content_map, desktop_map


def _fresh_closure(
    rpc: Rpc,
    roots: list[str],
    protected: list[str],
    expected: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    closures: dict[str, list[dict[str, Any]]] = {}
    all_rows: dict[str, dict[str, Any]] = {}
    for root in roots:
        fresh = plan_thread_deletion(rpc, [root], protected)
        closures[root] = fresh["affected"]
        for row in fresh["affected"]:
            if row["id"] in all_rows or row.get("status", {}).get("type") not in {
                "idle",
                "notLoaded",
            }:
                raise WoonError("fresh closure overlaps or includes an active task")
            all_rows[row["id"]] = row
    if [all_rows[key] for key in sorted(all_rows)] != expected:
        raise WoonError("thread metadata or descendant closure changed; review a fresh plan")
    return closures


def execute_reviewed_deletion(
    client: ReviewedDeleteAppServer,
    plan: dict[str, Any],
    review: dict[str, Any],
    *,
    plan_sha256: str,
    review_sha256: str,
    receipt_dir: Path,
    apply: bool = False,
) -> dict[str, Any]:
    protected, content, desktop = validate_review(plan, review, plan_sha256, now=datetime.now(UTC))
    closures = _fresh_closure(client.request, plan["root_ids"], protected, plan["affected"])
    result: dict[str, Any] = {
        "action": "reviewed-thread-delete",
        "applied": False,
        "status": "preview",
        "plan_sha256": plan_sha256,
        "review_sha256": review_sha256,
        "root_ids": plan["root_ids"],
        "affected_ids": sorted(content),
        "delete_calls": 0,
        "root_results": [],
        "delete_success_receipt": False,
        "effect": "permanent-root-and-descendants",
    }
    if not apply:
        return result
    if not receipt_dir.is_absolute() or receipt_dir.is_symlink():
        raise WoonError("receipt directory must be an explicit absolute local path")
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if receipt_dir.stat().st_mode & 0o077:
        raise WoonError("deletion receipts require an owner-only directory")
    key = plan_sha256 + "-" + review_sha256
    attempt_path = receipt_dir / (key + ".attempt.json")
    receipt_path = receipt_dir / (key + ".receipt.json")
    with exclusive_file_lock(receipt_dir / "executor.lock"):
        if attempt_path.exists() or receipt_path.exists():
            raise WoonError("this reviewed deletion was already attempted; no automatic retry")
        result.update(status="pending", started_at=datetime.now(UTC).isoformat())
        atomic_write(attempt_path, encode_json(result), mode=0o600)
        try:
            for root in plan["root_ids"]:
                expected = closures[root]
                _fresh_closure(client.request, [root], protected, expected)
                for row in expected:
                    _desktop_idle(desktop[row["id"]], row, datetime.now(UTC))
                    fresh = client.request(
                        "thread/read", {"threadId": row["id"], "includeTurns": True}
                    )
                    thread = fresh.get("thread", {})
                    if (
                        thread.get("id") != row["id"]
                        or not isinstance(thread.get("turns"), list)
                        or _content_hash(thread["turns"]) != content[row["id"]]["content_sha256"]
                    ):
                        raise WoonError("reviewed full-turn content changed; no delete dispatched")
                # The API has no conditional-revision delete. Operator-provided fresh Desktop
                # evidence and immediate rechecks narrow, but cannot eliminate, that race.
                _fresh_closure(client.request, [root], protected, expected)
                result.update(status="dispatching", current_root=root)
                result["delete_calls"] += 1
                atomic_write(attempt_path, encode_json(result), mode=0o600)
                observed = client.delete_reviewed(root, {row["id"] for row in expected})
                result["root_results"].append({"root_id": root, **observed})
                result.update(status="root-confirmed")
                atomic_write(attempt_path, encode_json(result), mode=0o600)
            requery = requery_thread_absence(client.request, plan)
            result["requery"] = requery
            if not requery["all_absent"]:
                raise WoonError("some reviewed tasks remain; deletion is incomplete")
            result.update(
                status="complete",
                applied=True,
                delete_success_receipt=True,
                completed_at=datetime.now(UTC).isoformat(),
            )
            receipt_bytes = encode_json(result)
            try:
                atomic_write(receipt_path, receipt_bytes, mode=0o600)
            except Exception:
                if receipt_path.is_file() and receipt_path.read_bytes() == receipt_bytes:
                    receipt_path.unlink()
                raise
            return {**result, "receipt": str(receipt_path)}
        except Exception as error:
            result.update(
                status="failed-or-uncertain",
                applied=False,
                delete_success_receipt=False,
                error_type=type(error).__name__,
                notifications=client.deleted_notifications,
                delete_calls=len(client.attempted),
                delete_responses=client.delete_responses,
            )
            try:
                result["requery"] = requery_thread_absence(client.request, plan)
            except WoonError:
                result["requery"] = {"complete": False, "all_absent": None}
            atomic_write(attempt_path, encode_json(result), mode=0o600)
            raise WoonError(
                f"deletion not verified; do not retry; inspect {attempt_path}"
            ) from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default="codex")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--expected-plan-sha256", required=True)
    parser.add_argument("--expected-review-sha256", required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = json.loads(_bound_file({"path": str(args.plan), "sha256": args.expected_plan_sha256}))
    review = json.loads(
        _bound_file({"path": str(args.review), "sha256": args.expected_review_sha256})
    )
    validate_review(plan, review, args.expected_plan_sha256, now=datetime.now(UTC))
    with ReviewedDeleteAppServer(args.codex, plan["root_ids"]) as client:
        result = execute_reviewed_deletion(
            client,
            plan,
            review,
            plan_sha256=args.expected_plan_sha256,
            review_sha256=args.expected_review_sha256,
            receipt_dir=args.receipt_dir,
            apply=args.apply,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
