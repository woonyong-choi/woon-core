"""Read-only, revision-bound plans for consumed generated source bodies.

Plans never write. ``apply_*`` functions re-read the four catalog files under
the writer lock, refuse on any hash drift since planning, and rewrite the
catalog with the same dumper the compiled Wiki uses, so untouched records keep
their bytes. A terminal record retains identity and hashes, never deleted
bytes. This contract only allows public curated Wiki revisions; books,
original evidence, private records and explicitly protected IDs remain intact.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock

_SHA = re.compile(r"[0-9a-f]{64}")
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z")
_CATALOG = "catalog/llm-wiki"
_LOCK = ".local/woon-knowledge/catalog-writer.lock"
_SUCCESSOR_CYCLE = "successor-cycle"


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


def validate_utc_timestamp(record: dict[str, Any], field: str) -> None:
    """Accept an optional ISO-8601 UTC (``Z``) timestamp such as ``archived_at``."""
    if field not in record:
        return
    value = record[field]
    if not isinstance(value, str) or not _ISO_UTC.fullmatch(value):
        raise WoonError(f"{field} must be an ISO-8601 UTC timestamp ending in Z")
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as error:
        raise WoonError(f"{field} must be an ISO-8601 UTC timestamp ending in Z") from error


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


class _Catalogs:
    """One consistent snapshot of the four catalog files."""

    def __init__(self, vault: Path) -> None:
        self.root = vault.expanduser().resolve()
        self.snapshots: dict[Path, bytes | None] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.source_list = self._records("sources.yaml", "sources")
        if any(not isinstance(s.get("source_id"), str) for s in self.source_list):
            raise WoonError("source compaction catalog has missing source IDs")
        self.sources: dict[str, dict[str, Any]] = {s["source_id"]: s for s in self.source_list}
        if len(self.sources) != len(self.source_list):
            raise WoonError("source compaction catalog has duplicate or missing IDs")
        self.claims = self._records("claims.yaml", "claims")
        self.pages = self._records("pages.yaml", "pages")
        self.reviews = self._records("review-queue.yaml", "items", optional=True)
        self.referenced = _strings(self.pages)
        self.referenced.update(
            _strings([c for c in self.claims if c.get("status") != "superseded"])
        )
        self.referenced.update(_strings([r for r in self.reviews if r.get("status") != "approved"]))

    def _records(self, name: str, key: str, *, optional: bool = False) -> list[dict[str, Any]]:
        path = self.root / _CATALOG / name
        if path.is_symlink():
            raise WoonError("source compaction refuses symlink catalogs")
        raw = path.read_bytes() if path.is_file() else None
        self.snapshots[path] = raw
        if raw is None and optional:
            return []
        try:
            value = yaml.load(raw or b"", Loader=yaml.CSafeLoader)
        except yaml.YAMLError as error:
            raise WoonError("source compaction catalog is invalid") from error
        result = value.get(key) if isinstance(value, dict) else None
        if not isinstance(result, list) or any(not isinstance(r, dict) for r in result):
            raise WoonError(f"source compaction needs a valid {name}")
        self.documents[name] = value
        return result

    def unchanged(self) -> bool:
        return all(
            (p.read_bytes() if p.is_file() else None) == raw for p, raw in self.snapshots.items()
        )

    def expected_sha256(self) -> dict[str, str | None]:
        return {
            p.relative_to(self.root).as_posix(): _sha(raw) if raw is not None else None
            for p, raw in self.snapshots.items()
        }

    def write(self, name: str) -> None:
        """Rewrite one catalog with the compiled Wiki dumper (see ``_write_yaml``)."""
        data = yaml.safe_dump(
            self.documents[name], allow_unicode=True, sort_keys=False, default_flow_style=False
        ).encode("utf-8")
        atomic_write(self.root / _CATALOG / name, data)


def _read_only_plan(catalogs: _Catalogs, results: list[dict[str, Any]]) -> dict[str, Any]:
    if not catalogs.unchanged():
        raise WoonError("source compaction catalogs changed during planning; reread")
    return {
        "version": 1,
        "writes": False,
        "ready": all(r["status"] != "blocked" for r in results),
        "expected_catalog_sha256": catalogs.expected_sha256(),
        "results": results,
    }


def _verified_catalogs(vault: Path, plan: dict[str, Any]) -> _Catalogs:
    """Re-read the catalogs for an apply and refuse any drift since planning."""
    if (
        not isinstance(plan, dict)
        or plan.get("version") != 1
        or plan.get("writes") is not False
        or plan.get("ready") is not True
        or not isinstance(plan.get("expected_catalog_sha256"), dict)
        or not isinstance(plan.get("results"), list)
    ):
        raise WoonError("apply needs a ready read-only plan from this planner")
    catalogs = _Catalogs(vault)
    if catalogs.expected_sha256() != plan["expected_catalog_sha256"]:
        raise WoonError("catalogs changed since the plan; rerun the plan and review it")
    return catalogs


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
    title match. ``superseded_by`` chains are followed to their terminal compiled
    successor; the attestation must name that terminal successor. Apply only
    under the shared writer lock with every catalog hash unchanged; rerun the
    plan after drift and audit before committing the write.
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
    catalogs = _Catalogs(vault)
    sources = catalogs.sources
    referenced = catalogs.referenced
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
        successor = _terminal_successor(source_id, sources) if source and not reason else None
        review = reviewed_successors[source_id]
        if not reason and successor == _SUCCESSOR_CYCLE:
            reason = _SUCCESSOR_CYCLE
        elif not reason and (
            not isinstance(successor, dict)
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
        assert source is not None and isinstance(successor, dict)
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
    return _read_only_plan(catalogs, results)


def apply_source_body_compaction(vault: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Apply a ready plan under the writer lock; any catalog drift refuses the write.

    Every ``ready`` result must still match its ``expected_record_sha256`` and the
    replacement must differ from the record only by ``body`` → ``body_retention``.
    Nothing is written when any result fails; ``already-compacted`` results are
    skipped. Other records keep their bytes because the catalog dumper is shared.
    """
    root = vault.expanduser().resolve()
    with exclusive_file_lock(root / _LOCK):
        catalogs = _verified_catalogs(vault, plan)
        before = catalogs.snapshots[root / _CATALOG / "sources.yaml"]
        assert before is not None
        applied: list[str] = []
        bytes_removed = 0
        for result in plan["results"]:
            if not isinstance(result, dict) or result.get("status") != "ready":
                continue
            source_id = result.get("source_id")
            replacement = result.get("replacement")
            source = catalogs.sources.get(str(source_id))
            if (
                source is None
                or not isinstance(replacement, dict)
                or _sha(encode_json(source)) != result.get("expected_record_sha256")
                or replacement.get("source_id") != source_id
                or {k: v for k, v in replacement.items() if k != "body_retention"}
                != {k: v for k, v in source.items() if k != "body"}
                or not isinstance(source.get("body"), str)
                or replacement["body_retention"].get("record_sha256") != _sha(encode_json(source))
                or replacement["body_retention"].get("body_sha256") != _sha(source["body"].encode())
            ):
                raise WoonError(f"plan result no longer matches source record: {source_id}")
            validate_compacted_source(replacement)
            bytes_removed += len(source["body"].encode())
            applied.append(str(source_id))
            source.clear()
            source.update(replacement)
        if applied:
            catalogs.write("sources.yaml")
        after = (root / _CATALOG / "sources.yaml").read_bytes()
        return {
            "version": 1,
            "writes": True,
            "applied": applied,
            "bytes_removed": bytes_removed,
            "sources_sha256_before": _sha(before),
            "sources_sha256_after": _sha(after),
        }


