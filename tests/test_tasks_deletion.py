import json
from io import StringIO
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.tasks import service as task_module
from woon_core.tasks.cli import run_tasks
from woon_core.tasks.service import TaskService


def _seed(root: Path) -> tuple[TaskService, Path, Path]:
    service = TaskService(root)
    service.upsert_recurring_todo(
        task_id="routine-one",
        title="반복 행동",
        purpose="사용자가 요청한 행동",
        area="life",
    )
    daily = root / "inbox/daily"
    daily.mkdir(parents=True)
    for day in ("2026-08-17", "2026-08-18"):
        (daily / f"{day}.md").write_text(
            f'---\ntype: Daily\ntitle: "{day}"\n---\n\n# {day}\n\n'
            "## 오늘의 할 일\n\n<!-- woon-tasks:start -->\n"
            f"- [x] 반복 행동 <!-- woon-task:routine-one:{day} -->\n"
            f"- [ ] 삭제된 정의의 행동 <!-- woon-task:missing-routine:{day} -->\n"
            "<!-- woon-tasks:end -->\n\n"
            "<!-- woon-codex-digest:start -->\n<!-- woon-codex-digest:end -->\n"
            "\n## 자유 메모\n",
        )
    keep = daily / "2026-08-17.md"
    keep.write_text(
        keep.read_text().replace(
            "<!-- woon-tasks:end -->",
            "- [x] 반복 행동 <!-- woon-task:other-routine:2026-08-17 -->\n<!-- woon-tasks:end -->",
        )
        + "\n직접 쓴 일기와 기억할 약속.\n"
    )
    return service, keep, daily / "2026-08-18.md"


def _request(service: TaskService) -> dict:
    preview = service.preview_recurring_deletion(("routine-one", "missing-routine"))
    return {
        key: value
        for key, value in preview.items()
        if key not in {"managed_rows", "completed_rows"}
    } | {
        "review_reference": "explicit user deletion of these two routine IDs",
    }


def test_cli_deletes_exact_ids_and_empty_dates_and_replays_without_regeneration(tmp_path: Path):
    service, keep, empty = _seed(tmp_path)
    request = tmp_path / "request.json"
    request.write_text(json.dumps(_request(service)))
    for _ in range(2):
        output = StringIO()
        run_tasks(["delete-recurring", "--request", str(request), "--vault", str(tmp_path)], output)
        receipt = json.loads(output.getvalue())
        assert receipt["status"] == "complete"
        assert receipt["managed_rows"] == 4
        assert receipt["completed_rows"] == 2
    assert "직접 쓴 일기와 기억할 약속." in keep.read_text()
    assert "woon-task:other-routine:" in keep.read_text()
    assert "woon-task:routine-one:" not in keep.read_text()
    assert not empty.exists()
    assert service.find("반복 행동") == ()
    assert not service.materialize_due().created_daily_note
    state = json.loads((tmp_path / ".local/woon-knowledge/tasks-state.json").read_text())
    assert "upsert:routine-one" not in state["operations"]


def test_changed_user_note_blocks_deletion_before_any_write(tmp_path: Path):
    service, keep, _ = _seed(tmp_path)
    request = _request(service)
    keep.write_text(keep.read_text() + "새로 쓴 내용\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.md")}
    with pytest.raises(WoonError, match="revision changed"):
        service.delete_recurring_todos(**request)
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.md")} == before


def test_interruption_keeps_pending_receipt_and_retries_only_pinned_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, keep, empty = _seed(tmp_path)
    request = _request(service)
    write = task_module.atomic_write

    def fail(path, *args, **kwargs):
        if path == keep:
            raise OSError("interrupted daily write")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(task_module, "atomic_write", fail)
    with pytest.raises(OSError, match="interrupted"):
        service.delete_recurring_todos(**request)
    state = json.loads((tmp_path / ".local/woon-knowledge/tasks-state.json").read_text())
    receipt = next(v for k, v in state["operations"].items() if k.startswith("routine-delete:"))
    assert receipt["status"] == "pending"
    assert not service.list_routines()
    monkeypatch.setattr(task_module, "atomic_write", write)
    assert service.delete_recurring_todos(**request)["status"] == "complete"
    assert not empty.exists()
    assert "직접 쓴 일기" in keep.read_text()


def test_empty_date_deletion_cannot_remove_personal_content(tmp_path: Path):
    service, keep, _ = _seed(tmp_path)
    request = _request(service)
    request["empty_daily_paths"].append(keep.relative_to(tmp_path).as_posix())
    with pytest.raises(WoonError, match="still has personal content"):
        service.delete_recurring_todos(**request)
    assert service.list_routines()
    assert "직접 쓴 일기" in keep.read_text()


def test_deletion_does_not_follow_a_routine_symlink(tmp_path: Path):
    service, _, _ = _seed(tmp_path)
    original = tmp_path / "inbox/tasks/routines/routine-one.md"
    external = tmp_path / "user.md"
    original.rename(external)
    original.symlink_to(external)
    with pytest.raises(WoonError, match="symlink"):
        service.preview_recurring_deletion(("routine-one",))
    assert external.is_file()
