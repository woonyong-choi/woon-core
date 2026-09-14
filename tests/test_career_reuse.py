import hashlib
import json
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

import woon_core.career.service as service_module
from woon_core.career.cli import run_career
from woon_core.career.evidence import CareerEvidence
from woon_core.career.service import CareerApplicationService
from woon_core.errors import WoonError


def _setup(tmp_path: Path) -> tuple[CareerApplicationService, Path, Path]:
    vault = tmp_path / "vault"
    page = vault / "wiki/projects/sql.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ncanonical_id: projects/sql\ntitle: SQL\naccess: local-only\npublish: false\n---\n"
        "# SQL\n\n## 저장 경로\n팀 학습에서 저장 경로를 검토했다. WAL 복구는 미지원.\n\n"
        "## 인덱스\n인덱스 선택을 비교했다. 운영 성능은 미검증.\n",
        encoding="utf-8",
    )
    jd = tmp_path / "jd.md"
    jd.write_text("SQL 데이터 경로를 구현할 수 있는 개발자", encoding="utf-8")
    service = CareerApplicationService(vault)
    for identifier in ("sql-backend", "sql-data"):
        service.create(application_id=identifier, company="Example", role="Backend", jd_path=jd)
    return service, vault, page


def _matrix(ref: dict, *, ownership: str = "team", classification: str = "adjacent") -> list[dict]:
    return [
        {
            "requirement": "SQL 구현",
            "classification": classification,
            "ownership": ownership,
            "rationale": "학습과 개인 구현 범위를 구분해 검토했다.",
            "evidence_refs": [ref],
        }
    ]


def _selection(section: str = "저장 경로", **values: object) -> list[dict]:
    return [
        {
            "requirement": "SQL 구현",
            "canonical_id": "projects/sql",
            "section": section,
            "text": "팀 SQL 엔진 학습에서 저장 경로를 검토.",
            "limitations": "교육용이며 개인 단독 구현 또는 WAL 복구를 주장하지 않는다.",
            "max_chars": 240,
            **values,
        }
    ]


def _prepare(
    service: CareerApplicationService, identifier: str, section: str = "저장 경로"
) -> dict:
    ref = service.evidence({"canonical_id": "projects/sql", "section": section})
    service.evaluate(identifier, _matrix(ref))
    service.compose(identifier, _selection(section))
    return ref


