import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
import yaml
from test_knowledge import metadata
from test_orchestration import write_policy
from test_second_brain_runtime import _request, _write_runnable_policy

from woon_core.errors import WoonError
from woon_core.knowledge.adapters import (
    GitKnowledgeHistory,
    MarkdownDocumentRepository,
    SQLiteFtsSearchIndex,
)
from woon_core.knowledge.codex_source_archive import record_codex_source_bundle
from woon_core.knowledge.compiled_wiki import (
    CompiledWikiWikilinkRewrite,
    _normalized_wikilink_replacements,
    _rewrite_exact_wikilinks,
)
from woon_core.knowledge.conversation_adapter import completed_turn_bundles
from woon_core.knowledge.orchestration import (
    load_orchestrator_settings,
    verify_codex_automation_registry,
)
from woon_core.knowledge.second_brain_runtime import AutomationRunStore
from woon_core.knowledge.service import KnowledgeService
from woon_core.knowledge.woon_wiki import preserve_managed_context
from woon_core.knowledge.workflow_health import summarize_health
from woon_core.registry import Registry, Repository
from woon_core.settings import apply_json_update


def test_many_predecessor_wikilinks_merge_without_losing_link_identity() -> None:
    targets = ("wiki/old/topic", "wiki/old/detail", "wiki/hubs/topic")
    successor = "wiki/Wiki/topic"
    rewrites = tuple(CompiledWikiWikilinkRewrite(old, successor, 1, 0) for old in targets)
    rewritten, counts = _rewrite_exact_wikilinks(
        "[[wiki/old/topic|topic]] ![[wiki/old/detail.md#section|detail]] [[wiki/hubs/topic]]",
        _normalized_wikilink_replacements(rewrites),
    )
    assert rewritten == (
        "[[wiki/Wiki/topic|topic]] ![[wiki/Wiki/topic.md#section|detail]] [[wiki/Wiki/topic|topic]]"
    )
    assert counts == dict.fromkeys(targets, 1)
    with pytest.raises(WoonError, match="current_target is duplicated"):
        _normalized_wikilink_replacements((*rewrites, rewrites[0]))


def test_retarget_keeps_original_display_and_explicit_embed_and_anchor_semantics() -> None:
    successor = "wiki/Wiki/keywords/ai-machine-learning-topic-ff856a774938"
    replacements = _normalized_wikilink_replacements(
        (CompiledWikiWikilinkRewrite("wiki/ai/softmax", successor, 6, 0),)
    )
    original = (
        "[[wiki/ai/softmax]] [[wiki/ai/softmax.md#미분]] "
        "[[wiki/ai/softmax#^example|기존 표현]] "
        "![[wiki/ai/softmax#미분]] ![[wiki/ai/softmax|300]] "
        "[[wiki/ai/softmax#^example]] [[wiki/private/softmax]]"
    )
    rewritten, counts = _rewrite_exact_wikilinks(original, replacements)
    assert rewritten == (
        f"[[{successor}|softmax]] [[{successor}.md#미분|softmax#미분]] "
        f"[[{successor}#^example|기존 표현]] "
        f"![[{successor}#미분]] ![[{successor}|300]] "
        f"[[{successor}#^example|softmax#^example]] [[wiki/private/softmax]]"
    )
    assert counts == {"wiki/ai/softmax": 6}
    assert _rewrite_exact_wikilinks(rewritten, replacements) == (
        rewritten,
        {"wiki/ai/softmax": 0},
    )


def test_reader_resolution_rejects_missing_but_generator_can_plan(tmp_path: Path) -> None:
    registry = Registry(1, {"theme": Repository("", "theme", local_only=True)})
    registry.validate()
    assert registry.resolve(tmp_path, "theme") == tmp_path / "theme"
    with pytest.raises(WoonError, match="does not exist"):
        registry.resolve(tmp_path, "theme", must_exist=True)


