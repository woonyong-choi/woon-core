import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.graph_colors import (
    ENTITY_COLORS,
    LEGACY_RGB,
    PRIVATE_RGB,
    RESOURCE_RGB,
    derive_graph_colors,
    graph_color_issues,
    graph_color_projection,
    graph_metadata,
)
from woon_core.knowledge.local_settings import configure_navigation


def color_tree() -> dict[str, dict]:
    nodes = {
        "wiki/README.md": {
            "canonical_id": "README",
            "navigation_groups": [
                {
                    "children": ["boundary", "concepts", "resources", "creative"],
                }
            ],
        },
        "wiki/opaque/root.md": {
            "canonical_id": "boundary",
            "title": "공개 Wiki 경계",
            "parent": "[[wiki/README]]",
            "access": "local-only",
            "public_taxonomy": {
                "roots": [
                    "CS",
                    "프로그래밍",
                    "시스템",
                    "Data",
                    "프론트엔드",
                    "백엔드",
                    "플랫폼",
                    "AI",
                    "프로젝트",
                ],
            },
        },
        "wiki/opaque/concepts.md": {
            "canonical_id": "concepts",
            "title": "개념",
            "domain": "concepts",
            "parent": "[[wiki/README]]",
        },
        "wiki/opaque/resources.md": {
            "canonical_id": "resources",
            "title": "리소스",
            "parent": "[[wiki/README]]",
        },
        "wiki/opaque/creative.md": {
            "canonical_id": "creative",
            "title": "창작",
            "facets": ["창작"],
            "parent": "[[wiki/README]]",
        },
        "wiki/old.md": {"canonical_id": "old", "parent": "[[wiki/opaque/concepts]]"},
        "wiki/unlinked.md": {"canonical_id": "unlinked", "parent": "[[wiki/missing]]"},
        "wiki/private/public-looking.md": {
            "canonical_id": "sensitive",
            "access": "public",
            "parent": "[[wiki/opaque/0]]",
        },
    }
    for index, name in enumerate(nodes["wiki/opaque/root.md"]["public_taxonomy"]["roots"]):
        nodes[f"wiki/opaque/{index}.md"] = {
            "canonical_id": f"field-{index}",
            "title": name,
            "access": "public",
            "parent": "[[wiki/opaque/root]]",
            "publication_state": "publish",
        }
    nodes["wiki/opaque/random+[1].md"] = {
        "canonical_id": "unrelated-name",
        "parent": "[[wiki/opaque/5]]",
        "access": "public",
        "content_status": "planned",
        "knowledge_state": "생각 중",
    }
    nodes["wiki/opaque/private.md"] = {
        "canonical_id": "private-leaf",
        "parent": "[[wiki/opaque/0]]",
        "access": "local-only",
    }
    nodes["wiki/opaque/resource.md"] = {
        "canonical_id": "resource-leaf",
        "parent": "[[wiki/opaque/resources]]",
    }
    nodes["wiki/opaque/story.md"] = {
        "canonical_id": "story",
        "parent": "[[wiki/opaque/creative]]",
    }
    return nodes


def test_colors_follow_parent_and_document_privacy_not_names_or_progress() -> None:
    metadata = color_tree()
    result = derive_graph_colors(metadata)
    colors = {row["category"]: row["rgb"] for row in result["legend"]}
    assigned = result["assignments"]
    assert len({colors[f"wiki/opaque/{i}.md"] for i in range(9)}) == 9
    assert assigned["wiki/opaque/root.md"] == "private"
    assert assigned["wiki/opaque/private.md"] == "private"
    assert assigned["wiki/private/public-looking.md"] == "private"
    assert colors["private"] == PRIVATE_RGB
    assert assigned["wiki/opaque/0.md"] == "wiki/opaque/0.md"
    assert assigned["wiki/opaque/random+[1].md"] == "wiki/opaque/5.md"
    assert assigned["wiki/old.md"] == assigned["wiki/unlinked.md"] == "legacy"
    assert colors["legacy"] == LEGACY_RGB
    assert assigned["wiki/opaque/resource.md"] == "resources"
    assert colors["resources"] == RESOURCE_RGB
    assert RESOURCE_RGB not in {colors[f"wiki/opaque/{i}.md"] for i in range(9)}
    assert assigned["wiki/opaque/story.md"] == "wiki/opaque/creative.md"
    planned = copy.deepcopy(metadata)
    planned["wiki/opaque/random+[1].md"]["content_status"] = "ready"
    assert derive_graph_colors(planned)["colorGroups"] == result["colorGroups"]
    # The escaped literal path matches only its exact target, even with regex punctuation.
    group = next(
        g for g in result["colorGroups"] if g["color"]["rgb"] == colors["wiki/opaque/5.md"]
    )
    pattern = group["query"].split("$/ ", 1)[0][6:] + "$"
    assert re.fullmatch(pattern, "wiki/opaque/random+[1].md")
    assert not re.fullmatch(pattern, "wiki/opaque/randommm1.md")
    assert "-[access:/^(local-only|private)$/]" in group["query"]
    assert "-[publication_state:private]" in group["query"]


