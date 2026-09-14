"""Read-only inventory for moving raw source bytes out of the Wiki tree."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write, exclusive_file_lock

LEGACY_SOURCE_ROOT = Path("wiki/private/_sources")
RESTRUCTURED_SOURCE_ROOTS: tuple[tuple[Path, str], ...] = (
    (Path("sources/knowledge/web"), "knowledge/web"),
    (Path("private/knowledge"), "knowledge"),
    (Path("private/novel"), "novel"),
    (Path("private/codex"), "codex"),
    (Path("private/legacy-wiki"), "legacy-wiki"),
)


@dataclass(frozen=True, slots=True)
class SourceRestructurePreflight:
    """Completeness and hash result for raw-source relocation instructions."""

    file_count: int
    byte_count: int
    disposition_counts: dict[str, int]
    catalog_pending_count: int
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SourceCatalogReferenceAudit:
    """Every catalog reference to one legacy raw-source byte path.

    This is deliberately read-only.  It is the evidence required before a
    relocation manifest can mark a source record as catalog-reconciled.
    """

    file_count: int
    catalog_record_count: int
    reference_count: int
    orphan_count: int
    duplicate_primary_count: int
    stale_reference_count: int
    issues: tuple[str, ...]
    records: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class SourceOwnerReconciliationReport:
    """Result of assigning each formerly orphaned raw file one catalog owner."""

    catalog_path: str
    created: bool
    records_added: int
    orphan_count_before: int
    orphan_count_after: int


@dataclass(frozen=True, slots=True)
class SourceRestructureApplyReport:
    """One completed, hash-verified raw source relocation."""

    manifest_path: str
    moved_files: int
    moved_bytes: int
    rewritten_files: int
    rewritten_references: int


@dataclass(frozen=True, slots=True)
class SourceRestructureManifestReconciliationReport:
    """A reviewable manifest whose ownership state is evidenced by the catalog."""

    input_manifest_path: str
    output_manifest_path: str
    reconciled_records: int


def render_source_restructure_template(vault: Path) -> bytes:
    """Create one hash-complete source manifest without moving a byte.

    Public web snapshots move below ``sources/``. Everything else stays private
    by default; the user-approved private Git boundary does not make it public.
    Every record still starts with catalog reconciliation pending, because a
    byte move without updating locator-bearing catalog and receipt records is
    unsafe.
    """

    root = vault.expanduser().resolve()
    source_root = root / LEGACY_SOURCE_ROOT
    if not source_root.is_dir():
        raise WoonError(f"legacy raw source root is missing: {source_root}")
    records: list[dict[str, object]] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise WoonError(f"raw source restructure rejects symlink: {path}")
        if not path.is_file():
            continue
        current = path.relative_to(root).as_posix()
        relative = path.relative_to(source_root).as_posix()
        disposition, target, storage_scope = _default_destination(relative)
        record: dict[str, object] = {
            "current_path": current,
            "current_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "storage_scope": storage_scope,
            "disposition": disposition,
            "catalog_reconciliation": "pending",
        }
        if target is not None:
            record["target_path"] = target
        records.append(record)
    return yaml.safe_dump(
        {"version": 1, "records": records}, allow_unicode=True, sort_keys=False, width=100
    ).encode("utf-8")


def write_source_restructure_template(vault: Path, output_path: Path) -> Path:
    """Write a local inventory and never overwrite an in-progress review."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/source-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "source restructure template must stay below .local/woon-knowledge/source-restructure"
        )
    if output.exists():
        raise WoonError(f"source restructure template already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_source_restructure_template(root), mode=0o600)
    return output


def audit_source_catalog_references(vault: Path) -> SourceCatalogReferenceAudit:
    """Inventory catalog locators before relocating raw source bytes.

    ``catalog/sources`` owns source records through ``records[*].target``.
    All YAML and JSON catalog documents are also scanned for legacy locators so
    claims, page specifications, and receipts cannot silently retain a path
    that will disappear.  A source can have many references but must have no
    more than one primary catalog owner.
    """

    root = vault.expanduser().resolve()
    catalog_root = root / "catalog"
    if not catalog_root.is_dir():
        raise WoonError(f"catalog root is missing: {catalog_root}")

    paths = _active_raw_source_paths(root)
    active = set(paths)
    primary_owners: dict[str, list[str]] = {path: [] for path in paths}
    references: dict[str, list[str]] = {path: [] for path in paths}
    issues: list[str] = []
    catalog_record_count = 0

    for document in sorted(catalog_root.rglob("*")):
        if not document.is_file() or document.is_symlink():
            continue
        suffix = document.suffix.lower()
        if suffix not in {".yaml", ".yml", ".json"}:
            continue
        relative_document = document.relative_to(root).as_posix()
        try:
            payload = _load_catalog_document(document)
        except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as error:
            issues.append(f"catalog document is unreadable: {relative_document}: {error}")
            continue
        for scalar_path, value in _catalog_scalar_strings(payload, relative_document):
            if _catalog_identity_scalar(scalar_path):
                continue
            for source_path, locator in _raw_source_locators(value, active):
                label = f"{relative_document}:{scalar_path}"
                if source_path not in active:
                    issues.append(f"stale raw-source locator {locator} at {label}")
                    continue
                references[source_path].append(f"{label}={locator}")
        if document.parent == catalog_root / "sources":
            records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(records, list):
                issues.append(f"source catalog has no records list: {relative_document}")
                continue
            for index, record in enumerate(records, start=1):
                if not isinstance(record, dict):
                    issues.append(
                        f"invalid source catalog record: {relative_document}:records[{index}]"
                    )
                    continue
                catalog_record_count += 1
                target = record.get("target")
                if not isinstance(target, str):
                    continue
                for source_path, _locator in _raw_source_locators(target, active):
                    if source_path in active and record.get("state") in {None, "canonical"}:
                        source_id = record.get("source_id", "<missing-source-id>")
                        primary_owners[source_path].append(
                            f"{relative_document}:records[{index}]={source_id}"
                        )

    audit_records: list[dict[str, object]] = []
    orphan_count = 0
    duplicate_primary_count = 0
    for path in paths:
        owners = sorted(primary_owners[path])
        refs = sorted(references[path])
        if not owners:
            orphan_count += 1
        if len(owners) > 1:
            duplicate_primary_count += 1
            issues.append(f"raw source has multiple primary catalog owners: {path}")
        audit_records.append(
            {
                "current_path": path,
                "primary_catalog_owners": owners,
                "catalog_references": refs,
                "catalog_reconciliation": "reconciled" if len(owners) == 1 else "pending",
            }
        )
    stale_reference_count = sum(issue.startswith("stale raw-source locator ") for issue in issues)
    return SourceCatalogReferenceAudit(
        file_count=len(paths),
        catalog_record_count=catalog_record_count,
        reference_count=sum(len(items) for items in references.values()),
        orphan_count=orphan_count,
        duplicate_primary_count=duplicate_primary_count,
        stale_reference_count=stale_reference_count,
        issues=tuple(issues),
        records=tuple(audit_records),
    )


def render_source_catalog_reference_audit(
    vault: Path, report: SourceCatalogReferenceAudit | None = None
) -> bytes:
    """Render a full, local-only locator inventory for human review."""

    report = report or audit_source_catalog_references(vault)
    return (
        json.dumps(
            {
                "version": 1,
                "file_count": report.file_count,
                "catalog_record_count": report.catalog_record_count,
                "reference_count": report.reference_count,
                "orphan_count": report.orphan_count,
                "duplicate_primary_count": report.duplicate_primary_count,
                "stale_reference_count": report.stale_reference_count,
                "issues": list(report.issues),
                "records": list(report.records),
            },
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )


def write_source_catalog_reference_audit(
    vault: Path, output_path: Path, report: SourceCatalogReferenceAudit | None = None
) -> Path:
    """Write a locator inventory below the local-only restructure workspace."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/source-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "source catalog reference audit must stay below "
            ".local/woon-knowledge/source-restructure"
        )
    if output.exists():
        raise WoonError(f"source catalog reference audit already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(output, render_source_catalog_reference_audit(root, report), mode=0o600)
    return output


def reconcile_raw_source_catalog_owners(vault: Path) -> SourceOwnerReconciliationReport:
    """Give every unowned raw source byte one hash-complete catalog record.

    Existing catalog owners remain authoritative.  This creates one dedicated
    ownership catalog only for the files that the reference audit identifies as
    orphaned, so a later relocation can prove that every moved byte has exactly
    one primary owner without rewriting user-owned source catalogs.
    """

    root = vault.expanduser().resolve()
    lock_path = root / ".local/woon-knowledge/mutation.lock"
    with exclusive_file_lock(lock_path):
        audit = audit_source_catalog_references(root)
        if audit.issues:
            raise WoonError("raw source owner reconciliation requires a clean catalog audit")
        if audit.duplicate_primary_count:
            raise WoonError("raw source owner reconciliation found duplicate primary owners")
        orphans = [
            str(record["current_path"])
            for record in audit.records
            if not record["primary_catalog_owners"]
        ]
        catalog_path = root / "catalog/sources/raw-archive-ownership.yaml"
        if not orphans:
            return SourceOwnerReconciliationReport(
                catalog_path=catalog_path.relative_to(root).as_posix(),
                created=False,
                records_added=0,
                orphan_count_before=0,
                orphan_count_after=0,
            )
        created = not catalog_path.exists()
        before = catalog_path.read_bytes() if catalog_path.exists() else None
        if before is None:
            payload: dict[str, object] = {
                "version": 1,
                "source": "raw-archive-ownership",
                "summary": {"canonical": 0},
                "records": [],
                "excluded": [],
            }
        else:
            try:
                existing = yaml.safe_load(before.decode("utf-8"))
            except (UnicodeError, yaml.YAMLError) as error:
                raise WoonError(f"raw archive ownership catalog is unreadable: {error}") from error
            if not isinstance(existing, dict) or not isinstance(existing.get("records"), list):
                raise WoonError("raw archive ownership catalog has an invalid records list")
            payload = existing
        records = payload["records"]
        if not isinstance(records, list):  # Narrowed above; keeps the write path type-safe.
            raise WoonError("raw archive ownership catalog has an invalid records list")
        additions: list[dict[str, object]] = []
        for current_path in sorted(orphans):
            path = root / current_path
            if not path.is_file() or path.is_symlink():
                raise WoonError(f"raw source owner changed during reconciliation: {current_path}")
            relative = _raw_source_locator(root, path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            additions.append(
                {
                    "source_id": f"source://raw-archive/{quote(relative, safe='/._-')}",
                    "locator": relative,
                    "sha256": digest,
                    "size": path.stat().st_size,
                    "role": "raw-archive-file",
                    "privacy": (
                        "public" if relative.startswith("knowledge/web/") else "private/local-only"
                    ),
                    "state": "canonical",
                    "target": current_path,
                    "target_sha256": digest,
                }
            )
        records.extend(additions)
        payload["summary"] = {"canonical": len(records)}
        try:
            catalog_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(
                catalog_path,
                yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).encode("utf-8"),
            )
            after = audit_source_catalog_references(root)
            if after.issues or after.duplicate_primary_count or after.orphan_count:
                raise WoonError(
                    "raw source owner reconciliation did not produce one owner per file"
                )
        except BaseException:
            if before is None:
                catalog_path.unlink(missing_ok=True)
            else:
                atomic_write(catalog_path, before)
            raise
        return SourceOwnerReconciliationReport(
            catalog_path=catalog_path.relative_to(root).as_posix(),
            created=created,
            records_added=len(additions),
            orphan_count_before=audit.orphan_count,
            orphan_count_after=after.orphan_count,
        )


def apply_source_restructure(vault: Path, manifest_path: Path) -> SourceRestructureApplyReport:
    """Atomically relocate every reviewed raw byte and its live locators.

    The manifest remains the move authority.  Before any write, this operation
    proves that source bytes, hashes, catalog ownership, and destinations still
    agree.  It updates only live text locators; compiled provenance history is
    deliberately retained as history in the compiler catalog.  If either text
    rewrite, byte relocation, or the post-move ownership audit fails, all
    touched text and moved bytes are restored.
    """

    root = vault.expanduser().resolve()
    manifest = manifest_path.expanduser().resolve()
    lock_path = root / ".local/woon-knowledge/mutation.lock"
    with exclusive_file_lock(lock_path):
        preflight = prepare_source_restructure_preflight(root, manifest)
        if preflight.issues:
            raise WoonError("raw source restructure preflight failed; refusing to move bytes")
        if preflight.catalog_pending_count:
            raise WoonError("raw source restructure requires catalog-reconciled manifest records")
        records = _move_records(root, manifest)
        replacements = _raw_source_relocation_prefixes(records)
        destination_roots = tuple(root / path for path, _prefix in RESTRUCTURED_SOURCE_ROOTS)
        occupied = [
            path.relative_to(root).as_posix() for path in destination_roots if path.exists()
        ]
        if occupied:
            raise WoonError(
                "raw source restructure requires empty destination roots; found: "
                + ", ".join(sorted(occupied))
            )
        for _current_path, target_path, _bytes in records:
            target = root / target_path
            if target.exists() or target.is_symlink():
                raise WoonError(f"raw source restructure target already exists: {target_path}")

        rewrites = _planned_live_locator_rewrites(root, replacements)
        source_moves = [(root / current, root / target) for current, target, _bytes in records]
        text_before = {path: path.read_bytes() for path, _text, _count in rewrites}
        moved: list[tuple[Path, Path]] = []
        created_directories: set[Path] = set()
        try:
            for path, text, _count in rewrites:
                atomic_write(path, text.encode("utf-8"))
            for destination_root in destination_roots:
                _mkdirs_for_move(destination_root, root, created_directories)
            for source, target in source_moves:
                _mkdirs_for_move(target.parent, root, created_directories)
                source.replace(target)
                moved.append((source, target))
            _remove_empty_legacy_source_directories(root)
            audit = audit_source_catalog_references(root)
            if audit.issues or audit.orphan_count or audit.duplicate_primary_count:
                detail = audit.issues[0] if audit.issues else ""
                raise WoonError(
                    "raw source restructure post-move catalog audit failed: "
                    f"orphan={audit.orphan_count} "
                    f"duplicate={audit.duplicate_primary_count} {detail}"
                )
        except BaseException:
            for source, target in reversed(moved):
                if target.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    target.replace(source)
            for path, content in text_before.items():
                atomic_write(path, content)
            for directory in sorted(
                created_directories, key=lambda path: len(path.parts), reverse=True
            ):
                directory.rmdir()
            raise
        return SourceRestructureApplyReport(
            manifest_path=manifest.as_posix(),
            moved_files=len(records),
            moved_bytes=sum(size for _current, _target, size in records),
            rewritten_files=len(rewrites),
            rewritten_references=sum(count for _path, _text, count in rewrites),
        )


def write_reconciled_source_restructure_manifest(
    vault: Path, manifest_path: Path, output_path: Path
) -> SourceRestructureManifestReconciliationReport:
    """Copy a hash-complete manifest only after every raw byte has one owner."""

    root = vault.expanduser().resolve()
    manifest = manifest_path.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/source-restructure"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "reconciled source restructure manifest must stay below "
            ".local/woon-knowledge/source-restructure"
        )
    if output.exists():
        raise WoonError(f"reconciled source restructure manifest already exists: {output}")
    preflight = prepare_source_restructure_preflight(root, manifest)
    if preflight.issues:
        raise WoonError("source restructure manifest is not hash-complete")
    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"source restructure manifest is unreadable: {error}") from error
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise WoonError("source restructure manifest requires a records list")
    audit = audit_source_catalog_references(root)
    if audit.issues or audit.orphan_count or audit.duplicate_primary_count:
        raise WoonError("raw source ownership audit is not ready for manifest reconciliation")
    states = {
        str(record["current_path"]): str(record["catalog_reconciliation"])
        for record in audit.records
    }
    reconciled: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("current_path"), str):
            raise WoonError("source restructure manifest contains an invalid record")
        current = record["current_path"]
        if states.get(current) != "reconciled":
            raise WoonError(f"raw source has no unique catalog owner: {current}")
        successor = dict(record)
        successor["catalog_reconciliation"] = "reconciled"
        reconciled.append(successor)
    payload["records"] = reconciled
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write(
            output,
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100).encode("utf-8"),
            mode=0o600,
        )
        result = prepare_source_restructure_preflight(root, output)
        if result.issues or result.catalog_pending_count or result.disposition_counts.get("review"):
            raise WoonError("reconciled source restructure manifest failed validation")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return SourceRestructureManifestReconciliationReport(
        input_manifest_path=manifest.relative_to(root).as_posix(),
        output_manifest_path=output.relative_to(root).as_posix(),
        reconciled_records=len(reconciled),
    )


def prepare_source_restructure_preflight(
    vault: Path, manifest_path: Path
) -> SourceRestructurePreflight:
    """Verify one raw-source manifest before catalog or filesystem mutation."""

    root = vault.expanduser().resolve()
    source_root = root / LEGACY_SOURCE_ROOT
    manifest = manifest_path.expanduser().resolve()
    if not manifest.is_file():
        raise WoonError(f"source restructure manifest is missing: {manifest}")
    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"source restructure manifest is unreadable: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise WoonError("source restructure manifest must use version: 1")
    records = payload.get("records")
    if not isinstance(records, list):
        raise WoonError("source restructure manifest requires a records list")
    paths = tuple(sorted(source_root.rglob("*")))
    active = {
        path.relative_to(root).as_posix(): path
        for path in paths
        if path.is_file() and not path.is_symlink()
    }
    counts: dict[str, int] = {}
    catalog_pending_count = 0
    issues = [
        f"raw source restructure rejects symlink: {path.relative_to(root).as_posix()}"
        for path in paths
        if path.is_symlink()
    ]
    seen: set[str] = set()
    targets: dict[str, str] = {}
    reconciled_records = [
        record
        for record in records
        if isinstance(record, dict) and record.get("catalog_reconciliation") == "reconciled"
    ]
    catalog_states: dict[str, str] = {}
    if reconciled_records:
        catalog_audit = audit_source_catalog_references(root)
        catalog_states = {
            str(record["current_path"]): str(record["catalog_reconciliation"])
            for record in catalog_audit.records
        }
        if catalog_audit.issues:
            issues.append(
                "catalog reference audit has unresolved issues; "
                "no source record may be marked reconciled"
            )
    for index, record in enumerate(records, start=1):
        label = f"records[{index}]"
        if not isinstance(record, dict):
            issues.append(f"{label}: record must be a mapping")
            continue
        current = _relative(record.get("current_path"), label, "current_path", issues)
        disposition = record.get("disposition")
        if disposition not in {"move", "review"}:
            issues.append(f"{label}: disposition must be move or review")
            continue
        counts[disposition] = counts.get(disposition, 0) + 1
        if current is None:
            continue
        if current in seen:
            issues.append(f"{label}: duplicate current_path {current}")
            continue
        seen.add(current)
        source = active.get(current)
        if source is None:
            issues.append(f"{label}: current_path is not an active raw source: {current}")
            continue
        if record.get("current_sha256") != hashlib.sha256(source.read_bytes()).hexdigest():
            issues.append(f"{label}: current_sha256 does not match: {current}")
        if record.get("bytes") != source.stat().st_size:
            issues.append(f"{label}: byte count does not match: {current}")
        scope = record.get("storage_scope")
        if scope not in {"public-tracked", "private-tracked", "local-only", "review"}:
            issues.append(f"{label}: invalid storage_scope {scope!r}")
        catalog_reconciliation = record.get("catalog_reconciliation")
        if catalog_reconciliation not in {"pending", "reconciled", "not-required"}:
            issues.append(f"{label}: invalid catalog_reconciliation {catalog_reconciliation!r}")
        elif catalog_reconciliation == "pending":
            catalog_pending_count += 1
        elif catalog_reconciliation == "reconciled" and catalog_states.get(current) != "reconciled":
            issues.append(f"{label}: catalog reconciliation is not evidenced for {current}")
        target = record.get("target_path")
        if disposition == "move":
            target_path = _relative(target, label, "target_path", issues)
            if target_path is None:
                continue
            if not target_path.startswith(("sources/", "private/")):
                issues.append(f"{label}: target_path must be below sources/ or private/")
                continue
            previous = targets.setdefault(target_path, current)
            if previous != current:
                issues.append(f"{label}: target_path collision {target_path} with {previous}")
        elif target not in {None, ""}:
            issues.append(f"{label}: review record must not define target_path")
    missing = set(active) - seen
    if missing:
        issues.append(f"manifest omits {len(missing)} active raw source files")
    extra = seen - set(active)
    if extra:
        issues.append(f"manifest names {len(extra)} non-active raw source files")
    return SourceRestructurePreflight(
        file_count=len(active),
        byte_count=sum(path.stat().st_size for path in active.values()),
        disposition_counts=counts,
        catalog_pending_count=catalog_pending_count,
        issues=tuple(issues),
    )


def _move_records(root: Path, manifest: Path) -> tuple[tuple[str, str, int], ...]:
    """Load preflight-approved move records in deterministic path order."""

    try:
        payload = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"source restructure manifest is unreadable: {error}") from error
    records = payload.get("records") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise WoonError("source restructure manifest requires a records list")
    moves: list[tuple[str, str, int]] = []
    for record in records:
        if not isinstance(record, dict):
            raise WoonError("source restructure manifest contains an invalid record")
        if record.get("disposition") != "move":
            raise WoonError("source restructure cannot apply a review record")
        current = record.get("current_path")
        target = record.get("target_path")
        size = record.get("bytes")
        if not isinstance(current, str) or not isinstance(target, str) or not isinstance(size, int):
            raise WoonError("source restructure manifest move record is incomplete")
        if not (root / current).is_file():
            raise WoonError(f"raw source changed after preflight: {current}")
        moves.append((current, target, size))
    return tuple(sorted(moves))


def _raw_source_relocation_prefixes(
    records: tuple[tuple[str, str, int], ...],
) -> tuple[tuple[str, str], ...]:
    """Derive and verify the fixed five-way relocation from manifest records."""

    mappings = (
        ("wiki/private/_sources/knowledge/web/", "sources/knowledge/web/"),
        ("wiki/private/_sources/knowledge/", "private/knowledge/"),
        ("wiki/private/_sources/novel/", "private/novel/"),
        ("wiki/private/_sources/codex/", "private/codex/"),
        ("wiki/private/_sources/legacy-wiki/", "private/legacy-wiki/"),
    )
    for current, target, _size in records:
        match = next(
            (
                (old_prefix, new_prefix)
                for old_prefix, new_prefix in mappings
                if current.startswith(old_prefix)
            ),
            None,
        )
        if match is None:
            raise WoonError(f"raw source path has no approved relocation: {current}")
        old_prefix, new_prefix = match
        expected = new_prefix + current.removeprefix(old_prefix)
        if target != expected:
            raise WoonError(
                "raw source manifest target does not match approved relocation: "
                f"{current} -> {target}"
            )
    return mappings


def _planned_live_locator_rewrites(
    root: Path, replacements: tuple[tuple[str, str], ...]
) -> tuple[tuple[Path, str, int], ...]:
    """Plan all live UTF-8 locator rewrites and reject ambiguous leftovers."""

    marker = LEGACY_SOURCE_ROOT.as_posix() + "/"
    rewrites: list[tuple[Path, str, int]] = []
    unresolved: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if _excluded_from_live_locator_rewrite(relative):
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rewritten = original
        count = 0
        for current, target in replacements:
            occurrences = rewritten.count(current)
            if occurrences:
                rewritten = rewritten.replace(current, target)
                count += occurrences
        if marker in rewritten:
            unresolved.append(relative)
            continue
        if count:
            rewrites.append((path, rewritten, count))
    if unresolved:
        preview = ", ".join(unresolved[:12])
        suffix = "" if len(unresolved) <= 12 else f" (+{len(unresolved) - 12} more)"
        raise WoonError(
            "raw source locator migration leaves an ambiguous legacy root in live text: "
            + preview
            + suffix
        )
    return tuple(rewrites)


def _excluded_from_live_locator_rewrite(relative: str) -> bool:
    """Keep raw bytes and compiler provenance history immutable during a move."""

    if relative.startswith((".git/", ".local/", LEGACY_SOURCE_ROOT.as_posix() + "/")):
        return True
    return relative in {
        "catalog/llm-wiki/sources.yaml",
        "catalog/llm-wiki/claims.yaml",
    }


def _mkdirs_for_move(directory: Path, root: Path, created: set[Path]) -> None:
    """Create move parents while retaining exactly what a rollback may remove."""

    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current == root:
            raise WoonError(f"raw source move target escapes the Vault: {directory}")
        current = current.parent
    for path in reversed(missing):
        path.mkdir()
        created.add(path)


def _remove_empty_legacy_source_directories(root: Path) -> None:
    """Remove only now-empty, manifest-owned legacy directories."""

    legacy_root = root / LEGACY_SOURCE_ROOT
    for path in sorted(legacy_root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
    legacy_root.rmdir()


def _default_destination(relative: str) -> tuple[str, str | None, str]:
    prefixes = (
        ("knowledge/web/", "sources/knowledge/web/", "public-tracked"),
        ("knowledge/", "private/knowledge/", "private-tracked"),
        ("novel/", "private/novel/", "private-tracked"),
        ("codex/", "private/codex/", "local-only"),
        ("legacy-wiki/", "private/legacy-wiki/", "private-tracked"),
    )
    for current, target, scope in prefixes:
        if relative.startswith(current):
            return "move", target + relative.removeprefix(current), scope
    return "review", None, "review"


def _load_catalog_document(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def _scalar_strings(value: object, prefix: str = "$") -> tuple[tuple[str, str], ...]:
    if isinstance(value, str):
        return ((prefix, value),)
    if isinstance(value, list):
        result: list[tuple[str, str]] = []
        for index, child in enumerate(value):
            result.extend(_scalar_strings(child, f"{prefix}[{index}]"))
        return tuple(result)
    if isinstance(value, dict):
        result = []
        for key, child in value.items():
            result.extend(_scalar_strings(child, f"{prefix}.{key}"))
        return tuple(result)
    return ()


def _catalog_scalar_strings(payload: object, relative_document: str) -> tuple[tuple[str, str], ...]:
    """Return locator-bearing values while retaining inactive provenance as history.

    Archived compiler sources and superseded claims remain in the catalog so an
    earlier conclusion can be traced, but none of their fields are live
    dependencies. Their historic raw locators must therefore not block an
    otherwise hash-complete archive relocation. Every compiled source and every
    accepted claim is still scanned without exception.
    """

    if relative_document == "catalog/llm-wiki/sources.yaml":
        records = payload.get("sources") if isinstance(payload, dict) else None
        if isinstance(records, list):
            values: list[tuple[str, str]] = []
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    values.extend(_scalar_strings(record, f"$.sources[{index}]"))
                    continue
                if record.get("lifecycle") == "archived":
                    continue
                for key, value in record.items():
                    values.extend(_scalar_strings(value, f"$.sources[{index}].{key}"))
            return tuple(values)
    if relative_document == "catalog/llm-wiki/claims.yaml":
        records = payload.get("claims") if isinstance(payload, dict) else None
        if isinstance(records, list):
            values = []
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    values.extend(_scalar_strings(record, f"$.claims[{index}]"))
                    continue
                if record.get("status") == "superseded":
                    continue
                for key, value in record.items():
                    values.extend(_scalar_strings(value, f"$.claims[{index}].{key}"))
            return tuple(values)
    return _scalar_strings(payload)


def _raw_source_locators(value: str, active: set[str]) -> tuple[tuple[str, str], ...]:
    """Extract raw-byte locators from either supported on-disk layout.

    The legacy tree stays readable until a hash-checked relocation completes.
    Afterward the five explicit public/private roots are the only valid raw
    byte locations.  Auditing both spellings makes a stale legacy locator a
    visible error rather than silently treating it as ordinary prose.
    """

    locators: list[tuple[str, str]] = []
    for root_path, _archive_prefix in ((LEGACY_SOURCE_ROOT, ""), *RESTRUCTURED_SOURCE_ROOTS):
        marker = root_path.as_posix() + "/"
        start = 0
        while True:
            offset = value.find(marker, start)
            if offset < 0:
                break
            end = len(value)
            for delimiter in ("\n", "\r", "`", '"', "'", ")", "]", ">", "|"):
                candidate = value.find(delimiter, offset)
                if candidate >= 0:
                    end = min(end, candidate)
            locator = value[offset:end].strip().rstrip(".,;:")
            source_path = locator.partition("#")[0]
            if source_path and (root_path == LEGACY_SOURCE_ROOT or source_path in active):
                locators.append((source_path, locator))
            start = offset + len(marker)
    return tuple(locators)


def _catalog_identity_scalar(scalar_path: str) -> bool:
    """Exclude canonical IDs, whose slugs may deliberately resemble raw paths."""

    fields = ("claim_id", "source_id", "source_ids", "page_id", "canonical_id", "parent_id")
    return any(f".{field}" in scalar_path for field in fields)


def _active_raw_source_paths(root: Path) -> tuple[str, ...]:
    """Return all raw bytes from one complete legacy or restructured layout."""

    roots = _raw_source_layout_roots(root)
    paths: list[str] = []
    for source_root, _archive_prefix in roots:
        for path in source_root.rglob("*"):
            if path.is_symlink():
                raise WoonError(f"raw source restructure rejects symlink: {path}")
            if path.is_file():
                paths.append(path.relative_to(root).as_posix())
    return tuple(sorted(paths))


def _raw_source_locator(root: Path, path: Path) -> str:
    """Map one active raw path to the archive-relative catalog locator."""

    for source_root, prefix in _raw_source_layout_roots(root):
        if path.is_relative_to(source_root):
            relative = path.relative_to(source_root).as_posix()
            return f"{prefix}/{relative}" if prefix else relative
    raise WoonError(f"path is not an active raw source: {path}")


def _raw_source_layout_roots(root: Path) -> tuple[tuple[Path, str], ...]:
    """Choose exactly one complete raw-source layout and reject mixed trees."""

    legacy_root = root / LEGACY_SOURCE_ROOT
    restructured = tuple(
        (root / relative, prefix) for relative, prefix in RESTRUCTURED_SOURCE_ROOTS
    )
    present = tuple((path, prefix) for path, prefix in restructured if path.exists())
    if legacy_root.exists():
        if not legacy_root.is_dir():
            raise WoonError(f"legacy raw source root is not a directory: {legacy_root}")
        if present:
            raise WoonError("raw source layout is mixed between legacy and restructured roots")
        return ((legacy_root, ""),)
    missing = [
        path.relative_to(root).as_posix() for path, _prefix in restructured if not path.is_dir()
    ]
    if missing:
        raise WoonError(
            "restructured raw source layout is incomplete; missing: " + ", ".join(sorted(missing))
        )
    return restructured


def _relative(value: object, label: str, field: str, issues: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        issues.append(f"{label}: {field} must be a non-empty relative path")
        return None
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        issues.append(f"{label}: {field} escapes the Vault: {value!r}")
        return None
    return candidate.as_posix()
