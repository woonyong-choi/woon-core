from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.source_compaction import (
    apply_retention_timestamps,
    apply_source_body_compaction,
    git_supersede_dates,
    plan_retention_compaction,
    plan_retention_timestamps,
    plan_source_body_compaction,
    read_source_record,
    validate_compacted_source,
    validate_utc_timestamp,
)


def _catalog(vault: Path) -> dict:
    root = vault / "catalog/llm-wiki"
    root.mkdir(parents=True)
    old = {
        "source_id": "old",
        "kind": "curated-wiki",
        "privacy": "public",
        "lifecycle": "archived",
        "superseded_by": "current",
        "body": "Earlier explanation\n",
        "original_sha256": "0" * 64,
        "locator": "wiki/concept.md",
        "purpose": "Reuse",
    }
    old["normalized_sha256"] = hashlib.sha256(old["body"].encode()).hexdigest()
    current = {**old, "source_id": "current", "lifecycle": "compiled"}
    current.pop("superseded_by")
    for name, value in {
        "sources.yaml": {"sources": [old, current]},
        "pages.yaml": {"pages": [{"source_ids": ["current"]}]},
        "claims.yaml": {"claims": [{"source_ids": ["old"], "status": "superseded"}]},
    }.items():
        (root / name).write_text(yaml.safe_dump(value))
    return current


def _plan(vault: Path, current: dict, **kwargs) -> dict:
    return plan_source_body_compaction(
        vault,
        **{
            "source_ids": ("old",),
            "protected_source_ids": (),
            "reviewed_successors": {
                "old": {
                    "source_id": "current",
                    "normalized_sha256": current["normalized_sha256"],
                }
            },
            "review_reference": "owner:reviewed-meaning",
            **kwargs,
        },
    )


def test_source_plan_is_read_only_and_terminal_records_keep_identity(tmp_path: Path) -> None:
    current = _catalog(tmp_path)
    paths = tuple((tmp_path / "catalog/llm-wiki").glob("*.yaml"))
    before = {p: p.read_bytes() for p in paths}
    plan = _plan(tmp_path, current)
    assert plan["ready"] and not plan["writes"]
    assert {p: p.read_bytes() for p in paths} == before
    compact = plan["results"][0]["replacement"]
    assert "body" not in compact
    assert compact["normalized_sha256"] == current["normalized_sha256"]
    assert read_source_record(compact)["body_available"] is False
    assert read_source_record(compact)["deleted_bytes_recoverable"] is False
    active = {**compact, "lifecycle": "compiled"}
    with pytest.raises(WoonError, match="invalid compact"):
        validate_compacted_source(active)


@pytest.mark.parametrize("blocker", ["accepted", "page", "private", "protected", "review", "cycle"])
def test_current_original_protected_and_unreviewed_sources_are_preserved(
    tmp_path: Path,
    blocker: str,
) -> None:
    current = _catalog(tmp_path)
    root = tmp_path / "catalog/llm-wiki"
    kwargs = {}
    if blocker == "accepted":
        (root / "claims.yaml").write_text(
            yaml.safe_dump(
                {
                    "claims": [{"source_ids": ["old"], "status": "accepted"}],
                }
            )
        )
    elif blocker == "page":
        (root / "pages.yaml").write_text(
            yaml.safe_dump(
                {
                    "pages": [{"source_ids": ["old", "current"]}],
                }
            )
        )
    elif blocker in {"private", "cycle"}:
        data = yaml.safe_load((root / "sources.yaml").read_text())
        data["sources"][0].update(
            {"privacy": "private"} if blocker == "private" else {"superseded_by": "old"},
        )
        (root / "sources.yaml").write_text(yaml.safe_dump(data))
    elif blocker == "protected":
        kwargs["protected_source_ids"] = ("old",)
    else:
        kwargs["reviewed_successors"] = {
            "old": {"source_id": "current", "normalized_sha256": "0" * 64}
        }
    plan = _plan(tmp_path, current, **kwargs)
    assert not plan["ready"]
    assert plan["results"][0]["status"] == "blocked"
    assert "replacement" not in plan["results"][0]


def test_compact_marker_cannot_be_used_to_hide_retained_body(tmp_path: Path) -> None:
    current = _catalog(tmp_path)
    compact = copy.deepcopy(_plan(tmp_path, current)["results"][0]["replacement"])
    compact["body"] = "Hidden replacement"
    with pytest.raises(WoonError, match="invalid compact"):
        read_source_record(compact)


