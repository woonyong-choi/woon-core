"""Validate evidence-bound writing reviews and learned-rule adoption.

The module is provider-neutral. Writers and reviewers run elsewhere; Core only
checks immutable inputs, evidence anchors, review separation, adoption gates,
and revision-guarded policy changes.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock

REVIEW_VERSION = 2
POLICY_VERSION = 1
PROFILES = {
    "translation",
    "technical-learning",
    "research-report",
    "general-guidance",
    "personal-record",
    "fiction",
}
CLAIM_KINDS = {"fact", "observed", "inference", "proposal", "example", "unknown"}
STATUSES = {"pass", "fail", "unknown", "not-applicable"}
RELATIONS = {
    "premise-conclusion",
    "condition-result",
    "coreference",
    "cause-change",
    "concept-example",
    "code-output-explanation",
    "question-answer",
}
COMMON_CRITERIA = {"semantic-fidelity", "preservation", "style"}
PROFILE_CRITERIA = {
    "translation": {"source-fidelity", "terminology", "evidence-boundary"},
    "technical-learning": {"factual-accuracy", "relation-accuracy", "evidence-boundary"},
    "research-report": {"factual-accuracy", "relation-accuracy", "evidence-boundary"},
    "general-guidance": {"factual-accuracy", "relation-accuracy", "evidence-boundary"},
    "personal-record": {"privacy-boundary", "evidence-boundary"},
    "fiction": {"internal-consistency"},
}
CORE_CRITERIA = {
    "semantic-fidelity",
    "preservation",
    "source-fidelity",
    "factual-accuracy",
    "relation-accuracy",
    "evidence-boundary",
    "privacy-boundary",
    "internal-consistency",
}
EVIDENCE_CRITERIA = {
    "source-fidelity",
    "factual-accuracy",
    "relation-accuracy",
    "evidence-boundary",
}
NOT_APPLICABLE_CRITERIA = {"terminology", "relation-accuracy", "privacy-boundary"}
RULE_STATUSES = {"candidate", "trial", "adopted", "held", "rejected", "disabled"}


def evaluate_writing_review(request_path: Path, review_path: Path) -> dict[str, object]:
    """Evaluate one review against current document, source, and standard bytes."""

    request = _load_object(request_path, "writing review request")
    _validate_request(request)
    document = _current_text(_mapping(request["document"], "document"), "document")
    standard = _current_text(_mapping(request["standard"], "standard"), "standard")
    references = _current_references(request["references"])
    review = _load_object(review_path, "writing review result")
    if review.get("version") == 1:
        return {
            "version": REVIEW_VERSION,
            "passed": False,
            "state": "incomplete",
            "legacy_review": True,
            "errors": ["version 1 review is readable but cannot satisfy version 2 semantics"],
        }
    if review.get("version") != REVIEW_VERSION:
        raise WoonError("writing review version must be 1 or 2")

    errors: list[str] = []
    request_digest = _file_sha256(request_path, "writing review request")
    _match_digest(review.get("request_sha256"), request_digest, "request", errors)
    _match_digest(review.get("document_sha256"), _sha256(document), "document", errors)
    _match_digest(review.get("final_document_sha256"), _sha256(document), "final document", errors)
    _match_digest(review.get("standard_sha256"), _sha256(standard), "standard", errors)
    document_id = _text(_mapping(request["document"], "document").get("id"), "document id")
    if review.get("document_id") != document_id:
        errors.append("writing review document id differs from its request")
    attempt = review.get("revision_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 0 <= attempt <= 2:
        errors.append("writing review revision_attempt must be between 0 and 2")
    if attempt != request["revision_attempt"]:
        errors.append("writing review revision_attempt differs from its request")

    writer = _identity(request.get("writer"), "writer")
    reviewer = _identity(review.get("reviewer"), "reviewer")
    if writer["run_id"] == reviewer["run_id"]:
        errors.append("writer and reviewer must use separate runs")

    profile = _text(request.get("profile"), "profile")
    expected_criteria = COMMON_CRITERIA | PROFILE_CRITERIA[profile]
    criterion_reviews = _indexed_reviews(
        review.get("criterion_reviews"), expected_criteria, "criterion"
    )
    claim_records = {
        _text(claim.get("id"), "claim id"): claim
        for claim in _mapping_list(request.get("claims"), "claims")
    }
    claim_reviews = _indexed_reviews(review.get("claim_reviews"), set(claim_records), "claim")
    unresolved: list[str] = []
    blocking_unknowns: list[str] = []
    failures: list[str] = []
    for criterion, item in criterion_reviews.items():
        status = _review_status(item, f"criterion {criterion}")
        _validate_document_anchor(item, document, status, f"criterion {criterion}")
        evidence = _validate_review_evidence(
            item.get("evidence", []), references, f"criterion {criterion}"
        )
        if status == "pass" and criterion in EVIDENCE_CRITERIA and not evidence:
            errors.append(f"passed criterion has no source evidence: {criterion}")
        if status == "not-applicable" and criterion not in NOT_APPLICABLE_CRITERIA:
            errors.append(f"criterion cannot be not-applicable: {criterion}")
        if (
            status == "pass"
            and criterion in EVIDENCE_CRITERIA
            and any(item["role"] != "fact" for item in evidence)
        ):
            errors.append(f"style reference cannot support a passed factual criterion: {criterion}")
        if status == "fail":
            failures.append(f"criterion:{criterion}")
        if status == "unknown":
            unresolved.append(f"criterion:{criterion}")
            if criterion in CORE_CRITERIA:
                blocking_unknowns.append(f"criterion:{criterion}")

    for claim_id, item in claim_reviews.items():
        claim = claim_records[claim_id]
        status = _review_status(item, f"claim {claim_id}")
        anchor = _text(item.get("document_anchor"), f"claim {claim_id} document_anchor")
        if anchor not in document or anchor != _text(claim.get("body_anchor"), "claim body_anchor"):
            errors.append(f"claim review anchor is absent or differs from request: {claim_id}")
        evidence = _validate_review_evidence(
            item.get("evidence", []), references, f"claim {claim_id}"
        )
        kind = _text(claim.get("kind"), "claim kind")
        if status == "pass" and kind in {"fact", "observed", "inference"}:
            if not evidence:
                errors.append(f"passed claim has no source evidence: {claim_id}")
            if any(item["role"] != "fact" for item in evidence):
                errors.append(f"style reference cannot support a passed claim: {claim_id}")
        if status == "pass" and kind == "unknown":
            errors.append(f"unknown claim cannot pass: {claim_id}")
        if status == "not-applicable":
            errors.append(f"claim cannot be not-applicable: {claim_id}")
        if status == "fail":
            failures.append(f"claim:{claim_id}")
        if status == "unknown":
            unresolved.append(f"claim:{claim_id}")
            if _boolean(claim.get("core"), f"claim {claim_id} core"):
                blocking_unknowns.append(f"claim:{claim_id}")

    hard_failures = _text_list(review.get("hard_failures"), "hard_failures")
    if hard_failures:
        failures.extend(f"hard:{failure}" for failure in hard_failures)
    expected_verdict = (
        "needs-revision"
        if failures
        else "incomplete"
        if blocking_unknowns or errors
        else "qualified"
        if unresolved
        else "passed"
    )
    if review.get("verdict") != expected_verdict:
        errors.append(f"writing review verdict must be {expected_verdict}")
    passed = not errors and expected_verdict in {"passed", "qualified"}
    return {
        "version": REVIEW_VERSION,
        "passed": passed,
        "state": expected_verdict if passed else "incomplete",
        "qualified": expected_verdict == "qualified",
        "profile": profile,
        "review_revision": attempt,
        "declared_models_differ": (
            writer["provider"],
            writer["model"],
        )
        != (reviewer["provider"], reviewer["model"]),
        "separation_evidence": "declared run metadata only",
        "failures": failures,
        "unknowns": unresolved,
        "errors": errors,
    }


def evaluate_writing_rule_candidate(candidate_path: Path) -> dict[str, object]:
    """Return the exact hold/adopt decision for one learned writing rule."""

    candidate = _load_object(candidate_path, "writing rule candidate")
    return _evaluate_writing_rule_candidate(candidate)


def _evaluate_writing_rule_candidate(candidate: dict[str, object]) -> dict[str, object]:
    if candidate.get("version") != POLICY_VERSION:
        raise WoonError("writing rule candidate version must be 1")
    rule = _mapping(candidate.get("rule"), "writing rule")
    _validate_rule(rule)
    reasons: list[str] = []
    synthetic = _boolean(candidate.get("synthetic"), "writing rule synthetic")
    if synthetic:
        reasons.append("synthetic cases cannot adopt a rule")
    if rule["status"] != "trial":
        reasons.append("only a trial rule can be adopted")
    cases = _mapping_list(candidate.get("cases"), "writing rule cases")
    if len(cases) < 20:
        reasons.append("at least 20 unique real cases are required")
    ids: set[str] = set()
    inputs: set[str] = set()
    source_units: set[str] = set()
    wins = ties = 0
    profile_cases: dict[str, list[dict[str, Any]]] = {profile: [] for profile in rule["profiles"]}
    outcomes: dict[str, str] = {}
    review_hashes: list[str] = []
    for case in cases:
        case_id = _text(case.get("id"), "writing rule case id")
        input_sha256 = _digest(case.get("input_sha256"), "writing rule case input_sha256")
        if case_id in ids or input_sha256 in inputs:
            reasons.append(f"duplicate case or input: {case_id}")
        ids.add(case_id)
        inputs.add(input_sha256)
        source_unit_id = _digest(case.get("source_unit_id"), "writing rule source_unit_id")
        source_id = _text(case.get("source_id"), "writing rule case source_id")
        task_id = _text(case.get("task_id"), "writing rule case task_id")
        profile = _text(case.get("profile"), "writing rule case profile")
        if profile not in profile_cases:
            reasons.append(f"case profile is outside rule scope: {case_id}")
        else:
            profile_cases[profile].append(case)
        artifacts = _mapping(case.get("artifacts"), f"case {case_id} artifacts")
        artifact_bytes = {
            name: _current_artifact(artifacts.get(name), f"case {case_id} {name}")
            for name in ("input", "baseline", "candidate", "source", "review")
        }
        review_hashes.append(hashlib.sha256(artifact_bytes["review"]).hexdigest())
        provenance = _mapping(case.get("provenance"), f"case {case_id} provenance")
        provenance_kind = _text(provenance.get("kind"), "case provenance kind")
        label_source = _text(provenance.get("label_source"), "case provenance label_source")
        reviewer_id = _text(provenance.get("reviewer_id"), "case provenance reviewer_id")
        _text(provenance.get("revision"), "case provenance revision")
        if (
            provenance_kind != "real"
            or label_source not in {"human", "reviewed-model"}
            or provenance.get("review_sha256")
            != hashlib.sha256(artifact_bytes["review"]).hexdigest()
        ):
            reasons.append(f"case lacks bound review provenance: {case_id}")
        baseline = _run_contract(case.get("baseline"), f"case {case_id} baseline")
        revised = _run_contract(case.get("candidate"), f"case {case_id} candidate")
        if any(
            baseline[key] != revised[key] for key in ("provider", "model", "tool", "input_sha256")
        ):
            reasons.append(f"baseline and candidate conditions differ: {case_id}")
        if baseline["run_id"] == revised["run_id"]:
            reasons.append(f"baseline and candidate runs are not separate: {case_id}")
        if (
            baseline["input_sha256"] != input_sha256
            or hashlib.sha256(artifact_bytes["input"]).hexdigest() != input_sha256
        ):
            reasons.append(f"case input artifact differs: {case_id}")
        if baseline["output_sha256"] != hashlib.sha256(artifact_bytes["baseline"]).hexdigest():
            reasons.append(f"baseline output artifact differs: {case_id}")
        if revised["output_sha256"] != hashlib.sha256(artifact_bytes["candidate"]).hexdigest():
            reasons.append(f"candidate output artifact differs: {case_id}")
        review = _json_object(artifact_bytes["review"], f"case {case_id} review")
        reviewer = _mapping(review.get("reviewer"), f"case {case_id} review reviewer")
        review_kind = _text(reviewer.get("kind"), "case review reviewer kind")
        if review_kind not in {"human", "reviewed-model"}:
            raise WoonError(f"case review reviewer kind is invalid: {case_id}")
        review_bindings = {
            "source_id": source_id,
            "task_id": task_id,
            "input_sha256": input_sha256,
            "baseline_sha256": baseline["output_sha256"],
            "candidate_sha256": revised["output_sha256"],
            "source_sha256": hashlib.sha256(artifact_bytes["source"]).hexdigest(),
        }
        if (
            review.get("version") != 1
            or review.get("case_id") != case_id
            or review.get("profile") != profile
            or any(review.get(key) != value for key, value in review_bindings.items())
        ):
            reasons.append(f"review artifact bindings differ: {case_id}")
        if (
            _text(reviewer.get("id"), "case review reviewer id") != reviewer_id
            or review_kind != label_source
        ):
            reasons.append(f"reviewer provenance differs: {case_id}")
        reviewer_run_id = _text(reviewer.get("run_id"), "case review reviewer run_id")
        if reviewer_run_id in {baseline["run_id"], revised["run_id"]}:
            reasons.append(f"writer and reviewer runs are not separate: {case_id}")
        if review_kind == "reviewed-model":
            for field in ("provider", "model", "tool"):
                _text(reviewer.get(field), f"case review reviewer {field}")
        _text(review.get("reason"), "case review reason")
        if case.get("held_out") is not True or case.get("used_for_rule") is not False:
            reasons.append(f"held-out leakage: {case_id}")
        outcome = _text(review.get("outcome"), "writing rule case outcome")
        outcomes[case_id] = outcome
        if case.get("outcome") != outcome:
            reasons.append(f"case outcome differs from review artifact: {case_id}")
        if outcome == "win":
            wins += 1
        elif outcome == "tie":
            ties += 1
        elif outcome in {"loss", "unknown"}:
            reasons.append(f"{outcome} case: {case_id}")
        else:
            raise WoonError(f"writing rule case outcome is invalid: {case_id}")
        controls = _mapping(case.get("required_controls"), "required_controls")
        review_controls = _mapping(review.get("required_controls"), "review required_controls")
        required_controls = {"meaning", "preservation", "evidence", "scope", f"profile:{profile}"}
        if controls != review_controls:
            reasons.append(f"case controls differ from review artifact: {case_id}")
        if set(review_controls) != required_controls or any(
            value != "pass" for value in review_controls.values()
        ):
            reasons.append(f"required control did not pass: {case_id}")
        for field in (
            "hard_failure",
            "core_unknown",
            "unresolved_disagreement",
            "preference_conflict",
        ):
            review_value = _boolean(review.get(field), f"case review {case_id} {field}")
            if case.get(field) != review_value:
                reasons.append(f"case {field} differs from review artifact: {case_id}")
            if review_value:
                reasons.append(f"{field}: {case_id}")
        evidence = _mapping(case.get("evidence"), "writing rule case evidence")
        review_evidence = _mapping(review.get("evidence"), "writing rule review evidence")
        if evidence != review_evidence:
            reasons.append(f"case evidence differs from review artifact: {case_id}")
        _text(evidence.get("document_anchor"), "writing rule document_anchor")
        _text(evidence.get("evidence_anchor"), "writing rule evidence_anchor")
        _relation(evidence.get("relation"), "writing rule evidence relation")
        if evidence["document_anchor"].encode() not in artifact_bytes["candidate"]:
            reasons.append(f"candidate evidence anchor is absent: {case_id}")
        if evidence["evidence_anchor"].encode() not in artifact_bytes["source"]:
            reasons.append(f"source evidence anchor is absent: {case_id}")
        expected_source_unit = hashlib.sha256(
            artifacts["source"]["sha256"].encode()
            + b"\0"
            + str(evidence["evidence_anchor"]).encode()
        ).hexdigest()
        if source_unit_id != expected_source_unit or source_unit_id in source_units:
            reasons.append(f"duplicate or unbound source unit: {case_id}")
        source_units.add(source_unit_id)
    for profile, scoped_cases in profile_cases.items():
        scoped_sources = {str(case["source_id"]) for case in scoped_cases}
        scoped_tasks = {str(case["task_id"]) for case in scoped_cases}
        scoped_wins = sum(outcomes[str(case["id"])] == "win" for case in scoped_cases)
        scoped_ties = sum(outcomes[str(case["id"])] == "tie" for case in scoped_cases)
        if len(scoped_cases) < 20:
            reasons.append(f"profile {profile} requires at least 20 unique real cases")
        if max(len(scoped_sources), len(scoped_tasks)) < 3:
            reasons.append(f"profile {profile} requires at least 3 sources or tasks")
        if scoped_wins < 12:
            reasons.append(f"profile {profile} requires at least 12 wins")
        if scoped_wins + scoped_ties != len(scoped_cases):
            reasons.append(f"profile {profile} requires every remaining case to be a tie")
    experiment = _mapping(candidate.get("experiment"), "writing rule experiment")
    frozen = hashlib.sha256(encode_json(rule)).hexdigest()
    if experiment.get("rule_frozen_sha256") != frozen:
        reasons.append("experiment rule freeze hash differs")
    derivation = set(_text_list(experiment.get("derivation_case_ids"), "derivation_case_ids"))
    evaluation = set(_text_list(experiment.get("evaluation_case_ids"), "evaluation_case_ids"))
    if derivation.intersection(evaluation) or evaluation != ids:
        reasons.append("experiment derivation and evaluation cases are not disjoint and complete")
    expected_review_hash = hashlib.sha256("\n".join(sorted(review_hashes)).encode()).hexdigest()
    if experiment.get("review_sha256") != expected_review_hash:
        reasons.append("experiment review hash differs")
    eligible = not reasons
    return {
        "version": POLICY_VERSION,
        "rule_id": rule["id"],
        "rule_version": rule["version"],
        "eligible": eligible,
        "status": "adopted" if eligible else "held",
        "cases": len(cases),
        "profiles": sorted(profile_cases),
        "wins": wins,
        "ties": ties,
        "reasons": list(dict.fromkeys(reasons)),
        "provenance_limit": (
            "Core validates declared provenance, hashes, and uniqueness; it cannot prove semantic "
            "independence or human judgment from metadata alone."
        ),
    }


def adopt_writing_rule(
    policy_path: Path, candidate_path: Path, expected_policy_sha256: str
) -> dict[str, object]:
    """Atomically adopt an eligible rule under an exact policy revision."""

    candidate = _load_object(candidate_path, "writing rule candidate")
    gate = _evaluate_writing_rule_candidate(candidate)
    if gate["eligible"] is not True:
        raise WoonError("writing rule candidate is not eligible for adoption")
    with exclusive_file_lock(policy_path.with_suffix(policy_path.suffix + ".lock")):
        _require_current_hash(policy_path, expected_policy_sha256, "writing policy")
        policy = _load_object(policy_path, "writing policy")
        _validate_policy(policy)
        rule = deepcopy(_mapping(candidate["rule"], "writing rule"))
        rules = _mapping_list(policy["rules"], "writing policy rules")
        previous = next((deepcopy(item) for item in rules if item.get("id") == rule["id"]), None)
        if previous is not None and previous.get("version") == rule["version"]:
            raise WoonError("writing rule adoption requires a new rule version")
        rules = [item for item in rules if item.get("id") != rule["id"]]
        rule["status"] = "adopted"
        rules.append(rule)
        affected = _text_list(candidate.get("affected_documents"), "affected_documents")
        history = _mapping_list(policy["history"], "writing policy history")
        history.append(
            {
                "action": "adopt",
                "rule_id": rule["id"],
                "rule_version": rule["version"],
                "previous_rule": previous,
                "affected_documents": affected,
            }
        )
        updated = {
            "version": POLICY_VERSION,
            "rules": rules,
            "history": history,
            "applications": _mapping_list(policy.get("applications", []), "writing applications"),
        }
        atomic_write(policy_path, encode_json(updated))
    return {
        "adopted": rule["id"],
        "policy_sha256": _file_sha256(policy_path, "writing policy"),
        "policy_revision": _rules_revision(updated),
        "affected_documents": affected,
    }


def rollback_writing_rule(
    policy_path: Path, rule_id: str, expected_policy_sha256: str
) -> dict[str, object]:
    """Restore the policy state immediately before the latest adoption."""

    rule_id = _text(rule_id, "writing rule id")
    with exclusive_file_lock(policy_path.with_suffix(policy_path.suffix + ".lock")):
        _require_current_hash(policy_path, expected_policy_sha256, "writing policy")
        policy = _load_object(policy_path, "writing policy")
        _validate_policy(policy)
        history = _mapping_list(policy["history"], "writing policy history")
        latest_rollback = max(
            (
                index
                for index, item in enumerate(history)
                if item.get("action") == "rollback" and item.get("rule_id") == rule_id
            ),
            default=-1,
        )
        adoption = next(
            (
                item
                for index, item in reversed(tuple(enumerate(history)))
                if index > latest_rollback
                and item.get("action") == "adopt"
                and item.get("rule_id") == rule_id
            ),
            None,
        )
        if adoption is None:
            raise WoonError(f"writing rule has no active adoption to roll back: {rule_id}")
        rules = [
            item
            for item in _mapping_list(policy["rules"], "writing policy rules")
            if item.get("id") != rule_id
        ]
        previous = adoption.get("previous_rule")
        if previous is not None:
            rules.append(_mapping(previous, "previous writing rule"))
        applications = _mapping_list(policy.get("applications", []), "writing applications")
        applied_documents = {
            _text(item.get("document_id"), "writing application document_id")
            for item in applications
            if any(
                rule.get("id") == rule_id
                for rule in _mapping_list(item.get("rules"), "writing application rules")
            )
        }
        affected = sorted(
            set(_text_list(adoption.get("affected_documents"), "affected_documents"))
            | applied_documents
        )
        history.append(
            {
                "action": "rollback",
                "rule_id": rule_id,
                "rule_version": adoption["rule_version"],
                "affected_documents": affected,
            }
        )
        updated = {
            "version": POLICY_VERSION,
            "rules": rules,
            "history": history,
            "applications": applications,
        }
        atomic_write(policy_path, encode_json(updated))
    return {
        "rolled_back": rule_id,
        "policy_sha256": _file_sha256(policy_path, "writing policy"),
        "policy_revision": _rules_revision(updated),
        "affected_documents": affected,
    }


def register_writing_rule_application(
    policy_path: Path, request_path: Path, expected_policy_sha256: str
) -> dict[str, object]:
    """Record which current document revision used adopted rule versions."""

    request = _load_object(request_path, "writing review request")
    _validate_request(request)
    applied_rules = _mapping_list(request.get("applied_rules"), "applied_rules")
    if not applied_rules:
        raise WoonError("writing review request has no applied rules")
    document = _mapping(request["document"], "document")
    request_policy_path = (
        Path(_text(request.get("policy_path"), "policy_path")).expanduser().resolve()
    )
    if request_policy_path != policy_path.expanduser().resolve():
        raise WoonError("writing review request policy path differs from registration policy")
    application = {
        "request_sha256": _file_sha256(request_path, "writing review request"),
        "document_id": _text(document.get("id"), "document id"),
        "document_sha256": _digest(document.get("sha256"), "document sha256"),
        "profile": _text(request.get("profile"), "profile"),
        "rules": applied_rules,
    }
    with exclusive_file_lock(policy_path.with_suffix(policy_path.suffix + ".lock")):
        _require_current_hash(policy_path, expected_policy_sha256, "writing policy")
        _validate_request(request)
        _current_text(document, "document")
        policy = _load_object(policy_path, "writing policy")
        _validate_policy(policy)
        applications = _mapping_list(policy.get("applications", []), "writing applications")
        if any(
            item.get("request_sha256") == application["request_sha256"] for item in applications
        ):
            raise WoonError("writing rule application is already registered")
        applications.append(application)
        updated = {
            "version": POLICY_VERSION,
            "rules": policy["rules"],
            "history": policy["history"],
            "applications": applications,
        }
        atomic_write(policy_path, encode_json(updated))
    return {
        "registered": application["document_id"],
        "policy_sha256": _file_sha256(policy_path, "writing policy"),
        "policy_revision": _rules_revision(updated),
        "rules": applied_rules,
    }


def _validate_request(request: dict[str, object]) -> None:
    if request.get("version") != REVIEW_VERSION:
        raise WoonError("writing review request version must be 2")
    profile = _text(request.get("profile"), "profile")
    if profile not in PROFILES:
        raise WoonError(f"writing review profile is invalid: {profile}")
    for field in ("purpose", "audience", "visibility", "edit_scope"):
        _text(request.get(field), field)
    _text_list(request.get("preserved_elements"), "preserved_elements")
    _text_list(request.get("completion_conditions"), "completion_conditions")
    _identity(request.get("writer"), "writer")
    document_record = _mapping(request.get("document"), "document")
    document_id = _text(document_record.get("id"), "document id")
    attempt = request.get("revision_attempt")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 0 <= attempt <= 2:
        raise WoonError("writing review request revision_attempt must be between 0 and 2")
    if attempt:
        prior_request_bytes = _current_artifact(request.get("prior_request"), "prior request")
        prior = _current_artifact(request.get("prior_review"), "prior review")
        prior_request = _json_object(prior_request_bytes, "prior request")
        prior_review = _json_object(prior, "prior review")
        if (
            prior_request.get("version") != REVIEW_VERSION
            or prior_request.get("revision_attempt") != attempt - 1
            or _mapping(prior_request.get("document"), "prior request document").get("id")
            != document_id
            or prior_review.get("version") != REVIEW_VERSION
            or prior_review.get("verdict") not in {"needs-revision", "incomplete"}
            or prior_review.get("revision_attempt") != attempt - 1
            or prior_review.get("document_id") != document_id
            or prior_review.get("request_sha256") != hashlib.sha256(prior_request_bytes).hexdigest()
        ):
            raise WoonError("a revision requires a prior non-passing review")
    _validate_applied_rules(request, profile)
    _mapping(request.get("standard"), "standard")
    references = _mapping_list(request.get("references"), "references")
    reference_roles = {
        _text(item.get("id"), "reference id"): _text(item.get("role"), "reference role")
        for item in references
    }
    reference_ids = set(reference_roles)
    if len(reference_ids) != len(references):
        raise WoonError("writing review references contain duplicate ids")
    claims = _mapping_list(request.get("claims"), "claims")
    claim_ids: set[str] = set()
    for claim in claims:
        claim_id = _text(claim.get("id"), "claim id")
        if claim_id in claim_ids:
            raise WoonError(f"writing review claim is duplicated: {claim_id}")
        claim_ids.add(claim_id)
        kind = _text(claim.get("kind"), "claim kind")
        if kind not in CLAIM_KINDS:
            raise WoonError(f"writing review claim kind is invalid: {claim_id}")
        _text(claim.get("body_anchor"), "claim body_anchor")
        _boolean(claim.get("core"), f"claim {claim_id} core")
        for evidence in _mapping_list(claim.get("evidence"), "claim evidence"):
            reference_id = _text(evidence.get("reference_id"), "claim reference_id")
            if reference_id not in reference_ids:
                raise WoonError(f"claim evidence has an unknown reference: {claim_id}")
            if (
                kind in {"fact", "observed", "inference"}
                and reference_roles[reference_id] != "fact"
            ):
                raise WoonError(f"source-grounded claim uses a style reference: {claim_id}")
            _text(evidence.get("anchor"), "claim evidence anchor")
            _relation(evidence.get("relation"), "claim evidence relation")
        if kind in {"fact", "observed", "inference"} and not claim.get("evidence"):
            raise WoonError(f"source-grounded claim has no evidence: {claim_id}")


def _current_references(value: object) -> dict[str, dict[str, str]]:
    references: dict[str, dict[str, str]] = {}
    for item in _mapping_list(value, "references"):
        reference_id = _text(item.get("id"), "reference id")
        role = _text(item.get("role"), "reference role")
        if role not in {"fact", "style"}:
            raise WoonError(f"writing reference role is invalid: {reference_id}")
        _text(item.get("locator"), "reference locator")
        _text(item.get("revision"), "reference revision")
        references[reference_id] = {
            "text": _current_text(item, f"reference {reference_id}"),
            "role": role,
        }
    return references


def _current_text(record: dict[str, Any], label: str) -> str:
    path = Path(_text(record.get("path"), f"{label} path")).expanduser().resolve()
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise WoonError(f"cannot read {label}: {error}") from error
    if hashlib.sha256(data).hexdigest() != _digest(record.get("sha256"), f"{label} sha256"):
        raise WoonError(f"{label} bytes are stale")
    _text(record.get("revision"), f"{label} revision")
    return text


def _indexed_reviews(value: object, expected: set[str], label: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for item in _mapping_list(value, f"{label}_reviews"):
        item_id = _text(item.get("id"), f"{label} review id")
        if item_id in records:
            raise WoonError(f"duplicate {label} review: {item_id}")
        records[item_id] = item
    if set(records) != expected:
        raise WoonError(f"{label} reviews do not match required ids")
    return records


def _review_status(item: dict[str, Any], label: str) -> str:
    status = _text(item.get("status"), f"{label} status")
    if status not in STATUSES:
        raise WoonError(f"{label} status is invalid")
    _text(item.get("reason"), f"{label} reason")
    if status == "not-applicable":
        _text(item.get("not_applicable_reason"), f"{label} not_applicable_reason")
    elif item.get("not_applicable_reason") is not None:
        raise WoonError(f"{label} not_applicable_reason is only valid for not-applicable")
    return status


def _validate_document_anchor(item: dict[str, Any], document: str, status: str, label: str) -> None:
    if status == "not-applicable":
        return
    anchor = _text(item.get("document_anchor"), f"{label} document_anchor")
    if anchor not in document:
        raise WoonError(f"{label} document anchor is absent")


def _validate_review_evidence(
    value: object, references: dict[str, dict[str, str]], label: str
) -> list[dict[str, Any]]:
    evidence = _mapping_list(value, f"{label} evidence")
    for item in evidence:
        reference_id = _text(item.get("reference_id"), f"{label} reference_id")
        reference = references.get(reference_id)
        if reference is None:
            raise WoonError(f"{label} evidence has an unknown reference")
        anchor = _text(item.get("anchor"), f"{label} evidence anchor")
        if anchor not in reference["text"]:
            raise WoonError(f"{label} evidence anchor is absent from its source")
        item["role"] = reference["role"]
        _relation(item.get("relation"), f"{label} evidence relation")
    return evidence


def _validate_rule(rule: dict[str, Any]) -> None:
    for field in (
        "id",
        "version",
        "instruction",
        "applicability",
        "expected_effect",
        "evaluation_revision",
    ):
        _text(rule.get(field), f"writing rule {field}")
    profiles = set(_text_list(rule.get("profiles"), "writing rule profiles"))
    if not profiles or not profiles.issubset(PROFILES):
        raise WoonError("writing rule profiles are invalid")
    for field in ("source_observations", "exclusions", "counterexamples"):
        _text_list(rule.get(field), f"writing rule {field}")
    status = _text(rule.get("status"), "writing rule status")
    if status not in RULE_STATUSES:
        raise WoonError("writing rule status is invalid")
    if rule.get("invariant_effect") != "preserve-only":
        raise WoonError("writing rule invariant_effect must be preserve-only")


def _validate_policy(policy: dict[str, object]) -> None:
    if policy.get("version") != POLICY_VERSION:
        raise WoonError("writing policy version must be 1")
    rules = _mapping_list(policy.get("rules"), "writing policy rules")
    seen: set[str] = set()
    for rule in rules:
        _validate_rule(rule)
        rule_id = str(rule["id"])
        if rule_id in seen:
            raise WoonError(f"writing policy rule is duplicated: {rule_id}")
        seen.add(rule_id)
    _mapping_list(policy.get("history"), "writing policy history")
    _mapping_list(policy.get("applications", []), "writing applications")


def _rules_revision(policy: dict[str, object]) -> str:
    rules = _mapping_list(policy.get("rules"), "writing policy rules")
    return hashlib.sha256(encode_json(rules)).hexdigest()


def _validate_applied_rules(request: dict[str, object], profile: str) -> None:
    applied = _mapping_list(request.get("applied_rules", []), "applied_rules")
    if not applied:
        return
    policy_path = Path(_text(request.get("policy_path"), "policy_path")).expanduser().resolve()
    policy = _load_object(policy_path, "writing policy")
    _validate_policy(policy)
    if request.get("policy_revision") != _rules_revision(policy):
        raise WoonError("writing review request uses a stale writing policy revision")
    adopted = {
        (
            _text(rule.get("id"), "writing rule id"),
            _text(rule.get("version"), "writing rule version"),
        ): rule
        for rule in _mapping_list(policy["rules"], "writing policy rules")
        if rule.get("status") == "adopted"
    }
    seen: set[tuple[str, str]] = set()
    for item in applied:
        key = (
            _text(item.get("id"), "applied rule id"),
            _text(item.get("version"), "applied rule version"),
        )
        if key in seen:
            raise WoonError("writing review request contains duplicate applied rules")
        seen.add(key)
        rule = adopted.get(key)
        if rule is None:
            raise WoonError(f"writing review request uses an unadopted rule: {key[0]}")
        if profile not in _text_list(rule.get("profiles"), "writing rule profiles"):
            raise WoonError(f"writing rule does not apply to profile {profile}: {key[0]}")


def _run_contract(value: object, label: str) -> dict[str, str]:
    record = _mapping(value, label)
    return {
        "provider": _text(record.get("provider"), f"{label} provider"),
        "model": _text(record.get("model"), f"{label} model"),
        "tool": _text(record.get("tool"), f"{label} tool"),
        "run_id": _text(record.get("run_id"), f"{label} run_id"),
        "input_sha256": _digest(record.get("input_sha256"), f"{label} input_sha256"),
        "output_sha256": _digest(record.get("output_sha256"), f"{label} output_sha256"),
    }


def _identity(value: object, label: str) -> dict[str, str]:
    record = _mapping(value, label)
    return {
        "provider": _text(record.get("provider"), f"{label} provider"),
        "model": _text(record.get("model"), f"{label} model"),
        "tool": _text(record.get("tool"), f"{label} tool"),
        "run_id": _text(record.get("run_id"), f"{label} run_id"),
    }


def _current_artifact(value: object, label: str) -> bytes:
    record = _mapping(value, label)
    path = Path(_text(record.get("path"), f"{label} path")).expanduser().resolve()
    try:
        data = path.read_bytes()
    except OSError as error:
        raise WoonError(f"cannot read {label}: {error}") from error
    if hashlib.sha256(data).hexdigest() != _digest(record.get("sha256"), f"{label} sha256"):
        raise WoonError(f"{label} bytes are stale")
    return data


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise WoonError(f"{field} must be true or false")
    return value


def _relation(value: object, field: str) -> str:
    relation = _text(value, field)
    if relation not in RELATIONS:
        raise WoonError(f"{field} is invalid")
    return relation


def _require_current_hash(path: Path, expected: str, label: str) -> None:
    expected = _digest(expected, f"expected {label} sha256")
    if _file_sha256(path, label) != expected:
        raise WoonError(f"{label} revision changed")


def _match_digest(value: object, actual: str, label: str, errors: list[str]) -> None:
    if _digest(value, f"{label} sha256") != actual:
        errors.append(f"writing review {label} hash is stale")


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WoonError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise WoonError(f"{label} must be an object")
    return value


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WoonError(f"{label} must be UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise WoonError(f"{label} must be an object")
    return value


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WoonError(f"{field} must be an object")
    return value


def _mapping_list(value: object, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise WoonError(f"{field} must be a list of objects")
    return value


def _text_list(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise WoonError(f"{field} must be a list of non-empty text")
    return [item.strip() for item in value]


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WoonError(f"{field} must be non-empty text")
    return value.strip()


def _digest(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise WoonError(f"{field} must be a SHA-256 digest")
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path, label: str) -> str:
    try:
        return hashlib.sha256(path.expanduser().read_bytes()).hexdigest()
    except OSError as error:
        raise WoonError(f"cannot read {label}: {error}") from error