@pytest.mark.parametrize("category_colors", [None, {"field-5": 0x123456}])
def test_color_adapter_preserves_search_zoom_and_other_plugins(
    tmp_path: Path, category_colors: dict[str, int] | None
) -> None:
    for name, metadata in color_tree().items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + json.dumps(metadata) + "\n---\nBody is not a color input.\n")
    path = tmp_path / ".obsidian/graph.json"
    path.parent.mkdir()
    current = {
        "search": "user-filter",
        "scale": 0.73,
        "showOrphans": True,
        "customSetting": {"preserve": True},
        "colorGroups": [],
    }
    path.write_text(json.dumps(current))
    if category_colors is not None:
        policy = tmp_path / "config/obsidian-navigation.json"
        policy.parent.mkdir()
        policy.write_text(json.dumps({"version": 1, "graph": {"category_colors": category_colors}}))
    other = tmp_path / ".obsidian/other.json"
    other.write_bytes(b'{"user":"owned"}\n')
    before = path.read_bytes()
    preview = configure_navigation(tmp_path, graph_colors=True)
    assert preview["allowed_keys"] == ["colorGroups"]
    assert path.read_bytes() == before
    applied = configure_navigation(tmp_path, graph_colors=True, apply=True)
    assert applied["ui_verified"] is False
    after = json.loads(path.read_bytes())
    assert {k: v for k, v in after.items() if k != "colorGroups"} == {
        k: v for k, v in current.items() if k != "colorGroups"
    }
    assert other.read_bytes() == b'{"user":"owned"}\n'
    assert not configure_navigation(tmp_path, graph_colors=True, apply=True)["settings"]["graph"][
        "changed"
    ]
    assert (
        graph_color_issues(
            after, derive_graph_colors(color_tree(), category_colors=category_colors)
        )
        == []
    )
    if category_colors:
        assert preview["input_sha256"] != derive_graph_colors(color_tree())["input_sha256"]
        assert any(
            row.get("canonical_id") == "field-5" and row["rgb"] == 0x123456
            for row in preview["legend"]
        )


def test_category_colors_survive_renaming_reordering_and_absorbing_a_branch() -> None:
    metadata = color_tree()
    original = derive_graph_colors(metadata)
    palette = {
        row["canonical_id"]: row["rgb"]
        for row in original["legend"]
        if row.get("canonical_id", "").startswith("field-") and row["canonical_id"] != "field-2"
    }
    taxonomy = metadata["wiki/opaque/root.md"]["public_taxonomy"]["roots"]
    taxonomy.remove("시스템")
    taxonomy[taxonomy.index("백엔드")] = "Backend"
    taxonomy.reverse()
    metadata["wiki/opaque/5.md"]["title"] = "Backend"
    metadata["wiki/opaque/2.md"]["parent"] = "[[wiki/opaque/0]]"
    before = copy.deepcopy(metadata)

    result = derive_graph_colors(metadata, category_colors=palette)

    assert metadata == before
    for row in result["legend"]:
        if row.get("canonical_id") in palette:
            assert row["rgb"] == palette[row["canonical_id"]]
    assert result["assignments"]["wiki/opaque/2.md"] == "wiki/opaque/0.md"
    assert result["assignments"]["wiki/opaque/random+[1].md"] == "wiki/opaque/5.md"
    assert result["assignments"]["wiki/opaque/private.md"] == "private"
    private = next(row for row in result["legend"] if row["category"] == "private")
    assert private["rgb"] == PRIVATE_RGB
    assert result["input_sha256"] != derive_graph_colors(metadata)["input_sha256"]
    with pytest.raises(WoonError, match="not one declared category"):
        derive_graph_colors(metadata, category_colors={"field-2": 0})


