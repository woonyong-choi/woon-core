import copy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from woon_core.career.current_jobs import JOBS_BASE, JOBS_PARENT, CurrentJobs, posting_id
from woon_core.errors import WoonError

NOW = datetime.fromisoformat("2026-09-11T08:00:00+09:00")


def _record() -> dict:
    return {
        "company": "Product",
        "team": "팀명 미기재",
        "role": "Backend",
        "location": "서울",
        "employment": "정규직",
        "experience": "신입",
        "education": "무관",
        "official_url": "https://careers.example.com/jobs/123",
        "list_url": "https://careers.example.com/jobs",
        "company_basis_url": "https://example.com/ir",
        "company_basis": "상장 확인",
        "posting_state": "open",
        "in_scope": True,
        "detail_verified": True,
        "list_verified": True,
        "detail_checked_at": NOW.isoformat(),
        "list_checked_at": NOW.isoformat(),
        "deadline": None,
        "conditions": "한 언어 구현",
        "work": "서비스 API",
        "fit": "교육용 SQL 개인 구현",
        "gap": "현재 숙련도 확인 필요",
        "checks": {
            key: {"state": "verified", "basis": "검토한 조건"}
            for key in (
                "company",
                "implementation",
                "experience",
                "location",
                "employment",
                "education",
                "availability",
            )
        },
        "requirements_complete": True,
        "requirements": [
            {
                "requirement": "구현",
                "classification": "adjacent",
                "ownership": "personal",
                "rationale": "교육용 구현만 확인",
            }
        ],
    }


def test_review_expiry_dedupe_and_partial_removal_do_not_write(tmp_path: Path) -> None:
    producer = CurrentJobs(tmp_path)
    record = _record()
    unknown = "job-" + "a" * 24
    spec = {
        "schema_version": 1,
        "records": [record, copy.deepcopy(record)],
        "previous_ids": [posting_id(record), unknown],
    }
    result = producer.prepare(spec, now=NOW)
    assert len(result["pages"]) == 1 and result["recommended"] == 0
    assert result["pages"][0]["metadata"]["updated"] == "2026-09-11"
    assert result["pages"][0]["metadata"]["summary"].endswith(".")
    assert result["remove_from_current"] == []
    before = result["pages"][0]["semantic_sha256"]
    record["summary"] = "교육용 구현과 실무 조건의 차이를 검토한다."
    later = NOW + timedelta(hours=1)
    record["detail_checked_at"] = record["list_checked_at"] = later.isoformat()
    spec["records"] = [record]
    assert producer.prepare(spec, now=later)["pages"][0]["semantic_sha256"] == before
    record["deadline"] = "2026-09-07T16:00:00+09:00"
    expired = producer.prepare(spec, now=NOW)
    assert expired["pages"] == []
    assert expired["remove_from_current"] == [f"{JOBS_PARENT}/{posting_id(record)}"]
    record["deadline"] = None
    assert producer.prepare(spec, now=later + timedelta(hours=36))["pages"] == []
    conflicting = copy.deepcopy(record)
    conflicting["conditions"] = "다른 조건"
    spec["records"] = [record, conflicting]
    with pytest.raises(WoonError, match="conflicting duplicate"):
        producer.prepare(spec, now=later)
    assert list(tmp_path.iterdir()) == []


def test_recommendation_requires_current_non_generated_personal_evidence(tmp_path: Path) -> None:
    page = tmp_path / "wiki/projects/sql.md"
    page.parent.mkdir(parents=True)
    page.write_text("# SQL\n\n## 구현\n개인 저장 엔진 구현, production 운영 미검증.\n")
    producer = CurrentJobs(tmp_path)
    record = _record()
    requirement = record["requirements"][0]
    requirement["classification"] = "verified"
    spec = {"schema_version": 1, "records": [record]}
    with pytest.raises(WoonError, match="pinned evidence"):
        producer.prepare(spec, now=NOW)
    requirement["evidence_refs"] = [
        producer.evidence.capture({"canonical_id": "projects/sql", "section": "구현"})
    ]
    assert producer.prepare(spec, now=NOW)["recommended"] == 1
    record["checks"]["availability"]["state"] = "unknown"
    assert producer.prepare(spec, now=NOW)["recommended"] == 0
    page.write_text(page.read_text() + "새 근거 필요.\n")
    with pytest.raises(WoonError, match="revision changed"):
        producer.prepare(spec, now=NOW)
    requirement["evidence_refs"] = [{"canonical_id": JOBS_PARENT}]
    with pytest.raises(WoonError, match="not personal career evidence"):
        producer.prepare(spec, now=NOW)


def test_base_is_owned_idempotent_and_never_overwrites_user_file(tmp_path: Path) -> None:
    producer = CurrentJobs(tmp_path)
    assert producer.refresh_base()["changed"] is True
    assert producer.refresh_base()["changed"] is False
    path = tmp_path / JOBS_BASE
    data = yaml.safe_load(path.read_text())
    assert all(field.startswith("formula.") for view in data["views"] for field in view["order"])
    assert "date(valid_until) > now()" in data["filters"]["and"]
    path.chmod(0o600)
    path.write_text("filters: []\n")
    with pytest.raises(WoonError, match="not owned"):
        producer.refresh_base()
    assert path.read_text() == "filters: []\n"
