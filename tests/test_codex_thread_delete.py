from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from woon_core.environment import codex_thread_delete as module
from woon_core.environment.codex_thread_review import plan_thread_deletion
from woon_core.errors import WoonError
from woon_core.io import encode_json


@pytest.fixture
def review_case(tmp_path):
    rows = {
        identity: {
            "id": identity,
            "name": identity,
            "cwd": "/reviewed/workspace",
            "ephemeral": False,
            "status": {"type": "notLoaded"},
            "updatedAt": 10,
            "parentThreadId": None if identity == "root" else "root",
        }
        for identity in ("root", "child")
    }
    turns = [{"id": "completed-turn", "status": "completed", "items": []}]
    calls, deleted = [], set()
    state = SimpleNamespace(
        incomplete=False, missing_notification=False, mutate_content=False, protected_child=False
    )

    class Client:
        attempted = set()
        deleted_notifications = []
        delete_responses = {}

        def request(self, method, params):
            calls.append((method, params))
            if method == "thread/read":
                row = copy.deepcopy(rows[params["threadId"]])
                if params.get("includeTurns"):
                    row["turns"] = [] if state.mutate_content else turns
                return {"thread": row}
            assert method == "thread/list"
            if state.incomplete and deleted:
                return {"data": []}
            selected = [row for key, row in rows.items() if key not in deleted]
            if "ancestorThreadId" in params:
                selected = [
                    row for row in selected if row["parentThreadId"] == params["ancestorThreadId"]
                ]
            return {"data": selected if params["archived"] else [], "nextCursor": None}

        def delete_reviewed(self, root, ids):
            self.attempted.add(root)
            deleted.update(ids)
            self.delete_responses[root] = {}
            self.deleted_notifications.extend(
                {"method": "thread/deleted", "params": {"threadId": x}} for x in sorted(ids)
            )
            if state.missing_notification:
                self.deleted_notifications.pop()
                raise WoonError("missing official notification")
            return {"response": {}, "notifications": list(self.deleted_notifications)}

    client = Client()
    plan = plan_thread_deletion(client.request, ["root"], ["protected"])
    plan_sha = module._sha(encode_json(plan))
    evidence = tmp_path / "content-review.md"
    evidence.write_text("Reviewed both tasks in full; no unique material to retain.")
    reference = {"path": str(evidence), "sha256": module._sha(evidence.read_bytes())}
    review = {
        "version": 1,
        "plan_sha256": plan_sha,
        "root_ids": ["root"],
        "protected_ids": ["protected"],
        "operator_thread_id": "master",
        "authorization": {"effect": "permanent-root-and-descendants", "request_reference": "user"},
        "content_reviews": [],
        "desktop_evidence": [],
    }
    for identity in rows:
        review["content_reviews"].append(
            {
                "thread_id": identity,
                "reviewer_thread_id": "reviewer",
                "all_history_reviewed": True,
                "retention": "no-unique-material",
                "content_sha256": module._content_hash(turns),
                "evidence": reference,
            }
        )
        raw = {
            "thread": {**rows[identity], "hostId": "local", "kind": "codex"},
            "page": {"order": "newest_first", "hasMore": False, "nextCursor": None},
            "turns": [{"status": "completed", "error": None, "completedAt": 10}],
        }
        snapshot = tmp_path / f"{identity}-desktop.json"
        snapshot.write_bytes(
            encode_json({"content": [{"type": "text", "text": json.dumps(raw)}], "isError": False})
        )
        review["desktop_evidence"].append(
            {
                "thread_id": identity,
                "observed_at": datetime.now(UTC).isoformat(),
                "evidence": {"path": str(snapshot), "sha256": module._sha(snapshot.read_bytes())},
            }
        )
    return SimpleNamespace(
        client=client,
        plan=plan,
        review=review,
        plan_sha=plan_sha,
        rows=rows,
        receipt_dir=tmp_path / "receipts",
        calls=calls,
        deleted=deleted,
        state=state,
    )


def execute(c, apply=True):
    return module.execute_reviewed_deletion(
        c.client,
        c.plan,
        c.review,
        plan_sha256=c.plan_sha,
        review_sha256=module._sha(encode_json(c.review)),
        receipt_dir=c.receipt_dir,
        apply=apply,
    )


def test_reviewed_root_and_archived_descendant_require_response_notifications_and_absence(
    review_case,
):
    c = review_case
    assert execute(c, apply=False)["delete_calls"] == 0
    assert not c.deleted and not c.receipt_dir.exists()
    result = execute(c)
    assert c.deleted == {"root", "child"} and c.client.attempted == {"root"}
    assert result["delete_calls"] == 1 and result["delete_success_receipt"] is True
    assert result["requery"]["all_absent"] is True
    assert {
        event["params"]["threadId"] for event in result["root_results"][0]["notifications"]
    } == c.deleted
    assert len(list(c.receipt_dir.glob("*.receipt.json"))) == 1


