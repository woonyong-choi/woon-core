from __future__ import annotations

import json
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.wiki_content_quality_audit import (
    audit_wiki_content_quality,
    render_wiki_content_quality_audit,
    write_wiki_content_quality_audit,
)


def _write_page(
    vault: Path,
    relative: str,
    canonical_id: str,
    *,
    node_kind: str = "topic",
    body: str = "내용입니다.\n",
    publication_state: str | None = None,
) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    state = f"publication_state: {publication_state}\n" if publication_state else ""
    path.write_text(
        "---\n"
        "type: Wiki\n"
        f"title: {path.stem}\n"
        f"canonical_id: {canonical_id}\n"
        f"node_kind: {node_kind}\n"
        "access: local-only\n"
        f"{state}"
        "---\n\n"
        f"# {path.stem}\n\n"
        f"{body}",
        encoding="utf-8",
    )
    return path


def test_audit_classifies_navigation_execution_and_publication_without_claiming_verification(
    tmp_path: Path,
) -> None:
    _write_page(tmp_path, "wiki/README.md", "README", node_kind="root")
    _write_page(
        tmp_path,
        "wiki/lesson.md",
        "lesson",
        body="```run-python\nprint('ok')\n```\n",
        publication_state="review",
    )
    _write_page(
        tmp_path,
        "wiki/legacy-books/example/chapter.md",
        "books/example/chapter",
        body="```run-kotlin\nfun main() = println(1)\n```\n",
    )
    catalog = tmp_path / "catalog/llm-wiki/pages.yaml"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(
        "version: 1\npages:\n- page_id: lesson\n  output_path: lesson.md\n",
        encoding="utf-8",
    )
    coverage = tmp_path / "catalog/book-coverage/example.json"
    coverage.parent.mkdir(exist_ok=True)
    coverage.write_text(
        '{"book_id":"books/example","nodes":[{"canonical_id":"books/example/chapter"}]}\n',
        encoding="utf-8",
    )

    report = audit_wiki_content_quality(tmp_path)

    assert report.document_count == 3
    assert report.compiler_documents == 1
    assert report.manual_documents == 2
    assert report.navigation_documents == 1
    assert report.reader_documents == 1
    assert report.runnable_code_blocks == 2
    by_id = {record.canonical_id: record for record in report.records}
    assert by_id["README"].quality_status == "navigation-exempt"
    assert by_id["lesson"].execution_contract == "runnable-unverified"
    assert by_id["lesson"].publication_readiness == "review-required"
    assert by_id["books/example/chapter"].execution_contract == "book-contract-required"
    assert by_id["books/example/chapter"].quality_status == "book-contract-required"


def test_audit_renders_and_writes_only_to_the_local_quality_workspace(tmp_path: Path) -> None:
    _write_page(tmp_path, "wiki/README.md", "README", node_kind="root")
    rendered = render_wiki_content_quality_audit(tmp_path)
    payload = json.loads(rendered)

    assert payload["kind"] == "wiki-content-quality-audit"
    assert payload["document_count"] == 1

    output = tmp_path / ".local/woon-knowledge/wiki-content-quality/audit.json"
    assert write_wiki_content_quality_audit(tmp_path, output) == output
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(WoonError, match="audit already exists"):
        write_wiki_content_quality_audit(tmp_path, output)
    with pytest.raises(WoonError, match="must stay below"):
        write_wiki_content_quality_audit(tmp_path, tmp_path / "outside.json")
