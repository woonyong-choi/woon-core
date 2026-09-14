"""Derive Obsidian colors from canonical parent edges, without changing the Wiki."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from woon_core.errors import WoonError
from woon_core.knowledge.wiki_tree import iter_wiki_pages, split_markdown, wikilink_path

ROOT = "wiki/README.md"
PRIVATE_RGB = 0xD9534F
RESOURCE_RGB = 0x4F8F9D
LEGACY_RGB = 0x969696
HUB_RGB = 0x537B7D
# Presentation palette only: branch identities and order come from the Wiki.
DOMAIN_PALETTE = (
    0x55A868,
    0x9467BD,
    0xCCB54B,
    0x4C78A8,
    0xD473AF,
    0xDD8452,
    0x8C6D31,
    0x7A8BD4,
    0x397BA3,
)
ENTITY_COLORS = {
    "책": 0x8B6F82,
    "프로젝트": 0x4D6276,
    "인물": 0xB15F6D,
    "일정": 0xBA634C,
    "창작": 0x476D3C,
    "커리어": 0xBF9F60,
}
_FIELDS = (
    "canonical_id",
    "title",
    "parent",
    "access",
    "publication_state",
    "public_slug",
    "domain",
    "facets",
    "entity_kind",
    "content_kind",
    "navigation_groups",
    "public_taxonomy",
)


def graph_metadata(vault: Path) -> dict[str, dict[str, Any]]:
    """Read Wiki metadata, explicit book reader links, and registered source assets.

    Reader paths are discovered from actual links, not an archive directory scan.
    Only their ownership and metadata enter the projection; prose is not classified.
    """
    wiki = vault / "wiki"
    if not (vault / ROOT).is_file():
        raise WoonError("Graph colors require the canonical Wiki root")
    result: dict[str, dict[str, Any]] = {}
    for path in iter_wiki_pages(wiki):
        if path.is_symlink():
            continue
        header: list[str] = []
        with path.open(encoding="utf-8") as stream:
            first = stream.readline()
            if first.strip() == "---":
                header.append(first)
                for line in stream:
                    header.append(line)
                    if line.strip() == "---":
                        break
        try:
            metadata, _body = split_markdown("".join(header))
        except WoonError:
            metadata = {}
        result[path.relative_to(vault).as_posix()] = {
            key: metadata[key] for key in _FIELDS if key in metadata
        }
    _add_book_readers(vault, result)
    _add_book_resources(vault, result)
    return result


def _is_book(value: dict[str, Any]) -> bool:
    return (
        value.get("entity_kind") == "book"
        or value.get("content_kind") == "book"
        or "책" in _strings(value.get("facets"))
    )


def _linked_markdown(vault: Path, path: Path, boundary: Path) -> set[Path]:
    """Resolve explicit Markdown/wikilinks inside one known reader boundary."""
    _metadata, body = _reader_markdown(path)
    body = re.sub(r"<!--[\s\S]*?-->", "", body)
    body = re.sub(r"(?ms)^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", body)
    body = re.sub(r"(`+)[^`\n]*?\1", "", body)
    links = [
        (vault, match.group(1))
        for match in re.finditer(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]", body)
    ]
    links.extend(
        (path.parent, match.group(1))
        for match in re.finditer(r"(?<!!)\[[^\]\n]*\]\(([^)\s]+)(?:\s+[^)]*)?\)", body)
    )
    targets: set[Path] = set()
    for base, href in links:
        url = urlsplit(href.strip("<>"))
        if url.scheme or url.netloc or not url.path or url.path.startswith("/"):
            continue
        target = base / unquote(url.path)
        if not target.suffix:
            target = target.with_suffix(".md")
        # Symlinks are not an alternate route into reader ownership.
        if any(part.is_symlink() for part in (target, *target.parents)):
            continue
        target = target.resolve()
        if target.is_relative_to(boundary) and target.suffix == ".md" and target.is_file():
            targets.add(target)
    return targets


def _add_book_readers(vault: Path, metadata: dict[str, dict[str, Any]]) -> None:
    vault = vault.resolve()
    archive = vault / "private/knowledge/local-only/books"
    for owner, value in sorted(tuple(metadata.items())):
        if not _is_book(value):
            continue
        for entry in sorted(_linked_markdown(vault, vault / owner, archive)):
            # Follow the reader index; relocated source archives are outside this boundary.
            # An unlinked sibling in the same folder never acquires book ownership.
            boundary = entry.parent
            pending = [entry]
            visited: set[Path] = set()
            while pending:
                path = pending.pop()
                if path in visited:
                    continue
                visited.add(path)
                relative = path.relative_to(vault).as_posix()
                reader, _body = _reader_markdown(path)
                metadata[relative] = {
                    **{key: reader[key] for key in _FIELDS if key in reader},
                    "_book_reader_of": owner,
                }
                pending.extend(sorted(_linked_markdown(vault, path, boundary) - visited))


def _reader_markdown(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    # Raw reader units may have no frontmatter; their linked book owns the color.
    return split_markdown(text) if text.startswith("---\n") else ({}, text)


def _registered_path(vault: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise WoonError(f"{label} must be an explicit Vault-relative path")
    relative = Path(value)
    if ".." in relative.parts:
        raise WoonError(f"{label} must not traverse outside its registered boundary")
    path = vault / relative
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise WoonError(f"{label} must not contain symlinks")
    if not path.resolve().is_relative_to(vault):
        raise WoonError(f"{label} must stay inside the Vault")
    return path


def _add_book_resources(vault: Path, metadata: dict[str, dict[str, Any]]) -> None:
    """Use the book owner's existing exact-file registry, never infer an archive by name."""
    vault = vault.resolve()
    policy = vault / "config/obsidian-navigation.json"
    if not policy.is_file():
        return
    try:
        configuration = json.loads(policy.read_bytes())
        graph = configuration.get("graph", {}) if isinstance(configuration, dict) else {}
        reference = graph.get("book_source_registry") if isinstance(graph, dict) else None
        if reference is None:
            return
        registry_path = _registered_path(vault, reference, "book source registry")
        registry = json.loads(registry_path.read_bytes())
    except (OSError, ValueError) as error:
        raise WoonError(
            "book source registry is unreadable; color projection not applied"
        ) from error
    if not isinstance(registry, dict):
        raise WoonError("book source registry must contain book records")
    registered: dict[str, dict[str, Any]] = {}
    archives = 0
    for slug, book in registry.items():
        if not isinstance(book, dict) or "resource_archive" not in book:
            continue
        archive = book["resource_archive"]
        if (
            not isinstance(archive, dict)
            or archive.get("role") != "book-source-archive"
            or archive.get("source_language") != "en"
        ):
            raise WoonError(f"book resource archive role/language is incomplete: {slug}")
        root = _registered_path(vault, archive.get("root"), "book resource archive root")
        boundary = vault / "private/knowledge/local-only/resources/books"
        if root == boundary or not root.is_relative_to(boundary):
            raise WoonError(f"book resource archive must stay inside its private boundary: {slug}")
        files = archive.get("files")
        if not isinstance(files, list) or not files:
            raise WoonError(f"book resource archive file registration is incomplete: {slug}")
        archives += 1
        for item in files:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("kind"), str)
                or not item["kind"].strip()
            ):
                raise WoonError(f"book resource archive requires explicit file kinds: {slug}")
            path = _registered_path(vault, item.get("path"), "book resource archive file")
            digest = item.get("sha256")
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise WoonError(f"book resource archive file is missing or invalid: {slug}")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                    raise WoonError(f"book resource archive hash mismatch: {slug}")
            relative = path.relative_to(vault).as_posix()
            if relative in registered:
                raise WoonError(f"book resource archive file has duplicate registration: {slug}")
            registered[relative] = {
                "_book_source_archive": slug,
                "_source_sha256": digest,
                "_source_kind": item["kind"],
            }
    if not archives:
        raise WoonError("book source registry has no completed resource archive registrations")
    metadata.update(registered)