@pytest.mark.parametrize("status", ["enabled", "paused"])
def test_v2_contract_uses_reviewed_digest_and_structured_guards(
    tmp_path: Path, status: str
) -> None:
    prompt = "명시된 계약에 따라 허용된 일정 후보만 검토합니다."
    write_policy(
        tmp_path,
        status=status,
        thread_id="thread-1",
        codex_automation_id="mail",
        rrule="FREQ=DAILY",
        notification_policy="failed_runs_only",
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
    )
    policy = tmp_path / "config/second-brain-orchestrator.yaml"
    raw = yaml.safe_load(policy.read_text())
    raw["version"] = 2
    policy.write_text(yaml.safe_dump(raw))
    root = tmp_path / "automations/mail"
    root.mkdir(parents=True)
    unrelated = root.parent / "retired"
    unrelated.mkdir()
    (unrelated / "automation.toml").write_text("invalid legacy TOML")
    actual_status = "ACTIVE" if status == "enabled" else "PAUSED"
    (root / "automation.toml").write_text(
        f'id="mail"\nkind="heartbeat"\nstatus="{actual_status}"\ntarget_thread_id="thread-1"\n'
        'rrule="FREQ=DAILY"\nnotification_policy="failed_runs_only"\n'
        f"prompt={json.dumps(prompt, ensure_ascii=False)}\n"
    )
    assert verify_codex_automation_registry(
        load_orchestrator_settings(tmp_path), root.parent, lane_id="mail-schedule-candidates"
    )
    raw["global_guards"]["advertising"]["mutate_mailbox"] = True
    policy.write_text(yaml.safe_dump(raw))
    with pytest.raises(WoonError, match="advertising"):
        load_orchestrator_settings(tmp_path)


def test_snapshot_search_labels_edits_and_never_serves_removed_or_excluded(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, root),
        SQLiteFtsSearchIndex(tmp_path / "index.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        snapshot_vault=tmp_path,
        snapshot_roots=(root,),
    )
    saved = service.archive(metadata(), "## 설명\n\n창작 연구를 위한 검증된 원문.")
    assert service.search("창작 연구")[0].freshness == "current"
    path = tmp_path / saved.document.relative_path
    path.write_text(path.read_text().replace("검증된 원문", "수정된 원문"))
    assert service.search("창작 연구")[0].freshness == "stale-snapshot"
    service._snapshot_exclusions = ("wiki/**",)
    assert service.search("창작 연구") == []
    service._snapshot_exclusions = ()
    path.unlink()
    assert service.search("창작 연구") == []


def test_planned_page_does_not_gain_evidence_label() -> None:
    rendered = "---\ncanonical_id: books/plan\ntitle: 계획\ncontent_status: planned\n---\n# 계획\n"
    result = preserve_managed_context("", rendered)
    assert 'knowledge_state: "생각 중"' in result
    assert "state_reason: planned-content" in result


def test_repeated_failure_keeps_success_checkpoint_and_failure_status_stable(
    tmp_path: Path,
) -> None:
    _write_runnable_policy(tmp_path)
    settings = load_orchestrator_settings(tmp_path)
    before = settings.checkpoint_path.read_bytes()
    store = AutomationRunStore(settings)

    def fail():
        raise ValueError("private payload must not enter status")

    for _ in range(2):
        with pytest.raises(WoonError, match="producer failed"):
            store.run("mail-schedule-candidates", _request(settings, tmp_path), fail)
        assert settings.checkpoint_path.read_bytes() == before
        path = settings.checkpoint_path.parent / "automation-status/mail-schedule-candidates.json"
        current = path.read_bytes()
        assert b"private payload" not in current
        if _ == 0:
            first = current
        else:
            assert current == first


def test_health_summary_is_bounded_without_hiding_counts() -> None:
    result = summarize_health(
        {"issue_counts": {"permissions": 50000}, "issues": {"permissions": ["example"] * 50000}}
    )
    assert result["issue_counts"]["permissions"] == 50000
    assert len(result["samples"]["permissions"]) == 3


def test_settings_preserve_backup_and_reject_concurrent_edit(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    before = b'{"search":""}\n'
    path.write_bytes(before)
    expected = hashlib.sha256(before).hexdigest()
    receipt_root = tmp_path / "receipts"
    result = apply_json_update(
        path, {"search": "path:wiki"}, expected_sha256=expected, receipt_root=receipt_root
    )
    assert result["changed"]
    assert (receipt_root / "backups" / f"{expected}.json").read_bytes() == before
    assert json.loads(path.read_bytes())["search"] == "path:wiki"
    with pytest.raises(WoonError, match="changed after inspection"):
        apply_json_update(path, {}, expected_sha256=expected, receipt_root=receipt_root)


@pytest.mark.parametrize("kind", ["codex", "chatgpt"])
def test_conversation_adapter_requires_opt_in_and_completed_visible_turns(
    tmp_path: Path, kind: str
) -> None:
    snapshot = {
        "thread": {"id": "a", "kind": kind, "title": "문장 다듬기"},
        "turns": [
            {
                "id": "turn-1",
                "status": "completed",
                "completedAt": "2026-09-08T01:00:00Z",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "타인의 문장"}]},
                    {
                        "type": "agentMessage",
                        "text": "다듬은 문장",
                        **({"phase": "final_answer"} if kind == "codex" else {}),
                    },
                    {"type": "agentMessage", "text": "진행 상황", "phase": "commentary"},
                    {"type": "toolCall", "text": "secret output"},
                ],
            },
            {"id": "turn-2", "status": "inProgress", "items": []},
        ],
    }
    with pytest.raises(WoonError, match="opted in"):
        completed_turn_bundles(snapshot, allowed_thread_ids=(), day=date(2026, 9, 8))
    bundles = completed_turn_bundles(snapshot, allowed_thread_ids=("a",), day=date(2026, 9, 8))
    assert [message.text for message in bundles[0].messages] == ["타인의 문장", "다듬은 문장"]
    assert not record_codex_source_bundle(tmp_path, bundles[0]).replayed
    assert record_codex_source_bundle(tmp_path, bundles[0]).replayed