def _sources(vault: Path) -> dict:
    return yaml.safe_load((vault / "catalog/llm-wiki/sources.yaml").read_text())


def _write_sources(vault: Path, data: dict) -> None:
    (vault / "catalog/llm-wiki/sources.yaml").write_text(yaml.safe_dump(data))


def _insert_middle(vault: Path) -> None:
    """old -> middle (archived) -> current: the review must name the terminal record."""
    data = _sources(vault)
    old, current = data["sources"]
    middle = {**old, "source_id": "middle", "superseded_by": "current"}
    old["superseded_by"] = "middle"
    data["sources"] = [old, middle, current]
    _write_sources(vault, data)


def test_chain_is_followed_to_terminal_successor(tmp_path: Path) -> None:
    current = _catalog(tmp_path)
    _insert_middle(tmp_path)
    plan = _plan(tmp_path, current)
    assert plan["ready"], plan
    marker = plan["results"][0]["replacement"]["body_retention"]
    assert marker["successor_id"] == "current"
    # Attesting the intermediate revision is not a review of what is current.
    blocked = _plan(
        tmp_path,
        current,
        reviewed_successors={
            "old": {"source_id": "middle", "normalized_sha256": current["normalized_sha256"]}
        },
    )
    assert blocked["results"][0] == {
        "source_id": "old",
        "status": "blocked",
        "reason": "successor-not-current-or-not-reviewed",
    }


def test_successor_cycle_blocks_with_its_own_reason(tmp_path: Path) -> None:
    current = _catalog(tmp_path)
    _insert_middle(tmp_path)
    data = _sources(tmp_path)
    data["sources"][1]["superseded_by"] = "old"
    _write_sources(tmp_path, data)
    plan = _plan(tmp_path, current)
    assert plan["results"][0] == {
        "source_id": "old",
        "status": "blocked",
        "reason": "successor-cycle",
    }


def test_apply_refuses_catalog_drift_and_tampered_plans(tmp_path: Path) -> None:
    current = _catalog(tmp_path)
    plan = _plan(tmp_path, current)
    path = tmp_path / "catalog/llm-wiki/pages.yaml"
    before = path.read_bytes()
    path.write_bytes(before + b"# touched\n")
    with pytest.raises(WoonError, match="changed since the plan"):
        apply_source_body_compaction(tmp_path, plan)
    path.write_bytes(before)
    for broken in ({**plan, "writes": True}, {**plan, "ready": False}):
        with pytest.raises(WoonError, match="ready read-only plan"):
            apply_source_body_compaction(tmp_path, broken)
    tampered = copy.deepcopy(plan)
    tampered["results"][0]["replacement"]["purpose"] = "Rewritten"
    with pytest.raises(WoonError, match="no longer matches"):
        apply_source_body_compaction(tmp_path, tampered)
    assert "body" in _sources(tmp_path)["sources"][0]


