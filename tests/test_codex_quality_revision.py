from __future__ import annotations

import hashlib

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge import codex_quality_revision as revision
from woon_core.knowledge.codex_quality_revision import (
    RevisionCandidate,
    _add_learning_scaffold,
    _collect_proposal_records,
    _compiled_body,
    _expand_evidence_scope,
    _is_generic_h2,
    _mask_protected_material,
    _proposal_file_name,
    _propose_revision,
    _restore_protected_material,
    _revision_prompt,
    _validate_proposal,
    apply_codex_quality_revisions,
    create_codex_quality_revision_proposals,
)

BODY = """## 시작

`reader`가 보는 값이 바뀌는 이유를 먼저 확인한다.
[[관련 문서]]와 [설명](https://example.com)를 함께 본다.

```python
value = 1
```

값을 한 곳에서 바꾸면 다른 곳에도 영향을 줄 수 있다.
"""


def _candidate() -> RevisionCandidate:
    return RevisionCandidate(
        page_id="os/example",
        output_sha256="a" * 64,
        source_body_sha256=hashlib.sha256(BODY.encode("utf-8")).hexdigest(),
        title="예시",
        purpose="값이 바뀌는 흐름을 다시 설명할 때 사용한다.",
        body=BODY,
        failures=("natural_korean",),
        failure_reasons=("문장 연결을 보완해야 한다.",),
    )


def test_revision_accepts_changed_prose_that_preserves_protected_material() -> None:
    proposal = {
        "body": """## 시작

`reader`가 값을 읽을 때는 한 곳의 변경이 어디까지 이어지는지 먼저 살펴봐야 한다.
[[관련 문서]]와 [설명](https://example.com)를 함께 보면 흐름을 따라가기 쉽다.

```python
value = 1
```

그래서 값을 한 곳에서 바꾼 뒤에는 다른 곳에 예상하지 못한 영향이 남는지도 확인한다.
""",
        "statement": "한 곳의 값 변경이 다른 곳에 미치는 영향을 설명한다.",
        "current_use": "값 변경이 어디까지 이어지는지 다시 확인하고 설명할 때 사용한다.",
    }

    _validate_proposal(proposal, _candidate())


def test_revision_prompt_requires_document_specific_headings() -> None:
    prompt = _revision_prompt(_candidate(), None)

    assert "`정리`, `이어서 읽기`" in prompt
    assert "바로 아래 문단과 코드가 답하는 고유한 장면" in prompt
    assert "짧은 명사형 키워드" in prompt
    assert "한 줄 요약" in prompt


def test_revision_rejects_a_generic_leading_summary_block() -> None:
    proposal = {
        "body": "> 한 줄 요약: 값이 바뀌는 흐름을 설명한다.\n\n" + BODY,
        "statement": "값 변경이 미치는 영향을 설명한다.",
        "current_use": "값 변경이 어디까지 이어지는지 다시 확인하고 설명할 때 사용한다.",
    }

    with pytest.raises(WoonError, match="generic leading summary"):
        _validate_proposal(proposal, _candidate())


def test_revision_prompt_hides_a_generic_summary_from_the_writer() -> None:
    body = "> 한 줄 요약: 값이 바뀌는 흐름을 설명한다.\n\n" + BODY
    candidate = RevisionCandidate(
        page_id="os/example",
        output_sha256="a" * 64,
        source_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        title="예시",
        purpose="값이 바뀌는 흐름을 다시 설명할 때 사용한다.",
        body=body,
        failures=("natural_korean",),
        failure_reasons=("문장 연결을 보완해야 한다.",),
    )

    prompt = _revision_prompt(candidate, None)

    assert "한 줄 요약: 값이 바뀌는 흐름을 설명한다." not in prompt
    assert "`reader`가 보는 값" in prompt


def test_revision_strips_a_model_created_generic_summary_before_validation() -> None:
    proposal = revision._proposal_without_generic_summary(
        {
            "body": "> 한 줄 요약: 값이 바뀌는 흐름을 설명한다.\n\n" + BODY,
            "statement": "값 변경이 미치는 영향을 설명한다.",
            "current_use": "값 변경이 어디까지 이어지는지 다시 확인하고 설명할 때 사용한다.",
        }
    )

    _validate_proposal(proposal, _candidate())
    assert proposal["body"] == BODY