def test_planned_migration_is_label_only_pinned_and_idempotent(tmp_path, monkeypatch):
    from test_compiled_transaction import _settings, _write_seed

    import woon_core.knowledge.compiled_wiki as module

    _write_seed(tmp_path)
    path = tmp_path / "wiki/seed.md"
    path.write_text(
        path.read_text().replace("status: Canonical", "content_status: planned\nstatus: Canonical")
    )
    compiler = module.CompiledWiki(_settings(tmp_path))
    current_renderer = module.preserve_managed_context

    def old_renderer(existing, rendered):
        return (
            current_renderer(existing, rendered)
            .replace('knowledge_state: "생각 중"', 'knowledge_state: "근거 확인됨"')
            .replace("state_reason: planned-content", "state_reason: accepted-evidence-receipt")
        )

    monkeypatch.setattr(module, "preserve_managed_context", old_renderer)
    compiler.migrate()
    monkeypatch.setattr(module, "preserve_managed_context", current_renderer)
    service = KnowledgeService(
        MarkdownDocumentRepository(tmp_path, tmp_path / "wiki"),
        SQLiteFtsSearchIndex(tmp_path / "index.sqlite3"),
        GitKnowledgeHistory(tmp_path),
        compiled_wiki=compiler,
    )
    before = compiler.snapshot_inputs()
    plan = service.migrate_planned_states()
    assert plan["page_ids"] == ["seed"]
    with pytest.raises(WoonError, match="current catalog revision"):
        service.migrate_planned_states(apply=True, expected_catalog_revision="0" * 64)
    assert compiler.snapshot_inputs() == before
    result = service.migrate_planned_states(
        apply=True, expected_catalog_revision=plan["catalog_revision"]
    )
    assert result["compiled"] == 1
    assert compiler.audit().complete
    assert service.migrate_planned_states()["page_ids"] == []
    assert "state_reason: planned-content" in path.read_text()
    for catalog, content in before.items():
        if catalog.name != "receipts.yaml":
            assert (catalog.read_bytes() if catalog.is_file() else None) == content


def test_diagnosis_returns_other_checks_when_health_fails(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import woon_core.knowledge.workflow_health as module

    _write_runnable_policy(tmp_path)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"issue_counts": {"broken": 12}, "issues": {"broken": ["one"]}}),
        ),
    )
    result = module.diagnose_workflow(tmp_path, tmp_path / "absent-automations")
    assert result["status"] == "blocked"
    assert result["receipt_recorded"] is False
    assert result["checks"]["instruction-inventory"]["status"] == "ok"
    assert result["checks"]["vault-health"]["issue_counts"]["broken"] == 12


def test_missing_orca_hook_repair_preserves_unrelated_commands(tmp_path):
    from woon_core.environment.codex_hooks import repair_missing_orca_hooks

    home = tmp_path / ".codex"
    home.mkdir()
    executable = tmp_path / ".orca/agent-hooks/codex-hook.sh"
    command = (
        f"if [ -f '{executable}' ] && [ -r '{executable}' ] && [ -x '{executable}' ]; "
        f"then /bin/sh '{executable}'; "
        "else { command -p cat 2>/dev/null || cat; } >/dev/null 2>&1 || :; fi"
    )
    hooks = home / "hooks.json"
    hooks.write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"command": command}, {"command": "herdr-safe-hook"}]}]}}
        )
    )
    assert repair_missing_orca_hooks(home, apply=True)["removed"] == 1
    assert json.loads(hooks.read_text())["hooks"]["Stop"][0]["hooks"] == [
        {"command": "herdr-safe-hook"}
    ]
    assert repair_missing_orca_hooks(home, apply=True)["changed"] is False


