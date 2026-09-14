from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.content_quality_evaluation import evaluate_content_quality
from woon_core.knowledge.content_quality_review_plan import (
    _batch_targets,
    assemble_content_quality_reviews,
    create_content_quality_review_plan,
    rebase_content_quality_review_plan,
)


def _write_vault(vault: Path) -> list[dict[str, str]]:
    pages = [
        {
            "page_id": "os/first",
            "output_path": "os/first.md",
            "title": "첫 문서",
            "markdown": _markdown("첫 문서"),
        },
        {
            "page_id": "os/second",
            "output_path": "os/second.md",
            "title": "둘째 문서",
            "markdown": _markdown("둘째 문서"),
        },
    ]
    catalog = vault / "catalog" / "llm-wiki"
    catalog.mkdir(parents=True)
    receipts: list[dict[str, str]] = []
    page_specs: list[dict[str, str]] = []
    for page in pages:
        path = vault / "wiki" / page["output_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page["markdown"], encoding="utf-8")
        digest = hashlib.sha256(page["markdown"].encode()).hexdigest()
        receipts.append({"page_id": page["page_id"], "output_sha256": digest})
        page_specs.append(
            {
                "page_id": page["page_id"],
                "output_path": page["output_path"],
                "title": page["title"],
            }
        )
    (catalog / "pages.yaml").write_text(
        yaml.safe_dump({"version": 1, "pages": page_specs}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    (catalog / "receipts.yaml").write_text(
        yaml.safe_dump({"version": 1, "receipts": receipts}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return pages


def _markdown(title: str) -> str:
    return f"""---
purpose: 현재 학습에 재사용할 목적을 기록한다.
---

# {title}

## 질문

독자가 무엇을 이해해야 하는지 먼저 확인한다.

## 흐름

관찰한 순서와 이유를 이어 설명한다.

## 문장

문장은 연결 표현으로 앞뒤 관계를 드러낸다.

## 근거

사실과 해석의 근거를 구분한다.

## 찾기

다시 찾을 제목과 용어를 남긴다.

## 목적

현재 학습에 재사용할 목적을 기록한다.
"""


def _criterion_evidence() -> dict[str, dict[str, str]]:
    return {
        "reader_goal": {
            "anchor": "질문",
            "reason": "질문은 독자가 무엇을 이해할지 밝혀 독자 목표를 보인다.",
        },
        "logical_flow": {
            "anchor": "흐름",
            "reason": "흐름은 관찰한 순서와 이유를 이어 논리 순서를 보인다.",
        },
        "natural_korean": {
            "anchor": "문장은 연결 표현으로 앞뒤 관계를 드러낸다.",
            "reason": ("“문장은 연결 표현으로 앞뒤 관계를 드러낸다.”는 자연스러운 문체를 보인다."),
        },
        "evidence_boundary": {
            "anchor": "사실과 해석의 근거를 구분한다.",
            "reason": ("“사실과 해석의 근거를 구분한다.”는 확인 가능한 근거 경계를 보인다."),
        },
        "revisitability": {
            "anchor": "찾기",
            "reason": "찾기는 다시 찾을 제목과 용어를 남겨 검색에 쓸 수 있다.",
        },
        "current_use": {
            "anchor": "purpose: 현재 학습에 재사용할 목적을 기록한다.",
            "reason": (
                "“purpose: 현재 학습에 재사용할 목적을 기록한다.”는 현재 재사용 목적을 분명히 한다."
            ),
        },
    }


def _write_standards(root: Path) -> tuple[Path, Path]:
    standard = root / "learning-writing-harness.md"
    prompt = root / "learning-quality-review-prompt.md"
    standard.write_text("현재 문체 표준\n", encoding="utf-8")
    prompt.write_text("현재 검토 프롬프트\n", encoding="utf-8")
    return standard, prompt


def _write_results(plan_dir: Path, results_dir: Path) -> None:
    manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
    results_dir.mkdir()
    for batch in manifest["batches"]:
        reviews = []
        for target in batch["targets"]:
            reviews.append(
                {
                    "page_id": target["page_id"],
                    "output_sha256": target["output_sha256"],
                    "verdict": "passed",
                    "rubric": {
                        "reader_goal": "pass",
                        "logical_flow": "pass",
                        "natural_korean": "pass",
                        "evidence_boundary": "pass",
                        "revisitability": "pass",
                        "current_use": "pass",
                    },
                    "hard_failures": [],
                    "criterion_evidence": _criterion_evidence(),
                }
            )
        (results_dir / batch["result_file"]).write_text(
            json.dumps(
                {"version": 1, "batch_id": batch["batch_id"], "reviews": reviews},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )


def test_batches_never_combine_pages_past_the_character_limit() -> None:
    targets = [
        {"page_id": "a", "markdown": "a" * 2_500},
        {"page_id": "b", "markdown": "b" * 2_500},
        {"page_id": "c", "markdown": "c" * 1_000},
        {"page_id": "d", "markdown": "d" * 4_500},
    ]

    batches = _batch_targets(targets, batch_size=2, max_batch_chars=4_000)

    assert [[target["page_id"] for target in batch] for batch in batches] == [
        ["a"],
        ["b", "c"],
        ["d"],
    ]


def test_plan_excludes_private_novel_bytes_before_any_review_runner(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    novel_markdown = _markdown("소설 원문")
    novel_path = vault / "wiki/private/novel/scene.md"
    novel_path.parent.mkdir(parents=True)
    novel_path.write_text(novel_markdown, encoding="utf-8")
    pages_path = vault / "catalog/llm-wiki/pages.yaml"
    receipts_path = vault / "catalog/llm-wiki/receipts.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    receipts = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))
    pages["pages"].append(
        {
            "page_id": "private/novel/scene",
            "output_path": "private/novel/scene.md",
            "title": "소설 원문",
        }
    )
    receipts["receipts"].append(
        {
            "page_id": "private/novel/scene",
            "output_sha256": hashlib.sha256(novel_markdown.encode("utf-8")).hexdigest(),
        }
    )
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    receipts_path.write_text(
        yaml.safe_dump(receipts, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    standard, prompt = _write_standards(tmp_path)

    report = create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        tmp_path / "plan",
        4,
    )
    manifest = json.loads((tmp_path / "plan/manifest.json").read_text(encoding="utf-8"))

    assert report["compiled_pages"] == 2
    assert all(
        target["page_id"] != "private/novel/scene"
        for batch in manifest["batches"]
        for target in batch["targets"]
    )


def test_plan_excludes_entire_explicit_book_reader_lineage(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    root_markdown = _markdown("책")
    child_markdown = _markdown("1장")
    for relative, markdown in {
        "personal/book.md": root_markdown,
        "personal/book/chapter-01.md": child_markdown,
    }.items():
        path = vault / "wiki" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
    pages_path = vault / "catalog/llm-wiki/pages.yaml"
    receipts_path = vault / "catalog/llm-wiki/receipts.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    receipts = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))
    pages["pages"].extend(
        [
            {
                "page_id": "personal/book",
                "output_path": "personal/book.md",
                "title": "책",
                "frontmatter": {"content_kind": "book"},
            },
            {
                "page_id": "personal/book/chapter-01",
                "output_path": "personal/book/chapter-01.md",
                "title": "1장",
            },
        ]
    )
    receipts["receipts"].extend(
        [
            {
                "page_id": "personal/book",
                "output_sha256": hashlib.sha256(root_markdown.encode("utf-8")).hexdigest(),
            },
            {
                "page_id": "personal/book/chapter-01",
                "output_sha256": hashlib.sha256(child_markdown.encode("utf-8")).hexdigest(),
            },
        ]
    )
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    receipts_path.write_text(
        yaml.safe_dump(receipts, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    standard, prompt = _write_standards(tmp_path)

    report = create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        tmp_path / "plan",
        4,
    )
    manifest = json.loads((tmp_path / "plan/manifest.json").read_text(encoding="utf-8"))

    assert report["compiled_pages"] == 2
    assert report["excluded_pages"] == 2
    assert [item["page_id"] for item in manifest["exclusions"]] == [
        "personal/book",
        "personal/book/chapter-01",
    ]
    assert all(
        not target["page_id"].startswith("personal/book")
        for batch in manifest["batches"]
        for target in batch["targets"]
    )


def test_plan_excludes_explicit_link_only_source_index_before_any_review_runner(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    source_markdown = "# 원자료 색인\n\n- [[wiki/os/first|첫 문서]]\n"
    source_path = vault / "wiki/resources/sources.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(source_markdown, encoding="utf-8")
    pages_path = vault / "catalog/llm-wiki/pages.yaml"
    receipts_path = vault / "catalog/llm-wiki/receipts.yaml"
    pages = yaml.safe_load(pages_path.read_text(encoding="utf-8"))
    receipts = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))
    pages["pages"].append(
        {
            "page_id": "resources/sources",
            "output_path": "resources/sources.md",
            "title": "원자료 색인",
            "frontmatter": {"content_kind": "source-index"},
        }
    )
    receipts["receipts"].append(
        {
            "page_id": "resources/sources",
            "output_sha256": hashlib.sha256(source_markdown.encode("utf-8")).hexdigest(),
        }
    )
    pages_path.write_text(
        yaml.safe_dump(pages, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    receipts_path.write_text(
        yaml.safe_dump(receipts, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    standard, prompt = _write_standards(tmp_path)

    report = create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        tmp_path / "plan",
        4,
    )
    manifest = json.loads((tmp_path / "plan/manifest.json").read_text(encoding="utf-8"))

    assert report["compiled_pages"] == 2
    assert manifest["exclusions"] == [
        {
            "page_id": "resources/sources",
            "relative_path": "wiki/resources/sources.md",
            "title": "원자료 색인",
            "output_sha256": hashlib.sha256(source_markdown.encode("utf-8")).hexdigest(),
            "reason": "source-index-contract",
        }
    ]
    assert all(
        target["page_id"] != "resources/sources"
        for batch in manifest["batches"]
        for target in batch["targets"]
    )


def test_creates_resumable_review_batches_and_assembles_current_payload(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    standard, prompt = _write_standards(tmp_path)
    plan_dir = tmp_path / "plan"

    plan = create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        plan_dir,
        1,
    )

    assert plan["compiled_pages"] == 2
    assert plan["batches"] == 2
    assert plan_dir.stat().st_mode & 0o777 == 0o700
    assert (plan_dir / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert (plan_dir / "quality-001.input.json").stat().st_mode & 0o777 == 0o600
    manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["max_batch_chars"] == 24_000
    first_batch = json.loads((plan_dir / "quality-001.input.json").read_text(encoding="utf-8"))
    assert first_batch["response_contract"]["do_not_rewrite_markdown"] is True
    assert "standard_text" not in first_batch
    assert first_batch["prompt_text"] == "현재 검토 프롬프트\n"
    assert "## 질문" in first_batch["targets"][0]["markdown"]

    results = tmp_path / "results"
    _write_results(plan_dir, results)
    assembled = tmp_path / "assembled.json"
    report = assemble_content_quality_reviews(
        vault, plan_dir / "manifest.json", results, standard, "test-judge", "1", assembled
    )

    assert report["reviews"] == 2
    assert evaluate_content_quality(vault, assembled, standard, prompt)["passed"] is True


def test_refuses_to_assemble_a_plan_after_a_compiled_receipt_changes(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    standard, prompt = _write_standards(tmp_path)
    plan_dir = tmp_path / "plan"
    create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        plan_dir,
        2,
    )
    results = tmp_path / "results"
    _write_results(plan_dir, results)

    page_path = vault / "wiki" / "os/first.md"
    changed = _markdown("첫 문서").replace("먼저 확인한다.", "먼저 점검한다.")
    page_path.write_text(changed, encoding="utf-8")
    receipts_path = vault / "catalog" / "llm-wiki" / "receipts.yaml"
    receipts = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))
    receipts["receipts"][0]["output_sha256"] = hashlib.sha256(changed.encode()).hexdigest()
    receipts_path.write_text(
        yaml.safe_dump(receipts, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    with pytest.raises(WoonError, match="plan is stale"):
        assemble_content_quality_reviews(
            vault,
            plan_dir / "manifest.json",
            results,
            standard,
            "test-judge",
            "1",
            tmp_path / "assembled.json",
        )


def test_refuses_to_assemble_a_nonpassed_review_without_a_failed_rubric_or_hard_failure(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    standard, prompt = _write_standards(tmp_path)
    plan_dir = tmp_path / "plan"
    create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        plan_dir,
        2,
    )
    results = tmp_path / "results"
    _write_results(plan_dir, results)
    result_path = results / "quality-001.result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    result["reviews"][0]["verdict"] = "needs-revision"
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(WoonError, match="needs-revision quality review has no failed rubric"):
        assemble_content_quality_reviews(
            vault,
            plan_dir / "manifest.json",
            results,
            standard,
            "test-judge",
            "1",
            tmp_path / "assembled.json",
        )


def test_rebases_only_complete_batches_with_current_markdown_and_same_standards(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    standard, prompt = _write_standards(tmp_path)
    prior_plan = tmp_path / "prior-plan"
    create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        prior_plan,
        1,
    )
    prior_results = tmp_path / "prior-results"
    _write_results(prior_plan, prior_results)
    (prior_results / "run-manifest.json").write_text(
        json.dumps({"version": 1, "context_tokens": 32_768}), encoding="utf-8"
    )

    changed = _markdown("첫 문서").replace("먼저 확인한다.", "먼저 점검한다.")
    (vault / "wiki" / "os/first.md").write_text(changed, encoding="utf-8")
    receipts_path = vault / "catalog" / "llm-wiki" / "receipts.yaml"
    receipts = yaml.safe_load(receipts_path.read_text(encoding="utf-8"))
    receipts["receipts"][0]["output_sha256"] = hashlib.sha256(changed.encode()).hexdigest()
    receipts_path.write_text(
        yaml.safe_dump(receipts, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    rebased_plan = tmp_path / "rebased-plan"
    rebased_results = tmp_path / "rebased-results"
    report = rebase_content_quality_review_plan(
        vault,
        prior_plan / "manifest.json",
        prior_results,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        rebased_plan,
        rebased_results,
        1,
    )

    assert report["reused_batches"] == ["quality-002"]
    assert report["reused_pages"] == 1
    assert report["batches_to_review"] == ["quality-001"]
    assert (rebased_results / "quality-002.result.json").is_file()
    assert not (rebased_results / "quality-001.result.json").exists()
    marker = json.loads((rebased_results / ".inherited-results.json").read_text(encoding="utf-8"))
    assert marker["version"] == 1
    assert marker["result_files"] == [
        {
            "path": "quality-002.result.json",
            "sha256": hashlib.sha256(
                (rebased_results / "quality-002.result.json").read_bytes()
            ).hexdigest(),
        }
    ]
    assert report["inherited_results"] == str(rebased_results / ".inherited-results.json")


def test_rebase_does_not_reuse_a_failed_review(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    standard, prompt = _write_standards(tmp_path)
    prior_plan = tmp_path / "prior-plan"
    create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        prior_plan,
        1,
    )
    prior_results = tmp_path / "prior-results"
    _write_results(prior_plan, prior_results)
    failed_path = prior_results / "quality-001.result.json"
    failed = json.loads(failed_path.read_text(encoding="utf-8"))
    failed["reviews"][0]["verdict"] = "needs-revision"
    failed["reviews"][0]["rubric"]["natural_korean"] = "fail"
    failed_path.write_text(json.dumps(failed, ensure_ascii=False), encoding="utf-8")

    report = rebase_content_quality_review_plan(
        vault,
        prior_plan / "manifest.json",
        prior_results,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        tmp_path / "rebased-plan",
        tmp_path / "rebased-results",
        1,
    )

    assert "quality-001" in report["batches_to_review"]
    assert "quality-001" not in report["reused_batches"]


def test_rebase_rejects_all_prior_reviews_when_the_writing_standard_changed(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    standard, prompt = _write_standards(tmp_path)
    prior_plan = tmp_path / "prior-plan"
    create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        prior_plan,
        1,
    )
    prior_results = tmp_path / "prior-results"
    _write_results(prior_plan, prior_results)
    standard.write_text("바뀐 문체 표준\n", encoding="utf-8")

    report = rebase_content_quality_review_plan(
        vault,
        prior_plan / "manifest.json",
        prior_results,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        tmp_path / "rebased-plan",
        tmp_path / "rebased-results",
        1,
    )

    assert report["reused_pages"] == 0
    assert report["reuse_skipped_reason"] == "standard-or-prompt-changed"


def test_refuses_incomplete_batch_results(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _write_vault(vault)
    standard, prompt = _write_standards(tmp_path)
    plan_dir = tmp_path / "plan"
    create_content_quality_review_plan(
        vault,
        standard,
        "repo://skills/standards/learning-writing-harness.md",
        prompt,
        "repo://skills/standards/learning-quality-review-prompt.md",
        plan_dir,
        2,
    )
    manifest = json.loads((plan_dir / "manifest.json").read_text(encoding="utf-8"))
    batch = manifest["batches"][0]
    results = tmp_path / "results"
    results.mkdir()
    (results / batch["result_file"]).write_text(
        json.dumps({"version": 1, "batch_id": batch["batch_id"], "reviews": []}),
        encoding="utf-8",
    )

    with pytest.raises(WoonError, match="does not match plan"):
        assemble_content_quality_reviews(
            vault,
            plan_dir / "manifest.json",
            results,
            standard,
            "test-judge",
            "1",
            tmp_path / "assembled.json",
        )
