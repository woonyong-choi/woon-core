from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

from woon_core.knowledge.source_restructure import (
    apply_source_restructure,
    audit_source_catalog_references,
    prepare_source_restructure_preflight,
    reconcile_raw_source_catalog_owners,
    render_source_restructure_template,
    write_reconciled_source_restructure_manifest,
)


def _write_source(vault: Path, relative: str, content: bytes) -> Path:
    path = vault / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_source_restructure_template_classifies_only_unambiguous_prefixes(tmp_path: Path) -> None:
    web = _write_source(tmp_path, "wiki/private/_sources/knowledge/web/official.html", b"official")
    local = _write_source(tmp_path, "wiki/private/_sources/knowledge/local-only/book.pdf", b"book")
    codex = _write_source(tmp_path, "wiki/private/_sources/codex/2026-09-05/talk.json", b"[]")

    payload = yaml.safe_load(render_source_restructure_template(tmp_path))
    records = {record["current_path"]: record for record in payload["records"]}

    assert records[web.relative_to(tmp_path).as_posix()] == {
        "current_path": "wiki/private/_sources/knowledge/web/official.html",
        "current_sha256": hashlib.sha256(b"official").hexdigest(),
        "bytes": 8,
        "storage_scope": "public-tracked",
        "disposition": "move",
        "catalog_reconciliation": "pending",
        "target_path": "sources/knowledge/web/official.html",
    }
    assert records[local.relative_to(tmp_path).as_posix()]["target_path"] == (
        "private/knowledge/local-only/book.pdf"
    )
    assert records[local.relative_to(tmp_path).as_posix()]["storage_scope"] == "private-tracked"
    assert records[codex.relative_to(tmp_path).as_posix()]["target_path"] == (
        "private/codex/2026-09-05/talk.json"
    )


def test_source_restructure_preflight_rejects_stale_or_incomplete_manifest(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/codex/day/talk.json", b"[]")
    manifest = tmp_path / "source-restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/private/_sources/codex/day/talk.json\n"
        "  current_sha256: stale\n"
        "  bytes: 2\n"
        "  storage_scope: local-only\n"
        "  disposition: move\n"
        "  catalog_reconciliation: pending\n"
        "  target_path: private/codex/day/talk.json\n",
        encoding="utf-8",
    )

    report = prepare_source_restructure_preflight(tmp_path, manifest)

    assert report.file_count == 1
    assert report.byte_count == source.stat().st_size
    assert report.catalog_pending_count == 1
    assert report.issues == (
        "records[1]: current_sha256 does not match: wiki/private/_sources/codex/day/talk.json",
    )


def test_source_restructure_preflight_rejects_an_extra_source_record(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/codex/day/talk.json", b"[]")
    manifest = tmp_path / "source-restructure.yaml"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/private/_sources/codex/day/talk.json\n"
        f"  current_sha256: {digest}\n"
        "  bytes: 2\n  storage_scope: local-only\n  disposition: move\n"
        "  catalog_reconciliation: pending\n"
        "  target_path: private/codex/day/talk.json\n"
        "- current_path: wiki/private/_sources/codex/day/missing.json\n"
        "  current_sha256: missing\n"
        "  bytes: 0\n  storage_scope: local-only\n  disposition: move\n"
        "  catalog_reconciliation: pending\n"
        "  target_path: private/codex/day/missing.json\n",
        encoding="utf-8",
    )

    report = prepare_source_restructure_preflight(tmp_path, manifest)

    assert report.issues == (
        "records[2]: current_path is not an active raw source: "
        "wiki/private/_sources/codex/day/missing.json",
        "manifest names 1 non-active raw source files",
    )