def test_revision_keeps_protected_material_when_removing_a_generic_summary_label() -> None:
    body = "> 한 줄 요약: `global_step`은 갱신 횟수를 센다.\n\n" + BODY

    normalized = revision._body_without_generic_summary(body)

    assert not normalized.startswith("> 한 줄 요약:")
    assert normalized.startswith("`global_step`은 갱신 횟수를 센다.")
    assert "`reader`" in normalized


def test_revision_rejects_narrative_h2_labels() -> None:
    assert _is_generic_h2("PintOS 코드에서 먼저 추적할 주소", "C 포인터와 배열")
    assert _is_generic_h2("멤버 주소를 역산하는 방식", "C 포인터와 배열")
    assert not _is_generic_h2("구조체와 인자 배열", "C 포인터와 배열")


def test_revision_rejects_a_prose_only_fix_for_a_failed_heading() -> None:
    body = """## 흐름

값이 어디에서 만들어지고 어느 순서로 함수에 전달되는지 따라가며 다음 동작을 판단한다. 이 장면을
직접 읽으면 이름과 값이 어떤 순서로 이어지는지 구분할 수 있다.
"""
    candidate = RevisionCandidate(
        page_id="concepts/example",
        output_sha256="a" * 64,
        source_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        title="예시",
        purpose="값의 전달 순서를 다시 설명할 때 사용한다.",
        body=body,
        failures=("revisitability",),
        failure_reasons=("인용한 부분 “흐름”를 확인했지만 제목이 너무 일반적이다.",),
    )
    proposal = {
        "body": body.replace("전달되는지", "전달되는지 차분히"),
        "statement": "값이 전달되는 순서를 설명한다.",
        "current_use": candidate.purpose,
    }

    with pytest.raises(WoonError, match="failed revisitability heading"):
        _validate_proposal(proposal, candidate)


def test_heading_repair_changes_only_the_failed_heading(monkeypatch) -> None:
    body = """## 흐름

    `batch_size`와 `global_step`이 무엇을 세는지 구분한다. 배치 하나를 처리한 뒤 갱신한 횟수와
    전체 데이터를 몇 번 읽었는지를 같은 숫자로 받아들이지 않도록 두 단위를 나누어 본다.
"""
    candidate = RevisionCandidate(
        page_id="ai/example",
        output_sha256="a" * 64,
        source_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        title="예시",
        purpose="학습 횟수의 단위를 다시 구분할 때 사용한다.",
        body=body,
        failures=("revisitability",),
        failure_reasons=("인용한 부분 “흐름”를 확인했지만 제목이 너무 일반적이다.",),
    )
    responses = iter(
        (
            {
                "heading": "흐름",
                "statement": "배치와 갱신 횟수의 차이를 설명한다.",
                "current_use": candidate.purpose,
            },
            {
                "heading": "배치와 갱신 횟수",
                "statement": "배치와 갱신 횟수의 차이를 설명한다.",
                "current_use": candidate.purpose,
            },
        )
    )
    monkeypatch.setattr(revision, "_run_codex", lambda *_: next(responses))

    proposal = revision._propose_with_heading_repair(
        candidate, "codex", "subscription-default", 60, WoonError("heading failed")
    )

    assert proposal["body"].startswith("## 배치와 갱신 횟수")
    assert "`batch_size`" in proposal["body"]


def test_section_heading_fallback_promotes_a_specific_child_heading() -> None:
    body = """## 값 이동 코드

이 문단은 포인터를 이동한다.

### 앞 노드와 현재 노드

두 포인터가 가리키는 위치를 나누고, 새 노드를 어느 연결 사이에 넣을지 판단한다. 이 구분을
먼저 해 두면 맨 앞과 중간, 마지막 삽입도 같은 기준으로 설명할 수 있다.
"""
    candidate = RevisionCandidate(
        page_id="algorithm/example",
        output_sha256="a" * 64,
        source_body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        title="값 이동",
        purpose="포인터 이동을 다시 설명할 때 사용한다.",
        body=body,
        failures=("revisitability",),
        failure_reasons=("인용한 부분 “값 이동 코드”를 확인했지만 제목이 너무 일반적이다.",),
    )

    proposal = revision._propose_with_section_heading(candidate)

    assert proposal["body"].startswith("## 앞 노드와 현재 노드")