def test_apply_replaces_only_planned_records(tmp_path: Path) -> None:
    current = _catalog(tmp_path)
    plan = json.loads(json.dumps(_plan(tmp_path, current)))
    sources_path = tmp_path / "catalog/llm-wiki/sources.yaml"
    others = {
        name: (tmp_path / "catalog/llm-wiki" / name).read_bytes()
        for name in ("pages.yaml", "claims.yaml")
    }
    before_records = _sources(tmp_path)["sources"]
    result = apply_source_body_compaction(tmp_path, plan)
    assert result["writes"] is True
    assert result["applied"] == ["old"]
    assert result["bytes_removed"] == len(before_records[0]["body"].encode())
    assert result["sources_sha256_before"] != result["sources_sha256_after"]
    assert result["sources_sha256_after"] == hashlib.sha256(sources_path.read_bytes()).hexdigest()
    after = _sources(tmp_path)
    assert list(after) == ["sources"]
    assert after["sources"][0] == plan["results"][0]["replacement"]
    assert after["sources"][1] == before_records[1]
    assert {n: (tmp_path / "catalog/llm-wiki" / n).read_bytes() for n in others} == others
    # A second apply of the same plan is refused: the catalog moved on.
    with pytest.raises(WoonError, match="changed since the plan"):
        apply_source_body_compaction(tmp_path, plan)
    # Rewriting the catalog again with the shared dumper is byte-stable.
    second = plan_retention_timestamps(tmp_path, {"old": "2025-01-01T00:00:00Z"})
    apply_retention_timestamps(tmp_path, second)
    assert _sources(tmp_path)["sources"][0]["archived_at"] == "2025-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("2025-01-01T00:00:00Z", True),
        ("2025-01-01T00:00:00.123456Z", True),
        ("2025-01-01T00:00:00+09:00", False),
        ("2025-13-01T00:00:00Z", False),
        (20250101, False),
    ],
)
def test_validators_accept_utc_timestamps(value: object, ok: bool) -> None:
    from woon_core.knowledge.compiled_wiki import _validate_claim_record, _validate_source

    source = {
        "source_id": "old",
        "kind": "curated-wiki",
        "locator": "wiki/concept.md",
        "original_sha256": "0" * 64,
        "normalized_sha256": hashlib.sha256(b"x\n").hexdigest(),
        "privacy": "public",
        "lifecycle": "archived",
        "superseded_by": "current",
        "purpose": "Reuse",
        "body": "x\n",
        "archived_at": value,
    }
    claim = {
        "claim_id": "c1",
        "kind": "statement",
        "statement": "s",
        "status": "superseded",
        "superseded_by": "c2",
        "source_ids": ["old"],
        "markdown": "s",
        "superseded_at": value,
    }
    if ok:
        _validate_source(source)
        _validate_claim_record(claim)
        validate_utc_timestamp(source, "archived_at")
    else:
        with pytest.raises(WoonError, match="ISO-8601"):
            _validate_source(source)
        with pytest.raises(WoonError, match="ISO-8601"):
            _validate_claim_record(claim)
    with pytest.raises(WoonError, match="only archived"):
        _validate_source({**source, "lifecycle": "compiled", "archived_at": "2025-01-01T00:00:00Z"})
    with pytest.raises(WoonError, match="only superseded"):
        _validate_claim_record(
            {**claim, "status": "accepted", "superseded_at": "2025-01-01T00:00:00Z"}
        )


def test_timestamp_plan_only_fills_missing_fields_and_git_dates_are_approximate(
    tmp_path: Path,
) -> None:
    _catalog(tmp_path)
    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims_path.write_text(
        yaml.safe_dump(
            {
                "claims": [
                    {"claim_id": "c1", "status": "superseded", "superseded_by": "c2"},
                    {"claim_id": "c2", "status": "accepted", "source_ids": ["current"]},
                ]
            }
        )
    )
    plan = plan_retention_timestamps(
        tmp_path,
        {"old": "2025-01-01T00:00:00Z", "c1": "2025-02-01T00:00:00Z", "c2": "2025-03-01T00:00:00Z"},
    )
    assert not plan["ready"] and plan["writes"] is False
    assert [r["status"] for r in plan["results"]] == ["ready", "ready", "blocked"]
    plan = plan_retention_timestamps(
        tmp_path, {"old": "2025-01-01T00:00:00Z", "c1": "2025-02-01T00:00:00Z"}
    )
    result = apply_retention_timestamps(tmp_path, plan)
    assert result["applied"] == ["old", "c1"] and result["catalogs"] == [
        "claims.yaml",
        "sources.yaml",
    ]
    again = plan_retention_timestamps(tmp_path, {"old": "2026-01-01T00:00:00Z"})
    assert again["results"][0]["status"] == "already-set"
    assert _sources(tmp_path)["sources"][0]["archived_at"] == "2025-01-01T00:00:00Z"
    with pytest.raises(WoonError, match="ISO-8601"):
        plan_retention_timestamps(tmp_path, {"old": "yesterday"})
    # Git approximation: the oldest commit whose diff introduces the successor reference.
    env = {
        "GIT_AUTHOR_DATE": "2025-04-05T06:07:08+09:00",
        "GIT_COMMITTER_DATE": "2025-04-05T06:07:08+09:00",
    }
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "catalog"]):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@x", *args],
            cwd=tmp_path,
            check=True,
            env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )
    dates = git_supersede_dates(tmp_path, ("old", "current"), ("c1",))
    assert dates == {"old": "2025-04-04T21:07:08Z", "c1": "2025-04-04T21:07:08Z"}