def git_supersede_dates(
    vault: Path,
    source_ids: tuple[str, ...] | list[str],
    claim_ids: tuple[str, ...] | list[str] = (),
) -> dict[str, str]:
    """Approximate when each record was superseded from the vault's Git history.

    Catalog records carry no timestamps, so the date is the author date of the
    oldest commit whose diff changes the count of ``superseded_by: <successor>``
    in the catalog (Git ``-S``), falling back to the successor ID alone. This is
    an approximation: the successor usually first appears in the commit that
    archived its predecessor, but a later reformatting or an earlier reference
    in another record can shift it. IDs without a successor or Git evidence are
    omitted. Values are ISO-8601 UTC ``Z`` timestamps.
    """
    catalogs = _Catalogs(vault)
    claims = {c.get("claim_id"): c for c in catalogs.claims if isinstance(c.get("claim_id"), str)}
    dates: dict[str, str] = {}
    for name, ids, table in (
        ("sources.yaml", source_ids, catalogs.sources),
        ("claims.yaml", claim_ids, claims),
    ):
        for record_id in ids:
            record = table.get(record_id)
            successor = record.get("superseded_by") if record else None
            if not isinstance(successor, str) or not successor:
                continue
            for needle in (f"superseded_by: {successor}", successor):
                found = _git_oldest_author_date(catalogs.root, needle, f"{_CATALOG}/{name}")
                if found:
                    dates[record_id] = found
                    break
    return dates


