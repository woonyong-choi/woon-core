"""Retrieval benchmark for the bounded knowledge search index.

The benchmark measures the search index itself: it reads Markdown under a Wiki
root, builds one throwaway FTS index with the production adapter and chunker,
and reports ranking quality next to how much text an answer costs to read.

It deliberately does not go through ``KnowledgeService``. Ranking lives entirely
in the index, so routing through the canonical repository would only add vault
schema validation that a retrieval measurement has no reason to depend on.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.adapters.sqlite_search import SQLiteFtsSearchIndex
from woon_core.knowledge.domain import IndexedDocument

DEFAULT_LIMIT = 5
DEFAULT_MAX_CHUNK_CHARS = 6000
PRIVATE_DIRECTORY = "private"


@dataclass(frozen=True, slots=True)
class BenchQuery:
    """One benchmark question and the single Wiki document that answers it."""

    query: str
    expected: str


@dataclass(frozen=True, slots=True)
class BenchQueryResult:
    """Ranking and reading-cost measurement for one benchmark query."""

    query: str
    expected: str
    hit_rank: int | None
    elapsed_ms: float
    document_chars: int
    section_chars: int
    section_window_chars: int


@dataclass(frozen=True, slots=True)
class BenchReport:
    """Aggregate benchmark result written to ``bench/results/<date>.json``."""

    generated_at: str
    wiki_root: str
    documents: int
    chunks: int
    queries: int
    limit: int
    precision_at_1: float
    precision_at_5: float
    mean_elapsed_ms: float
    mean_document_chars: float
    mean_section_chars: float
    mean_section_window_chars: float
    results: tuple[BenchQueryResult, ...]


def load_bench_queries(path: Path) -> tuple[BenchQuery, ...]:
    """Read the query set, rejecting duplicates so each answer stays unique."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise WoonError(f"benchmark query file is not readable YAML: {path}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("queries"), list):
        raise WoonError("benchmark query file must map 'queries' to a list")
    queries: list[BenchQuery] = []
    seen: set[str] = set()
    for entry in raw["queries"]:
        if not isinstance(entry, dict):
            raise WoonError("each benchmark query must be a mapping")
        text = str(entry.get("query", "")).strip()
        expected = str(entry.get("expected", "")).strip()
        if not text or not expected:
            raise WoonError("each benchmark query needs 'query' and 'expected'")
        if text in seen:
            raise WoonError(f"benchmark query is duplicated: {text}")
        seen.add(text)
        queries.append(BenchQuery(text, expected))
    if not queries:
        raise WoonError("benchmark query file contains no queries")
    return tuple(queries)


def read_wiki_documents(wiki_root: Path) -> list[IndexedDocument]:
    """Load every Markdown page under the Wiki root as an indexable document.

    Frontmatter that fails to parse is treated as absent rather than fatal: a
    retrieval benchmark measures ranking, not canonical schema compliance.
    """

    if not wiki_root.is_dir():
        raise WoonError(f"benchmark Wiki root does not exist: {wiki_root}")
    documents: list[IndexedDocument] = []
    for path in sorted(wiki_root.rglob("*.md")):
        # The production search config excludes wiki/private/**; a benchmark must
        # not pull private pages into a recorded, committed result file.
        if path.relative_to(wiki_root).parts[0] == PRIVATE_DIRECTORY:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        metadata, body = _split_frontmatter(text)
        relative = path.relative_to(wiki_root).as_posix()
        documents.append(
            IndexedDocument(
                document_id=relative,
                canonical_id=_optional_string(metadata.get("id")),
                title=_title(metadata, body, path),
                summary=str(metadata.get("summary") or ""),
                body=body,
                relative_path=relative,
                revision=str(metadata.get("updated") or ""),
                source_type="wiki",
            )
        )
    if not documents:
        raise WoonError(f"benchmark Wiki root has no Markdown documents: {wiki_root}")
    return documents


