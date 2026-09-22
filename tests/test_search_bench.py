"""Behaviour of the retrieval benchmark that measures the bounded search index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.adapters.sqlite_search import SQLiteFtsSearchIndex
from woon_core.knowledge.search_bench import (
    load_bench_queries,
    read_wiki_documents,
    run_search_bench,
    write_bench_result,
)


def _page(root: Path, relative: str, title: str, body: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {title}\n---\n\n# {title}\n\n{body}\n", encoding="utf-8")


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path / "wiki"
    _page(
        root,
        "ai/beam.md",
        "빔 검색",
        "## 후보\n\n여러 후보를 유지한다.\n\n## 비용\n\n계산이 늘어난다.",
    )
    _page(root, "ai/greedy.md", "그리디", "매 스텝 최고 확률 토큰 하나만 고른다.")
    _page(root, "private/secret.md", "비공개", "여러 후보를 유지한다.")
    return root


def test_private_pages_never_enter_the_benchmark_corpus(wiki: Path) -> None:
    paths = {document.relative_path for document in read_wiki_documents(wiki)}
    assert paths == {"ai/beam.md", "ai/greedy.md"}


def test_unparsable_frontmatter_still_indexes_the_body(tmp_path: Path, wiki: Path) -> None:
    (wiki / "ai/broken.md").write_text(
        "---\nsummary: 상태: 채택\n---\n\n# 깨진 머리말\n\n본문은 살아 있다.\n", encoding="utf-8"
    )
    documents = {item.relative_path: item for item in read_wiki_documents(wiki)}
    assert "본문은 살아 있다." in documents["ai/broken.md"].body


def test_bench_reports_rank_and_the_cost_of_each_context_width(tmp_path: Path, wiki: Path) -> None:
    queries = load_bench_queries(_queries(tmp_path, "여러 후보를 유지한다", "ai/beam.md"))
    report = run_search_bench(wiki, queries, tmp_path / "bench.sqlite3")

    assert report.precision_at_1 == 1.0
    assert report.precision_at_5 == 1.0
    result = report.results[0]
    # A section costs less to read than the whole page, and the neighbour window
    # sits between the two; that ordering is the point of position-based context.
    assert 0 < result.section_chars < result.section_window_chars < result.document_chars


def test_bench_records_a_miss_instead_of_failing(tmp_path: Path, wiki: Path) -> None:
    queries = load_bench_queries(
        _queries(tmp_path, "존재하지 않는 완전히 다른 주제 문자열", "ai/beam.md")
    )
    report = run_search_bench(wiki, queries, tmp_path / "bench.sqlite3")

    assert report.results[0].hit_rank is None
    assert report.precision_at_1 == 0.0


def test_expected_document_outside_the_wiki_is_rejected(tmp_path: Path, wiki: Path) -> None:
    queries = load_bench_queries(_queries(tmp_path, "후보", "ai/missing.md"))
    with pytest.raises(WoonError, match="not in the Wiki"):
        run_search_bench(wiki, queries, tmp_path / "bench.sqlite3")


def test_a_query_without_its_evidence_line_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "unsourced.yaml"
    path.write_text(
        "version: 1\nqueries:\n  - query: 근거 없는 질의\n    expected: a.md\n", encoding="utf-8"
    )
    with pytest.raises(WoonError, match="evidence"):
        load_bench_queries(path)


def test_a_real_vault_page_returns_its_neighbour_sections(tmp_path: Path) -> None:
    """The neighbour window has to hold on real Wiki prose, not only fixtures.

    Fixture pages are short and regular; vault pages carry code fences, tables
    and sections long enough to be split again. This indexes one real page and
    asks for the sections either side of a hit.
    """

    wiki = _vault_wiki_or_skip()
    document = next(
        (item for item in read_wiki_documents(wiki) if item.body.count("\n## ") >= 3), None
    )
    assert document is not None, "vault has no page with three sections"
    index = SQLiteFtsSearchIndex(tmp_path / "vault.sqlite3")
    index.rebuild([document])

    hits = index.search(document.title, 5)
    excerpts = [index.read_excerpt(hit.document_id, hit.chunk_id, 1, 1) for hit in hits]
    neighboured = [item for item in excerpts if item.context_before or item.context_after]

    assert neighboured, f"{document.relative_path}에서 이웃 조각이 하나도 오지 않았다"
    for excerpt in neighboured:
        for neighbor in (*excerpt.context_before, *excerpt.context_after):
            assert abs(neighbor.position - excerpt.position) == 1
            assert neighbor.text in document.body
        assert [item.position for item in excerpt.context_before] < [excerpt.position]
        assert [item.position for item in excerpt.context_after] > [excerpt.position]


def _vault_wiki_or_skip() -> Path:
    from woon_core.knowledge.factory import resolve_knowledge_vault

    try:
        wiki = resolve_knowledge_vault() / "wiki"
    except Exception:  # noqa: BLE001 - no registered vault is a skip, not a failure
        pytest.skip("a registered knowledge vault is required for the real-page gate")
    if not wiki.is_dir():
        pytest.skip("a registered knowledge vault is required for the real-page gate")
    return wiki


def test_duplicate_queries_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "duplicated.yaml"
    path.write_text(
        "version: 1\nqueries:\n"
        "  - query: 같은 질의\n    expected: a.md\n    evidence: 어느 절\n"
        "  - query: 같은 질의\n    expected: b.md\n    evidence: 어느 절\n",
        encoding="utf-8",
    )
    with pytest.raises(WoonError, match="duplicated"):
        load_bench_queries(path)


def test_recorded_result_keeps_no_machine_path(tmp_path: Path, wiki: Path) -> None:
    queries = load_bench_queries(_queries(tmp_path, "여러 후보를 유지한다", "ai/beam.md"))
    report = run_search_bench(wiki, queries, tmp_path / "bench.sqlite3")

    written = write_bench_result(report, tmp_path / "results")

    assert written.name == f"{report.generated_at[:10]}.json"
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["wiki_root"] == "wiki"
    assert str(tmp_path) not in written.read_text(encoding="utf-8")


def _queries(tmp_path: Path, query: str, expected: str) -> Path:
    path = tmp_path / "queries.yaml"
    path.write_text(
        f"version: 1\nqueries:\n  - query: {query}\n    expected: {expected}\n"
        f"    evidence: 픽스처 본문 한 줄\n",
        encoding="utf-8",
    )
    return path