def test_revision_runtime_artifacts_are_private(tmp_path, monkeypatch) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text("{}\n", encoding="utf-8")
    output = tmp_path / "proposals"
    output.mkdir(mode=0o755)
    proposal = {
        "body": BODY.replace("값을 한 곳에서", "값을 한 지점에서"),
        "statement": "값 변경이 미치는 영향을 설명한다.",
        "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
    }

    def candidates(*_args, page_ids=()):
        assert page_ids == ("os/example",)
        return ((_candidate(),), "a" * 64)

    monkeypatch.setattr(revision, "_revision_candidates", candidates)
    monkeypatch.setattr(revision, "_codex_binary", lambda _: "/usr/bin/codex")
    monkeypatch.setattr(revision, "_require_chatgpt_login", lambda _: None)
    monkeypatch.setattr(revision, "_propose_revision", lambda *_: proposal)

    create_codex_quality_revision_proposals(
        tmp_path,
        plan,
        tmp_path / "reviews",
        output,
        model="gpt-5.6-sol",
        max_attempts=2,
        page_ids=("os/example",),
    )

    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / "run-manifest.json").stat().st_mode & 0o777 == 0o600
    assert (output / _proposal_file_name("os/example")).stat().st_mode & 0o777 == 0o600


def test_apply_can_select_an_adjudicated_subset(tmp_path, monkeypatch) -> None:
    first = _candidate()
    second = RevisionCandidate(
        page_id="os/second",
        output_sha256="b" * 64,
        source_body_sha256=first.source_body_sha256,
        title="두 번째",
        purpose=first.purpose,
        body=first.body,
        failures=first.failures,
        failure_reasons=first.failure_reasons,
    )

    def candidates(*_args, page_ids=()):
        assert page_ids == ("os/example",)
        return ((first, second), "c" * 64)

    monkeypatch.setattr(revision, "_revision_candidates", candidates)

    def records(candidates, *_args, allow_unselected=False, **_kwargs):
        assert allow_unselected is True
        return (
            {
                candidates[0].page_id: {
                    "body": BODY,
                    "statement": "값 변경의 영향을 설명한다.",
                    "current_use": first.purpose,
                }
            },
            {candidates[0].page_id: "proposal.json"},
        )

    monkeypatch.setattr(revision, "_collect_proposal_records", records)

    class FakeReport:
        curated = 1
        compiled = 1
        unchanged = 0
        page_ids = ("os/example",)

    class FakeService:
        def curate_compiled_wiki_revisions(self, records):
            assert [record.page_id for record in records] == ["os/example"]
            return FakeReport()

    monkeypatch.setattr(revision, "build_knowledge_service", lambda _: (None, FakeService()))

    report = apply_codex_quality_revisions(
        tmp_path,
        tmp_path / "plan.json",
        tmp_path / "reviews",
        (tmp_path / "proposals",),
        page_ids=("os/example",),
    )

    assert report["page_ids"] == ["os/example"]


def test_revision_rejects_changed_code_fence() -> None:
    proposal = {
        "body": BODY.replace("value = 1", "value = 2"),
        "statement": "한 곳의 값 변경이 다른 곳에 미치는 영향을 설명한다.",
        "current_use": "값 변경이 어디까지 이어지는지 다시 확인하고 설명할 때 사용한다.",
    }

    with pytest.raises(WoonError, match="protected fenced block"):
        _validate_proposal(proposal, _candidate())


def test_compiled_body_excludes_compiler_frontmatter_and_h1() -> None:
    markdown = "---\ntitle: 예시\n---\n\n# 예시\n\n" + BODY

    assert _compiled_body(markdown) == BODY


def test_evidence_scope_adds_a_plain_boundary_without_rewriting_source() -> None:
    candidate = _candidate()
    evidence_only = RevisionCandidate(
        page_id=candidate.page_id,
        output_sha256=candidate.output_sha256,
        source_body_sha256=candidate.source_body_sha256,
        title=candidate.title,
        purpose=candidate.purpose,
        body=candidate.body,
        failures=("evidence_boundary",),
        failure_reasons=("사실과 적용 범위의 경계를 보완해야 한다.",),
    )

    proposal = _expand_evidence_scope(
        {
            "scope": (
                "이 문서는 기본 원리를 설명하며, 실제 동작은 해당 코드와 실행 기록에서 다시 "
                "확인해야 한다."
            )
        },
        evidence_only,
    )

    _validate_proposal(proposal, evidence_only)
    assert proposal["body"].startswith("> 확인 범위: 이 문서는 기본 원리를 설명하며")