def _git_oldest_author_date(root: Path, needle: str, relative: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "log", "--format=%aI", f"-S{needle}", "--", relative],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise WoonError("git history is unavailable for supersede dates") from error
    if completed.returncode != 0:
        raise WoonError("git history is unavailable for supersede dates")
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        parsed = datetime.fromisoformat(lines[-1])
    except ValueError:
        return None
    return _utc(parsed)


def plan_retention_timestamps(vault: Path, dates: dict[str, str]) -> dict[str, Any]:
    """Plan ``archived_at`` (sources) / ``superseded_at`` (claims); write nothing.

    Only archived sources and superseded claims lacking the field are ready;
    records that already carry one are reported as ``already-set`` and unknown or
    active records block the plan.
    """
    if not isinstance(dates, dict) or not dates:
        raise WoonError("retention timestamps need a non-empty {record_id: timestamp} mapping")
    catalogs = _Catalogs(vault)
    claims = {c.get("claim_id"): c for c in catalogs.claims if isinstance(c.get("claim_id"), str)}
    results: list[dict[str, Any]] = []
    for record_id, value in dates.items():
        validate_utc_timestamp({"timestamp": value}, "timestamp")
        if record_id in catalogs.sources:
            record, catalog, field, active = (
                catalogs.sources[record_id],
                "sources.yaml",
                "archived_at",
                catalogs.sources[record_id].get("lifecycle") != "archived",
            )
        elif record_id in claims:
            record, catalog, field, active = (
                claims[record_id],
                "claims.yaml",
                "superseded_at",
                claims[record_id].get("status") != "superseded",
            )
        else:
            results.append(
                {"record_id": record_id, "status": "blocked", "reason": "missing-record"}
            )
            continue
        entry = {"record_id": record_id, "catalog": catalog, "field": field}
        if active:
            results.append({**entry, "status": "blocked", "reason": "record-not-superseded"})
        elif field in record:
            results.append({**entry, "status": "already-set", "value": record[field]})
        else:
            results.append(
                {
                    **entry,
                    "status": "ready",
                    "value": value,
                    "expected_record_sha256": _sha(encode_json(record)),
                }
            )
    return _read_only_plan(catalogs, results)


def apply_retention_timestamps(vault: Path, plan: dict[str, Any]) -> dict[str, Any]:
    """Add planned timestamps under the writer lock; refuse on any catalog drift."""
    root = vault.expanduser().resolve()
    with exclusive_file_lock(root / _LOCK):
        catalogs = _verified_catalogs(vault, plan)
        claims = {
            c.get("claim_id"): c for c in catalogs.claims if isinstance(c.get("claim_id"), str)
        }
        touched: set[str] = set()
        applied: list[str] = []
        for result in plan["results"]:
            if not isinstance(result, dict) or result.get("status") != "ready":
                continue
            record_id = result.get("record_id")
            catalog, field, value = result.get("catalog"), result.get("field"), result.get("value")
            record = (
                catalogs.sources.get(str(record_id))
                if catalog == "sources.yaml"
                else claims.get(str(record_id))
                if catalog == "claims.yaml"
                else None
            )
            if (
                record is None
                or field not in {"archived_at", "superseded_at"}
                or field in record
                or _sha(encode_json(record)) != result.get("expected_record_sha256")
            ):
                raise WoonError(f"plan result no longer matches catalog record: {record_id}")
            validate_utc_timestamp({field: value}, str(field))
            record[field] = value
            touched.add(str(catalog))
            applied.append(str(record_id))
        for name in sorted(touched):
            catalogs.write(name)
        return {"version": 1, "writes": True, "applied": applied, "catalogs": sorted(touched)}


