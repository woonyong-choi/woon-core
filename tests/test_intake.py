from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import intake
from woon_core.knowledge.intake import (
    IntakeSource,
    IntakeTarget,
    complete_intake,
    prepare_intake,
    read_intake,
    register_intake,
    run_intake,
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def source(name: str = "memo:one") -> IntakeSource:
    return IntakeSource(name, "decision-1", digest(b"source"), "private/memo.md")


def register(vault: Path, **overrides: object) -> dict:
    return register_intake(
        vault,
        **{
            "sources": (source(),),
            "title": "면접 준비",
            "summary": "역할별 근거를 대조한다.",
            "keywords": ("면접",),
            "explicit_request": True,
            **overrides,
        },
    )


def target(vault: Path) -> tuple[Path, bytes, IntakeTarget]:
    path = vault / "wiki/personal/interview.md"
    path.parent.mkdir(parents=True)
    before = b"---\ncanonical_id: personal/interview\n---\n# Interview\n\nBefore\n"
    after = before.replace(b"Before", b"Verified personal contribution; team result is separate.")
    path.write_bytes(before)
    return (
        path,
        after,
        IntakeTarget(
            "personal/interview",
            path.relative_to(vault).as_posix(),
            digest(before),
            digest(after),
        ),
    )


def test_intake_identity_survives_titles_and_revisions(tmp_path: Path) -> None:
    first = register(tmp_path)
    changed_title = register(tmp_path, title="면접", summary="같은 정리 단위를 짧게 표시한다.")
    assert changed_title["intake_id"] == first["intake_id"]
    assert changed_title["card"]["path"] == first["card"]["path"]
    assert len(list((tmp_path / "inbox/capture").glob("*.md"))) == 1
    distinct = register(tmp_path, sources=(source("memo:two"),))
    assert distinct["intake_id"] != first["intake_id"]
    revised = replace(source(), revision=digest(b"corrected source"))
    with pytest.raises(WoonError, match="current input revision"):
        register(tmp_path, sources=(revised,))
    updated = register(
        tmp_path,
        sources=(revised,),
        expected_input_revision=first["input_revision"],
    )
    assert updated["intake_id"] == first["intake_id"]
    assert updated["input_revision"] != first["input_revision"]


def test_cold_replay_after_writer_crash_does_not_write_again(tmp_path: Path) -> None:
    item = register(tmp_path)
    path, after, planned = target(tmp_path)
    calls = []

    def crash_after_write() -> None:
        calls.append("write")
        path.write_bytes(after)
        raise RuntimeError("process stopped after canonical write")

    with pytest.raises(RuntimeError):
        run_intake(
            tmp_path,
            item["intake_id"],
            item["input_revision"],
            targets=(planned,),
            writer=crash_after_write,
            reviewed_revisions=(planned.after_revision,),
        )
    # The next execution only has IDs, the current request and the existing
    # writer plan. No chat history, master state, or completed note is loaded.
    result = run_intake(
        tmp_path,
        item["intake_id"],
        item["input_revision"],
        targets=(planned,),
        writer=crash_after_write,
        reviewed_revisions=(planned.after_revision,),
    )
    assert calls == ["write"]
    assert result["state"] == "complete"
    assert result["results"][0]["available"] is True
    assert not (tmp_path / item["card"]["path"]).exists()
    replay = register(tmp_path, title="바뀐 표시명")
    assert replay["state"] == "complete"
    assert replay["results"][0]["canonical_id"] == planned.canonical_id
    assert list((tmp_path / "inbox/capture").glob("*.md")) == []
    assert path.read_bytes() == after


def test_archived_application_completes_but_semantically_retired_target_does_not(
    tmp_path: Path,
) -> None:
    item = register(tmp_path)
    path, after, planned = target(tmp_path)
    after = after.replace(
        b"---\n#",
        "status: Archived\nknowledge_state: 확인 필요\n---\n#".encode(),
    )
    planned = replace(planned, after_revision=digest(after))
    result = run_intake(
        tmp_path,
        item["intake_id"],
        item["input_revision"],
        targets=(planned,),
        writer=lambda: path.write_bytes(after),
        reviewed_revisions=(planned.after_revision,),
    )
    assert result["state"] == "complete"
    assert read_intake(tmp_path, item["intake_id"])["results"][0]["available"]
    retired = after.replace("확인 필요".encode(), "폐기됨".encode())
    path.write_bytes(retired)
    assert not read_intake(tmp_path, item["intake_id"])["results"][0]["available"]
    another = register(tmp_path, sources=(source("memo:two"),))
    retired_target = replace(
        planned,
        before_revision=digest(retired),
        after_revision=digest(retired),
    )
    with pytest.raises(WoonError, match="retired"):
        prepare_intake(tmp_path, another["intake_id"], another["input_revision"], (retired_target,))
    assert (tmp_path / another["card"]["path"]).exists()
    assert path.read_bytes() == retired


def test_cleanup_retry_preserves_user_edits_and_does_not_repeat_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = register(tmp_path)
    path, after, planned = target(tmp_path)
    card = tmp_path / item["card"]["path"]
    prepare_intake(tmp_path, item["intake_id"], item["input_revision"], (planned,))
    path.write_bytes(after)
    original_unlink = Path.unlink

    def interrupted_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self == card:
            raise OSError("cleanup interrupted")
        original_unlink(self, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted_unlink)
        with pytest.raises(OSError):
            complete_intake(
                tmp_path,
                item["intake_id"],
                item["input_revision"],
                reviewed_revisions=(planned.after_revision,),
            )
    assert read_intake(tmp_path, item["intake_id"])["state"] == "cleanup-pending"
    card.write_text(card.read_text() + "\n사용자가 남긴 생각\n")
    with pytest.raises(WoonError, match="edited"):
        complete_intake(
            tmp_path,
            item["intake_id"],
            item["input_revision"],
            reviewed_revisions=(planned.after_revision,),
        )
    assert "사용자가 남긴 생각" in card.read_text()
    assert path.read_bytes() == after


def test_registration_interruption_reuses_reserved_card_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_write = intake.atomic_write

    def interrupt_card(path: Path, data: bytes, **kwargs: object) -> None:
        if path.suffix == ".md":
            raise OSError("stopped before card write")
        real_write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(intake, "atomic_write", interrupt_card)
        with pytest.raises(OSError):
            register(tmp_path)
    item = register(tmp_path)
    assert item["state"] == "pending"
    assert len(list((tmp_path / "inbox/capture").glob("*.md"))) == 1
    assert item["card"]["path"].endswith("면접-준비.md")


def test_missing_intake_unreviewed_hub_and_concurrent_target_change_do_not_close(
    tmp_path: Path,
) -> None:
    with pytest.raises(WoonError, match="explicit request"):
        register(tmp_path, explicit_request=False)
    path, after, planned = target(tmp_path)
    with pytest.raises(WoonError, match="register"):
        prepare_intake(tmp_path, "intake-" + "0" * 32, "0" * 64, (planned,))
    item = register(tmp_path)
    prepare_intake(tmp_path, item["intake_id"], item["input_revision"], (planned,))
    with pytest.raises(WoonError, match="semantic review"):
        complete_intake(tmp_path, item["intake_id"], item["input_revision"], reviewed_revisions=())
    with pytest.raises(WoonError, match="not been verified"):
        complete_intake(
            tmp_path,
            item["intake_id"],
            item["input_revision"],
            reviewed_revisions=(planned.after_revision,),
        )
    path.write_bytes(after + b"\nUser concurrent edit\n")
    with pytest.raises(WoonError, match="changed"):
        prepare_intake(tmp_path, item["intake_id"], item["input_revision"], (planned,))
    assert path.read_bytes().endswith(b"User concurrent edit\n")
    assert (tmp_path / item["card"]["path"]).is_file()


def test_cli_register_and_missing_review_never_invoke_writer(tmp_path: Path) -> None:
    import json
    from dataclasses import asdict
    from io import StringIO

    from woon_core.cli import run

    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "sources": [asdict(source())],
                "title": "면접",
                "summary": "근거 대조",
                "explicit_request": True,
            }
        )
    )
    output = StringIO()
    run(
        ["knowledge", "intake", "register", "--request", str(request), "--vault", str(tmp_path)],
        output,
    )
    item = json.loads(output.getvalue())
    path, after, planned = target(tmp_path)
    with pytest.raises(WoonError, match="semantic review"):
        run_intake(
            tmp_path,
            item["intake_id"],
            item["input_revision"],
            targets=(planned,),
            writer=lambda: path.write_bytes(after),
            reviewed_revisions=(),
        )
    assert path.read_bytes() != after
    assert read_intake(tmp_path, item["intake_id"])["state"] == "pending"