def test_apply_source_restructure_moves_bytes_and_live_locators_atomically(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/knowledge/local-only/book.pdf", b"book")
    web_source = _write_source(
        tmp_path, "wiki/private/_sources/knowledge/web/official.html", b"web"
    )
    catalog = tmp_path / "catalog/sources/book.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "version: 1\nsource: book\nrecords:\n"
        "- source_id: source://book\n"
        f"  target: {source.relative_to(tmp_path).as_posix()}\n"
        "- source_id: source://web\n"
        f"  target: {web_source.relative_to(tmp_path).as_posix()}\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs/workflow.md"
    docs.parent.mkdir(parents=True, exist_ok=True)
    docs.write_text(
        f"원본은 `{source.relative_to(tmp_path).as_posix()}`와 "
        f"`{web_source.relative_to(tmp_path).as_posix()}`에 보존한다.\n",
        encoding="utf-8",
    )
    manifest = tmp_path / ".local/woon-knowledge/source-restructure/reconciled.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "version: 1\nrecords:\n"
        f"- current_path: {source.relative_to(tmp_path).as_posix()}\n"
        f"  current_sha256: {hashlib.sha256(source.read_bytes()).hexdigest()}\n"
        "  bytes: 4\n"
        "  storage_scope: private-tracked\n"
        "  disposition: move\n"
        "  catalog_reconciliation: reconciled\n"
        "  target_path: private/knowledge/local-only/book.pdf\n"
        f"- current_path: {web_source.relative_to(tmp_path).as_posix()}\n"
        f"  current_sha256: {hashlib.sha256(web_source.read_bytes()).hexdigest()}\n"
        "  bytes: 3\n"
        "  storage_scope: public-tracked\n"
        "  disposition: move\n"
        "  catalog_reconciliation: reconciled\n"
        "  target_path: sources/knowledge/web/official.html\n",
        encoding="utf-8",
    )

    report = apply_source_restructure(tmp_path, manifest)

    target = tmp_path / "private/knowledge/local-only/book.pdf"
    web_target = tmp_path / "sources/knowledge/web/official.html"
    assert report.moved_files == 2
    assert report.moved_bytes == 7
    assert report.rewritten_files == 2
    assert report.rewritten_references == 4
    assert not source.exists()
    assert not web_source.exists()
    assert target.read_bytes() == b"book"
    assert web_target.read_bytes() == b"web"
    assert "private/knowledge/local-only/book.pdf" in docs.read_text(encoding="utf-8")
    assert "sources/knowledge/web/official.html" in docs.read_text(encoding="utf-8")
    assert "private/knowledge/local-only/book.pdf" in catalog.read_text(encoding="utf-8")
    assert audit_source_catalog_references(tmp_path).issues == ()


def test_reconciled_source_restructure_manifest_requires_catalog_evidence(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/novel/work/scene.md", b"scene")
    catalog = tmp_path / "catalog/sources/novel.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "version: 1\nsource: novel\nrecords:\n"
        "- source_id: source://novel/scene\n"
        f"  target: {source.relative_to(tmp_path).as_posix()}\n",
        encoding="utf-8",
    )
    manifest = tmp_path / ".local/woon-knowledge/source-restructure/pending.yaml"
    output = tmp_path / ".local/woon-knowledge/source-restructure/reconciled.yaml"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "version: 1\nrecords:\n"
        f"- current_path: {source.relative_to(tmp_path).as_posix()}\n"
        f"  current_sha256: {hashlib.sha256(source.read_bytes()).hexdigest()}\n"
        "  bytes: 5\n"
        "  storage_scope: private-tracked\n"
        "  disposition: move\n"
        "  catalog_reconciliation: pending\n"
        "  target_path: private/novel/work/scene.md\n",
        encoding="utf-8",
    )

    report = write_reconciled_source_restructure_manifest(tmp_path, manifest, output)

    assert report.reconciled_records == 1
    assert report.output_manifest_path.endswith("reconciled.yaml")
    preflight = prepare_source_restructure_preflight(tmp_path, output)
    assert preflight.issues == ()
    assert preflight.catalog_pending_count == 0


def test_source_catalog_reference_audit_tracks_owners_and_stale_locators(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/knowledge/local-only/book.pdf", b"book")
    catalog = tmp_path / "catalog/sources/book.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "version: 1\nsource: book\nrecords:\n"
        "- source_id: source://book\n"
        f"  target: {source.relative_to(tmp_path).as_posix()}\n"
        "- source_id: source://stale\n"
        "  target: wiki/private/_sources/knowledge/local-only/missing.pdf\n",
        encoding="utf-8",
    )
    claims = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims.parent.mkdir(parents=True, exist_ok=True)
    claims.write_text(
        "claims:\n"
        "- claim_id: claim://private/knowledge/example\n"
        "  source_ids: [source://private/knowledge/example]\n"
        f"  locator: {source.relative_to(tmp_path).as_posix()}\n",
        encoding="utf-8",
    )

    report = audit_source_catalog_references(tmp_path)

    assert report.file_count == 1
    assert report.catalog_record_count == 2
    assert report.reference_count == 2
    assert report.orphan_count == 0
    assert report.duplicate_primary_count == 0
    assert report.stale_reference_count == 1
    assert report.issues == (
        "stale raw-source locator wiki/private/_sources/knowledge/local-only/missing.pdf "
        "at catalog/sources/book.yaml:$.records[1].target",
    )
    assert report.records == (
        {
            "current_path": source.relative_to(tmp_path).as_posix(),
            "primary_catalog_owners": [
                "catalog/sources/book.yaml:records[1]=source://book",
            ],
            "catalog_references": [
                "catalog/llm-wiki/claims.yaml:$.claims[0].locator="
                "wiki/private/_sources/knowledge/local-only/book.pdf",
                "catalog/sources/book.yaml:$.records[0].target="
                "wiki/private/_sources/knowledge/local-only/book.pdf",
            ],
            "catalog_reconciliation": "reconciled",
        },
    )


