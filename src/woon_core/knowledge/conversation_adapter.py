"""Opt-in adapter for completed, user-visible Codex and ChatGPT turns."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from woon_core.errors import WoonError
from woon_core.knowledge.codex_source_archive import CodexSourceBundle, bundle_from_record


def completed_turn_bundles(
    snapshot: dict[str, Any], *, allowed_thread_ids: tuple[str, ...], day: date
) -> tuple[CodexSourceBundle, ...]:
    """Return per-turn source bundles with stable identities; perform no writes.

    Consent is required for the exact thread, regardless of whether it is pinned.
    Recording an utterance never establishes its subject or career contribution.
    A completed turn is immutable evidence; edited turns require explicit review.
    """
    thread = snapshot.get("thread", {})
    identifier = thread.get("id")
    kind = thread.get("kind")
    if identifier not in allowed_thread_ids:
        raise WoonError("conversation thread is not explicitly opted in")
    if kind not in {"codex", "chatgpt"}:
        raise WoonError("unsupported conversation source kind")
    bundles = []
    seen: set[str] = set()
    for turn in sorted(snapshot.get("turns", []), key=lambda t: str(t.get("id", ""))):
        if turn.get("status") != "completed" or not turn.get("id") or turn.get("error"):
            continue
        turn_id = str(turn["id"])
        if turn_id in seen:
            raise WoonError("duplicate conversation turn requires source reconciliation")
        seen.add(turn_id)
        completed = turn.get("completedAt")
        if isinstance(completed, (int, float)) and not isinstance(completed, bool):
            stamp = datetime.fromtimestamp(
                completed / 1000 if completed > 10**11 else completed, UTC
            )
        elif isinstance(completed, str):
            stamp = datetime.fromisoformat(completed.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise WoonError("conversation completion time requires a timezone")
        else:
            continue
        if stamp.astimezone(ZoneInfo("Asia/Seoul")).date() != day:
            continue
        messages = []
        for item in turn.get("items", []):
            item_type = item.get("type")
            if item_type == "userMessage":
                role = "user"
            elif item_type == "agentMessage" and (
                item.get("phase") == "final_answer"
                or (kind == "chatgpt" and item.get("phase") is None)
            ):
                role = "assistant"
            else:
                continue
            text = item.get("text")
            if item_type == "userMessage" and isinstance(item.get("content"), list):
                text = "\n".join(
                    part["text"]
                    for part in item["content"]
                    if isinstance(part, dict)
                    and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            if not isinstance(text, str) or not text.strip():
                continue
            messages.append({"role": role, "text": text, "created_at": stamp.isoformat()})
        if not any(message["role"] == "assistant" for message in messages):
            continue
        bundles.append(
            bundle_from_record(
                {
                    "day": day.isoformat(),
                    "source_locator": f"{kind}-thread:{identifier}:turn:{turn_id}",
                    "title": thread.get("title") or "대화",
                    "messages": messages,
                }
            )
        )
    return tuple(bundles)
