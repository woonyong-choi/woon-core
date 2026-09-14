"""Terminal receipts for explicitly reviewed legacy Inbox cards."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock


def _paths(vault: Path, relative_path: str, source_sha256: str) -> tuple[Path, Path]:
    root = vault.expanduser().resolve()
    relative = Path(relative_path)
    if (
        not relative.is_relative_to("brain/review")
        or relative.suffix != ".md"
        or ".." in relative.parts
        or not re.fullmatch(r"[0-9a-f]{64}", source_sha256)
    ):
        raise WoonError("review resolution requires an exact card path and SHA-256")
    identity = hashlib.sha256(f"{relative_path}\0{source_sha256}".encode()).hexdigest()[:32]
    receipt = Path(f".local/woon-knowledge/document-intake/resolutions/review-{identity}.json")
    for relative_item in (relative, receipt):
        current = root
        for part in relative_item.parts:
            current /= part
            if current.is_symlink():
                raise WoonError("review resolution refuses symlink paths")
    return root / relative, root / receipt


def read_review_resolution(
    vault: Path,
    *,
    relative_path: str,
    source_sha256: str,
) -> dict[str, Any] | None:
    """Read one reviewed input's terminal state without needing the removed card."""
    card, receipt = _paths(vault, relative_path, source_sha256)
    if not receipt.exists():
        return None
    try:
        value = json.loads(receipt.read_text())
    except (OSError, ValueError) as error:
        raise WoonError("review terminal receipt is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("path") != relative_path
        or value.get("source_sha256") != source_sha256
        or value.get("state") not in {"cleanup-pending", "complete"}
    ):
        raise WoonError("invalid review terminal receipt")
    if value["state"] == "complete" and card.exists():
        raise WoonError("completed review card reappeared; preserve it for reconciliation")
    return value


def complete_review(
    vault: Path,
    *,
    relative_path: str,
    source_sha256: str,
    disposition: str,
    review_reference: str,
) -> dict[str, Any]:
    """Remove only a hash-pinned, reviewed Candidate and retain minimal result metadata.

    The caller checks actual canonical meaning (integrated) or obsolete premises
    before invoking this operation. A reference is that owner's bounded evidence
    pointer, not a content-verification claim inferred from a title.
    """
    if (
        disposition not in {"integrated", "obsolete"}
        or not isinstance(review_reference, str)
        or not 1 <= len(review_reference.strip()) <= 512
    ):
        raise WoonError("review completion needs a disposition and bounded evidence reference")
    card, receipt = _paths(vault, relative_path, source_sha256)
    with exclusive_file_lock(receipt.with_suffix(".lock")):
        value = read_review_resolution(
            vault,
            relative_path=relative_path,
            source_sha256=source_sha256,
        )
        if value and value["state"] == "complete":
            return {**value, "replayed": True}
        if value is None:
            if not card.is_file() or hashlib.sha256(card.read_bytes()).hexdigest() != source_sha256:
                raise WoonError("review card changed; preserve it and reread")
            from woon_core.knowledge.wiki_tree import split_markdown

            header, _ = split_markdown(card.read_text())
            if header.get("type") != "Candidate" or header.get("status") != "Review":
                raise WoonError("review completion only owns unmodified Candidate cards")
            value = {
                "version": 1,
                "path": relative_path,
                "source_sha256": source_sha256,
                "state": "cleanup-pending",
                "disposition": disposition,
                "review_reference": review_reference,
            }
            atomic_write(receipt, encode_json(value), mode=0o600)
        if card.exists():
            if hashlib.sha256(card.read_bytes()).hexdigest() != source_sha256:
                raise WoonError("review card changed before cleanup; preserve it")
            card.unlink()
        value["state"] = "complete"
        atomic_write(receipt, encode_json(value), mode=0o600)
        return {**value, "replayed": False}
