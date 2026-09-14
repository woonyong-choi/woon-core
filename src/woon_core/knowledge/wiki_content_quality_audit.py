"""Read-only readiness inventory for every active Wiki document.

The compiler proves provenance, and a semantic review proves whether prose is
ready for readers.  This module deliberately proves neither by heuristic.  It
only gives a complete, hash-pinned inventory of the document kinds, fenced
code, execution evidence still required, and current publication boundary so
that a full Wiki migration cannot silently treat navigation or source-bound
material like ordinary runnable lessons.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.wiki_tree import iter_wiki_pages, split_markdown

WIKI_CONTENT_QUALITY_AUDIT_VERSION = 1
_FENCE = re.compile(r"(?ms)^```(?P<info>[^\n]*)\n(?P<body>.*?)^```[ \t]*$")
_NAVIGATION_KINDS = frozenset({"root", "hub", "entity"})
_PUBLICATION_STATES = frozenset({"private", "review", "publish"})


@dataclass(frozen=True, slots=True)
class WikiContentQualityRecord:
    """One active Wiki document classified without inferring prose quality."""

    canonical_id: str
    relative_path: str
    sha256: str
    source_owner: str
    document_kind: str
    quality_status: str
    fenced_code_blocks: int
    runnable_code_blocks: int
    static_code_blocks: int
    execution_contract: str
    publication_state: str
    publication_readiness: str


@dataclass(frozen=True, slots=True)
class WikiContentQualityAudit:
    """Complete, deterministic readiness state for current active Wiki files."""

    document_count: int
    compiler_documents: int
    manual_documents: int
    navigation_documents: int
    reader_documents: int
    runnable_code_blocks: int
    execution_contract_counts: dict[str, int]
    quality_status_counts: dict[str, int]
    publication_readiness_counts: dict[str, int]
    errors: tuple[str, ...]
    records: tuple[WikiContentQualityRecord, ...]


def audit_wiki_content_quality(vault: Path) -> WikiContentQualityAudit:
    """Inventory every active Wiki page without rewriting content or metadata.

    ``runnable-unverified`` is intentionally not promoted to verified merely
    because a Markdown fence uses a ``run-*`` language.  Verification needs a
    separate runtime receipt (and, for book source material, its coverage
    contract).  Likewise, normal detail pages remain ``pending-semantic-review``
    until a review result is bound to their current hash.
    """

    root = vault.expanduser().resolve()
    wiki_root = root / "wiki"
    if not wiki_root.is_dir():
        raise WoonError(f"Wiki content quality audit requires wiki/: {wiki_root}")
    compiler_paths = _compiler_output_paths(root)
    book_canonical_ids = _book_canonical_ids(root)
    records: list[WikiContentQualityRecord] = []
    errors: list[str] = []
    seen_canonical_ids: set[str] = set()
    for path in iter_wiki_pages(wiki_root):
        relative_path = path.relative_to(root).as_posix()
        try:
            metadata, body = split_markdown(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as error:
            errors.append(f"unreadable Wiki document: {relative_path}: {type(error).__name__}")
            continue
        canonical_id = metadata.get("canonical_id")
        if not isinstance(canonical_id, str) or not canonical_id.strip():
            errors.append(f"Wiki document has no canonical_id: {relative_path}")
            continue
        canonical_id = canonical_id.strip()
        if canonical_id in seen_canonical_ids:
            errors.append(f"duplicate canonical_id: {canonical_id}")
            continue
        seen_canonical_ids.add(canonical_id)
        owner = compiler_paths.get(relative_path)
        records.append(
            _record(
                relative_path,
                canonical_id,
                path.read_bytes(),
                metadata,
                body,
                "compiler" if owner is not None else "manual",
                canonical_id in book_canonical_ids,
            )
        )
    ordered = tuple(sorted(records, key=lambda item: item.relative_path))
    return WikiContentQualityAudit(
        document_count=len(ordered),
        compiler_documents=sum(item.source_owner == "compiler" for item in ordered),
        manual_documents=sum(item.source_owner == "manual" for item in ordered),
        navigation_documents=sum(item.document_kind == "navigation" for item in ordered),
        reader_documents=sum(item.document_kind == "reader" for item in ordered),
        runnable_code_blocks=sum(item.runnable_code_blocks for item in ordered),
        execution_contract_counts=_counts(item.execution_contract for item in ordered),
        quality_status_counts=_counts(item.quality_status for item in ordered),
        publication_readiness_counts=_counts(item.publication_readiness for item in ordered),
        errors=tuple(sorted(set(errors))),
        records=ordered,
    )


def render_wiki_content_quality_audit(
    vault: Path, report: WikiContentQualityAudit | None = None
) -> bytes:
    """Render the local-only audit as deterministic JSON."""

    audit = report or audit_wiki_content_quality(vault)
    payload = {
        "kind": "wiki-content-quality-audit",
        "version": WIKI_CONTENT_QUALITY_AUDIT_VERSION,
        "document_count": audit.document_count,
        "compiler_documents": audit.compiler_documents,
        "manual_documents": audit.manual_documents,
        "navigation_documents": audit.navigation_documents,
        "reader_documents": audit.reader_documents,
        "runnable_code_blocks": audit.runnable_code_blocks,
        "execution_contract_counts": audit.execution_contract_counts,
        "quality_status_counts": audit.quality_status_counts,
        "publication_readiness_counts": audit.publication_readiness_counts,
        "errors": list(audit.errors),
        "records": [asdict(record) for record in audit.records],
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_wiki_content_quality_audit(
    vault: Path, output_path: Path, report: WikiContentQualityAudit | None = None
) -> Path:
    """Write exactly one immutable audit below the local quality workspace."""

    root = vault.expanduser().resolve()
    output = output_path.expanduser().resolve()
    local_root = root / ".local/woon-knowledge/wiki-content-quality"
    if not output.is_relative_to(local_root):
        raise WoonError(
            "Wiki content quality audit must stay below .local/woon-knowledge/wiki-content-quality"
        )
    if output.exists():
        raise WoonError(f"Wiki content quality audit already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write(output, render_wiki_content_quality_audit(root, report), mode=0o600)
    return output


def _compiler_output_paths(root: Path) -> dict[str, str]:
    path = root / "catalog/llm-wiki/pages.yaml"
    if not path.is_file():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise WoonError(f"Wiki compiler page catalog is unreadable: {path}") from error
    raw_pages = payload.get("pages") if isinstance(payload, dict) else None
    if not isinstance(raw_pages, list):
        raise WoonError("Wiki compiler page catalog requires pages")
    outputs: dict[str, str] = {}
    for raw in raw_pages:
        if not isinstance(raw, dict):
            raise WoonError("Wiki compiler page catalog entry must be a mapping")
        page_id = raw.get("page_id")
        output_path = raw.get("output_path")
        if not isinstance(page_id, str) or not page_id.strip():
            raise WoonError("Wiki compiler page catalog entry has no page_id")
        if not isinstance(output_path, str) or not output_path.strip():
            raise WoonError("Wiki compiler page catalog entry has no output_path")
        relative = Path(output_path)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
            raise WoonError("Wiki compiler page output_path is unsafe")
        key = (Path("wiki") / relative).as_posix()
        if key in outputs:
            raise WoonError(f"duplicate Wiki compiler output_path: {key}")
        # ``page_id`` is a stable compiler provenance identifier. It is not
        # necessarily the document's public/private canonical identity after a
        # path migration, so only use this catalog for writer ownership here.
        outputs[key] = page_id.strip()
    return outputs


def _record(
    relative_path: str,
    canonical_id: str,
    content: bytes,
    metadata: dict[str, Any],
    body: str,
    owner: str,
    is_book_document: bool,
) -> WikiContentQualityRecord:
    node_kind = metadata.get("node_kind")
    navigation = isinstance(node_kind, str) and node_kind in _NAVIGATION_KINDS
    reader = is_book_document and not navigation
    fences = tuple(_FENCE.finditer(body))
    run_count = sum(_is_run_fence(match.group("info")) for match in fences)
    static_count = len(fences) - run_count
    execution_contract = _execution_contract(len(fences), run_count, reader)
    state = metadata.get("publication_state")
    publication_state = (
        state if isinstance(state, str) and state in _PUBLICATION_STATES else "unset"
    )
    if navigation:
        document_kind = "navigation"
        quality_status = "navigation-exempt"
    elif reader:
        document_kind = "reader"
        quality_status = "book-contract-required"
    else:
        document_kind = "detail"
        quality_status = "pending-semantic-review"
    return WikiContentQualityRecord(
        canonical_id=canonical_id,
        relative_path=relative_path,
        sha256=hashlib.sha256(content).hexdigest(),
        source_owner=owner,
        document_kind=document_kind,
        quality_status=quality_status,
        fenced_code_blocks=len(fences),
        runnable_code_blocks=run_count,
        static_code_blocks=static_count,
        execution_contract=execution_contract,
        publication_state=publication_state,
        publication_readiness=_publication_readiness(publication_state, metadata),
    )


def _is_run_fence(info: str) -> bool:
    language = info.strip().split(maxsplit=1)[0] if info.strip() else ""
    return language.startswith("run-") and len(language) > len("run-")


def _execution_contract(fence_count: int, run_count: int, reader: bool) -> str:
    if reader:
        return "book-contract-required"
    if fence_count == 0:
        return "not-applicable"
    if run_count == 0:
        return "static-only"
    if run_count == fence_count:
        return "runnable-unverified"
    return "mixed-review"


def _publication_readiness(state: str, metadata: dict[str, Any]) -> str:
    if state == "publish":
        return "requires-projection-preflight"
    if state == "review":
        return "review-required"
    if state == "private":
        return "private"
    if metadata.get("access") == "local-only":
        return "private"
    return "unclassified"


def _book_canonical_ids(root: Path) -> set[str]:
    """Load coverage-owned book identities without inferring them from paths.

    Book reader pages may temporarily live under a legacy path during a tree
    migration.  The coverage manifest is the stable ownership source, so a
    path heuristic would incorrectly classify those pages as ordinary detail
    documents exactly when the audit matters most.
    """

    coverage_root = root / "catalog/book-coverage"
    if not coverage_root.is_dir():
        return set()
    canonical_ids: set[str] = set()
    for path in sorted(coverage_root.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise WoonError(f"book coverage manifest is unreadable: {path}") from error
        if not isinstance(payload, dict):
            raise WoonError(f"book coverage manifest must be a mapping: {path}")
        book_id = payload.get("book_id")
        if not isinstance(book_id, str) or not book_id.strip():
            raise WoonError(f"book coverage manifest has no book_id: {path}")
        canonical_ids.add(book_id.strip())
        nodes = payload.get("nodes")
        if not isinstance(nodes, list):
            raise WoonError(f"book coverage manifest requires nodes: {path}")
        for node in nodes:
            if not isinstance(node, dict):
                raise WoonError(f"book coverage node must be a mapping: {path}")
            canonical_id = node.get("canonical_id")
            if not isinstance(canonical_id, str) or not canonical_id.strip():
                raise WoonError(f"book coverage node has no canonical_id: {path}")
            canonical_ids.add(canonical_id.strip())
    return canonical_ids


def _counts(values: Any) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        result[value] = result.get(value, 0) + 1
    return dict(sorted(result.items()))