@pytest.mark.parametrize("failure", ["missing", "ambiguous", "boolean", "range", "shape"])
def test_category_colors_reject_invalid_values_and_unresolved_identities(failure: str) -> None:
    metadata = color_tree()
    values = {"field-0": 0}
    if failure == "missing":
        values = {"missing-id": 0}
    elif failure == "ambiguous":
        metadata["wiki/duplicate.md"] = {"canonical_id": "field-0"}
    elif failure == "boolean":
        values = {"field-0": True}
    elif failure == "range":
        values = {"field-0": 0x1000000}
    elif failure == "shape":
        values = []
    with pytest.raises(WoonError, match="graph.category_colors"):
        derive_graph_colors(metadata, category_colors=values)


def test_books_override_private_without_coloring_unrelated_book_mentions() -> None:
    metadata = color_tree()
    metadata["wiki/README.md"]["navigation_groups"][0]["children"].append("reading")
    metadata.update(
        {
            "wiki/opaque/reading.md": {
                "canonical_id": "reading",
                "parent": "[[wiki/README]]",
                "facets": ["책"],
            },
            "wiki/opaque/genre.md": {
                "parent": "[[wiki/opaque/reading]]",
                "access": "local-only",
            },
            "wiki/opaque/edition.md": {
                "parent": "[[wiki/opaque/genre]]",
                "entity_kind": "book",
                "publication_state": "private",
                "publish": False,
            },
            "wiki/private/section+[1].md": {
                "parent": "[[wiki/opaque/edition]]",
                "access": "local-only",
            },
            "wiki/opaque/book-review.md": {
                "parent": "[[wiki/opaque/creative]]",
                "title": "책을 읽은 감정",
                "access": "local-only",
            },
        }
    )
    before = copy.deepcopy(metadata)
    result = derive_graph_colors(metadata)
    assert metadata == before
    for path in (
        "wiki/opaque/reading.md",
        "wiki/opaque/genre.md",
        "wiki/opaque/edition.md",
        "wiki/private/section+[1].md",
    ):
        assert result["assignments"][path] == "books"
    assert result["assignments"]["wiki/opaque/book-review.md"] == "private"
    assert result["assignments"]["wiki/opaque/private.md"] == "private"
    book = next(g for g in result["colorGroups"] if g["color"]["rgb"] == ENTITY_COLORS["책"])
    assert "-[access:" not in book["query"]
    assert "-[publication_state:" not in book["query"]
    pattern = book["query"][6:-1]
    assert re.fullmatch(pattern, "wiki/private/section+[1].md")
    assert not re.fullmatch(pattern, "wiki/opaque/book-review.md")
    for private in (g for g in result["colorGroups"] if g["color"]["rgb"] == PRIVATE_RGB):
        assert private["query"].endswith(" -" + book["query"])
    # An entity outside the book hub is also an explicit book, independent of its filename.
    metadata["wiki/opaque/random+[1].md"]["entity_kind"] = "book"
    assert derive_graph_colors(metadata)["assignments"]["wiki/opaque/random+[1].md"] == "books"


def test_linked_private_readers_join_book_color_without_scanning_or_changing_sources(
    tmp_path: Path,
) -> None:
    nodes = color_tree()
    nodes["wiki/opaque/edition.md"] = {
        "canonical_id": "edition",
        "parent": "[[wiki/opaque/0]]",
        "content_kind": "book",
        "access": "local-only",
        "publish": False,
    }
    for name, metadata in nodes.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + json.dumps(metadata) + "\n---\n")
    archive = "private/knowledge/local-only/books/opaque-edition"
    edition = tmp_path / "wiki/opaque/edition.md"
    edition.write_text(edition.read_text() + f"[Reader](../../{archive}/index.md)\n")
    bodies = {
        f"{archive}/index.md": (
            "- [Original](original/unit.md)\n"
            f"- [[{archive}/ko/unit|한국어]]\n"
            "- [Outside](../../../../diary.md)\n"
            "- [Symlink](linked.md)\n"
            "```md\n[Example](not-a-reader.md)\n```\n"
        ),
        f"{archive}/original/unit.md": "[Subsection](subsection.md)\n[Back](../index.md)\n",
        f"{archive}/original/subsection.md": "Source text.\n",
        f"{archive}/ko/unit.md": "한국어 본문.\n",
        f"{archive}/not-a-reader.md": "Unlinked work.\n",
        "private/diary.md": "Private diary.\n",
    }
    for name, body in bodies.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            ""
            if name.endswith("subsection.md")
            else ("---\naccess: local-only\npublish: false\n---\n")
        )
        path.write_text(header + body)
    (tmp_path / archive / "linked.md").symlink_to(tmp_path / "private/diary.md")
    before = {name: (tmp_path / name).read_bytes() for name in bodies}
    metadata = graph_metadata(tmp_path)
    result = graph_color_projection(tmp_path)
    for suffix in ("index.md", "original/unit.md", "original/subsection.md", "ko/unit.md"):
        path = f"{archive}/{suffix}"
        if not suffix.endswith("subsection.md"):
            assert metadata[path]["access"] == "local-only"
        assert result["assignments"][path] == "books"
        group = next(g for g in result["colorGroups"] if g["color"]["rgb"] == ENTITY_COLORS["책"])
        assert re.fullmatch(group["query"][6:-1], path)
    for name in (f"{archive}/not-a-reader.md", f"{archive}/linked.md", "private/diary.md"):
        assert name not in metadata
    assert before == {name: (tmp_path / name).read_bytes() for name in bodies}
    assert graph_color_projection(tmp_path) == result