def derive_graph_colors(
    metadata: dict[str, dict[str, Any]], *, category_colors: dict[str, int] | None = None
) -> dict[str, Any]:
    """Color registered book assets and readers, then private records and parent branches.

    Privacy belongs to each document, never its boundary ancestor. Content
    progress is deliberately not an input: a planned node in a declared branch
    has that branch's color. Legacy concept descendants remain neutral until
    their canonical parent joins the declared developer tree. Explicit category
    colors follow canonical identity instead of the current navigation position.
    """
    root = metadata.get(ROOT, {})
    if not root.get("canonical_id"):
        raise WoonError("Graph colors require a canonical root identity")
    parents = {
        path: wikilink_path(value.get("parent"), path, [])
        for path, value in metadata.items()
        if path != ROOT
    }
    direct = {path: value for path, value in metadata.items() if parents.get(path) == ROOT}
    groups = root.get("navigation_groups", [])
    declared = (
        [
            child
            for group in groups
            if isinstance(group, dict)
            for child in _strings(group.get("children"))
        ]
        if isinstance(groups, list)
        else []
    )
    roots = {path: value for path, value in direct.items() if value.get("canonical_id") in declared}
    if not declared or any(
        sum(value.get("canonical_id") == child for value in roots.values()) != 1
        for child in declared
    ):
        raise WoonError("Graph colors require unambiguous declared root navigation")
    categories: dict[str, tuple[str, int]] = {ROOT: ("Wiki", HUB_RGB)}
    legacy_roots: set[str] = set()
    boundaries: set[str] = set()
    for path, value in sorted(
        roots.items(), key=lambda item: declared.index(item[1]["canonical_id"])
    ):
        label = str(value.get("title") or value["canonical_id"])
        position = declared.index(value["canonical_id"])
        facets = _strings(value.get("facets"))
        rgb = next(
            (color for name, color in ENTITY_COLORS.items() if name in facets),
            DOMAIN_PALETTE[position % len(DOMAIN_PALETTE)],
        )
        categories[path] = (label, rgb)
        if value.get("domain") == "concepts":
            legacy_roots.add(path)
        taxonomy = value.get("public_taxonomy")
        if not isinstance(taxonomy, dict):
            continue
        names = taxonomy.get("roots")
        if (
            not isinstance(names, list)
            or not names
            or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)
            or len(names) > len(DOMAIN_PALETTE)
        ):
            raise WoonError("Graph colors require distinct declared taxonomy roots")
        boundaries.add(path)
        children = {p: m for p, m in metadata.items() if parents.get(p) == path}
        for index, name in enumerate(names):
            matches = [p for p, m in children.items() if m.get("title") == name]
            if len(matches) != 1:
                raise WoonError(f"Graph taxonomy root must resolve to one direct child: {name}")
            categories[matches[0]] = (name, DOMAIN_PALETTE[index])
        for child, child_metadata in children.items():
            if child_metadata.get("public_slug") == "home":
                categories[child] = (str(child_metadata.get("title", "Home")), HUB_RGB)

    if category_colors is not None:
        if not isinstance(category_colors, dict):
            raise WoonError("graph.category_colors must map canonical IDs to RGB integers")
        for identity, rgb in category_colors.items():
            if not isinstance(identity, str) or not identity or type(rgb) is not int:
                raise WoonError("graph.category_colors requires canonical IDs and integer RGBs")
            if not 0 <= rgb <= 0xFFFFFF:
                raise WoonError("graph.category_colors RGB must be between 0 and 16777215")
            matches = [
                path for path, value in metadata.items() if value.get("canonical_id") == identity
            ]
            if len(matches) != 1 or matches[0] not in categories:
                raise WoonError(
                    f"graph.category_colors key is not one declared category: {identity}"
                )
            category = matches[0]
            categories[category] = (categories[category][0], rgb)

    categories.update(
        {
            "books": ("책", ENTITY_COLORS["책"]),
            "book-resources": ("책 원문 리소스", RESOURCE_RGB),
            "private": ("Private", PRIVATE_RGB),
            "resources": ("리소스", RESOURCE_RGB),
            "legacy": ("미정리·legacy", LEGACY_RGB),
        }
    )
    assignments: dict[str, str] = {}
    for path, value in metadata.items():
        if value.get("_book_source_archive"):
            assignments[path] = "book-resources"
            continue
        chain: list[str] = []
        current: str | None = path
        while current is not None and current in metadata and current not in chain:
            chain.append(current)
            current = parents.get(current)
        reader_owner = value.get("_book_reader_of")
        if any(_is_book(metadata[p]) for p in chain) or (
            isinstance(reader_owner, str) and _is_book(metadata.get(reader_owner, {}))
        ):
            assignments[path] = "books"
            continue
        if (
            path.startswith("wiki/private/")
            or value.get("access") in ("local-only", "private")
            or value.get("publication_state") == "private"
        ):
            assignments[path] = "private"
            continue
        if ROOT not in chain:
            assignments[path] = "legacy"
            continue
        if (
            value.get("entity_kind") == "resource"
            or "리소스" in _strings(value.get("facets"))
            or any(metadata[p].get("title") == "리소스" for p in chain if p in roots)
        ):
            assignments[path] = "resources"
            continue
        branch = next((p for p in chain if p in categories), None)
        if (
            branch is None
            or (branch in legacy_roots and path != branch)
            or (branch in boundaries and path != branch)
            or (branch == ROOT and path != ROOT)
        ):
            branch = "legacy"
        assignments[path] = branch

    members: dict[str, list[str]] = defaultdict(list)
    for path, category in assignments.items():
        members[category].append(path)
    private_queries = (
        "[access:/^(local-only|private)$/]",
        "[publication_state:private]",
        r"path:/^(?:wiki\/private|private)\//",
    )
    book_paths = members["books"] + members["book-resources"]
    book_query = _path_query(book_paths) if book_paths else None
    book_exclusion = f" -{book_query}" if book_query else ""
    color_groups = [
        {"query": query + book_exclusion, "color": {"a": 1, "rgb": PRIVATE_RGB}}
        for query in private_queries
    ]
    private_exclusion = " ".join(f"-{query}" for query in private_queries)
    for category, (_label, rgb) in categories.items():
        if category == "private" or not members[category]:
            continue
        # Exact paths are the output of parent traversal, not filename classifiers.
        exclusions = "" if category in {"books", "book-resources"} else f" {private_exclusion}"
        color_groups.append(
            {
                "query": _path_query(members[category]) + exclusions,
                "color": {"a": 1, "rgb": rgb},
            }
        )
    return {
        "colorGroups": color_groups,
        "legend": [
            {
                "category": key,
                "label": label,
                "rgb": rgb,
                "count": len(members[key]),
                **(
                    {"canonical_id": metadata[key]["canonical_id"]}
                    if key in metadata and "canonical_id" in metadata[key]
                    else {}
                ),
            }
            for key, (label, rgb) in categories.items()
        ],
        "assignments": assignments,
        "input_sha256": hashlib.sha256(
            json.dumps(
                {"metadata": metadata, "category_colors": category_colors}
                if category_colors
                else metadata,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode()
        ).hexdigest(),
    }


def graph_color_projection(vault: Path) -> dict[str, Any]:
    """Read optional canonical category colors without applying any Vault settings."""
    policy = vault / "config/obsidian-navigation.json"
    before = policy.read_bytes() if policy.is_file() else None
    category_colors = None
    if before is not None:
        try:
            configuration = json.loads(before)
        except ValueError as error:
            raise WoonError("Graph color policy must contain valid JSON") from error
        graph = configuration.get("graph") if isinstance(configuration, dict) else None
        if not isinstance(graph, dict):
            raise WoonError("Graph color policy requires a graph object")
        if "category_colors" in graph:
            category_colors = graph["category_colors"]
            if not isinstance(category_colors, dict):
                raise WoonError("graph.category_colors must map canonical IDs to RGB integers")
    projection = derive_graph_colors(graph_metadata(vault), category_colors=category_colors)
    if (policy.read_bytes() if policy.is_file() else None) != before:
        raise WoonError("Graph color policy changed during projection; prepare again")
    return projection


def graph_color_issues(config: dict[str, Any], projection: dict[str, Any]) -> list[str]:
    if config.get("colorGroups") != projection["colorGroups"]:
        return ["colorGroups must match the canonical parent-derived color projection"]
    return []


def _regex_literal(value: str) -> str:
    """Escape JavaScript regex literals, including the query's slash delimiter."""
    return re.sub(r"[.*+?^${}()|\[\]\\/]", lambda match: "\\" + match[0], value)


def _path_query(paths: list[str]) -> str:
    pattern = "|".join(_regex_literal(path) for path in sorted(paths))
    return f"path:/^(?:{pattern})$/"


def _strings(value: object) -> tuple[str, ...]:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else ()
