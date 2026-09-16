"""Shared, material-safe scope for semantic Wiki quality review.

General learning prose can be assessed against the learning-writing harness.
Book readers and private Novel material have different ownership contracts: a
book is verified through its edition/coverage contract, and Novel bytes never
leave the private boundary.  Link-only source indexes are instead checked by
the navigation contract. This module names those exclusions without copying
their Markdown into a hosted-review plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from woon_core.errors import WoonError

PRIVATE_NOVEL_REASON = "private-novel-hosted-review-prohibited"
BOOK_READER_REASON = "book-reader-contract"
SOURCE_INDEX_REASON = "source-index-contract"


def quality_review_page_ids(value: object) -> tuple[str, ...]:
    """Validate exact, unique page IDs; an omitted/empty selection means all pages."""

    if not isinstance(value, (list, tuple)) or any(
        not isinstance(page_id, str) or not page_id or page_id != page_id.strip()
        for page_id in value
    ):
        raise WoonError("quality review page_ids must contain nonblank exact page IDs")
    if len(set(value)) != len(value):
        raise WoonError("quality review page_ids must not contain duplicate pages")
    return tuple(sorted(value))


def book_reader_roots(pages: Iterable[Mapping[str, object]]) -> set[str]:
    """Find explicit book entities whose descendants are reader-owned.

    The root record is the canonical, schema-owned signal.  Path names are not
    guessed: a page becomes a reader only when it is the book entity itself or
    lies below that entity's canonical identifier.
    """

    roots: set[str] = set()
    for page in pages:
        page_id = page.get("page_id")
        frontmatter = page.get("frontmatter")
        if (
            not isinstance(page_id, str)
            or not page_id.strip()
            or not isinstance(frontmatter, Mapping)
        ):
            continue
        if frontmatter.get("content_kind") == "book" or frontmatter.get("entity_kind") == "book":
            roots.add(page_id.strip())
    return roots


def content_quality_exclusion_reason(
    page_id: str,
    output_path: str,
    book_roots: Iterable[str],
    frontmatter: Mapping[str, object] | None = None,
) -> str | None:
    """Return the contract that owns a page outside general prose review."""

    if output_path == "private/novel" or output_path.startswith("private/novel/"):
        return PRIVATE_NOVEL_REASON
    for root in book_roots:
        if page_id == root or page_id.startswith(root + "/"):
            return BOOK_READER_REASON
    if isinstance(frontmatter, Mapping) and frontmatter.get("content_kind") == "source-index":
        return SOURCE_INDEX_REASON
    return None