def test_collect_proposals_rejects_duplicate_page_across_retry_runs(tmp_path, monkeypatch) -> None:
    candidate = _candidate()
    record = {
        "version": revision.REVISION_VERSION,
        "page_id": candidate.page_id,
        "output_sha256": candidate.output_sha256,
        "source_body_sha256": candidate.source_body_sha256,
        "body": BODY.replace("값을 한 곳에서", "값을 한 지점에서"),
        "statement": "값 변경이 미치는 영향을 설명한다.",
        "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
    }
    monkeypatch.setattr(revision, "_validate_manifest", lambda *_: None)
    paths = []
    for name in ("first", "retry"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "run-manifest.json").write_text("{}", encoding="utf-8")
        (directory / _proposal_file_name(candidate.page_id)).write_bytes(
            revision.encode_json(record)
        )
        paths.append(directory)

    # The loader must reject duplicate page IDs before it can promote either run.
    with pytest.raises(WoonError, match="proposal is duplicated"):
        _collect_proposal_records((candidate,), tuple(paths), tmp_path / "plan.json", "reviews")


def test_collect_proposals_keeps_first_valid_duplicate_when_requested(
    tmp_path, monkeypatch
) -> None:
    candidate = _candidate()
    first = {
        "version": revision.REVISION_VERSION,
        "page_id": candidate.page_id,
        "output_sha256": candidate.output_sha256,
        "source_body_sha256": candidate.source_body_sha256,
        "body": BODY.replace("값을 한 곳에서", "값을 한 지점에서"),
        "statement": "값 변경이 미치는 영향을 설명한다.",
        "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
    }
    second = {**first, "body": BODY.replace("값을 한 곳에서", "값을 한 위치에서")}
    monkeypatch.setattr(revision, "_validate_manifest", lambda *_: None)
    paths = []
    for name, record in (("first", first), ("retry", second)):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "run-manifest.json").write_text("{}", encoding="utf-8")
        (directory / _proposal_file_name(candidate.page_id)).write_bytes(
            revision.encode_json(record)
        )
        paths.append(directory)

    records, sources = _collect_proposal_records(
        (candidate,), tuple(paths), tmp_path / "plan.json", "reviews", "first-valid"
    )

    assert records[candidate.page_id]["body"] == first["body"]
    assert sources[candidate.page_id].endswith("first/" + _proposal_file_name(candidate.page_id))


def test_protected_template_restores_original_tokens_in_order() -> None:
    template, replacements = _mask_protected_material(BODY)

    assert "@@WOON_KEEP_001@@" in template
    assert _restore_protected_material(template, replacements) == BODY
    with pytest.raises(WoonError, match="protected material order or count"):
        _restore_protected_material(template.replace("@@WOON_KEEP_001@@", ""), replacements)


def test_learning_scaffold_preserves_entire_original_body() -> None:
    revised = _add_learning_scaffold(
        BODY,
        "이 글은 값이 바뀌는 흐름을 따라가며 어디까지 영향이 이어지는지 살펴본다.",
        "값을 바꾼 뒤에는 다른 곳에 남는 영향도 함께 확인하면 된다.",
    )

    assert BODY.rstrip() in revised
    _validate_proposal(
        {
            "body": revised,
            "statement": "값 변경이 미치는 영향을 설명한다.",
            "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
        },
        _candidate(),
    )


def test_revision_reaches_learning_scaffold_after_protected_material_failures(monkeypatch) -> None:
    candidate = _candidate()
    responses = iter(
        (
            {
                "body": BODY.replace("`reader`", "reader"),
                "statement": "값 변경이 미치는 영향을 설명한다.",
                "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
            },
            {
                "body": "보호 표식을 잃은 수정본입니다. " * 5,
                "statement": "값 변경이 미치는 영향을 설명한다.",
                "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
            },
            {
                "opening": (
                    "이 글은 값이 바뀌는 흐름을 따라가며 어디까지 영향이 이어지는지 살펴본다."
                ),
                "revisit": "값을 바꾼 뒤에는 다른 곳에 남는 영향도 함께 확인하면 된다.",
                "statement": "값 변경이 미치는 영향을 설명한다.",
                "current_use": "값 변경의 영향을 다시 설명할 때 사용한다.",
            },
        )
    )
    monkeypatch.setattr(revision, "_run_codex", lambda *_: next(responses))

    proposal = _propose_revision(candidate, "codex", "subscription-default", 60, 1)

    _validate_proposal(proposal, candidate)
    assert BODY.rstrip() in proposal["body"]