def plan_retention_compaction(
    vault: Path,
    *,
    older_than_days: int = 365,
    now: datetime | None = None,
    limit: int = 24,
    protected_source_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Select stale archived public revisions and delegate to the reviewed planner.

    A source qualifies when it is ``lifecycle: archived`` with an ``archived_at``
    older than the threshold, is not referenced by any page, accepted claim or
    pending review item, and follows its ``superseded_by`` chain to a compiled
    successor that current content does reference. The attestation is computed
    from that terminal successor and recorded as ``retention:<days>d:<date>``.
    Nothing is written. Claim ``markdown`` is never removed here.
    """
    # TODO(retention): superseded claim markdown is left untouched; a later task
    # may plan claim compaction with its own review attestation.
    if not isinstance(older_than_days, int) or older_than_days < 1 or not 1 <= limit <= 24:
        raise WoonError("retention needs older_than_days >= 1 and 1 <= limit <= 24")
    current = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    threshold = current - timedelta(days=older_than_days)
    catalogs = _Catalogs(vault)
    protected = set(protected_source_ids)
    candidates: list[tuple[str, str]] = []
    for source_id, source in catalogs.sources.items():
        archived_at = source.get("archived_at")
        if (
            source.get("lifecycle") != "archived"
            or source.get("kind") != "curated-wiki"
            or source.get("privacy") != "public"
            or source.get("source_asset_inventory")
            or "body_retention" in source
            or not isinstance(archived_at, str)
            or source_id in catalogs.referenced
            or source_id in protected
            or _strings(source) & protected
        ):
            continue
        try:
            validate_utc_timestamp(source, "archived_at")
        except WoonError:
            continue
        if datetime.fromisoformat(archived_at.replace("Z", "+00:00")) >= threshold:
            continue
        successor = _terminal_successor(source_id, catalogs.sources)
        if not isinstance(successor, dict) or successor["source_id"] not in catalogs.referenced:
            continue
        candidates.append((archived_at, source_id))
    selected = tuple(source_id for _, source_id in sorted(candidates)[:limit])
    retention = {
        "older_than_days": older_than_days,
        "threshold": _utc(threshold),
        "candidates": len(candidates),
        "selected": len(selected),
    }
    if not selected:
        return {**_read_only_plan(catalogs, []), "retention": retention}
    reviewed = {}
    for source_id in selected:
        successor = _terminal_successor(source_id, catalogs.sources)
        assert isinstance(successor, dict)
        reviewed[source_id] = {
            "source_id": successor["source_id"],
            "normalized_sha256": successor["normalized_sha256"],
        }
    if not catalogs.unchanged():
        raise WoonError("source compaction catalogs changed during planning; reread")
    plan = plan_source_body_compaction(
        vault,
        source_ids=selected,
        protected_source_ids=tuple(protected_source_ids),
        reviewed_successors=reviewed,
        review_reference=f"retention:{older_than_days}d:{current.date().isoformat()}",
    )
    return {**plan, "retention": retention}


def _terminal_successor(
    source_id: str,
    sources: dict[str, dict[str, Any]],
) -> dict[str, Any] | str | None:
    """Follow ``superseded_by`` to the compiled terminal record, or report a cycle."""
    seen = {source_id}
    current = sources[source_id]
    while current.get("lifecycle") == "archived":
        next_id = current.get("superseded_by")
        if not isinstance(next_id, str) or next_id not in sources:
            return None
        if next_id in seen:
            return _SUCCESSOR_CYCLE
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


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