def run_search_bench(
    wiki_root: Path,
    queries: tuple[BenchQuery, ...],
    database: Path,
    *,
    limit: int = DEFAULT_LIMIT,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
) -> BenchReport:
    """Index the Wiki once, then time every query and measure its reading cost."""

    documents = read_wiki_documents(wiki_root)
    body_chars = {document.document_id: len(document.body) for document in documents}
    known = set(body_chars)
    missing = sorted({query.expected for query in queries} - known)
    if missing:
        raise WoonError(f"benchmark expects a document that is not in the Wiki: {missing[0]}")

    database.parent.mkdir(parents=True, exist_ok=True)
    database.unlink(missing_ok=True)
    index = SQLiteFtsSearchIndex(database, max_chunk_chars=max_chunk_chars)
    index.rebuild(documents)

    results: list[BenchQueryResult] = []
    for query in queries:
        started = time.perf_counter()
        hits = index.search(query.query, limit)
        elapsed_ms = (time.perf_counter() - started) * 1000
        rank = next(
            (
                position
                for position, hit in enumerate(hits[:limit], start=1)
                if hit.relative_path == query.expected
            ),
            None,
        )
        section_chars = 0
        window_chars = 0
        if rank is not None:
            hit = hits[rank - 1]
            excerpt = index.read_excerpt(hit.document_id, hit.chunk_id, before=1, after=1)
            section_chars = len(excerpt.text)
            window_chars = section_chars + sum(
                len(neighbor.text) for neighbor in (*excerpt.context_before, *excerpt.context_after)
            )
        results.append(
            BenchQueryResult(
                query=query.query,
                expected=query.expected,
                hit_rank=rank,
                elapsed_ms=round(elapsed_ms, 3),
                document_chars=body_chars[query.expected],
                section_chars=section_chars,
                section_window_chars=window_chars,
            )
        )

    found = [item for item in results if item.hit_rank is not None]
    total = len(results)
    return BenchReport(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        wiki_root=str(wiki_root),
        documents=len(documents),
        chunks=index.statistics().chunks,
        queries=total,
        limit=limit,
        precision_at_1=_ratio(sum(1 for item in results if item.hit_rank == 1), total),
        precision_at_5=_ratio(len(found), total),
        mean_elapsed_ms=_mean(item.elapsed_ms for item in results),
        mean_document_chars=_mean(item.document_chars for item in found),
        mean_section_chars=_mean(item.section_chars for item in found),
        mean_section_window_chars=_mean(item.section_window_chars for item in found),
        results=tuple(results),
    )


def format_bench_table(report: BenchReport) -> str:
    """Render the benchmark as the two tables the README and handoff quote."""

    lines = [
        f"wiki: {report.wiki_root}",
        f"documents: {report.documents}  chunks: {report.chunks}  queries: {report.queries}",
        "",
        "| 지표 | 값 |",
        "| --- | --- |",
        f"| P@1 | {report.precision_at_1:.2f} |",
        f"| P@5 | {report.precision_at_5:.2f} |",
        f"| 평균 소요 ms | {report.mean_elapsed_ms:.2f} |",
        f"| 문맥 문자 수 (전체) | {report.mean_document_chars:.0f} |",
        f"| 문맥 문자 수 (절) | {report.mean_section_chars:.0f} |",
        f"| 문맥 문자 수 (절±1) | {report.mean_section_window_chars:.0f} |",
        "",
        "| 질의 | 정답 | 순위 | ms | 전체 | 절 | 절±1 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report.results:
        rank = "-" if item.hit_rank is None else str(item.hit_rank)
        lines.append(
            f"| {item.query} | {item.expected} | {rank} | {item.elapsed_ms:.2f} "
            f"| {item.document_chars} | {item.section_chars} | {item.section_window_chars} |"
        )
    return "\n".join(lines)


def write_bench_result(report: BenchReport, results_dir: Path) -> Path:
    """Record the run under ``<results_dir>/<UTC date>.json``.

    The Wiki root is reduced to its final path component: the result file is
    committed, and the absolute path is a personal machine path.
    """

    results_dir.mkdir(parents=True, exist_ok=True)
    path = results_dir / f"{report.generated_at[:10]}.json"
    payload = asdict(report)
    payload["wiki_root"] = Path(report.wiki_root).name
    atomic_write(path, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode())
    return path


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    body = text[end + 4 :].lstrip("\n")
    try:
        metadata = yaml.safe_load(text[4:end])
    except yaml.YAMLError:
        return {}, body
    return (metadata if isinstance(metadata, dict) else {}), body


def _title(metadata: dict[str, object], body: str, path: Path) -> str:
    declared = _optional_string(metadata.get("title"))
    if declared:
        return declared
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem


def _optional_string(value: object) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _ratio(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _mean(values: object) -> float:
    items = list(values)  # type: ignore[call-overload]
    return round(sum(items) / len(items), 3) if items else 0.0
