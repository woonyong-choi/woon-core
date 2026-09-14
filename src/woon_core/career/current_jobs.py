"""Project reviewed official postings; no crawler, application or account side effects."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import yaml

from woon_core.career.evidence import CareerEvidence, bytes_digest, digest
from woon_core.errors import WoonError
from woon_core.io import atomic_write

JOBS_PARENT = "personal/career/job-search"
JOBS_BASE = "inbox/career/current-jobs.base"
JOBS_PRODUCER = "career-current-jobs-v1"
_MARKER = f"# woon_projection: {JOBS_PRODUCER}\n"
_CHECKS = {
    "company",
    "implementation",
    "experience",
    "location",
    "employment",
    "education",
    "availability",
}
_OWNERSHIP = {"personal", "team", "mixed", "post_project"}
_FRESHNESS = timedelta(hours=36)


def current_job_sources() -> dict[str, Any]:
    """Read the durable seed lists, not a frozen inventory of open vacancies."""
    path = Path(__file__).with_name("current-job-sources.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise WoonError("invalid current job source configuration")
    sources = data.get("sources")
    if not isinstance(sources, list) or not all(isinstance(x, dict) for x in sources):
        raise WoonError("current job sources must be an array")
    identifiers = set()
    for source in sources:
        identifier = _text(source.get("source_id"), "source_id")
        if identifier in identifiers:
            raise WoonError("duplicate current job source_id")
        identifiers.add(identifier)
        _text(source.get("company"), "company")
        _url(source.get("list_url"))
        _url(source.get("company_basis_url"))
    return data


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\0"):
        raise WoonError(f"current jobs {field} must be nonempty single-line text")
    return value.strip()


def _moment(value: Any, field: str) -> datetime:
    try:
        result = datetime.fromisoformat(_text(value, field))
    except ValueError as error:
        raise WoonError(f"current jobs {field} must be an ISO datetime") from error
    if result.utcoffset() is None:
        raise WoonError(f"current jobs {field} requires a timezone")
    return result


def _url(value: Any) -> str:
    url = _text(value, "public URL")
    try:
        parts = urlsplit(url)
        valid = (
            parts.scheme == "https"
            and parts.hostname
            and not parts.username
            and not parts.password
            and parts.port in {None, 443}
        )
    except ValueError as error:
        raise WoonError("current jobs URL is invalid") from error
    if not valid or any(c in url for c in '<>"\\ '):
        raise WoonError("current jobs requires credential-free HTTPS source URLs")
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))


def posting_id(record: dict[str, Any]) -> str:
    """Deduplicate exactly company/team/role/location/official URL; do not merge teams."""
    identity = [
        _text(record.get(field), field) for field in ("company", "team", "role", "location")
    ]
    return "job-" + digest([*identity, _url(record.get("official_url"))])[:24]


def _plain(value: str) -> str:
    # Reviewed JD text stays text, never raw HTML, Wiki embeds or Markdown actions.
    return re.sub(r"([\\`*_{\[\]}<>!#|])", r"\\\1", value)


def _summary(record: dict[str, Any]) -> str:
    if record.get("summary") is not None:
        summary = _text(record["summary"], "summary")
        if len(summary) > 64 or summary[-1] not in ".!?":
            raise WoonError("current jobs summary must be a complete sentence within 64 characters")
        return summary
    first = re.match(r".*?[.!?](?=\s|$)", record["gap"])
    if first and len(first[0]) <= 64:
        return first[0]
    return "공식 공고의 필수 조건과 현재 구현 근거를 대조한다."


class CurrentJobs:
    """Read-only writer payload preparation and one owned read-only Base projection.

    An agent must first read the official list and detail and review hard constraints.
    This class validates that review's structure, freshness and pinned personal evidence;
    it does not claim that successful HTTP access proves recruitment or eligibility.
    The single Wiki writer applies pages/removals with current revision checks.
    """

    def __init__(self, vault: Path, repositories: dict[str, Path] | None = None) -> None:
        self.vault = vault.expanduser().resolve()
        self.evidence = CareerEvidence(self.vault, repositories)

    def prepare(self, spec: dict[str, Any], *, now: datetime) -> dict[str, Any]:
        """Return deterministic page inputs and scoped removals without writing files."""
        if now.utcoffset() is None or spec.get("schema_version") != 1:
            raise WoonError("current jobs requires schema_version 1 and timezone-aware now")
        records = spec.get("records")
        if not isinstance(records, list) or not all(isinstance(x, dict) for x in records):
            raise WoonError("current jobs records must be an array of reviewed objects")
        unique: dict[str, dict[str, Any]] = {}
        for record in records:
            identifier = posting_id(record)
            if identifier in unique and digest(unique[identifier]) != digest(record):
                raise WoonError("conflicting duplicate posting; review before merging")
            unique[identifier] = record
        pages, omitted = [], []
        for identifier, record in sorted(unique.items()):
            page, reason = self._page(identifier, record, now)
            if page:
                pages.append(page)
            else:
                omitted.append({"posting_id": identifier, "reason": reason})
        previous = spec.get("previous_ids", [])
        if not isinstance(previous, list) or not all(
            isinstance(x, str) and re.fullmatch(r"job-[0-9a-f]{24}", x) for x in previous
        ):
            raise WoonError("previous_ids must contain only this producer's posting IDs")
        current = {page["metadata"]["posting_id"] for page in pages}
        reviewed = set(unique)
        removals = set(previous) - current
        if spec.get("scope_complete") is not True:
            removals &= reviewed  # Partial access must not imply unobserved jobs closed.
        return {
            "schema_version": 1,
            "producer": JOBS_PRODUCER,
            "recommended": sum(p["metadata"]["review_state"] == "recommended" for p in pages),
            "review": sum(p["metadata"]["review_state"] == "review" for p in pages),
            "pages": pages,
            "remove_from_current": [f"{JOBS_PARENT}/{x}" for x in sorted(removals)],
            "omitted": omitted,  # Run output only; never a Wiki exclusion archive.
            "base_path": JOBS_BASE,
            "base_content": render_jobs_base(),
            "index_body": f"![[{JOBS_BASE}#현재 공고]]\n",
        }

    def _page(
        self, identifier: str, record: dict[str, Any], now: datetime
    ) -> tuple[dict[str, Any] | None, str]:
        deadline = _moment(record["deadline"], "deadline") if record.get("deadline") else None
        if deadline and deadline <= now:
            return None, "확인된 마감 시각 경과"
        if record.get("posting_state") != "open":
            return None, "모집 중임을 확인하지 못함; 삭제·404 판정은 별도 근거 필요"
        if record.get("in_scope") is not True:
            return None, _text(record.get("scope_reason"), "scope_reason")
        if record.get("detail_verified") is not True or record.get("list_verified") is not True:
            return None, "공식 목록과 원문 양쪽의 현재 모집 확인이 필요함"
        observed = [
            _moment(record.get(field), field) for field in ("detail_checked_at", "list_checked_at")
        ]
        if any(moment > now for moment in observed):
            raise WoonError("current jobs observations cannot be in the future")
        if any(now - moment >= _FRESHNESS for moment in observed):
            return None, "공식 확인이 36시간 이상 지나 재확인 필요"
        checks = record.get("checks")
        if not isinstance(checks, dict) or not _CHECKS.issubset(checks):
            raise WoonError("current jobs requires all hard constraint checks")
        for key, check in checks.items():
            if not isinstance(check, dict) or check.get("state") not in {
                "verified",
                "unknown",
                "failed",
            }:
                raise WoonError(f"invalid current jobs constraint {key}")
            _text(check.get("basis"), f"{key} basis")
        if checks["company"]["state"] != "verified":
            return None, "기업의 투자·상장·인수 기준 근거 미확인"
        if any(check["state"] == "failed" for check in checks.values()):
            return None, "명시적 hard constraint 불충족"
        requirements = record.get("requirements")
        if not isinstance(requirements, list) or not all(isinstance(x, dict) for x in requirements):
            raise WoonError("current jobs requirements must be an array")
        all_verified = bool(requirements) and record.get("requirements_complete") is True
        for requirement in requirements:
            _text(requirement.get("requirement"), "requirement")
            _text(requirement.get("rationale"), "requirement rationale")
            classification = requirement.get("classification")
            if classification not in {"verified", "adjacent", "gap", "unknown"}:
                raise WoonError("invalid current jobs requirement classification")
            if classification == "verified":
                refs = requirement.get("evidence_refs")
                if (
                    requirement.get("ownership") not in _OWNERSHIP
                    or not isinstance(refs, list)
                    or not refs
                    or not all(isinstance(x, dict) for x in refs)
                ):
                    raise WoonError("verified requirements need ownership and pinned evidence")
                for reference in refs:
                    if str(reference.get("canonical_id", "")).startswith(JOBS_PARENT):
                        raise WoonError("generated job reviews are not personal career evidence")
                    self.evidence.validate(reference)
            else:
                all_verified = False
        recommended = all_verified and all(x["state"] == "verified" for x in checks.values())
        fields = (
            "company",
            "team",
            "role",
            "location",
            "employment",
            "experience",
            "education",
            "conditions",
            "work",
            "fit",
            "gap",
            "company_basis",
        )
        metadata: dict[str, Any] = {field: _text(record.get(field), field) for field in fields}
        metadata.update(
            {
                field: _url(record.get(field))
                for field in ("official_url", "list_url", "company_basis_url")
            }
        )
        title = " · ".join(metadata[field] for field in ("company", "team", "role"))
        canonical_id = f"{JOBS_PARENT}/{identifier}"
        metadata.update(
            {
                "canonical_id": canonical_id,
                "title": title,
                "type": "Wiki",
                "node_kind": "detail",
                "entity_kind": "career-job-posting",
                "view_mode": "article",
                "parent": f"[[wiki/{JOBS_PARENT}]]",
                "keywords": [title],
                "facets": ["커리어"],
                "summary": metadata["gap"],
                "purpose": "공식 공고 조건과 현재 개인 기여 근거를 대조해 지원 여부를 판단한다.",
                "access": "local-only",
                "publish": False,
                "publication_state": "private",
                "status": "Active",
                "posting_id": identifier,
                "job_producer": JOBS_PRODUCER,
                "posting_state": "open",
                "review_state": "recommended" if recommended else "review",
                "eligibility": "조건 충족 · 지원 권장"
                if recommended
                else "검토 · 지원 가능 미확정",
                "detail_verified": True,
                "list_verified": True,
                "detail_checked_at": observed[0].isoformat(),
                "list_checked_at": observed[1].isoformat(),
                "valid_until": min(
                    [x + _FRESHNESS for x in observed] + ([deadline] if deadline else [])
                ).isoformat(),
                "deadline": deadline.isoformat() if deadline else None,
                "posted_at": record.get("posted_at"),
                "checks": checks,
                "requirements": requirements,
                "requirements_complete": record.get("requirements_complete") is True,
                "in_scope": True,
            }
        )
        if record.get("process"):
            metadata["process"] = _text(record["process"], "process")
        condition_details = " · ".join(
            f"{label}: {metadata[field]}"
            for field, label in (
                ("employment", "고용"),
                ("experience", "경력"),
                ("education", "학력"),
                ("location", "근무지"),
            )
        )
        condition_details += (
            f" · 게시일: {metadata['posted_at'] or '미기재'}"
            f" · 마감: {metadata['deadline'] or '미기재'}"
        )
        sections = [
            ("조건", condition_details + ". " + metadata["conditions"]),
            ("하는 일", metadata["work"]),
            ("부합 근거", metadata["fit"]),
            ("핵심 격차", metadata["gap"]),
        ]
        body = f"{metadata['eligibility']}. [{_plain(title)}](<{metadata['official_url']}>)\n\n"
        body += "\n\n".join(f"## {heading}\n\n{_plain(text)}" for heading, text in sections)
        body += (
            f"\n\n## 기업 기준\n\n{_plain(metadata['company_basis'])} "
            f"[근거](<{metadata['company_basis_url']}>) · "
            f"[공식 채용 목록](<{metadata['list_url']}>)\n"
        )
        if metadata.get("process"):
            body += f"\n## 전형\n\n{_plain(metadata['process'])}\n"
        semantic = {
            key: value
            for key, value in metadata.items()
            if key not in {"detail_checked_at", "list_checked_at", "valid_until"}
        }
        semantic_sha256 = digest({"metadata": semantic, "body": body})
        # These reader fields are not new JD or personal evidence. Keep the v1
        # evidence fingerprint stable when only the display summary/date changes.
        metadata["summary"] = _summary(record)
        metadata["updated"] = now.date().isoformat()
        metadata["job_semantic_sha256"] = semantic_sha256
        return {
            "canonical_id": canonical_id,
            "metadata": metadata,
            "body": body,
            "semantic_sha256": semantic_sha256,
        }, ""

    def refresh_base(self) -> dict[str, Any]:
        """Write only the owned Base; caller must hold the Wiki writer window."""
        path = self.vault / JOBS_BASE
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise WoonError("career Base must not follow symlinks")
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        if previous is not None and not previous.startswith(_MARKER):
            raise WoonError("career Base is not owned by this producer")
        content = render_jobs_base()
        changed = previous != content
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content.encode(), mode=0o400)
        if path.read_text(encoding="utf-8") != content:
            raise WoonError("career Base readback mismatch")
        return {
            "changed": changed,
            "relative_path": JOBS_BASE,
            "sha256": bytes_digest(content.encode()),
        }


def render_jobs_base() -> str:
    """Formula-only cells cannot silently edit compiler-owned posting metadata."""
    columns = {
        "company": ("company", "회사"),
        "team": ("team", "팀·조직"),
        "role": ("link(official_url, role)", "직무 · 공식 원문"),
        "state": ("eligibility", "판정"),
        "location": ("location", "근무지"),
        "employment": ("employment", "고용형태"),
        "deadline": ('if(deadline, date(deadline).format("YYYY-MM-DD HH:mm"), "미기재")', "마감"),
        "checked": ('date(detail_checked_at).format("YYYY-MM-DD HH:mm")', "공식 확인일"),
        "conditions": ("conditions", "조건"),
        "work": ("work", "하는 일"),
        "fit": ("fit", "부합 근거"),
        "gap": ("gap", "핵심 격차"),
        "detail": ('file.asLink("조건·근거 상세")', "상세"),
    }
    data = {
        "filters": {
            "and": [
                'file.ext == "md"',
                f'job_producer == "{JOBS_PRODUCER}"',
                'entity_kind == "career-job-posting"',
                'access == "local-only"',
                "publish == false",
                'posting_state == "open"',
                "detail_verified == true",
                "list_verified == true",
                "date(valid_until) > now()",
                "(!deadline || date(deadline) > now())",
            ]
        },
        "formulas": {key: value for key, (value, _) in columns.items()},
        "properties": {
            "formula." + key: {"displayName": label} for key, (_, label) in columns.items()
        },
        "views": [
            {
                "type": "table",
                "name": name,
                "filters": filters,
                "groupBy": {"property": "formula.company", "direction": "ASC"},
                "order": ["formula." + field for field in fields],
            }
            for name, filters, fields in (
                (
                    "현재 공고",
                    'review_state == "recommended" || review_state == "review"',
                    [
                        "company",
                        "team",
                        "role",
                        "state",
                        "location",
                        "deadline",
                        "checked",
                        "detail",
                    ],
                ),
                (
                    "지원 권장",
                    'review_state == "recommended"',
                    ["company", "team", "role", "conditions", "fit", "gap", "deadline"],
                ),
                (
                    "조건·근거",
                    'review_state == "recommended" || review_state == "review"',
                    ["company", "role", "employment", "conditions", "work", "fit", "gap", "detail"],
                ),
            )
        ],
    }
    return _MARKER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=110)