def test_source_catalog_reference_audit_keeps_archived_locator_history_non_blocking(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/knowledge/local-only/book.pdf", b"book")
    catalog = tmp_path / "catalog/sources/book.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "version: 1\nsource: book\nrecords:\n"
        "- source_id: source://book\n"
        f"  target: {source.relative_to(tmp_path).as_posix()}\n",
        encoding="utf-8",
    )
    compiler_sources = tmp_path / "catalog/llm-wiki/sources.yaml"
    compiler_sources.parent.mkdir(parents=True, exist_ok=True)
    compiler_sources.write_text(
        "sources:\n"
        "- source_id: source://archived\n"
        "  lifecycle: archived\n"
        "  locator: wiki/private/_sources/knowledge/local-only/missing-archived.pdf\n"
        "  body: wiki/private/_sources/knowledge/local-only/missing-archived.pdf\n"
        "- source_id: source://compiled\n"
        "  lifecycle: compiled\n"
        "  body: wiki/private/_sources/knowledge/local-only/missing-compiled.pdf\n",
        encoding="utf-8",
    )
    claims = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims.write_text(
        "claims:\n"
        "- status: superseded\n"
        "  locator: wiki/private/_sources/knowledge/local-only/missing-superseded.pdf\n"
        "  markdown: wiki/private/_sources/knowledge/local-only/missing-superseded.pdf\n"
        "- status: accepted\n"
        "  markdown: wiki/private/_sources/knowledge/local-only/missing-accepted.pdf\n",
        encoding="utf-8",
    )

    report = audit_source_catalog_references(tmp_path)

    assert report.stale_reference_count == 2
    assert report.issues == (
        "stale raw-source locator wiki/private/_sources/knowledge/local-only/missing-accepted.pdf "
        "at catalog/llm-wiki/claims.yaml:$.claims[1].markdown",
        "stale raw-source locator wiki/private/_sources/knowledge/local-only/missing-compiled.pdf "
        "at catalog/llm-wiki/sources.yaml:$.sources[1].body",
    )


