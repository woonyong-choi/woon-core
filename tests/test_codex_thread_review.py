import pytest

from woon_core.environment.codex_thread_review import (
    SOURCE_KINDS,
    ReadOnlyAppServer,
    plan_thread_deletion,
    requery_thread_absence,
)
from woon_core.errors import WoonError


def _thread(identity):
    return {
        "id": identity,
        "cwd": "/reviewed/workspace",
        "ephemeral": False,
        "name": identity,
        "status": {"type": "notLoaded"},
        "updatedAt": 10,
    }


def test_delete_plan_includes_archived_descendants_and_every_cursor_without_mutations():
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        if method == "thread/read":
            assert params == {"threadId": "root", "includeTurns": False}
            return {"thread": _thread("root")}
        assert method == "thread/list"
        assert params["sourceKinds"] == SOURCE_KINDS
        assert params["ancestorThreadId"] == "root"
        if params["archived"]:
            return {"data": [_thread("archived-grandchild")], "nextCursor": None}
        if params["cursor"] is None:
            return {"data": [_thread("child")], "nextCursor": "second"}
        assert params["cursor"] == "second"
        return {"data": [_thread("grandchild")], "nextCursor": None}

    result = plan_thread_deletion(rpc, ["root"], [])
    assert {row["id"] for row in result["affected"]} == {
        "root",
        "child",
        "grandchild",
        "archived-grandchild",
    }
    assert result["delete_calls"] == 0 and result["desktop_idle_verified"] is False
    assert result["content_reviewed"] is False
    assert len(calls) == 4
    with pytest.raises(WoonError, match="protected thread"):
        plan_thread_deletion(rpc, ["root"], ["archived-grandchild"])


def test_requery_keeps_absence_separate_from_success_and_includes_archived_records():
    def rpc(method, params):
        assert method == "thread/list" and params["cwd"] == ["/reviewed/workspace"]
        return {"data": [_thread("child")] if params["archived"] else [], "nextCursor": None}

    result = requery_thread_absence(rpc, {"affected": [_thread("root"), _thread("child")]})
    assert result["remaining_ids"] == ["child"] and result["all_absent"] is False
    assert result["delete_success_receipt"] is False
    with pytest.raises(WoonError, match="complete page"):
        requery_thread_absence(lambda *_: {"data": []}, {"affected": [_thread("root")]})


def test_read_only_transport_rejects_delete_before_writing_to_process():
    # No process is created: the method allowlist must reject mutations first.
    client = object.__new__(ReadOnlyAppServer)
    with pytest.raises(WoonError, match="does not dispatch mutations"):
        client.request("thread/delete", {"threadId": "root"})
