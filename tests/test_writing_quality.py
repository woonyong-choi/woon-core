from __future__ import annotations

import hashlib
import json
from io import StringIO
from pathlib import Path

import pytest

from woon_core.cli import run
from woon_core.errors import WoonError
from woon_core.writing_quality import (
    adopt_writing_rule,
    evaluate_writing_review,
    evaluate_writing_rule_candidate,
    register_writing_rule_application,
    rollback_writing_rule,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _review_files(tmp_path: Path, profile: str = "technical-learning") -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    document = tmp_path / "document.md"
    source = tmp_path / "source.md"
    standard = tmp_path / "standard.md"
    document.write_text(
        "# 설명\n\n입력이 없으면 결과는 0이다.\n\n기존 예시는 그대로 둔다.\n",
        encoding="utf-8",
    )
    source.write_text("원문은 입력이 없을 때 결과가 0이라고 설명한다.\n", encoding="utf-8")
    standard.write_text("근거와 표현을 분리해 검토한다.\n", encoding="utf-8")
    references = []
    claims = []
    if profile != "fiction":
        references = [
            {
                "id": "source-1",
                "role": "fact",
                "locator": "source://example",
                "revision": "r1",
                "path": str(source),
                "sha256": _sha256(source),
            }
        ]
        claims = [
            {
                "id": "claim-1",
                "kind": "fact",
                "core": True,
                "body_anchor": "입력이 없으면 결과는 0이다.",
                "evidence": [
                    {
                        "reference_id": "source-1",
                        "anchor": "입력이 없을 때 결과가 0",
                        "relation": "condition-result",
                    }
                ],
            }
        ]
    request = {
        "version": 2,
        "profile": profile,
        "purpose": "짧은 설명을 검증한다.",
        "audience": "Kotlin 학습자",
        "visibility": "local-only",
        "edit_scope": "본문 한 문단",
        "revision_attempt": 0,
        "preserved_elements": ["코드와 예시"],
        "completion_conditions": ["별도 검토 통과"],
        "document": {
            "id": "document-1",
            "path": str(document),
            "sha256": _sha256(document),
            "revision": "document-r1",
        },
        "standard": {
            "path": str(standard),
            "sha256": _sha256(standard),
            "revision": "standard-r1",
        },
        "references": references,
        "claims": claims,
        "writer": {
            "provider": "openai",
            "model": "writer-model",
            "tool": "visible-task",
            "run_id": "writer-run",
        },
    }
    request_path = tmp_path / "request.json"
    _write_json(request_path, request)
    criteria = {
        "translation": {
            "semantic-fidelity",
            "preservation",
            "style",
            "source-fidelity",
            "terminology",
            "evidence-boundary",
        },
        "technical-learning": {
            "semantic-fidelity",
            "preservation",
            "style",
            "factual-accuracy",
            "relation-accuracy",
            "evidence-boundary",
        },
        "research-report": {
            "semantic-fidelity",
            "preservation",
            "style",
            "factual-accuracy",
            "relation-accuracy",
            "evidence-boundary",
        },
        "general-guidance": {
            "semantic-fidelity",
            "preservation",
            "style",
            "factual-accuracy",
            "relation-accuracy",
            "evidence-boundary",
        },
        "personal-record": {
            "semantic-fidelity",
            "preservation",
            "style",
            "privacy-boundary",
            "evidence-boundary",
        },
        "fiction": {
            "semantic-fidelity",
            "preservation",
            "style",
            "internal-consistency",
        },
    }[profile]
    criterion_reviews = []
    for criterion in sorted(criteria):
        evidence = []
        if criterion in {
            "source-fidelity",
            "factual-accuracy",
            "relation-accuracy",
            "evidence-boundary",
        }:
            evidence = [
                {
                    "reference_id": "source-1",
                    "anchor": "입력이 없을 때 결과가 0",
                    "relation": "condition-result",
                }
            ]
        criterion_reviews.append(
            {
                "id": criterion,
                "status": "pass",
                "document_anchor": "입력이 없으면 결과는 0이다.",
                "reason": f"{criterion} 근거를 확인했다.",
                "evidence": evidence,
            }
        )
    claim_reviews = []
    if claims:
        claim_reviews = [
            {
                "id": "claim-1",
                "status": "pass",
                "document_anchor": "입력이 없으면 결과는 0이다.",
                "reason": "원문의 조건과 결과가 일치한다.",
                "evidence": [
                    {
                        "reference_id": "source-1",
                        "anchor": "입력이 없을 때 결과가 0",
                        "relation": "condition-result",
                    }
                ],
            }
        ]
    review = {
        "version": 2,
        "document_id": "document-1",
        "request_sha256": _sha256(request_path),
        "document_sha256": _sha256(document),
        "final_document_sha256": _sha256(document),
        "standard_sha256": _sha256(standard),
        "revision_attempt": 0,
        "reviewer": {
            "provider": "openai",
            "model": "reviewer-model",
            "tool": "visible-task",
            "run_id": "reviewer-run",
        },
        "verdict": "passed",
        "criterion_reviews": criterion_reviews,
        "claim_reviews": claim_reviews,
        "hard_failures": [],
    }
    review_path = tmp_path / "review.json"
    _write_json(review_path, review)
    return request_path, review_path


@pytest.mark.parametrize(
    "profile",
    [
        "translation",
        "technical-learning",
        "research-report",
        "general-guidance",
        "personal-record",
        "fiction",
    ],
)
def test_review_applies_profile_specific_checks_without_penalizing_short_text(
    tmp_path: Path, profile: str
) -> None:
    request, review = _review_files(tmp_path, profile)

    result = evaluate_writing_review(request, review)

    assert result["passed"] is True
    assert result["profile"] == profile


@pytest.mark.parametrize("failure", ["missing-evidence", "wrong-verdict", "revision-limit"])
def test_review_never_turns_an_unresolved_required_check_into_pass(
    tmp_path: Path, failure: str
) -> None:
    request, review = _review_files(tmp_path)
    payload = json.loads(review.read_text(encoding="utf-8"))
    if failure == "missing-evidence":
        factual = next(
            item for item in payload["criterion_reviews"] if item["id"] == "factual-accuracy"
        )
        factual["evidence"] = []
    elif failure == "wrong-verdict":
        factual = next(
            item for item in payload["criterion_reviews"] if item["id"] == "factual-accuracy"
        )
        factual["status"] = "fail"
    else:
        payload["revision_attempt"] = 3
    _write_json(review, payload)

    result = evaluate_writing_review(request, review)

    assert result["passed"] is False
    assert result["state"] == "incomplete"


@pytest.mark.parametrize("stale", ["document", "source", "standard"])
def test_review_rejects_changed_inputs_and_fabricated_anchors(tmp_path: Path, stale: str) -> None:
    request, review = _review_files(tmp_path)
    request_payload = json.loads(request.read_text(encoding="utf-8"))
    path = Path(
        request_payload["document"]["path"]
        if stale == "document"
        else request_payload["standard"]["path"]
        if stale == "standard"
        else request_payload["references"][0]["path"]
    )
    path.write_text(path.read_text(encoding="utf-8") + "변경\n", encoding="utf-8")

    with pytest.raises(WoonError, match="stale"):
        evaluate_writing_review(request, review)


def test_review_v1_is_readable_but_never_upgraded(tmp_path: Path) -> None:
    request, review = _review_files(tmp_path)
    _write_json(review, {"version": 1, "verdict": "passed"})

    result = evaluate_writing_review(request, review)

    assert result == {
        "version": 2,
        "passed": False,
        "state": "incomplete",
        "legacy_review": True,
        "errors": ["version 1 review is readable but cannot satisfy version 2 semantics"],
    }


def test_claim_not_applicable_and_core_unknown_cannot_bypass_review(tmp_path: Path) -> None:
    request, review = _review_files(tmp_path)
    payload = json.loads(review.read_text(encoding="utf-8"))
    payload["claim_reviews"][0].update(
        {
            "status": "not-applicable",
            "not_applicable_reason": "생략",
            "evidence": [],
        }
    )
    relation = next(
        item for item in payload["criterion_reviews"] if item["id"] == "relation-accuracy"
    )
    relation["status"] = "unknown"
    payload["verdict"] = "incomplete"
    _write_json(review, payload)

    result = evaluate_writing_review(request, review)

    assert result["passed"] is False
    assert "claim cannot be not-applicable: claim-1" in result["errors"]
    assert "criterion:relation-accuracy" in result["unknowns"]


def test_review_rejects_a_fabricated_source_anchor(tmp_path: Path) -> None:
    request, review = _review_files(tmp_path)
    payload = json.loads(review.read_text(encoding="utf-8"))
    payload["claim_reviews"][0]["evidence"][0]["anchor"] = "원문에 없는 문장"
    _write_json(review, payload)

    with pytest.raises(WoonError, match="anchor is absent"):
        evaluate_writing_review(request, review)


@pytest.mark.parametrize(("core", "passed"), [(True, False), (False, True)])
def test_review_distinguishes_core_and_qualified_claim_unknowns(
    tmp_path: Path, core: bool, passed: bool
) -> None:
    request, review = _review_files(tmp_path)
    request_payload = json.loads(request.read_text(encoding="utf-8"))
    request_payload["claims"][0]["core"] = core
    _write_json(request, request_payload)
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    review_payload["request_sha256"] = _sha256(request)
    review_payload["claim_reviews"][0]["status"] = "unknown"
    review_payload["verdict"] = "incomplete" if core else "qualified"
    _write_json(review, review_payload)

    result = evaluate_writing_review(request, review)

    assert result["passed"] is passed
    assert result["unknowns"] == ["claim:claim-1"]
    assert result["state"] == ("incomplete" if core else "qualified")
    assert result["qualified"] is (not core)


@pytest.mark.parametrize(
    "invalid", ["version", "attempt", "document", "verdict", "request-binding"]
)
def test_revision_requires_the_immediately_previous_review(tmp_path: Path, invalid: str) -> None:
    request, review = _review_files(tmp_path)
    prior_request = tmp_path / "prior-request.json"
    prior_request.write_bytes(request.read_bytes())
    prior = tmp_path / "prior-review.json"
    prior_payload = json.loads(review.read_text(encoding="utf-8"))
    prior_payload["verdict"] = "incomplete"
    if invalid == "version":
        prior_payload["version"] = 1
    elif invalid == "attempt":
        prior_payload["revision_attempt"] = 1
    elif invalid == "document":
        prior_payload["document_id"] = "other-document"
    elif invalid == "verdict":
        prior_payload["verdict"] = "passed"
    elif invalid == "request-binding":
        prior_payload["request_sha256"] = "0" * 64
    _write_json(prior, prior_payload)
    request_payload = json.loads(request.read_text(encoding="utf-8"))
    request_payload["revision_attempt"] = 1
    request_payload["prior_request"] = {
        "path": str(prior_request),
        "sha256": _sha256(prior_request),
    }
    request_payload["prior_review"] = {"path": str(prior), "sha256": _sha256(prior)}
    _write_json(request, request_payload)

    with pytest.raises(WoonError, match="prior non-passing review"):
        evaluate_writing_review(request, review)


def test_revision_accepts_a_bound_previous_request_and_review(tmp_path: Path) -> None:
    request, review = _review_files(tmp_path)
    prior_request = tmp_path / "prior-request.json"
    prior_request.write_bytes(request.read_bytes())
    prior = tmp_path / "prior-review.json"
    prior_payload = json.loads(review.read_text(encoding="utf-8"))
    prior_payload["verdict"] = "incomplete"
    _write_json(prior, prior_payload)
    request_payload = json.loads(request.read_text(encoding="utf-8"))
    request_payload["revision_attempt"] = 1
    request_payload["prior_request"] = {
        "path": str(prior_request),
        "sha256": _sha256(prior_request),
    }
    request_payload["prior_review"] = {"path": str(prior), "sha256": _sha256(prior)}
    _write_json(request, request_payload)
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    review_payload["request_sha256"] = _sha256(request)
    review_payload["revision_attempt"] = 1
    _write_json(review, review_payload)

    assert evaluate_writing_review(request, review)["passed"] is True


def test_review_hashes_exact_crlf_bytes(tmp_path: Path) -> None:
    request, review = _review_files(tmp_path)
    payload = json.loads(request.read_text(encoding="utf-8"))
    document = Path(payload["document"]["path"])
    document.write_bytes(document.read_bytes().replace(b"\n", b"\r\n"))
    payload["document"]["sha256"] = _sha256(document)
    _write_json(request, payload)
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    review_payload["request_sha256"] = _sha256(request)
    review_payload["document_sha256"] = _sha256(document)
    review_payload["final_document_sha256"] = _sha256(document)
    _write_json(review, review_payload)

    assert evaluate_writing_review(request, review)["passed"] is True


def _rule_candidate(tmp_path: Path, case_count: int = 20) -> Path:
    cases = []
    for index in range(case_count):
        artifacts = {}
        artifact_bytes = {
            "input": f"case-{index}",
            "baseline": "baseline sentence",
            "candidate": "candidate sentence",
            "source": f"source sentence {index}",
        }
        for name, content in artifact_bytes.items():
            path = tmp_path / f"case-{index}-{name}.txt"
            path.write_text(content, encoding="utf-8")
            artifacts[name] = {"path": str(path), "sha256": _sha256(path)}
        outcome = "win" if index < 12 else "tie"
        controls = {
            "meaning": "pass",
            "preservation": "pass",
            "evidence": "pass",
            "scope": "pass",
            "profile:technical-learning": "pass",
        }
        evidence = {
            "document_anchor": "candidate sentence",
            "evidence_anchor": f"source sentence {index}",
            "relation": "concept-example",
        }
        review_path = tmp_path / f"case-{index}-review.json"
        _write_json(
            review_path,
            {
                "version": 1,
                "case_id": f"case-{index}",
                "profile": "technical-learning",
                "source_id": f"source-{index % 3}",
                "task_id": f"task-{index % 3}",
                "input_sha256": artifacts["input"]["sha256"],
                "baseline_sha256": artifacts["baseline"]["sha256"],
                "candidate_sha256": artifacts["candidate"]["sha256"],
                "source_sha256": artifacts["source"]["sha256"],
                "outcome": outcome,
                "required_controls": controls,
                "hard_failure": False,
                "core_unknown": False,
                "unresolved_disagreement": False,
                "preference_conflict": False,
                "evidence": evidence,
                "reason": "후보가 의미와 근거를 보존했다.",
                "reviewer": {
                    "id": f"reviewer-{index % 3}",
                    "kind": "human",
                    "run_id": f"review-run-{index}",
                },
            },
        )
        artifacts["review"] = {"path": str(review_path), "sha256": _sha256(review_path)}
        input_hash = artifacts["input"]["sha256"]
        run = {
            "provider": "openai",
            "model": "same-model",
            "tool": "visible-task",
            "input_sha256": input_hash,
        }
        cases.append(
            {
                "id": f"case-{index}",
                "source_id": f"source-{index % 3}",
                "task_id": f"task-{index % 3}",
                "source_unit_id": hashlib.sha256(
                    artifacts["source"]["sha256"].encode()
                    + b"\0"
                    + f"source sentence {index}".encode()
                ).hexdigest(),
                "profile": "technical-learning",
                "input_sha256": input_hash,
                "held_out": True,
                "used_for_rule": False,
                "outcome": outcome,
                "baseline": {
                    **run,
                    "run_id": f"baseline-run-{index}",
                    "output_sha256": artifacts["baseline"]["sha256"],
                },
                "candidate": {
                    **run,
                    "run_id": f"candidate-run-{index}",
                    "output_sha256": artifacts["candidate"]["sha256"],
                },
                "artifacts": artifacts,
                "required_controls": controls,
                "hard_failure": False,
                "core_unknown": False,
                "unresolved_disagreement": False,
                "preference_conflict": False,
                "provenance": {
                    "kind": "real",
                    "label_source": "human",
                    "reviewer_id": f"reviewer-{index % 3}",
                    "revision": "r1",
                    "review_sha256": artifacts["review"]["sha256"],
                },
                "evidence": evidence,
            }
        )
    candidate = {
        "version": 1,
        "synthetic": False,
        "rule": {
            "id": "rule-1",
            "version": "2",
            "instruction": "조건과 결과를 같은 순서로 쓴다.",
            "source_observations": ["세 출처에서 조건과 결과 순서를 확인했다."],
            "profiles": ["technical-learning"],
            "applicability": "조건문 설명",
            "exclusions": ["창작 문장"],
            "expected_effect": "조건과 결과 관계를 보존한다.",
            "counterexamples": ["순서를 바꾸면 의미가 달라지는 사례"],
            "status": "trial",
            "evaluation_revision": "eval-r2",
            "invariant_effect": "preserve-only",
        },
        "cases": cases,
        "affected_documents": ["doc-1", "doc-2"],
    }
    candidate["experiment"] = {
        "rule_frozen_sha256": hashlib.sha256(
            (
                json.dumps(candidate["rule"], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            ).encode()
        ).hexdigest(),
        "derivation_case_ids": ["derivation-1"],
        "evaluation_case_ids": [case["id"] for case in cases],
        "review_sha256": hashlib.sha256(
            "\n".join(sorted(case["provenance"]["review_sha256"] for case in cases)).encode()
        ).hexdigest(),
    }
    path = tmp_path / "candidate.json"
    _write_json(path, candidate)
    return path


def _update_case_review(case: dict[str, object], **changes: object) -> None:
    artifacts = case["artifacts"]
    assert isinstance(artifacts, dict)
    review_artifact = artifacts["review"]
    assert isinstance(review_artifact, dict)
    path = Path(str(review_artifact["path"]))
    review = json.loads(path.read_text(encoding="utf-8"))
    review.update(changes)
    _write_json(path, review)
    review_artifact["sha256"] = _sha256(path)
    provenance = case["provenance"]
    assert isinstance(provenance, dict)
    provenance["review_sha256"] = review_artifact["sha256"]


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("nineteen", "20 unique"),
        ("two-sources", "3 sources or tasks"),
        ("eleven-wins", "12 wins"),
        ("loss", "loss case"),
        ("unknown", "unknown case"),
        ("leakage", "held-out leakage"),
        ("duplicate", "duplicate case"),
        ("duplicate-source-unit", "duplicate or unbound source unit"),
        ("mismatched-model", "conditions differ"),
        ("same-generation-run", "runs are not separate"),
        ("shared-review-run", "writer and reviewer runs are not separate"),
        ("missing-control", "required control did not pass"),
        ("forged-outcome", "differs from review artifact"),
        ("forged-source-id", "review artifact bindings differ"),
        ("synthetic", "synthetic cases"),
    ],
)
def test_rule_gate_holds_every_failed_adoption_control(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    candidate = _rule_candidate(tmp_path, 19 if mutation == "nineteen" else 20)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    cases = payload["cases"]
    if mutation == "two-sources":
        for case in cases:
            suffix = int(case["id"].split("-")[1]) % 2
            case["source_id"] = f"source-{suffix}"
            case["task_id"] = f"task-{suffix}"
            _update_case_review(case, source_id=case["source_id"], task_id=case["task_id"])
    elif mutation == "eleven-wins":
        cases[11]["outcome"] = "tie"
        _update_case_review(cases[11], outcome="tie")
    elif mutation in {"loss", "unknown"}:
        cases[0]["outcome"] = mutation
        _update_case_review(cases[0], outcome=mutation)
    elif mutation == "leakage":
        cases[0]["used_for_rule"] = True
    elif mutation == "duplicate":
        cases[1]["input_sha256"] = cases[0]["input_sha256"]
        cases[1]["baseline"]["input_sha256"] = cases[0]["input_sha256"]
        cases[1]["candidate"]["input_sha256"] = cases[0]["input_sha256"]
        cases[1]["artifacts"]["input"] = cases[0]["artifacts"]["input"]
    elif mutation == "duplicate-source-unit":
        cases[1]["artifacts"]["source"] = cases[0]["artifacts"]["source"]
        cases[1]["evidence"]["evidence_anchor"] = cases[0]["evidence"]["evidence_anchor"]
        cases[1]["source_unit_id"] = cases[0]["source_unit_id"]
    elif mutation == "mismatched-model":
        cases[0]["candidate"]["model"] = "other-model"
    elif mutation == "same-generation-run":
        cases[0]["candidate"]["run_id"] = cases[0]["baseline"]["run_id"]
    elif mutation == "shared-review-run":
        _update_case_review(
            cases[0],
            reviewer={
                "id": "reviewer-0",
                "kind": "human",
                "run_id": cases[0]["baseline"]["run_id"],
            },
        )
    elif mutation == "missing-control":
        cases[0]["required_controls"].pop("evidence")
        _update_case_review(cases[0], required_controls=cases[0]["required_controls"])
    elif mutation == "forged-outcome":
        cases[0]["outcome"] = "tie"
    elif mutation == "forged-source-id":
        cases[0]["source_id"] = "forged-source"
    elif mutation == "synthetic":
        payload["synthetic"] = True
    payload["experiment"]["review_sha256"] = hashlib.sha256(
        "\n".join(sorted(case["provenance"]["review_sha256"] for case in cases)).encode()
    ).hexdigest()
    _write_json(candidate, payload)

    result = evaluate_writing_rule_candidate(candidate)

    assert result["eligible"] is False
    assert expected in " ".join(result["reasons"])


def test_rule_gate_rejects_an_unstructured_review_artifact(tmp_path: Path) -> None:
    candidate = _rule_candidate(tmp_path)
    payload = json.loads(candidate.read_text(encoding="utf-8"))
    case = payload["cases"][0]
    review_path = Path(case["artifacts"]["review"]["path"])
    review_path.write_text("reviewed outcome", encoding="utf-8")
    case["artifacts"]["review"]["sha256"] = _sha256(review_path)
    case["provenance"]["review_sha256"] = _sha256(review_path)
    _write_json(candidate, payload)

    with pytest.raises(WoonError, match="must be UTF-8 JSON"):
        evaluate_writing_rule_candidate(candidate)


def test_rule_adoption_is_revision_guarded_and_rollback_reports_affected_documents(
    tmp_path: Path,
) -> None:
    candidate = _rule_candidate(tmp_path)
    policy = tmp_path / "policy.json"
    _write_json(policy, {"version": 1, "rules": [], "history": [], "applications": []})
    original_hash = _sha256(policy)

    adopted = adopt_writing_rule(policy, candidate, original_hash)
    with pytest.raises(WoonError, match="revision changed"):
        adopt_writing_rule(policy, candidate, original_hash)
    request, _ = _review_files(tmp_path / "application")
    request_payload = json.loads(request.read_text(encoding="utf-8"))
    request_payload["policy_path"] = str(policy)
    request_payload["policy_revision"] = adopted["policy_revision"]
    request_payload["applied_rules"] = [{"id": "rule-1", "version": "2"}]
    _write_json(request, request_payload)
    registered = register_writing_rule_application(policy, request, adopted["policy_sha256"])
    rolled_back = rollback_writing_rule(policy, "rule-1", registered["policy_sha256"])

    assert adopted["affected_documents"] == ["doc-1", "doc-2"]
    assert rolled_back["affected_documents"] == ["doc-1", "doc-2", "document-1"]
    assert json.loads(policy.read_text(encoding="utf-8"))["rules"] == []


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("stale-policy", "stale writing policy revision"),
        ("other-policy", "policy path differs"),
        ("stale-document", "document bytes are stale"),
    ],
)
def test_rule_application_binds_policy_and_current_document(
    tmp_path: Path, failure: str, message: str
) -> None:
    candidate = _rule_candidate(tmp_path)
    policy = tmp_path / "policy.json"
    _write_json(policy, {"version": 1, "rules": [], "history": [], "applications": []})
    adopted = adopt_writing_rule(policy, candidate, _sha256(policy))
    request, _ = _review_files(tmp_path / "application")
    request_payload = json.loads(request.read_text(encoding="utf-8"))
    request_payload["policy_path"] = str(policy)
    request_payload["policy_revision"] = adopted["policy_revision"]
    request_payload["applied_rules"] = [{"id": "rule-1", "version": "2"}]
    target_policy = policy
    if failure == "stale-policy":
        request_payload["policy_revision"] = "0" * 64
    elif failure == "other-policy":
        target_policy = tmp_path / "other-policy.json"
        target_policy.write_bytes(policy.read_bytes())
    else:
        document = Path(request_payload["document"]["path"])
        document.write_text(document.read_text(encoding="utf-8") + "변경\n", encoding="utf-8")
    _write_json(request, request_payload)

    with pytest.raises(WoonError, match=message):
        register_writing_rule_application(target_policy, request, _sha256(target_policy))


def test_writing_review_is_available_through_the_public_cli(tmp_path: Path) -> None:
    request, review = _review_files(tmp_path)
    output = StringIO()

    run(
        [
            "knowledge",
            "evaluate-writing-review",
            "--request",
            str(request),
            "--review",
            str(review),
        ],
        output,
    )

    assert json.loads(output.getvalue())["passed"] is True