@pytest.mark.parametrize("fault", ["protected-child", "metadata", "content"])
def test_protected_or_changed_descendant_prevents_delete(review_case, fault):
    c = review_case
    if fault == "protected-child":
        c.review["protected_ids"].append("child")
    elif fault == "metadata":
        c.rows["child"]["updatedAt"] = 11
    else:
        c.state.mutate_content = True
    with pytest.raises(WoonError):
        execute(c)
    assert not c.deleted and not c.client.attempted
    assert not list(c.receipt_dir.glob("*.receipt.json"))


@pytest.mark.parametrize("fault", ["notification", "requery"])
def test_delete_uncertainty_never_becomes_success_from_absence_alone(review_case, fault):
    c = review_case
    c.state.missing_notification = fault == "notification"
    c.state.incomplete = fault == "requery"
    with pytest.raises(WoonError, match="do not retry"):
        execute(c)
    attempt = json.loads(next(c.receipt_dir.glob("*.attempt.json")).read_bytes())
    assert c.deleted == {"root", "child"} and attempt["delete_success_receipt"] is False
    assert attempt["delete_responses"] == {"root": {}}
    assert not list(c.receipt_dir.glob("*.receipt.json"))
    if fault == "requery":
        assert attempt["requery"]["all_absent"] is None
    else:
        assert attempt["requery"]["all_absent"] is True


@pytest.mark.parametrize("fault", ["stdio", "active"])
def test_stdio_notloaded_is_not_desktop_evidence_and_active_turn_blocks(review_case, fault):
    c = review_case
    entry = c.review["desktop_evidence"][0]
    from pathlib import Path

    path = Path(entry["evidence"]["path"])
    if fault == "stdio":
        path.write_bytes(encode_json({"thread": c.rows["root"]}))
    else:
        raw = json.loads(json.loads(path.read_bytes())["content"][0]["text"])
        raw["turns"][0]["status"] = "inProgress"
        path.write_bytes(encode_json(raw))
    entry["evidence"]["sha256"] = module._sha(path.read_bytes())
    with pytest.raises(WoonError, match="Desktop"):
        execute(c)
    assert not c.deleted


@pytest.mark.parametrize(
    ("status", "completed_at", "error", "accepted"),
    [
        ("interrupted", 10, None, True),
        ("interrupted", None, None, False),
        ("interrupted", 10, {"message": "failure"}, False),
        ("failed", 10, None, False),
    ],
)
def test_interrupted_terminal_requires_end_time_and_no_error(
    review_case,
    status,
    completed_at,
    error,
    accepted,
):
    c = review_case
    entry = c.review["desktop_evidence"][0]
    path = Path(entry["evidence"]["path"])
    raw = json.loads(json.loads(path.read_bytes())["content"][0]["text"])
    raw["turns"][0].update(status=status, completedAt=completed_at, error=error)
    path.write_bytes(encode_json(raw))
    entry["evidence"]["sha256"] = module._sha(path.read_bytes())
    metadata = next(row for row in c.plan["affected"] if row["id"] == entry["thread_id"])
    if accepted:
        assert module._desktop_idle(entry, metadata, datetime.now(UTC)) == (
            "desktop-notLoaded-latest-interrupted"
        )
    else:
        with pytest.raises(WoonError, match="terminal inactive"):
            module._desktop_idle(entry, metadata, datetime.now(UTC))


def test_write_entrypoint_is_exact_once_and_retains_notifications_before_response():
    client = object.__new__(module.ReviewedDeleteAppServer)
    client.allowed_roots = frozenset({"root"})
    client.attempted = set()
    client.deleted_notifications = []
    client.delete_responses = {}
    client.sequence = 0
    client.process = SimpleNamespace(stdout=object())
    sends = []
    client._send = sends.append
    frames = [
        {"method": "thread/deleted", "params": {"threadId": "child"}},
        {"id": 1, "result": {}},
        {"method": "thread/deleted", "params": {"threadId": "root"}},
    ]
    client._receive = lambda _deadline: frames.pop(0)
    with pytest.raises(WoonError, match="unreviewed"):
        client.delete_reviewed("other", {"other"})
    with pytest.raises(WoonError, match="does not dispatch mutations"):
        client.request("thread/delete", {"threadId": "root"})
    assert not sends
    result = client.delete_reviewed("root", {"root", "child"})
    assert sends == [{"id": 1, "method": "thread/delete", "params": {"threadId": "root"}}]
    assert len(result["notifications"]) == 2
    with pytest.raises(WoonError, match="already attempted"):
        client.delete_reviewed("root", {"root", "child"})


