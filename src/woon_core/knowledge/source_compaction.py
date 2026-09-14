"""Read-only, revision-bound plans for consumed generated source bodies.

The shared catalog writer applies reviewed replacements through its existing
transaction. A terminal record retains identity and hashes, never deleted bytes.
This first contract only allows public curated Wiki revisions; books, original
evidence, private records and explicitly protected IDs remain intact.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import encode_json

_SHA = re.compile(r"[0-9a-f]{64}")


def validate_compacted_source(source: dict[str, Any]) -> None:
    """Validate a terminal marker without claiming to reconstruct removed bytes."""
    marker = source.get("body_retention")
    if (
        source.get("lifecycle") != "archived"
        or source.get("kind") != "curated-wiki"
        or source.get("privacy") != "public"
        or "body" in source
        or not isinstance(marker, dict)
        or marker.get("version") != 1
        or marker.get("state") != "removed"
        or not _SHA.fullmatch(str(marker.get("body_sha256", "")))
        or not _SHA.fullmatch(str(marker.get("record_sha256", "")))
        or not isinstance(marker.get("review_reference"), str)
        or not 1 <= len(marker["review_reference"].strip()) <= 512
        or not isinstance(marker.get("successor_id"), str)
        or not _SHA.fullmatch(str(marker.get("successor_normalized_sha256", "")))
    ):
        raise WoonError("invalid compact source; preserve the original record")


def read_source_record(source: dict[str, Any]) -> dict[str, Any]:
    """Return source identity and body availability, including terminal revisions."""
    if "body_retention" in source:
        validate_compacted_source(source)
    return {
        **source,
        "body_available": isinstance(source.get("body"), str),
        "deleted_bytes_recoverable": False if "body_retention" in source else None,
    }


def compact_reference_errors(
    sources: dict[str, dict[str, Any]],
    claims: dict[str, dict[str, Any]],
    pages: dict[str, dict[str, Any]],
) -> tuple[str, ...]:
    """Reject compact source records referenced by current pages or accepted claims."""
    compact = {key for key, source in sources.items() if "body_retention" in source}
    if not compact:
        return ()
    referenced = _strings(list(pages.values()))
    referenced.update(_strings([c for c in claims.values() if c.get("status") != "superseded"]))
    return tuple(
        f"{key}: compact source is referenced by current content"
        for key in sorted(compact & referenced)
    )


def plan_source_body_compaction(
    vault: Path,
    *,
    source_ids: tuple[str, ...],
    protected_source_ids: tuple[str, ...],
    reviewed_successors: dict[str, dict[str, str]],
    review_reference: str,
) -> dict[str, Any]:
    """Plan at most 24 explicitly reviewed public generated revisions; write nothing.

    The caller supplies protected IDs from its current ownership/recovery scope.
    A matching successor hash attests semantic review by the owner, not merely a
    title match. Apply only under the shared writer lock with every catalog hash
    unchanged; rerun the plan after drift and audit before committing the write.
    """
    if (
        not source_ids
        or len(source_ids) > 24
        or len(set(source_ids)) != len(source_ids)
        or not isinstance(review_reference, str)
        or not 1 <= len(review_reference) <= 512
        or not review_reference.strip()
        or set(reviewed_successors) != set(source_ids)
    ):
        raise WoonError("source compaction needs explicit IDs and reviewed successors")
    root = vault.expanduser().resolve()
    snapshots: dict[Path, bytes | None] = {}

    def records(name: str, key: str, *, optional: bool = False) -> list[dict[str, Any]]:
        path = root / "catalog/llm-wiki" / name
        if path.is_symlink():
            raise WoonError("source compaction refuses symlink catalogs")
        raw = path.read_bytes() if path.is_file() else None
        snapshots[path] = raw
        if raw is None and optional:
            return []
        try:
            value = yaml.load(raw or b"", Loader=yaml.CSafeLoader)
        except yaml.YAMLError as error:
            raise WoonError("source compaction catalog is invalid") from error
        result = value.get(key) if isinstance(value, dict) else None
        if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
            raise WoonError(f"source compaction needs a valid {name}")
        return result

    source_list = records("sources.yaml", "sources")
    if any(not isinstance(s.get("source_id"), str) for s in source_list):
        raise WoonError("source compaction catalog has missing source IDs")
    sources: dict[str, dict[str, Any]] = {s["source_id"]: s for s in source_list}
    if len(sources) != len(source_list):
        raise WoonError("source compaction catalog has duplicate or missing IDs")
    claims = records("claims.yaml", "claims")
    pages = records("pages.yaml", "pages")
    reviews = records("review-queue.yaml", "items", optional=True)
    referenced = _strings(pages)
    referenced.update(_strings([c for c in claims if c.get("status") != "superseded"]))
    referenced.update(_strings([r for r in reviews if r.get("status") != "approved"]))
    protected = set(protected_source_ids)
    results: list[dict[str, Any]] = []
    for source_id in source_ids:
        source = sources.get(source_id)
        reason = None
        if source is None:
            reason = "missing-source"
        elif source_id in protected or _strings(source) & protected:
            reason = "protected-source"
        elif source_id in referenced:
            reason = "current-or-pending-reference"
        elif (
            source.get("kind") != "curated-wiki"
            or source.get("privacy") != "public"
            or source.get("lifecycle") != "archived"
            or source.get("source_asset_inventory")
        ):
            reason = "original-private-book-or-active-source"
        successor = _current_successor(source_id, sources) if source and not reason else None
        review = reviewed_successors[source_id]
        if not reason and (
            successor is None
            or successor["source_id"] not in referenced
            or review
            != {
                "source_id": successor["source_id"],
                "normalized_sha256": successor["normalized_sha256"],
            }
        ):
            reason = "successor-not-current-or-not-reviewed"
        if reason:
            results.append({"source_id": source_id, "status": "blocked", "reason": reason})
            continue
        assert source is not None and successor is not None
        if "body_retention" in source:
            validate_compacted_source(source)
            results.append({"source_id": source_id, "status": "already-compacted"})
            continue
        body = source.get("body")
        if not isinstance(body, str) or _sha(_normalize(body).encode()) != source.get(
            "normalized_sha256"
        ):
            raise WoonError("source compaction body hash mismatch")
        replacement = {k: v for k, v in source.items() if k != "body"}
        replacement["body_retention"] = {
            "version": 1,
            "state": "removed",
            "body_sha256": _sha(body.encode()),
            "record_sha256": _sha(encode_json(source)),
            "review_reference": review_reference,
            "successor_id": successor["source_id"],
            "successor_normalized_sha256": successor["normalized_sha256"],
        }
        validate_compacted_source(replacement)
        results.append(
            {
                "source_id": source_id,
                "status": "ready",
                "bytes": len(body.encode()),
                "expected_record_sha256": _sha(encode_json(source)),
                "replacement": replacement,
            }
        )
    if any((p.read_bytes() if p.is_file() else None) != raw for p, raw in snapshots.items()):
        raise WoonError("source compaction catalogs changed during planning; reread")
    return {
        "version": 1,
        "writes": False,
        "ready": all(r["status"] != "blocked" for r in results),
        "expected_catalog_sha256": {
            p.relative_to(root).as_posix(): _sha(raw) if raw is not None else None
            for p, raw in snapshots.items()
        },
        "results": results,
    }


def _current_successor(
    source_id: str,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    seen = {source_id}
    current = sources[source_id]
    while current.get("lifecycle") == "archived":
        next_id = current.get("superseded_by")
        if not isinstance(next_id, str) or next_id in seen or next_id not in sources:
            return None
        seen.add(next_id)
        current = sources[next_id]
    return current if current.get("lifecycle") == "compiled" else None


def _strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set().union(*(_strings(v) for v in value)) if value else set()
    if isinstance(value, dict):
        return _strings(list(value.values()))
    return set()


def _normalize(body: str) -> str:
    normalized = "\n".join(line.rstrip() for line in body.replace("\r\n", "\n").split("\n"))
    return normalized.strip() + "\n"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