def _pdf(path: Path) -> Path:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def test_section_change_only_affects_its_composition_and_resume_is_idempotent(
    tmp_path: Path,
) -> None:
    service, vault, page = _setup(tmp_path)
    first = _prepare(service, "sql-backend")
    _prepare(service, "sql-data", "인덱스")
    with pytest.raises(WoonError, match="draft preparation requires"):
        service.draft("sql-backend")
    service.approve_draft("sql-backend", confirmed=True)
    resumed = CareerApplicationService(vault)
    before = (vault / "wiki/personal/career/applications/sql-backend.md").read_bytes()
    assert resumed.compose("sql-backend", _selection()).changed is False
    assert (vault / "wiki/personal/career/applications/sql-backend.md").read_bytes() == before
    page.write_text(page.read_text().replace("인덱스 선택을 비교했다.", "인덱스 후보를 정정했다."))
    assert [item["application_id"] for item in resumed.impact()] == ["sql-data"]
    draft = resumed.draft("sql-backend")
    assert draft["visibility"] == "private-local-only" and draft["submitted"] is False
    assert "팀 성과" in draft["markdown"] and "인접 근거" in draft["markdown"]
    assert "WAL 복구를 주장하지 않는다" in draft["markdown"]
    assert draft["evidence"][0]["revision"] == first["revision"]
    assert draft["evidence"][0]["current_revision"] != first["revision"]
    assert (vault / "wiki/personal/career/applications/sql-backend.md").read_bytes() == before
    updated = resumed.evidence({"canonical_id": "projects/sql", "section": "인덱스"})
    resumed.evaluate("sql-data", _matrix(updated))
    resumed.compose("sql-data", _selection("인덱스"))
    assert resumed.impact() == []


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "sql-code"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    source = repo / "engine.c"
    source.write_text("int lookup(void) { return 1; }\n")
    subprocess.run(["git", "-C", str(repo), "add", "engine.c"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    return repo, source, commit


def test_code_change_is_reported_without_network_or_authorship_inference(tmp_path: Path) -> None:
    _, vault, _ = _setup(tmp_path)
    repo, source, commit = _repository(tmp_path)
    service = CareerApplicationService(vault, repositories={"sql": repo})
    ref = service.evidence(
        {
            "canonical_id": "projects/sql",
            "section": "저장 경로",
            "code_refs": [{"locator": "repo://sql/engine.c", "commit": commit, "scope": "current"}],
        }
    )
    service.evaluate(
        "sql-backend", _matrix(ref, ownership="post_project", classification="verified")
    )
    service.compose("sql-backend", _selection())
    service.approve_draft("sql-backend", confirmed=True)
    assert "종료 후 개인 확장" in service.draft("sql-backend")["markdown"]
    _prepare(service, "sql-data", "인덱스")
    source.write_text("int lookup(void) { return 2; }\n")
    assert [item["application_id"] for item in service.impact()] == ["sql-backend"]
    with pytest.raises(WoonError, match="composition is stale"):
        service.draft("sql-backend")


def test_historical_commit_survives_checkout_change_and_removal_with_submitted_pdf(
    tmp_path: Path,
) -> None:
    _, vault, _ = _setup(tmp_path)
    repo, source, commit = _repository(tmp_path)
    service = CareerApplicationService(vault, repositories={"sql": repo})
    spec = {
        "canonical_id": "projects/sql",
        "section": "저장 경로",
        "code_refs": [{"locator": "repo://sql/engine.c", "commit": commit, "scope": "historical"}],
    }
    ref = service.evidence(spec)
    service.evaluate("sql-backend", _matrix(ref, ownership="personal", classification="verified"))
    service.compose("sql-backend", _selection())
    service.approve_draft("sql-backend", confirmed=True)
    source.write_text("int lookup(void) { return 9; }\n")
    assert service.impact(["sql-backend"])[0]["requires_review"] is False
    assert "과거 commit" in service.draft("sql-backend")["markdown"]
    pdf = _pdf(tmp_path / "historical.pdf")
    service.attach_pdf("sql-backend", pdf, kind="draft")
    service.mark_reviewed("sql-backend", confirmed=True)
    service.mark_ready("sql-backend", confirmed=True)
    service.attach_pdf("sql-backend", pdf, kind="submitted", confirmed=True)
    submitted = service.show("sql-backend")["artifacts"][-1]
    before = (vault / submitted["source"]).read_bytes()
    source.unlink()
    # The file still exists in the reviewed Git object and can support a new application.
    assert service.evidence(spec) == ref
    service.evaluate("sql-data", _matrix(ref, ownership="personal", classification="verified"))
    service.compose("sql-data", _selection())
    service.approve_draft("sql-data", confirmed=True)
    draft = service.draft("sql-data")
    assert draft["evidence"][0]["checkout"][0]["state"] == "missing"
    assert service.show("sql-backend")["application_state"] == "submitted"
    assert (vault / submitted["source"]).read_bytes() == before
    assert all(item["requires_review"] is False for item in service.impact())


def test_display_metadata_does_not_revoke_evidence_but_dates_and_rights_do(tmp_path: Path) -> None:
    service, _, page = _setup(tmp_path)
    page.write_text(
        page.read_text().replace(
            "publish: false",
            "publish: false\nllm_wiki: {schema_version: 1, build_id: first, page_id: projects/sql}",
        )
    )
    _prepare(service, "sql-backend")
    service.approve_draft("sql-backend", confirmed=True)
    displayed = (
        page.read_text()
        .replace("title: SQL", "title: SQL 엔진\naliases: [MiniDB]")
        .replace("build_id: first", "build_id: second")
    )
    page.write_text(displayed)
    assert service.impact() == []
    assert service.draft("sql-backend")["status"] == "draft-preparation"
    page.write_text(displayed.replace("schema_version: 1", "schema_version: 2"))
    assert service.impact()[0]["requires_review"] is True
    page.write_text(
        displayed.replace("access: local-only", "occurred_on: 2026-04-22\naccess: local-only")
    )
    assert service.impact()[0]["requires_review"] is True
    page.write_text(displayed.replace("publish: false", "publish: true"))
    with pytest.raises(WoonError, match="composition is stale"):
        service.draft("sql-backend")


def test_historical_blob_is_independent_of_unreadable_checkout_and_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, vault, _ = _setup(tmp_path)
    repo, source, commit = _repository(tmp_path)
    evidence = CareerEvidence(vault, repositories={"sql": repo})
    spec = {
        "canonical_id": "projects/sql",
        "section": "저장 경로",
        "code_refs": [{"locator": "repo://sql/engine.c", "commit": commit}],
    }
    reference = evidence.capture(spec)
    read_bytes = Path.read_bytes
    git = evidence._git

    def unavailable_source(path: Path) -> bytes:
        if path == source:
            raise PermissionError("checkout unavailable")
        return read_bytes(path)

    def unavailable_head(root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        if arguments == ("rev-parse", "HEAD"):
            raise WoonError("HEAD unavailable")
        return git(root, *arguments)

    monkeypatch.setattr(Path, "read_bytes", unavailable_source)
    monkeypatch.setattr(evidence, "_git", unavailable_head)
    assert evidence.validate(reference) == reference
    result = evidence.inspect(reference, include_excerpt=True)
    assert result["state"] == "current"
    assert result["checkout"][0]["state"] == "unavailable"
    assert result["checkout"][0]["head"] is None
    current_ref = {**reference, "code_refs": [{**reference["code_refs"][0], "scope": "current"}]}
    with pytest.raises(WoonError, match="current career code differs"):
        evidence.validate(current_ref)


def test_exact_heading_ignores_code_fences_and_preserves_language_hash(tmp_path: Path) -> None:
    service, _, page = _setup(tmp_path)
    page.write_text(
        page.read_text() + "\n```text\n## C#\n```not-a-close\n"
        "## C#\n```\n\n## C#\n실제 언어 절의 근거.\n"
    )
    ref = service.evidence({"canonical_id": "projects/sql", "section": "C#"})
    assert ref["section"] == "C#"


def test_stale_evidence_blocks_new_submission_but_preserves_submitted_bytes(tmp_path: Path) -> None:
    service, vault, page = _setup(tmp_path)
    _prepare(service, "sql-backend")
    service.approve_draft("sql-backend", confirmed=True)
    pdf = _pdf(tmp_path / "draft.pdf")
    service.attach_pdf("sql-backend", pdf, kind="draft")
    service.mark_reviewed("sql-backend", confirmed=True)
    original = page.read_bytes()
    page.write_bytes(original.replace("미지원".encode(), "범위 정정".encode()))
    with pytest.raises(WoonError, match="composition is stale"):
        service.mark_ready("sql-backend", confirmed=True)
    page.write_bytes(original)
    service.mark_ready("sql-backend", confirmed=True)
    service.attach_pdf("sql-backend", pdf, kind="submitted", confirmed=True)
    record = service.show("sql-backend")
    submitted = vault / record["artifacts"][-1]["source"]
    submitted_bytes = submitted.read_bytes()
    page.unlink()
    assert service.impact(["sql-backend"])[0]["state"] == "submitted"
    assert service.show("sql-backend")["application_state"] == "submitted"
    assert not service.attach_pdf("sql-backend", pdf, kind="submitted", confirmed=True).changed
    assert submitted.read_bytes() == submitted_bytes
    with pytest.raises(WoonError, match="composition requires"):
        service.compose("sql-backend", _selection())


def test_composition_rollback_and_optimistic_conflict_preserve_other_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, vault, _ = _setup(tmp_path)
    _prepare(service, "sql-backend")
    target = vault / "wiki/personal/career/applications/sql-backend.md"
    before = target.read_bytes()
    original_write = service_module.atomic_write
    failed = False

    def fail_once(path: Path, data: bytes, *, mode: int) -> None:
        nonlocal failed
        if path == target and not failed:
            failed = True
            raise OSError("interrupted composition")
        original_write(path, data, mode=mode)

    monkeypatch.setattr(service_module, "atomic_write", fail_once)
    with pytest.raises(OSError, match="interrupted"):
        service.compose("sql-backend", _selection(text="저장 경로의 범위를 더 좁힌 초안."))
    assert target.read_bytes() == before
    stale = service._load("sql-backend")
    other = CareerApplicationService(vault)
    assert other.compose("sql-backend", _selection(text="검토한 다음 초안.")).changed
    updated = target.read_bytes()
    stale["company"] = "stale overwrite"
    with pytest.raises(WoonError, match="revision conflict"):
        service._save(stale)
    assert target.read_bytes() == updated
    assert not other.compose("sql-backend", _selection(text="검토한 다음 초안.")).changed


def test_privacy_revision_and_uncertain_evidence_fail_closed(tmp_path: Path) -> None:
    service, vault, page = _setup(tmp_path)
    spec = {"canonical_id": "projects/sql", "section": "저장 경로"}
    ref = service.evidence(spec)
    with pytest.raises(WoonError, match="revision changed"):
        service.evaluate("sql-backend", _matrix({**ref, "revision": "0" * 64}))
    with pytest.raises(WoonError, match="known ownership"):
        service.evaluate(
            "sql-backend", _matrix(ref, ownership="unknown", classification="verified")
        )
    service.evaluate("sql-backend", _matrix(ref, ownership="unknown"))
    with pytest.raises(WoonError, match="known contribution"):
        service.compose("sql-backend", _selection())
    service.evaluate("sql-backend", _matrix(ref))
    with pytest.raises(WoonError, match="max_chars"):
        service.compose("sql-backend", _selection(max_chars=1))
    with pytest.raises(WoonError, match="duplicate contribution"):
        service.compose("sql-backend", _selection() * 2)
    outside = tmp_path / "private.md"
    outside.write_text("# Private\n## 비밀\n밖의 자료\n")
    (vault / "wiki/projects/escape.md").symlink_to(outside)
    with pytest.raises(WoonError, match="escapes"):
        service.evidence({"canonical_id": "projects/escape", "section": "비밀"})
    with pytest.raises(WoonError, match="generated applications"):
        service.evidence(
            {"canonical_id": "personal/career/applications/sql-data", "section": "현재 상태"}
        )
    page.write_text(page.read_text() + "\n## 저장 경로\n중복 제목\n")
    with pytest.raises(WoonError, match="unambiguous"):
        service.evidence(spec)


def test_cli_reuses_pins_and_returns_private_draft_without_creating_artifacts(
    tmp_path: Path,
) -> None:
    service, vault, _ = _setup(tmp_path)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"canonical_id": "projects/sql", "section": "저장 경로"}))
    output = StringIO()
    run_career(["evidence", "--vault", str(vault), "--spec", str(spec)], output)
    ref = json.loads(output.getvalue())
    matrix = tmp_path / "matrix.json"
    matrix.write_text(json.dumps(_matrix(ref)))
    run_career(
        ["evaluate", "--vault", str(vault), "--id", "sql-backend", "--matrix", str(matrix)],
        StringIO(),
    )
    selected = tmp_path / "selection.json"
    selected.write_text(json.dumps(_selection()))
    run_career(
        ["compose", "--vault", str(vault), "--id", "sql-backend", "--selections", str(selected)],
        StringIO(),
    )
    service.approve_draft("sql-backend", confirmed=True)
    output = StringIO()
    run_career(["draft", "--vault", str(vault), "--id", "sql-backend"], output)
    draft = json.loads(output.getvalue())
    assert len(draft["composition_sha256"]) == hashlib.sha256().digest_size * 2
    assert draft["submitted"] is False and service.show("sql-backend")["artifacts"] == []
    assert not list((vault / ".local").rglob("*.json"))