def _registered_book_vault(vault: Path) -> tuple[Path, dict, str, str]:
    nodes = color_tree()
    nodes["wiki/opaque/edition.md"] = {
        "canonical_id": "edition",
        "parent": "[[wiki/opaque/0]]",
        "entity_kind": "book",
        "access": "local-only",
        "publish": False,
    }
    for name, metadata in nodes.items():
        path = vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + json.dumps(metadata) + "\n---\n")
    reader = "private/knowledge/local-only/books/reader-edition"
    archive = "private/knowledge/local-only/resources/books/source-edition"
    edition = vault / "wiki/opaque/edition.md"
    edition.write_text(edition.read_text() + f"[[{reader}/index]]\n")
    files = {
        f"{reader}/index.md": f"[[{reader}/ko/lesson]]\n[[{archive}/원자료]]\n".encode(),
        f"{reader}/ko/lesson.md": "한국어 본문.\n".encode(),
        f"{archive}/원자료.md": b"Original Markdown.\n",
        f"{archive}/source.pdf": b"%PDF-1.4\nOriginal PDF fixture.\n",
        f"{archive}/figure.png": b"Source-only asset fixture.\n",
        f"{archive}/not-registered.md": b"Unrelated private material.\n",
    }
    for name, data in files.items():
        path = vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    registry = {
        "owner-key-unrelated-to-path": {
            "order": ["original-unit.md"],
            "original_index": "Not a file inventory",
            "resource_archive": {
                "role": "book-source-archive",
                "source_language": "en",
                "root": archive,
                "files": [
                    {"path": name, "kind": kind, "sha256": hashlib.sha256(files[name]).hexdigest()}
                    for name, kind in (
                        (f"{archive}/원자료.md", "markdown"),
                        (f"{archive}/source.pdf", "pdf"),
                        (f"{archive}/figure.png", "image"),
                    )
                ],
            },
        },
        "korean-original-without-english-archive": {"order": []},
    }
    registry_path = vault / ".local/owner/navigation.json"
    registry_path.parent.mkdir(parents=True)
    registry_path.write_text(json.dumps(registry))
    policy = vault / "config/obsidian-navigation.json"
    policy.parent.mkdir()
    policy.write_text(
        json.dumps(
            {
                "version": 1,
                "graph": {
                    "search": "tag:#graph/overview",
                    "book_source_registry": registry_path.relative_to(vault).as_posix(),
                },
                "omnisearch": {"hideExcluded": True, "downrankedFoldersFilters": ["private"]},
            }
        )
    )
    graph = vault / ".obsidian/graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps({"search": "user-filter", "scale": 0.7, "colorGroups": []}))
    return registry_path, registry, reader, archive


