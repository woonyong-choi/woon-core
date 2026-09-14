"""Evidence-first career application pipeline backed by one Wiki document."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pypdf import PdfReader

from woon_core.career.evidence import CareerEvidence, digest, wiki_path
from woon_core.errors import WoonError
from woon_core.io import atomic_write, exclusive_file_lock
from woon_core.knowledge.context_bundle import ContextBundle, build_context_bundle
from woon_core.knowledge.factory import build_knowledge_service
from woon_core.knowledge.service import KnowledgeService
from woon_core.knowledge.source_boundary import private_source_relative

APPLICATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,80}$")
STATES = (
    "discovered",
    "evaluated",
    "approved_for_draft",
    "drafted",
    "reviewed",
    "ready",
    "submitted",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
    "closed",
)
TERMINAL_STATES = {"offer", "rejected", "withdrawn", "closed"}
CLASSIFICATIONS = {"verified", "adjacent", "gap"}
OWNERSHIP_SCOPES = {"personal", "team", "mixed", "post_project", "unknown"}
JD_SUFFIXES = {".md", ".txt", ".yaml", ".yml", ".json", ".pdf"}
STATE_LABELS = {
    "discovered": "검토 시작",
    "evaluated": "근거 대조 완료",
    "approved_for_draft": "초안 작성 승인",
    "drafted": "초안 연결",
    "reviewed": "초안 검토 완료",
    "ready": "제출 준비 완료",
    "submitted": "제출 완료",
    "interview": "면접 단계",
    "offer": "합격",
    "rejected": "불합격",
    "withdrawn": "지원 철회",
    "closed": "종료",
}
CLASSIFICATION_LABELS = {
    "verified": "직접 근거",
    "adjacent": "인접 근거",
    "gap": "근거 공백",
}
OWNERSHIP_LABELS = {
    "personal": "개인 기여",
    "team": "팀 성과",
    "mixed": "개인·팀 혼합",
    "post_project": "종료 후 개인 확장",
    "unknown": "미확인",
}
ARTIFACT_LABELS = {"draft": "초안", "submitted": "실제 제출본"}
OUTCOME_EVIDENCE_KINDS = {"email", "portal", "manual"}


@dataclass(frozen=True, slots=True)
class CareerResult:
    application_id: str
    state: str
    relative_path: str
    changed: bool


class CareerApplicationService:
    """Own application state while keeping JD/PDF files as immutable sources."""

    def __init__(
        self,
        vault: Path,
        knowledge: KnowledgeService | None = None,
        *,
        repositories: dict[str, Path] | None = None,
    ) -> None:
        self._vault = vault.expanduser().resolve()
        self._knowledge = knowledge
        self._wiki_root = self._vault / "wiki/personal/career/applications"
        self._source_root = self._vault / private_source_relative(
            self._vault,
            "knowledge",
            "private",
            "career",
            "applications",
        )
        self._lock = self._vault / ".local/woon-knowledge/career-pipeline.lock"
        self._evidence = CareerEvidence(self._vault, repositories)
        self._loaded_revisions: dict[Path, str] = {}

    def create(
        self,
        *,
        application_id: str,
        company: str,
        role: str,
        jd_path: Path,
        deadline: str | None = None,
    ) -> CareerResult:
        identifier = self._identifier(application_id)
        page = self._page_path(identifier)
        if page.exists():
            raise WoonError(f"career application already exists: {identifier}")
        jd = jd_path.expanduser().resolve()
        if not jd.is_file():
            raise WoonError(f"JD source does not exist: {jd}")
        if jd.suffix.casefold() not in JD_SUFFIXES:
            raise WoonError("career JD must be Markdown, text, YAML, JSON, or PDF")
        if not company.strip() or not role.strip():
            raise WoonError("career application company and role must not be empty")
        source_rel = self._application_source_relative(identifier, f"jd{jd.suffix.lower()}")
        source = self._vault / source_rel
        now = _now()
        record: dict[str, Any] = {
            "schema_version": 1,
            "application_id": identifier,
            "company": company.strip(),
            "role": role.strip(),
            "application_state": "discovered",
            "lifecycle_status": "active",
            "started_on": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
            "deadline": deadline,
            "jd_source": source_rel,
            "jd_sha256": _sha256(jd.read_bytes()),
            "jd_trust": "untrusted-data",
            "requirements": [],
            "artifacts": [],
            "related_paths": [],
            "history": [{"at": now, "event": "지원 검토 시작", "reason": "JD 원본 보존"}],
        }
        self._commit_files(
            {source: jd.read_bytes(), page: self._render(record)}, expected={page: None}
        )
        return self._result(record, changed=True)

    def analyze(self, application_id: str, *, max_requirements: int = 12) -> CareerResult:
        """Create conservative evidence candidates; never auto-verify a career claim."""

        if not 1 <= max_requirements <= 50:
            raise WoonError("career analysis max_requirements must be between 1 and 50")
        record = self._load(application_id)
        self._require_state(record, {"discovered", "evaluated"}, "career analysis")
        source = self._vault / str(record["jd_source"])
        text = _read_source_text(source)
        requirements = _requirement_lines(text, max_requirements=max_requirements)
        knowledge = self._knowledge_service()
        knowledge.reindex()
        suggestions: list[dict[str, object]] = []
        for requirement in requirements:
            hits = knowledge.search(requirement, 3)
            evidence_paths = list(
                dict.fromkeys(
                    hit.relative_path for hit in hits if hit.relative_path.startswith("wiki/")
                )
            )
            suggestions.append(
                {
                    "requirement": requirement,
                    "classification": "adjacent" if evidence_paths else "gap",
                    "ownership": "unknown",
                    "rationale": "자동 검색 후보이며 사람 검토 전에는 경력 근거로 확정하지 않는다.",
                    "evidence_paths": evidence_paths,
                    "reviewed": False,
                }
            )
        record["requirements"] = suggestions
        self._transition(record, "evaluated", "JD 요구사항과 Wiki 근거 후보를 보수적으로 대조")
        return self._save(record)

    def evaluate(self, application_id: str, matrix: list[dict[str, object]]) -> CareerResult:
        """Store reviewed file/section evidence; structural checks never establish authorship."""

        if not matrix:
            raise WoonError("career evaluation matrix must not be empty")
        record = self._load(application_id)
        if record["application_state"] in TERMINAL_STATES:
            raise WoonError("terminal career records cannot be re-evaluated automatically")
        normalized: list[dict[str, object]] = []
        for item in matrix:
            requirement = str(item.get("requirement", "")).strip()
            classification = str(item.get("classification", "")).strip()
            ownership = str(item.get("ownership", "")).strip()
            rationale = str(item.get("rationale", "")).strip()
            raw_paths = item.get("evidence_paths", [])
            if (
                not requirement
                or classification not in CLASSIFICATIONS
                or ownership not in OWNERSHIP_SCOPES
                or not rationale
            ):
                raise WoonError(
                    "each career requirement needs requirement, classification, "
                    "ownership, rationale"
                )
            if not isinstance(raw_paths, list) or not all(
                isinstance(path, str) for path in raw_paths
            ):
                raise WoonError("career evidence_paths must be a list of Wiki paths")
            paths = [self._evidence_path(path) for path in raw_paths]
            raw_refs = item.get("evidence_refs", [])
            if not isinstance(raw_refs, list) or not all(isinstance(ref, dict) for ref in raw_refs):
                raise WoonError("career evidence_refs must be a list of section references")
            refs = [self._evidence.validate(ref) for ref in raw_refs]
            if len({(ref["canonical_id"], ref["section"]) for ref in refs}) != len(refs):
                raise WoonError("career requirement contains duplicate evidence sections")
            paths = list(
                dict.fromkeys([*paths, *(f"wiki/{ref['canonical_id']}.md" for ref in refs)])
            )
            if classification == "verified" and not paths:
                raise WoonError("verified career requirements need at least one Wiki evidence path")
            if classification == "verified" and ownership == "unknown":
                raise WoonError("verified career requirements need a known ownership scope")
            normalized.append(
                {
                    "requirement": requirement,
                    "classification": classification,
                    "ownership": ownership,
                    "rationale": rationale,
                    "evidence_paths": paths,
                    "reviewed": True,
                }
            )
            if refs:
                normalized[-1]["evidence_refs"] = refs
        if record.get("requirements") == normalized:
            return self._result(record, changed=False)
        record["requirements"] = normalized
        if record["application_state"] in {"discovered", "evaluated"}:
            self._transition(record, "evaluated", "사람이 요구사항별 근거와 공백을 검토")
        else:
            self._event(record, "JD와 경력 근거 재검토", "지원 단계는 유지하고 근거표만 갱신")
        return self._save(record)

    def evidence(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Capture current canonical section/code pins without storing or verifying a claim."""
        return self._evidence.capture(spec)

    def compose(self, application_id: str, selections: list[dict[str, Any]]) -> CareerResult:
        """Persist selection order and bounded draft wording in the existing application."""
        record = self._load(application_id)
        self._require_state(
            record, {"evaluated", "approved_for_draft", "drafted", "reviewed"}, "career composition"
        )
        if not selections:
            raise WoonError("career composition needs at least one reviewed selection")
        normalized: list[dict[str, Any]] = []
        identities: set[tuple[str, str]] = set()
        for selection in selections:
            item, ref = self._selection_source(record, selection)
            if (
                item.get("reviewed") is not True
                or item.get("classification") == "gap"
                or item.get("ownership") == "unknown"
            ):
                raise WoonError("career selection needs reviewed, known contribution evidence")
            if self._evidence.inspect(ref)["state"] != "current":
                raise WoonError("career selection evidence changed; re-evaluate before composing")
            identity = (str(ref["canonical_id"]), str(ref["section"]))
            if identity in identities:
                raise WoonError("career composition contains a duplicate contribution section")
            identities.add(identity)
            text = str(selection.get("text", "")).strip()
            limits = str(selection.get("limitations", "")).strip()
            max_chars = selection.get("max_chars")
            if (
                not text
                or not limits
                or type(max_chars) is not int
                or not 1 <= max_chars <= 8000
                or len(text) + len(limits) > max_chars
            ):
                raise WoonError("career wording and limitations must fit the explicit max_chars")
            normalized.append(
                {
                    "requirement": item["requirement"],
                    "canonical_id": ref["canonical_id"],
                    "section": ref["section"],
                    "text": text,
                    "limitations": limits,
                    "max_chars": max_chars,
                    "evidence_ref": ref,
                    "basis_sha256": self._selection_basis(item, ref),
                }
            )
        composition = {"selections": normalized, "sha256": digest(normalized)}
        if record.get("composition") == composition:
            return self._result(record, changed=False)
        record["composition"] = composition
        self._event(
            record, "지원 초안 조합 갱신", "검토한 기여의 선택·순서·분량을 조합; 제출본은 보존"
        )
        return self._save(record)

    def impact(self, application_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Read only affected compositions; never mutate evidence, PDFs or lifecycle state."""
        identifiers = (
            application_ids
            if application_ids is not None
            else sorted(path.stem for path in self._wiki_root.glob("*.md"))
        )
        affected = []
        for identifier in dict.fromkeys(identifiers):
            record = self._load(identifier)
            changes = self._composition_changes(record)
            if changes:
                affected.append(
                    {
                        "application_id": identifier,
                        "state": record["application_state"],
                        "requires_review": any(item["blocking"] for item in changes),
                        "changes": changes,
                    }
                )
        return affected

    def draft(self, application_id: str) -> dict[str, Any]:
        """Return a private preparation draft; this neither creates a PDF nor submits it."""
        record = self._load(application_id)
        self._require_state(
            record,
            {"approved_for_draft", "drafted", "reviewed", "ready"},
            "career draft preparation",
        )
        self._require_current_composition(record, required=True)
        lines = [
            f"# {record['company']} {record['role']} 초안",
            "",
            "> 검토용 초안. 근거 연결 검사는 문장 의미·개인 저자성 검토를 대신하지 않는다.",
            "",
        ]
        sources = []
        for selection in record["composition"]["selections"]:
            item, ref = self._selection_source(record, selection)
            current = self._evidence.inspect(ref, include_excerpt=True)
            if current["state"] != "current":
                raise WoonError("career evidence changed while preparing the draft")
            lines.extend(
                [
                    f"## {selection['section']}",
                    "",
                    selection["text"],
                    "",
                    f"- 기여 범위: {OWNERSHIP_LABELS[item['ownership']]}",
                    f"- JD 대조: {CLASSIFICATION_LABELS[item['classification']]} · "
                    f"{item['requirement']}",
                    f"- 한계: {selection['limitations']}",
                    f"- 검토 이유: {item['rationale']}",
                    f"- 근거: [[wiki/{ref['canonical_id']}#{ref['section']}]]",
                    "",
                ]
            )
            sources.append(
                {
                    **ref,
                    "current_revision": current["current_revision"],
                    "excerpt": current["excerpt"],
                    "checkout": current["checkout"],
                }
            )
            if any(code.get("scope") == "historical" for code in ref["code_refs"]):
                lines.extend(
                    [
                        "- 코드 범위: 과거 commit의 기여 근거다. 현재 서비스 기능을 뜻하지 않는다.",
                        "",
                    ]
                )
        return {
            "application_id": application_id,
            "status": "draft-preparation",
            "composition_sha256": record["composition"]["sha256"],
            "visibility": "private-local-only",
            "markdown": "\n".join(lines),
            "evidence": sources,
            "submitted": False,
        }

    def _selection_source(
        self,
        record: dict[str, Any],
        selection: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        matches = [
            item
            for item in record.get("requirements", [])
            if item.get("requirement") == selection.get("requirement")
        ]
        if len(matches) != 1:
            raise WoonError("career selection must identify one reviewed JD requirement")
        refs = [
            ref
            for ref in matches[0].get("evidence_refs", [])
            if (ref.get("canonical_id"), ref.get("section"))
            == (selection.get("canonical_id"), selection.get("section"))
        ]
        if len(refs) != 1:
            raise WoonError("career selection needs a revision-pinned canonical section")
        return matches[0], refs[0]

    def _selection_basis(self, item: dict[str, Any], ref: dict[str, Any]) -> str:
        return digest(
            {
                key: item.get(key)
                for key in ("requirement", "classification", "ownership", "rationale", "reviewed")
            }
            | {"evidence_ref": ref}
        )

    def _composition_changes(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        changes = []
        for index, selection in enumerate(record.get("composition", {}).get("selections", [])):
            try:
                item, ref = self._selection_source(record, selection)
                if self._selection_basis(item, ref) != selection["basis_sha256"]:
                    status = {"state": "changed", "reason": "reviewed requirement/evidence changed"}
                else:
                    status = self._evidence.inspect(selection["evidence_ref"])
            except WoonError as error:
                status = {"state": "unavailable", "reason": str(error)}
            if status["state"] != "current" or status.get("code_updates"):
                changes.append(
                    {
                        "selection": index,
                        "canonical_id": selection["canonical_id"],
                        "section": selection["section"],
                        "blocking": status["state"] != "current",
                        **status,
                    }
                )
        return changes

    def _require_current_composition(
        self, record: dict[str, Any], *, required: bool = False
    ) -> None:
        if required and not record.get("composition"):
            raise WoonError("career draft preparation requires a selected composition")
        if any(item["blocking"] for item in self._composition_changes(record)):
            raise WoonError("career composition is stale; inspect impact and re-evaluate/recompose")

    def approve_draft(self, application_id: str, *, confirmed: bool) -> CareerResult:
        if not confirmed:
            raise WoonError("career draft approval requires explicit confirmation")
        record = self._load(application_id)
        if record["application_state"] != "evaluated":
            raise WoonError("career draft approval requires evaluated state")
        requirements = record.get("requirements", [])
        if not requirements or not all(item.get("reviewed") is True for item in requirements):
            raise WoonError("career draft approval requires a fully reviewed requirement matrix")
        self._require_current_composition(record)
        self._transition(record, "approved_for_draft", "사용자가 지원 문서 작성 범위를 승인")
        return self._save(record)

    def attach_pdf(
        self,
        application_id: str,
        pdf_path: Path,
        *,
        kind: str,
        confirmed: bool = False,
    ) -> CareerResult:
        if kind not in {"draft", "submitted"}:
            raise WoonError("career PDF kind must be draft or submitted")
        pdf = pdf_path.expanduser().resolve()
        if not pdf.is_file() or pdf.suffix.casefold() != ".pdf":
            raise WoonError("career artifact must be an existing PDF")
        try:
            page_count = len(PdfReader(str(pdf)).pages)
        except Exception as error:
            raise WoonError(f"career PDF could not be validated: {error}") from error
        if page_count < 1:
            raise WoonError("career PDF must contain at least one page")

        record = self._load(application_id)
        state = str(record["application_state"])
        if kind == "draft" and state not in {"approved_for_draft", "drafted", "reviewed"}:
            raise WoonError("draft PDF requires approved_for_draft, drafted, or reviewed state")
        if kind == "submitted" and (not confirmed or state not in {"ready", "submitted"}):
            raise WoonError("submitted PDF requires ready state and explicit confirmation")
        if state != "submitted":
            self._require_current_composition(record)
        digest = _sha256(pdf.read_bytes())
        source_rel = self._application_source_relative(
            str(record["application_id"]), f"{kind}-{digest[:12]}.pdf"
        )
        source = self._vault / source_rel
        artifacts = list(record.get("artifacts", []))
        existing = next(
            (
                item
                for item in artifacts
                if item.get("kind") == kind and item.get("sha256") == digest
            ),
            None,
        )
        if existing is not None and (
            (
                kind == "draft"
                and state == "drafted"
                and existing.get("composition_sha256")
                == record.get("composition", {}).get("sha256")
            )
            or (kind == "submitted" and state == "submitted")
        ):
            return self._result(record, changed=False)
        if kind == "submitted" and state != "ready":
            raise WoonError("an existing submitted PDF cannot be replaced")
        if kind == "submitted" and record.get("composition"):
            drafts = [item for item in artifacts if item.get("kind") == "draft"]
            if (
                not drafts
                or drafts[-1]["sha256"] != digest
                or drafts[-1].get("composition_sha256") != record["composition"]["sha256"]
            ):
                raise WoonError("submitted PDF must match the reviewed current composition draft")
        if source.exists() and source.read_bytes() != pdf.read_bytes():
            raise WoonError("immutable career PDF source already contains different bytes")
        artifacts.append(
            {
                "kind": kind,
                "source": source_rel,
                "sha256": digest,
                "pages": page_count,
                "recorded_at": _now(),
            }
        )
        if record.get("composition"):
            artifacts[-1]["composition_sha256"] = record["composition"]["sha256"]
        record["artifacts"] = artifacts
        self._transition(
            record,
            "submitted" if kind == "submitted" else "drafted",
            "실제 제출본을 확인해 기록" if kind == "submitted" else "검증 가능한 PDF 초안을 연결",
        )
        self._commit_files(
            {
                source: pdf.read_bytes(),
                self._page_path(str(record["application_id"])): self._render(record),
            },
            expected=self._record_precondition(record),
        )
        return self._result(record, changed=True)

    def mark_reviewed(self, application_id: str, *, confirmed: bool) -> CareerResult:
        if not confirmed:
            raise WoonError("career review requires explicit confirmation")
        record = self._load(application_id)
        if record["application_state"] != "drafted":
            raise WoonError("career review requires drafted state")
        self._require_current_composition(record)
        if record.get("composition"):
            drafts = [item for item in record["artifacts"] if item.get("kind") == "draft"]
            if (
                not drafts
                or drafts[-1].get("composition_sha256") != record["composition"]["sha256"]
            ):
                raise WoonError("career review requires a PDF for the current composition")
        self._transition(record, "reviewed", "사용자가 PDF 초안의 내용과 표현을 검토")
        return self._save(record)

    def mark_ready(self, application_id: str, *, confirmed: bool) -> CareerResult:
        if not confirmed:
            raise WoonError("career ready transition requires explicit confirmation")
        record = self._load(application_id)
        if record["application_state"] != "reviewed":
            raise WoonError("career ready transition requires reviewed state")
        self._require_current_composition(record)
        self._transition(record, "ready", "사용자가 제출 가능한 최종본으로 승인")
        return self._save(record)

    def outcome(
        self,
        application_id: str,
        outcome: str,
        *,
        confirmed: bool,
        occurred_on: str,
        evidence: dict[str, str],
    ) -> CareerResult:
        if not confirmed:
            raise WoonError("career outcome requires explicit confirmation")
        if outcome not in {"interview", *TERMINAL_STATES}:
            raise WoonError(
                "career outcome must be interview, offer, rejected, withdrawn, or closed"
            )
        record = self._load(application_id)
        state = str(record["application_state"])
        if outcome == "interview" and state != "submitted":
            raise WoonError("interview outcome requires submitted state")
        same_outcome = state == outcome
        if (
            outcome in TERMINAL_STATES
            and state not in {"submitted", "interview"}
            and not same_outcome
        ):
            raise WoonError("terminal career outcome requires submitted or interview state")
        result_date = _iso_date(occurred_on, "career outcome occurred_on")
        normalized_evidence = self._outcome_evidence(evidence)
        record["outcome_evidence"] = normalized_evidence
        if outcome in TERMINAL_STATES:
            record["lifecycle_status"] = "completed"
            record["ended_on"] = result_date
            record.pop("occurred_on", None)
        reason = f"{result_date} · {normalized_evidence['summary']}"
        event_at = normalized_evidence.get("received_at", f"{result_date}T00:00:00+09:00")
        if same_outcome:
            self._replace_terminal_event(record, outcome, reason=reason, at=event_at)
        else:
            self._transition(record, outcome, reason, at=event_at)
        return self._save(record)

    def reopen(
        self,
        application_id: str,
        *,
        state: str,
        reason: str,
        confirmed: bool,
    ) -> CareerResult:
        """Correct an unsubmitted local workflow state without rewriting history."""

        if not confirmed:
            raise WoonError("career reopen requires explicit confirmation")
        if state not in {"discovered", "evaluated", "approved_for_draft", "drafted"}:
            raise WoonError("career reopen target must be a pre-submission state")
        if not reason.strip():
            raise WoonError("career reopen requires a correction reason")
        record = self._load(application_id)
        if record["application_state"] in {"submitted", "interview", *TERMINAL_STATES}:
            raise WoonError("submitted or terminal career records cannot be reopened automatically")
        self._transition(record, state, reason.strip())
        return self._save(record)

    def context(self, application_id: str, *, max_items: int = 12) -> ContextBundle:
        record = self._load(application_id)
        queries = tuple(
            dict.fromkeys(
                [
                    str(record["company"]),
                    str(record["role"]),
                    *[
                        str(item["requirement"])
                        for item in record.get("requirements", [])
                        if item.get("requirement")
                    ],
                ]
            )
        )
        knowledge = self._knowledge_service()
        knowledge.reindex()
        return build_context_bundle(knowledge, queries, max_items=max_items)

    def show(self, application_id: str) -> dict[str, Any]:
        return self._load(application_id)

    def _load(self, application_id: str) -> dict[str, Any]:
        page = self._page_path(self._identifier(application_id))
        if not page.is_file():
            raise WoonError(f"career application not found: {application_id}")
        raw_bytes = page.read_bytes()
        text = raw_bytes.decode("utf-8").replace("\r\n", "\n")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            raise WoonError(f"career application frontmatter is invalid: {application_id}")
        frontmatter = text.split("\n---\n", 1)[0][4:]
        try:
            record = yaml.safe_load(frontmatter)
        except yaml.YAMLError as error:
            raise WoonError(f"career application YAML is invalid: {error}") from error
        if not isinstance(record, dict) or record.get("application_id") != application_id:
            raise WoonError(f"career application identity mismatch: {application_id}")
        self._validate_record(record)
        self._loaded_revisions[page] = _sha256(raw_bytes)
        return record

    def _validate_record(self, record: dict[str, Any]) -> None:
        if record.get("schema_version") != 1:
            raise WoonError("career application schema_version must be 1")
        for field in ("company", "role", "application_state", "jd_source", "jd_sha256"):
            if not isinstance(record.get(field), str) or not str(record[field]).strip():
                raise WoonError(f"career application requires non-empty {field}")
        if "display_role" in record and (
            not isinstance(record["display_role"], str) or not record["display_role"].strip()
        ):
            raise WoonError("career application display_role must be non-empty text")
        if record["application_state"] not in STATES:
            raise WoonError(f"unknown career state: {record['application_state']}")
        composition = record.get("composition")
        if composition is not None:
            if (
                not isinstance(composition, dict)
                or not isinstance(composition.get("selections"), list)
                or not composition["selections"]
                or composition.get("sha256") != digest(composition["selections"])
            ):
                raise WoonError("career composition integrity is invalid")
            for selection in composition["selections"]:
                if (
                    not isinstance(selection, dict)
                    or not all(
                        isinstance(selection.get(key), str)
                        for key in (
                            "requirement",
                            "canonical_id",
                            "section",
                            "text",
                            "limitations",
                            "basis_sha256",
                        )
                    )
                    or not isinstance(selection.get("evidence_ref"), dict)
                ):
                    raise WoonError("career composition selection is invalid")
        expected_prefix = f"{self._application_source_relative(str(record['application_id']))}/"
        source_fields = [(str(record["jd_source"]), str(record["jd_sha256"]))]
        for collection in ("requirements", "artifacts", "history"):
            if not isinstance(record.get(collection), list):
                raise WoonError(f"career application {collection} must be a list")
        related_paths = record.get("related_paths", [])
        if not isinstance(related_paths, list):
            raise WoonError("career application related_paths must be a list")
        for related in related_paths:
            self._evidence_path(str(related))
        outcome_evidence = record.get("outcome_evidence")
        if outcome_evidence is not None:
            if not isinstance(outcome_evidence, dict):
                raise WoonError("career application outcome_evidence must be an object")
            self._outcome_evidence(
                {str(key): str(value) for key, value in outcome_evidence.items()}
            )
        for artifact in record["artifacts"]:
            if not isinstance(artifact, dict) or artifact.get("kind") not in {
                "draft",
                "submitted",
            }:
                raise WoonError("career application artifact is invalid")
            source_fields.append((str(artifact.get("source", "")), str(artifact.get("sha256", ""))))
        for source, expected_sha256 in source_fields:
            if not source.startswith(expected_prefix) or ".." in Path(source).parts:
                raise WoonError("career application source must stay in its private source root")
            resolved = (self._vault / source).resolve()
            try:
                resolved.relative_to(self._source_root / str(record["application_id"]))
            except ValueError as error:
                raise WoonError(
                    "career application source escapes its private source root"
                ) from error
            if not resolved.is_file():
                raise WoonError(f"career application source is missing: {source}")
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise WoonError(f"career application source hash is invalid: {source}")
            if _sha256(resolved.read_bytes()) != expected_sha256:
                raise WoonError(f"career application source hash mismatch: {source}")

    def _save(self, record: dict[str, Any]) -> CareerResult:
        page = self._page_path(str(record["application_id"]))
        before = page.read_bytes() if page.exists() else None
        content = self._render(record)
        if before == content:
            return self._result(record, changed=False)
        self._commit_files({page: content}, expected=self._record_precondition(record))
        return self._result(record, changed=True)

    def _record_precondition(self, record: dict[str, Any]) -> dict[Path, str | None]:
        path = self._page_path(str(record["application_id"]))
        return {path: self._loaded_revisions.get(path)}

    def _transition(
        self,
        record: dict[str, Any],
        state: str,
        reason: str,
        *,
        at: str | None = None,
    ) -> None:
        if state not in STATES:
            raise WoonError(f"unknown career state: {state}")
        previous = str(record["application_state"])
        record["application_state"] = state
        self._event(record, f"{previous} → {state}", reason, at=at)

    def _event(
        self,
        record: dict[str, Any],
        event: str,
        reason: str,
        *,
        at: str | None = None,
    ) -> None:
        record.setdefault("history", []).append(
            {"at": at or _now(), "event": event, "reason": reason}
        )

    def _replace_terminal_event(
        self,
        record: dict[str, Any],
        outcome: str,
        *,
        reason: str,
        at: str,
    ) -> None:
        suffix = f"→ {outcome}"
        for event in reversed(record.get("history", [])):
            if isinstance(event, dict) and str(event.get("event", "")).endswith(suffix):
                event["at"] = at
                event["reason"] = reason
                return
        raise WoonError("terminal career outcome is missing its history event")

    def _require_state(self, record: dict[str, Any], allowed: set[str], operation: str) -> None:
        state = str(record.get("application_state", ""))
        if state not in allowed:
            choices = ", ".join(sorted(allowed))
            raise WoonError(f"{operation} requires one of these states: {choices}")

    def _evidence_path(self, value: str) -> str:
        path = value.strip().replace("\\", "/")
        wiki_path(self._vault, path)
        return path

    def _related_link(self, value: str) -> str:
        path = self._evidence_path(value)
        text = (self._vault / path).read_text(encoding="utf-8")
        title = Path(path).stem
        if text.startswith("---\n") and "\n---\n" in text[4:]:
            metadata = yaml.safe_load(text.split("\n---\n", 1)[0][4:])
            if isinstance(metadata, dict) and str(metadata.get("title", "")).strip():
                title = str(metadata["title"]).strip()
        return f"[[{path[:-3]}|{title}]]"

    def _outcome_evidence(self, evidence: dict[str, str]) -> dict[str, str]:
        kind = str(evidence.get("kind", "")).strip()
        summary = str(evidence.get("summary", "")).strip()
        if kind not in OUTCOME_EVIDENCE_KINDS:
            raise WoonError("career outcome evidence kind must be email, portal, or manual")
        if not summary:
            raise WoonError("career outcome evidence summary must not be empty")
        normalized = {"kind": kind, "summary": summary}
        for key in ("subject", "sender", "locator", "received_at"):
            value = str(evidence.get(key, "")).strip()
            if value:
                normalized[key] = value
        locator = normalized.get("locator")
        if locator and not locator.startswith("https://"):
            raise WoonError("career outcome evidence locator must be an HTTPS URL")
        received_at = normalized.get("received_at")
        if received_at:
            try:
                datetime.fromisoformat(received_at)
            except ValueError as error:
                raise WoonError("career outcome evidence received_at must be ISO8601") from error
        if kind == "email" and not all(
            normalized.get(field) for field in ("subject", "sender", "locator", "received_at")
        ):
            raise WoonError(
                "email outcome evidence requires subject, sender, locator, and received_at"
            )
        return normalized

    def _page_path(self, application_id: str) -> Path:
        path = self._wiki_root / f"{application_id}.md"
        if not path.resolve().is_relative_to(self._vault / "wiki") or path.is_symlink():
            raise WoonError("career application path escapes its canonical root")
        return path

    def _application_source_relative(self, application_id: str, *parts: str) -> str:
        return private_source_relative(
            self._vault,
            "knowledge",
            "private",
            "career",
            "applications",
            application_id,
            *parts,
        ).as_posix()

    def _knowledge_service(self) -> KnowledgeService:
        if self._knowledge is not None:
            return self._knowledge
        _, service = build_knowledge_service(self._vault)
        return service

    def _identifier(self, value: str) -> str:
        normalized = value.strip()
        if not APPLICATION_ID.fullmatch(normalized):
            raise WoonError("career application id must use lowercase letters, digits, and hyphens")
        return normalized

    def _render(self, record: dict[str, Any]) -> bytes:
        title = _application_title(record)
        state = str(record["application_state"])
        requirements = record.get("requirements", [])
        has_unresolved_requirements = any(
            item.get("classification") == "gap" or item.get("reviewed") is not True
            for item in requirements
        )
        frontmatter = {
            **record,
            "type": "Wiki",
            "title": title,
            "canonical_id": f"personal/career/applications/{record['application_id']}",
            "record_owner": "choi-woonyoung",
            "publish": False,
            "access": "local-only",
            "status": "Active" if state not in TERMINAL_STATES else "Archived",
            "facets": ["커리어"],
            "node_kind": "detail",
            "view_mode": "project",
            "entity_kind": "career-application",
            "parent": "[[wiki/personal/career/README|커리어]]",
            "keywords": [title, record["company"], record["role"]],
            "aliases": list(record.get("aliases", [])),
            "updated": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
            "knowledge_state": (
                "근거 확인됨"
                if state in {"submitted", "interview", "offer"} and not has_unresolved_requirements
                else "확인 필요"
            ),
            "summary": (
                f"{record['company']} {record['role']} 지원의 JD 근거, "
                "문서 상태, 결과를 한곳에서 관리한다."
            ),
        }
        body = [
            "---",
            yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).rstrip(),
            "---",
            "",
            f"# {title}",
            "",
            "## 현재 상태",
            "",
            f"- 단계: {_state_label(state)}",
            f"- 기간: {_application_period(record)}",
            f"- 직무: {record['role']}",
        ]
        if record.get("deadline"):
            body.append(f"- 마감: {record['deadline']}")
        outcome_evidence = record.get("outcome_evidence")
        if isinstance(outcome_evidence, dict):
            locator = str(outcome_evidence.get("locator", "")).strip()
            subject = str(outcome_evidence.get("subject", "지원 결과 근거")).strip()
            evidence_label = f"[결과 메일 열기]({locator}) · {subject}" if locator else subject
            body.extend(
                [
                    f"- 결과 근거: {evidence_label}",
                    f"- 결과 요약: {outcome_evidence['summary']}",
                ]
            )
        body.extend(["", "## 지원 당시 자료", ""])
        body.append(f"- [[{record['jd_source']}|JD 자료]]")
        for artifact in record.get("artifacts", []):
            label = ARTIFACT_LABELS[str(artifact["kind"])]
            body.append(
                f"- [[{artifact['source']}|{label} PDF]] · {artifact['pages']}쪽 · "
                f"보관 {_display_time(str(artifact['recorded_at']))}"
            )
        if not record.get("artifacts"):
            body.append("- 아직 연결된 PDF가 없다.")
        related_paths = [str(path) for path in record.get("related_paths", [])]
        body.extend(f"- {self._related_link(path)}" for path in related_paths)
        body.extend(["", "PDF 보관 시각은 지원일이 아니다. 실제 제출본은 당시 원본으로 보존한다."])
        body.extend(
            [
                "",
                "## JD와 경력 근거 대조",
                "",
                "| 요구사항 | 판정 | 개인·팀 범위 | 이유 | 근거 |",
                "|---|---|---|---|---|",
            ]
        )
        for item in record.get("requirements", []):
            links = ", ".join(f"[[{path[:-3]}]]" for path in item.get("evidence_paths", [])) or "-"
            body.append(
                f"| {_cell(item['requirement'])} | "
                f"{CLASSIFICATION_LABELS[str(item['classification'])]} | "
                f"{OWNERSHIP_LABELS[str(item['ownership'])]} | "
                f"{_cell(item['rationale'])} | {links} |"
            )
        if not record.get("requirements"):
            body.append("| 아직 분석하지 않음 | - | - | JD 분석을 실행하면 후보가 나타난다. | - |")
        composition = record.get("composition")
        if composition:
            body.extend(
                [
                    "",
                    "## 초안 조합",
                    "",
                    "선택한 기여와 표현이다. 근거 변경 여부는 초안 생성·검토 전에 다시 확인한다.",
                    "",
                ]
            )
            for index, selection in enumerate(composition["selections"], 1):
                ref = selection["evidence_ref"]
                body.extend(
                    [
                        f"### {index}. {selection['section']}",
                        "",
                        selection["text"],
                        "",
                        f"- 한계: {selection['limitations']}",
                        f"- 분량: 본문·한계 {selection['max_chars']}자 이내",
                        f"- JD: {selection['requirement']}",
                        f"- 근거: [[wiki/{ref['canonical_id']}#{ref['section']}]] · "
                        f"`{ref['revision']}`",
                        "",
                    ]
                )
        body.extend(["", "## 시간 이력", ""])
        for event in record.get("history", []):
            body.append(
                f"- {_display_time(str(event['at']))} · "
                f"{_event_label(str(event['event']))} — {event['reason']}"
            )
        body.append("")
        return "\n".join(body).encode("utf-8")

    def _commit_files(
        self,
        files: dict[Path, bytes],
        *,
        expected: dict[Path, str | None] | None = None,
    ) -> None:
        backups: dict[Path, bytes | None] = {}
        with exclusive_file_lock(self._lock):
            for path, revision in (expected or {}).items():
                actual = _sha256(path.read_bytes()) if path.exists() else None
                if actual != revision:
                    raise WoonError("career application revision conflict; reload before writing")
            try:
                for path, data in files.items():
                    backups[path] = path.read_bytes() if path.exists() else None
                    atomic_write(
                        path,
                        data,
                        mode=self._file_mode(path),
                    )
            except Exception:
                for path, previous in reversed(backups.items()):
                    if previous is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_write(
                            path,
                            previous,
                            mode=self._file_mode(path),
                        )
                raise
            for path, data in files.items():
                if path.parent == self._wiki_root:
                    self._loaded_revisions[path] = _sha256(data)

    def _result(self, record: dict[str, Any], *, changed: bool) -> CareerResult:
        identifier = str(record["application_id"])
        return CareerResult(
            identifier,
            str(record["application_state"]),
            self._page_path(identifier).relative_to(self._vault).as_posix(),
            changed,
        )

    def _file_mode(self, path: Path) -> int:
        return 0o600 if path.resolve().is_relative_to(self._source_root) else 0o644


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).replace(microsecond=0).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_source_text(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in {".md", ".txt", ".yaml", ".yml", ".json"}:
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    raise WoonError("career JD analysis supports Markdown, text, YAML, JSON, or PDF")


def _requirement_lines(text: str, *, max_requirements: int) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"^[\s#>*+\-\d.)]+", "", raw).strip()
        if 12 <= len(line) <= 240 and line not in lines:
            lines.append(line)
        if len(lines) >= max_requirements:
            break
    if not lines:
        normalized = " ".join(text.split())
        if normalized:
            lines.append(normalized[:240])
    if not lines:
        raise WoonError("career JD source contains no readable requirements")
    return lines


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _state_label(state: str) -> str:
    return STATE_LABELS.get(state, state)


def _iso_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError as error:
        raise WoonError(f"{field} must be YYYY-MM-DD") from error


def _application_title(record: dict[str, Any]) -> str:
    role = record.get("display_role", record["role"])
    label = f"{record['company']} - {role}"
    terminal = record["application_state"] in TERMINAL_STATES
    day = record.get("ended_on") if terminal else record.get("started_on")
    if day:
        return f"{_iso_date(str(day), 'application display date')} - {label}"
    return label


def _application_period(record: dict[str, Any]) -> str:
    occurred_on = record.get("occurred_on")
    if occurred_on:
        return str(occurred_on)
    started_on = record.get("started_on")
    ended_on = record.get("ended_on")
    if started_on and ended_on:
        return f"{started_on} → {ended_on}"
    if started_on:
        return f"{started_on} →"
    if ended_on:
        return f"→ {ended_on}"
    return "확인되지 않음"


def _event_label(event: str) -> str:
    match = re.fullmatch(r"([^ ]+) → ([^ ]+)", event)
    if match is None:
        return event
    return f"{_state_label(match.group(1))} → {_state_label(match.group(2))}"


def _display_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    local = parsed.astimezone(ZoneInfo("Asia/Seoul"))
    return local.strftime("%Y년 %m월 %d일 %H:%M")
