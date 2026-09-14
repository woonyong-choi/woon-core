from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.source_compaction import (
    plan_source_body_compaction,
    read_source_record,
    validate_compacted_source,
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
