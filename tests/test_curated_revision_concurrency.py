from __future__ import annotations

import hashlib
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from test_compiled_wiki import compiled_settings, write_page

from woon_core.errors import WoonError
from woon_core.knowledge import codex_quality_revision as revision
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.compiled_wiki import CompiledWiki, CuratedRevision
from woon_core.knowledge.service import KnowledgeService


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_review_apply_rechecks_revision_inside_writer_lock(
    tmp_path: Path,
    monkeypatch,
    change: str,
) -> None:
    """A reviewed proposal must not replace a change made as its writer acquires the lock."""
    original_body = "## 설명\n\n처음 읽은 문장이다.\n"
    write_page(tmp_path, "os/example.md", "예시", original_body)
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    repository = MarkdownDocumentRepository(tmp_path, tmp_path / "wiki")
    service = KnowledgeService(
        repository,
        SQLiteFtsSearchIndex(tmp_path / "search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    current = service.get("os/example")
    reviewed = revision.RevisionCandidate(
        page_id="os/example",
        output_sha256=current.revision,
        source_body_sha256=hashlib.sha256(original_body.encode()).hexdigest(),
        title="예시",
        purpose="읽은 문장을 확인한다.",
        body=original_body,
        failures=("natural_korean",),
        failure_reasons=("문장 연결을 개선한다.",),
    )
    monkeypatch.setattr(revision, "_revision_candidates", lambda *a, **k: ((reviewed,), "a" * 64))
    monkeypatch.setattr(
        revision,
        "_collect_proposal_records",
        lambda *a, **k: (
            {
                "os/example": {
                    "body": "## 설명\n\n이전 문장을 검토해서 만든 늦은 변경이다.\n",
                    "statement": "이전 문장을 다듬는다.",
                    "current_use": reviewed.purpose,
                }
            },
            {"os/example": "proposal.json"},
        ),
    )
    monkeypatch.setattr(revision, "build_knowledge_service", lambda _: (None, service))
    acquire = repository.exclusive
    interleaved = False
    after_other_writer = {}

    @contextmanager
    def acquire_after_concurrent_change():
        nonlocal interleaved
        if not interleaved:
            interleaved = True
            if change == "edit":
                service.curate_compiled_wiki_revisions(
                    (
                        CuratedRevision(
                            "os/example",
                            "## 설명\n\n다른 작업이 먼저 저장한 최신 문장이다.\n",
                            "최신 문장을 보존한다.",
                        ),
                    )
                )
            else:
                (tmp_path / current.relative_path).unlink()
            after_other_writer["inputs"] = compiler.snapshot_inputs()
            after_other_writer["outputs"] = compiler.snapshot_outputs()
        with acquire():
            yield

    monkeypatch.setattr(repository, "exclusive", acquire_after_concurrent_change)

    with pytest.raises(WoonError, match="changed after it was read"):
        revision.apply_codex_quality_revisions(
            tmp_path,
            tmp_path / "plan.json",
            tmp_path / "reviews",
            (tmp_path / "proposals",),
        )

    assert interleaved
    assert compiler.snapshot_inputs() == after_other_writer["inputs"]
    assert compiler.snapshot_outputs() == after_other_writer["outputs"]
    if change == "edit":
        assert "최신 문장" in service.get("os/example").body
        assert service.search("최신 문장")[0].canonical_id == "os/example"
        assert not service.search("늦은 변경")


def test_curated_batch_checks_every_revision_before_applying_any_page(tmp_path: Path) -> None:
    for name in ("first", "second"):
        write_page(tmp_path, f"os/{name}.md", name, f"## 설명\n\n{name} 원문이다.\n")
    compiler = CompiledWiki(compiled_settings(tmp_path))
    compiler.migrate()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, tmp_path / "wiki"),
        SQLiteFtsSearchIndex(tmp_path / "search.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    first = CuratedRevision(
        "os/first",
        "## 설명\n\n첫 문서의 검토된 교정이다.\n",
        "첫 문서를 교정한다.",
        expected_revision=service.get("os/first").revision,
    )
    second = CuratedRevision(
        "os/second",
        "## 설명\n\n둘째 문서의 검토된 교정이다.\n",
        "둘째 문서를 교정한다.",
        expected_revision="0" * 64,
    )
    before_inputs = compiler.snapshot_inputs()
    before_outputs = compiler.snapshot_outputs()

    with pytest.raises(WoonError, match="changed after it was read"):
        service.curate_compiled_wiki_revisions((first, second))

    assert compiler.snapshot_inputs() == before_inputs
    assert compiler.snapshot_outputs() == before_outputs
    current_second = replace(second, expected_revision=service.get("os/second").revision)
    report = service.curate_compiled_wiki_revisions((first, current_second))
    assert report.curated == 2
    assert compiler.audit().complete
    assert service.search("검토된 교정")