def test_parallel_same_intake_serializes_writer(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    item = register(tmp_path)
    path, after, planned = target(tmp_path)
    calls = []

    def writer() -> None:
        calls.append(1)
        path.write_bytes(after)

    def execute() -> dict:
        return run_intake(
            tmp_path,
            item["intake_id"],
            item["input_revision"],
            targets=(planned,),
            writer=writer,
            reviewed_revisions=(planned.after_revision,),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: execute(), range(2)))
    assert calls == [1]
    assert all(result["state"] == "complete" for result in results)


def test_cleanup_interrupted_after_unlink_resumes_without_card(tmp_path: Path, monkeypatch) -> None:
    import json

    item = register(tmp_path)
    path, after, planned = target(tmp_path)
    prepare_intake(tmp_path, item["intake_id"], item["input_revision"], (planned,))
    path.write_bytes(after)
    real_write = intake.atomic_write

    def interrupt_completed_state(path: Path, content: bytes, **kwargs: object) -> None:
        if json.loads(content)["state"] == "complete":
            raise OSError("stopped after removing card")
        real_write(path, content, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(intake, "atomic_write", interrupt_completed_state)
        with pytest.raises(OSError):
            complete_intake(
                tmp_path,
                item["intake_id"],
                item["input_revision"],
                reviewed_revisions=(planned.after_revision,),
            )
    assert not (tmp_path / item["card"]["path"]).exists()
    result = complete_intake(
        tmp_path,
        item["intake_id"],
        item["input_revision"],
        reviewed_revisions=(planned.after_revision,),
    )
    assert result["state"] == "complete"