def test_registered_book_sources_have_resource_color_and_readers_keep_book_color(
    tmp_path: Path,
) -> None:
    registry_path, registry, reader, archive = _registered_book_vault(tmp_path)
    before = {
        path: path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and ".obsidian" not in path.parts
    }
    result = graph_color_projection(tmp_path)
    for path in ("wiki/opaque/edition.md", f"{reader}/index.md", f"{reader}/ko/lesson.md"):
        assert result["assignments"][path] == "books"
    for suffix in ("원자료.md", "source.pdf", "figure.png"):
        assert result["assignments"][f"{archive}/{suffix}"] == "book-resources"
    assert f"{archive}/not-registered.md" not in result["assignments"]
    assert result["assignments"]["wiki/opaque/private.md"] == "private"
    assert (
        next(row for row in result["legend"] if row["category"] == "book-resources")["rgb"]
        == RESOURCE_RGB
    )
    sources = next(
        group
        for group in result["colorGroups"]
        if "원자료" in group["query"] and group["color"]["rgb"] == RESOURCE_RGB
    )
    assert re.fullmatch(sources["query"][6:-1], f"{archive}/source.pdf")
    assert not re.fullmatch(sources["query"][6:-1], f"{archive}/not-registered.md")
    for group in (g for g in result["colorGroups"] if g["color"]["rgb"] == PRIVATE_RGB):
        excluded = group["query"].rsplit("-path:/", 1)[1][:-1]
        assert re.fullmatch(excluded, f"{archive}/source.pdf")
        assert re.fullmatch(excluded, f"{reader}/ko/lesson.md")
        assert not re.fullmatch(excluded, f"{archive}/not-registered.md")
    applied = configure_navigation(tmp_path, graph_colors=True, apply=True)
    assert applied["allowed_keys"] == ["colorGroups"]
    actual = json.loads((tmp_path / ".obsidian/graph.json").read_bytes())
    assert actual["search"] == "user-filter" and actual["scale"] == 0.7
    assert actual["colorGroups"] == result["colorGroups"]
    assert all(path.read_bytes() == data for path, data in before.items())
    assert json.loads(registry_path.read_bytes()) == registry
    assert not configure_navigation(tmp_path, graph_colors=True, apply=True)["settings"]["graph"][
        "changed"
    ]


@pytest.mark.parametrize(
    "failure",
    [
        "hash",
        "missing",
        "empty",
        "language",
        "outside",
        "symlink",
        "duplicate",
        "unknown-color",
        "invalid-color",
    ],
)
def test_incomplete_source_registration_blocks_colors_without_settings_or_success_receipt(
    tmp_path: Path,
    failure: str,
) -> None:
    registry_path, registry, _reader, _archive = _registered_book_vault(tmp_path)
    archive = registry["owner-key-unrelated-to-path"]["resource_archive"]
    item = archive["files"][0]
    source = tmp_path / item["path"]
    if failure == "hash":
        source.write_bytes(b"Source changed after its owner's registration.\n")
    elif failure == "missing":
        item["path"] += ".missing"
    elif failure == "empty":
        archive["files"] = []
    elif failure == "language":
        archive["source_language"] = "ko"
    elif failure == "outside":
        outside = tmp_path / "private/diary.md"
        outside.write_bytes(source.read_bytes())
        item["path"] = "private/diary.md"
    elif failure == "symlink":
        link = source.with_name("link.md")
        link.symlink_to(source)
        item["path"] = link.relative_to(tmp_path).as_posix()
    elif failure == "duplicate":
        archive["files"].append(copy.deepcopy(item))
    elif failure in {"unknown-color", "invalid-color"}:
        policy = tmp_path / "config/obsidian-navigation.json"
        configuration = json.loads(policy.read_bytes())
        configuration["graph"]["category_colors"] = (
            {"missing-id": 123} if failure == "unknown-color" else {"field-0": "#123456"}
        )
        policy.write_text(json.dumps(configuration))
    registry_path.write_text(json.dumps(registry))
    graph = tmp_path / ".obsidian/graph.json"
    before = graph.read_bytes()
    receipts = tmp_path / ".local/woon-knowledge/settings-receipts"
    receipts.mkdir(parents=True)
    previous = receipts / "previous-success.json"
    previous.write_bytes(b'{"previous":"success"}\n')
    with pytest.raises(WoonError, match="book resource archive|graph.category_colors"):
        configure_navigation(tmp_path, graph_colors=True, apply=True)
    assert graph.read_bytes() == before
    assert list(receipts.iterdir()) == [previous]
    assert previous.read_bytes() == b'{"previous":"success"}\n'


def test_navigation_source_registry_reference_is_not_an_obsidian_setting(tmp_path: Path) -> None:
    _registered_book_vault(tmp_path)
    policy = tmp_path / "config/obsidian-navigation.json"
    configuration = json.loads(policy.read_bytes())
    configuration["graph"]["category_colors"] = {"field-0": 0, "field-1": 0xFFFFFF}
    policy.write_text(json.dumps(configuration))
    omnisearch = tmp_path / ".obsidian/plugins/omnisearch/data.json"
    omnisearch.parent.mkdir(parents=True)
    omnisearch.write_bytes(b'{"userSetting":"preserve"}\n')
    result = configure_navigation(tmp_path, apply=True)
    graph = json.loads((tmp_path / ".obsidian/graph.json").read_bytes())
    assert result["applied"] is True
    assert graph == {"search": "tag:#graph/overview", "scale": 0.7, "colorGroups": []}
    assert json.loads(omnisearch.read_bytes())["userSetting"] == "preserve"