@pytest.mark.parametrize("status", ["completed", "interrupted"])
@pytest.mark.parametrize("started_at", [1, None])
def test_legacy_terminal_requires_complete_history_and_explicit_nullable_timing(
    review_case,
    status,
    started_at,
):
    c = review_case
    entry = c.review["desktop_evidence"][0]
    path = Path(entry["evidence"]["path"])
    raw = json.loads(json.loads(path.read_bytes())["content"][0]["text"])
    metadata = next(row for row in c.plan["affected"] if row["id"] == entry["thread_id"])
    raw["turns"][0].update(status=status, startedAt=started_at, completedAt=None, error=None)
    valid = copy.deepcopy(raw)

    def evaluate(candidate):
        path.write_bytes(encode_json(candidate))
        entry["evidence"]["sha256"] = module._sha(path.read_bytes())
        return module._desktop_idle(entry, metadata, datetime.now(UTC))

    assert evaluate(valid) == f"desktop-notLoaded-legacy-{status}"
    for field, value in (("hasMore", True), ("nextCursor", "older")):
        candidate = copy.deepcopy(valid)
        candidate["page"][field] = value
        with pytest.raises(WoonError, match="terminal inactive"):
            evaluate(candidate)
    for field, value in (
        ("startedAt", 0),
        ("startedAt", True),
        ("startedAt", metadata["updatedAt"] + 1),
        ("status", "failed"),
        ("error", {"message": "failed"}),
        ("status", "inProgress"),
        ("status", "unknown"),
    ):
        candidate = copy.deepcopy(valid)
        candidate["turns"][0][field] = value
        with pytest.raises(WoonError, match="Desktop"):
            evaluate(candidate)
    for container, field in (
        ("turns", "startedAt"),
        ("turns", "completedAt"),
        ("turns", "error"),
        ("page", "nextCursor"),
    ):
        candidate = copy.deepcopy(valid)
        del (candidate["turns"][0] if container == "turns" else candidate["page"])[field]
        with pytest.raises(WoonError, match="terminal inactive"):
            evaluate(candidate)
    candidate = copy.deepcopy(valid)
    candidate["turns"].append({"status": "failed", "error": None})
    with pytest.raises(WoonError, match="terminal inactive"):
        evaluate(candidate)


def test_desktop_page_manifest_preserves_latest_turn_and_rejects_incomplete_evidence(review_case):
    from datetime import timedelta

    c = review_case
    entry = c.review["desktop_evidence"][0]
    original = Path(entry["evidence"]["path"])
    raw = json.loads(json.loads(original.read_bytes())["content"][0]["text"])
    metadata = next(row for row in c.plan["affected"] if row["id"] == entry["thread_id"])
    pages = [copy.deepcopy(raw), copy.deepcopy(raw)]
    for index, page in enumerate(pages):
        page["page"].update(hasMore=index == 0, nextCursor="older" if index == 0 else None)
        page["turns"] = [
            {
                "id": f"turn-{index}",
                "status": "completed" if index == 0 else "interrupted",
                "startedAt": None,
                "completedAt": None,
                "error": None,
            }
        ]
    manifest = {
        "schemaVersion": 1,
        "kind": "codex-desktop-read-thread-pages",
        "threadId": entry["thread_id"],
        "pages": [
            {
                "request_cursor": None if index == 0 else "older",
                "observed_at": entry["observed_at"],
                "evidence": {},
            }
            for index in range(2)
        ],
    }

    def save(candidate, snapshots):
        for index, snapshot in enumerate(snapshots):
            path = original.with_name(f"page-{index}.json")
            path.write_bytes(encode_json(snapshot))
            if index < len(candidate["pages"]):
                candidate["pages"][index]["evidence"] = {
                    "path": str(path),
                    "sha256": module._sha(path.read_bytes()),
                }
        original.write_bytes(encode_json(candidate))
        entry["evidence"]["sha256"] = module._sha(original.read_bytes())

    save(copy.deepcopy(manifest), pages)
    assert module._desktop_idle(entry, metadata, datetime.now(UTC)) == (
        "desktop-notLoaded-legacy-completed"
    )
    assert execute(c, apply=False)["delete_calls"] == 0
    faults = (
        "missing-first",
        "missing-last",
        "cursor",
        "metadata",
        "state",
        "stale",
        "duplicate",
        "failed",
        "active",
        "unknown",
        "missing-error",
        "tamper",
    )
    for fault in faults:
        candidate, snapshots = copy.deepcopy(manifest), copy.deepcopy(pages)
        if fault == "missing-first":
            candidate["pages"].pop(0)
            snapshots.pop(0)
        elif fault == "missing-last":
            candidate["pages"].pop()
            snapshots.pop()
        elif fault == "cursor":
            candidate["pages"][1]["request_cursor"] = "unrelated"
        elif fault == "metadata":
            snapshots[1]["thread"]["updatedAt"] += 1
        elif fault == "state":
            snapshots[1]["thread"]["status"] = {"type": "active"}
        elif fault == "stale":
            candidate["pages"][0]["observed_at"] = (
                datetime.now(UTC) - timedelta(seconds=301)
            ).isoformat()
        elif fault == "duplicate":
            snapshots[1]["turns"][0]["id"] = snapshots[0]["turns"][0]["id"]
        elif fault in {"failed", "active", "unknown"}:
            snapshots[0]["turns"][0]["status"] = "inProgress" if fault == "active" else fault
        elif fault == "missing-error":
            del snapshots[1]["turns"][0]["error"]
        save(candidate, snapshots)
        if fault == "tamper":
            original.with_name("page-1.json").write_text("{}")
        with pytest.raises(WoonError):
            execute(c, apply=False)
        assert not c.deleted and not c.client.attempted