def _retention_catalog(vault: Path) -> None:
    """Only stale, unreferenced archived revisions with a current successor qualify."""
    data = _sources(vault)
    old, current = data["sources"]
    stale = {**old, "source_id": "stale", "archived_at": "2024-01-01T00:00:00Z"}
    fresh = {**old, "source_id": "fresh", "archived_at": "2026-09-01T00:00:00Z"}
    undated = {**old, "source_id": "undated"}
    cited = {**old, "source_id": "cited", "archived_at": "2024-01-01T00:00:00Z"}
    orphan_end = {**current, "source_id": "orphan-end"}
    orphan = {
        **old,
        "source_id": "orphan",
        "superseded_by": "orphan-end",
        "archived_at": "2024-01-01T00:00:00Z",
    }
    private = {
        **old,
        "source_id": "private",
        "privacy": "private",
        "archived_at": "2024-01-01T00:00:00Z",
    }
    data["sources"] = [stale, fresh, undated, cited, orphan, orphan_end, private, current]
    _write_sources(vault, data)
    (vault / "catalog/llm-wiki/claims.yaml").write_text(
        yaml.safe_dump(
            {
                "claims": [
                    {
                        "claim_id": "c-old",
                        "status": "superseded",
                        "superseded_by": "c-new",
                        "source_ids": ["stale"],
                        "markdown": "Earlier statement",
                    },
                    {
                        "claim_id": "c-new",
                        "status": "accepted",
                        "source_ids": ["current", "cited"],
                        "markdown": "Current statement",
                    },
                ]
            }
        )
    )


def test_retention_selects_stale_unreferenced_sources_with_current_successor(
    tmp_path: Path,
) -> None:
    _catalog(tmp_path)
    _retention_catalog(tmp_path)
    now = datetime(2026, 9, 22, tzinfo=UTC)
    plan = plan_retention_compaction(tmp_path, older_than_days=365, now=now)
    assert plan["writes"] is False and plan["ready"]
    assert [r["source_id"] for r in plan["results"]] == ["stale"]
    assert plan["retention"] == {
        "older_than_days": 365,
        "threshold": "2025-09-22T00:00:00Z",
        "candidates": 1,
        "selected": 1,
    }
    marker = plan["results"][0]["replacement"]["body_retention"]
    assert marker["review_reference"] == "retention:365d:2026-09-22"
    assert marker["successor_id"] == "current"
    protected = plan_retention_compaction(
        tmp_path, older_than_days=365, now=now, protected_source_ids=("stale",)
    )
    assert protected["results"] == [] and protected["ready"]
    assert plan_retention_compaction(tmp_path, older_than_days=3650, now=now)["results"] == []


def test_retention_apply_leaves_claim_markdown_untouched(tmp_path: Path) -> None:
    _catalog(tmp_path)
    _retention_catalog(tmp_path)
    claims_path = tmp_path / "catalog/llm-wiki/claims.yaml"
    claims_before = claims_path.read_bytes()
    plan = json.loads(
        json.dumps(plan_retention_compaction(tmp_path, now=datetime(2026, 9, 22, tzinfo=UTC)))
    )
    result = apply_source_body_compaction(tmp_path, plan)
    assert result["applied"] == ["stale"]
    assert claims_path.read_bytes() == claims_before
    sources = {s["source_id"]: s for s in _sources(tmp_path)["sources"]}
    assert "body" not in sources["stale"] and "body_retention" in sources["stale"]
    assert all("body" in s for sid, s in sources.items() if sid != "stale")
    # TODO(retention): superseded claim markdown is intentionally not compacted yet.
    assert yaml.safe_load(claims_before)["claims"][0]["markdown"] == "Earlier statement"


def test_intake_cli_exposes_apply_and_retention_actions(tmp_path: Path) -> None:
    from woon_core.knowledge.intake_cli import execute_intake_request

    current = _catalog(tmp_path)
    plan = execute_intake_request(
        tmp_path,
        "source-body-plan",
        {
            "source_ids": ["old"],
            "protected_source_ids": [],
            "reviewed_successors": {
                "old": {"source_id": "current", "normalized_sha256": current["normalized_sha256"]}
            },
            "review_reference": "owner:reviewed-meaning",
        },
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    with pytest.raises(WoonError, match="missing or unsupported"):
        execute_intake_request(tmp_path, "source-body-apply", {"plan": "x"})
    result = execute_intake_request(tmp_path, "source-body-apply", {"plan_path": str(plan_path)})
    assert result["applied"] == ["old"]
    with pytest.raises(WoonError, match="needs dates"):
        execute_intake_request(tmp_path, "retention-timestamps-plan", {})
    stamp = execute_intake_request(
        tmp_path, "retention-timestamps-plan", {"dates": {"old": "2024-01-01T00:00:00Z"}}
    )
    plan_path.write_text(json.dumps(stamp))
    execute_intake_request(tmp_path, "retention-timestamps-apply", {"plan_path": str(plan_path)})
    retention = execute_intake_request(tmp_path, "retention-plan", {"older_than_days": 30})
    assert retention["results"] == [] and retention["retention"]["candidates"] == 0