def test_affected_book_writer_preserves_unrelated_stale_page(tmp_path):
    from test_compiled_wiki import atomic_book_service, write_page

    write_page(tmp_path, "creative/research.md", "창작 연구", "개인 창작의 근거")
    compiler, service, _, record, revision, body_hash = atomic_book_service(tmp_path)
    unrelated = tmp_path / "wiki/creative/research.md"
    unrelated.write_text(unrelated.read_text() + "\n사용자의 미완성 변경\n")
    before = unrelated.read_bytes()
    inputs = compiler.snapshot_inputs()
    args = (
        (record,),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": revision},
        {"books/atomic-book/part-01": body_hash},
    )
    with pytest.raises(WoonError, match="stale catalog"):
        service.apply_verified_book_update(*args)
    assert compiler.snapshot_inputs() == inputs
    result = service.apply_verified_book_update(*args, audit_scope="affected")
    assert result.audit_scope == "affected"
    assert result.remaining_audit_errors == 1
    assert unrelated.read_bytes() == before
    compiler.assert_affected_pages_current(("books/atomic-book",))


def test_affected_gate_rejects_stale_navigation_dependency(tmp_path):
    from test_compiled_wiki import atomic_book_service

    compiler, _, _, _, _, _ = atomic_book_service(tmp_path)
    parent = tmp_path / "wiki/books/programming-language.md"
    parent.write_text(parent.read_text() + "\n아직 검증하지 않은 변경\n")
    with pytest.raises(WoonError, match="affected page books/programming-language"):
        compiler.assert_affected_pages_current(("books/atomic-book/chapter-01",))


@pytest.mark.parametrize(
    "references,retained", [([], False), (["old"], True), (None, True), (["old", 1], True)]
)
def test_shared_claim_scan_is_conservative_without_global_schema_gate(references, retained):
    from woon_core.knowledge.compiled_wiki import CompiledWiki

    claims = {"old": {"status": "accepted"}}
    pages = {"target": {"claim_ids": ["new"]}, "other": {"claim_ids": references}}
    CompiledWiki._supersede_unshared_claims(["old"], "new", "target", pages, claims)
    assert (claims["old"]["status"] == "accepted") is retained


def test_affected_book_writer_does_not_adopt_unrelated_invalid_empty_claims(tmp_path):
    from test_compiled_wiki import atomic_book_service, write_page

    write_page(tmp_path, "creative/draft.md", "창작 초안", "아직 별도 검토할 초안")
    compiler, service, _, record, revision, body_hash = atomic_book_service(tmp_path)
    path = tmp_path / "catalog/llm-wiki/pages.yaml"
    data = yaml.safe_load(path.read_text())
    other = next(page for page in data["pages"] if page["page_id"] == "creative/draft")
    other["claim_ids"] = []
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    errors = compiler.audit().errors
    assert any("creative/draft: page claim_ids" in error for error in errors)
    result = service.apply_verified_book_update(
        (record,),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": revision},
        {"books/atomic-book/part-01": body_hash},
        audit_scope="affected",
    )
    assert result.remaining_audit_errors == len(errors)
    assert (
        next(
            page
            for page in yaml.safe_load(path.read_text())["pages"]
            if page["page_id"] == "creative/draft"
        )["claim_ids"]
        == []
    )


def test_book_tree_receipts_do_not_revalidate_unchanged_scanned_pages(tmp_path, monkeypatch):
    from dataclasses import replace

    from test_compiled_wiki import atomic_book_service, write_page

    import woon_core.knowledge.compiled_wiki as module

    write_page(tmp_path, "creative/research.md", "창작 연구", "창작의 근거")
    compiler, service, _, record, revision, body_hash = atomic_book_service(tmp_path)
    unrelated = (tmp_path / "wiki/creative/research.md").resolve()
    unrelated.write_text(unrelated.read_text() + "\n미완성 사용자 메모\n")
    before = unrelated.read_bytes()
    prepare = module.prepare_wiki_tree_refresh

    def include_unchanged_scanned_page(*args, **kwargs):
        report = prepare(*args, **kwargs)
        return replace(report, pages={**report.pages, unrelated: before})

    monkeypatch.setattr(module, "prepare_wiki_tree_refresh", include_unchanged_scanned_page)
    result = service.apply_verified_book_update(
        (record,),
        {"books/atomic-book/part-01": "books/atomic-book"},
        {"books/atomic-book/part-01": revision},
        {"books/atomic-book/part-01": body_hash},
        audit_scope="affected",
    )
    assert result.remaining_audit_errors == 1
    assert unrelated.read_bytes() == before