def test_source_catalog_reference_audit_does_not_count_alias_as_primary_owner(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/knowledge/local-only/book.pdf", b"book")
    catalog = tmp_path / "catalog/sources/book.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    target = source.relative_to(tmp_path).as_posix()
    catalog.write_text(
        "version: 1\nsource: book\nrecords:\n"
        "- source_id: source://book\n"
        "  state: canonical\n"
        f"  target: {target}\n"
        "- source_id: source://book-alias\n"
        "  state: content-alias\n"
        f"  target: {target}\n",
        encoding="utf-8",
    )

    report = audit_source_catalog_references(tmp_path)

    assert report.duplicate_primary_count == 0
    assert report.records[0]["primary_catalog_owners"] == [
        "catalog/sources/book.yaml:records[1]=source://book"
    ]


def test_source_catalog_reference_audit_accepts_the_complete_restructured_layout(
    tmp_path: Path,
) -> None:
    source = _write_source(tmp_path, "private/knowledge/local-only/book.pdf", b"book")
    for relative in (
        "sources/knowledge/web",
        "private/novel",
        "private/codex",
        "private/legacy-wiki",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    catalog = tmp_path / "catalog/sources/book.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "version: 1\nsource: book\nrecords:\n"
        "- source_id: source://book\n"
        f"  target: {source.relative_to(tmp_path).as_posix()}\n",
        encoding="utf-8",
    )
    claims = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims.parent.mkdir(parents=True, exist_ok=True)
    claims.write_text(
        "claims:\n"
        "- markdown: '[[private/knowledge/human-wiki-document]]'\n"
        f"  locator: {source.relative_to(tmp_path).as_posix()}\n",
        encoding="utf-8",
    )

    report = audit_source_catalog_references(tmp_path)

    assert report.file_count == 1
    assert report.orphan_count == 0
    assert report.stale_reference_count == 0
    assert report.issues == ()
    assert report.records[0]["primary_catalog_owners"] == [
        "catalog/sources/book.yaml:records[1]=source://book"
    ]


def test_raw_source_owner_reconciliation_adds_only_missing_primary_owners(tmp_path: Path) -> None:
    owned = _write_source(
        tmp_path, "wiki/private/_sources/knowledge/local-only/owned.pdf", b"owned"
    )
    orphan = _write_source(tmp_path, "wiki/private/_sources/novel/work/scene.md", b"scene")
    catalog = tmp_path / "catalog/sources/existing.yaml"
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog.write_text(
        "version: 1\nsource: existing\nsummary: {canonical: 1}\nrecords:\n"
        "- source_id: source://existing/owned\n"
        "  locator: knowledge/local-only/owned.pdf\n"
        f"  sha256: {hashlib.sha256(owned.read_bytes()).hexdigest()}\n"
        f"  size: {owned.stat().st_size}\n"
        "  role: document\n  privacy: private/local-only\n  state: canonical\n"
        f"  target: {owned.relative_to(tmp_path).as_posix()}\n"
        f"  target_sha256: {hashlib.sha256(owned.read_bytes()).hexdigest()}\n"
        "excluded: []\n",
        encoding="utf-8",
    )

    report = reconcile_raw_source_catalog_owners(tmp_path)

    assert report.created is True
    assert report.records_added == 1
    assert report.orphan_count_before == 1
    assert report.orphan_count_after == 0
    generated = yaml.safe_load((tmp_path / report.catalog_path).read_text(encoding="utf-8"))
    assert generated["records"] == [
        {
            "source_id": "source://raw-archive/novel/work/scene.md",
            "locator": "novel/work/scene.md",
            "sha256": hashlib.sha256(orphan.read_bytes()).hexdigest(),
            "size": orphan.stat().st_size,
            "role": "raw-archive-file",
            "privacy": "private/local-only",
            "state": "canonical",
            "target": orphan.relative_to(tmp_path).as_posix(),
            "target_sha256": hashlib.sha256(orphan.read_bytes()).hexdigest(),
        }
    ]

    later = _write_source(
        tmp_path, "wiki/private/_sources/codex/day/follow-up.json", b'{"ok": true}'
    )
    refreshed = reconcile_raw_source_catalog_owners(tmp_path)

    assert refreshed.created is False
    assert refreshed.records_added == 1
    updated = yaml.safe_load((tmp_path / report.catalog_path).read_text(encoding="utf-8"))
    assert updated["summary"] == {"canonical": 2}
    assert updated["records"][-1]["target"] == later.relative_to(tmp_path).as_posix()
    assert audit_source_catalog_references(tmp_path).orphan_count == 0

    repeated = reconcile_raw_source_catalog_owners(tmp_path)

    assert repeated.created is False
    assert repeated.records_added == 0


def test_source_restructure_preflight_rejects_unevidenced_reconciliation(tmp_path: Path) -> None:
    source = _write_source(tmp_path, "wiki/private/_sources/codex/day/talk.json", b"[]")
    (tmp_path / "catalog").mkdir()
    manifest = tmp_path / "source-restructure.yaml"
    manifest.write_text(
        "version: 1\nrecords:\n"
        "- current_path: wiki/private/_sources/codex/day/talk.json\n"
        f"  current_sha256: {hashlib.sha256(source.read_bytes()).hexdigest()}\n"
        "  bytes: 2\n  storage_scope: local-only\n  disposition: move\n"
        "  catalog_reconciliation: reconciled\n"
        "  target_path: private/codex/day/talk.json\n",
        encoding="utf-8",
    )

    report = prepare_source_restructure_preflight(tmp_path, manifest)

    assert report.issues == (
        "records[1]: catalog reconciliation is not evidenced for "
        "wiki/private/_sources/codex/day/talk.json",
    )
