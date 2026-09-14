"""Project private Novel evidence into the canonical keyword tree.

Raw files are resolved through the Vault source-boundary resolver and excluded
from the human keyword tree. The editable projection below
``wiki/private/novel`` is the sole navigation view.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import quote

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.knowledge.source_boundary import private_source_relative
from woon_core.knowledge.wiki_tree import (
    CHILDREN_END,
    CHILDREN_START,
    SOURCE_INDEX_END,
    SOURCE_INDEX_START,
    iter_wiki_pages,
    parent_link,
    prepare_wiki_tree_refresh,
    preserve_generated_wiki_views,
    render_markdown,
    split_markdown,
)
from woon_core.knowledge.woon_wiki import is_retired_wiki_record

_LINK = re.compile(r"(?m)^- \[(?P<label>[^]]+)]\((?P<target>[^)]+)\)\s*$")
_NUMBERED_H2 = re.compile(r"(?m)^## (?P<number>\d+)\. (?P<title>.+?)\s*$")
_H2 = re.compile(r"(?m)^## (?P<title>.+?)\s*$")
_SLUG = re.compile(r"[^0-9A-Za-z가-힣_-]+")
_EVENT_GROUP = re.compile(r"^사건 (?P<start>\d+)(?:–(?P<end>\d+))?$")
_EVENT_BODY_GROUP = re.compile(r"(?m)^- 사건 (?P<start>\d+)(?:–(?P<end>\d+))?$")
_WIKILINK = re.compile(r"^\[\[(?P<target>[^\]|#]+)(?:\|[^\]]+)?\]\]$")
_PROJECTION_SCHEMA_VERSION = 3
_PROJECT_CANONICAL_ID = "personal/projects/private-novel-writing"


@dataclass(frozen=True, slots=True)
class NovelWikiProjectionReport:
    category_count: int
    source_count: int
    event_count: int
    judgment_count: int
    relation_count: int
    changed_count: int
    pages: dict[Path, bytes]
    stale_pages: tuple[Path, ...]
    manifest: bytes
    project_path: Path


def prepare_novel_wiki_projection(
    vault: Path, novel: Path, *, projection_day: date
) -> NovelWikiProjectionReport:
    """Create a complete private Wiki projection from the internal source archive."""

    root = vault.expanduser().resolve()
    novel_root = novel.expanduser().resolve()
    expected_source_root = root / private_source_relative(root, "novel")
    if novel_root != expected_source_root:
        raise WoonError("Novel source must live at the configured private/novel root")
    navigation = novel_root / "work/navigation"
    category_files = tuple(
        path for path in sorted(navigation.glob("*.md")) if path.name != "README.md"
    )
    if not category_files:
        raise WoonError("Novel navigation has no keyword categories")

    category_ids = {f"private/novel/{_slug(_h1(path))}" for path in category_files}
    canonical_pages = _canonical_projection_pages(root, category_ids | {_PROJECT_CANONICAL_ID})
    project = canonical_pages.get(_PROJECT_CANONICAL_ID)
    if project is None:
        raise WoonError("Novel Wiki projection requires its canonical project entity")
    project_path, project_metadata = project
    project_relative = project_path.relative_to(root).as_posix()
    project_title = str(project_metadata.get("title", "")).strip()
    if not project_title:
        raise WoonError("Novel project entity has no title")
    project_subject = project_title.removesuffix(" 집필").strip() or project_title

    input_sha256 = _projection_input_sha256(root, novel_root, project_title, category_files)
    aliases = project_metadata.get("aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(alias, str) for alias in aliases):
        raise WoonError("Novel project aliases must be a list of titles")
    former_title_inputs = (
        _projection_input_sha256(root, novel_root, alias, category_files)
        for alias in aliases
        if alias != project_title
    )
    effective_day = _effective_projection_day(
        root,
        input_sha256,
        projection_day,
        former_title_inputs=former_title_inputs,
    )
    previous_source_receipts = _previous_source_receipts(root)

    output_root = root / "wiki/private/novel"
    pages: dict[Path, bytes] = {}
    source_receipts: dict[str, dict[str, str]] = {}
    category_paths: dict[str, str] = {}
    category_metadata: dict[str, dict[str, object]] = {}
    category_source_groups: dict[str, list[tuple[str, list[tuple[str, Path]]]]] = {}
    source_count = 0
    event_count = _count_event_sections(novel_root)
    judgment_count = _count_judgment_sections(novel_root)
    for sequence, category_file in enumerate(category_files, start=1):
        category = _h1(category_file)
        category_slug = _slug(category)
        canonical_id = f"private/novel/{category_slug}"
        existing = canonical_pages.get(canonical_id)
        category_path = existing[0] if existing else output_root / category_slug / "README.md"
        if not category_path.is_relative_to(output_root):
            raise WoonError("Novel projection category must remain inside wiki/private/novel")
        if existing is None and category_path.exists():
            raise WoonError(f"Novel category path belongs to another canonical ID: {category_path}")
        category_relative = category_path.relative_to(root).as_posix()
        category_paths[category_file.stem] = category_relative
        metadata = _metadata(
            title=f"소설 · {category}",
            canonical_id=canonical_id,
            parent=parent_link(project_relative, project_title),
            keyword=f"소설 · {category}",
            summary=f"{project_subject}의 {category} 키워드다.",
            day=effective_day,
            node_kind="hub",
            view_mode="tree",
            sequence=sequence,
        )
        if existing is not None:
            # Curated names and ownership survive regeneration; source changes
            # retain the existing projection date-update policy below.
            metadata.update(existing[1])
            metadata["updated"] = effective_day.isoformat()
            metadata["state_updated"] = effective_day.isoformat()
            if not str(metadata.get("title", "")).strip():
                raise WoonError(f"Novel category entity has no title: {canonical_id}")
            if metadata.get("publish") is not False or metadata.get("access") != "local-only":
                raise WoonError(f"Novel category must remain local-only: {canonical_id}")
        category_metadata[category_file.stem] = metadata
        category_source_groups[category_file.stem] = []
        source_group_children: dict[str, list[tuple[str, Path]]] = {}
        entries = _navigation_entries(category_file)
        for group_label, label, target in entries:
            source = (category_file.parent / target).resolve()
            if not source.is_relative_to(novel_root) or not source.is_file():
                raise WoonError(f"Novel navigation target is missing or escapes root: {source}")
            source_relative = source.relative_to(novel_root).as_posix()
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if group_label not in source_group_children:
                source_group_children[group_label] = []
                category_source_groups[category_file.stem].append(
                    (group_label, source_group_children[group_label])
                )
            source_group_children[group_label].append((label, source))
            source_receipts[source_relative] = {
                "source_path": source_relative,
                "sha256": digest,
            }
            source_count += 1

    related_people = _relation_people(
        novel_root,
        source_receipts,
    )
    for category_name, metadata in category_metadata.items():
        category_path = root / category_paths[category_name]
        source_index = _source_index_body(
            category_path,
            str(metadata["title"]),
            category_source_groups[category_name],
            root,
            related_people=related_people if category_name == "인물" else (),
        )
        pages[category_path] = _render_page(
            category_path,
            metadata,
            source_index,
            event_count=event_count if category_name == "사건-히스토리" else 0,
            preserve_dates=not _category_sources_changed(
                novel_root,
                category_source_groups[category_name],
                previous_source_receipts,
                source_receipts,
            ),
        )

    relation_count = len(related_people)

    expected = set(pages)
    previously_owned = _previously_owned_pages(root, output_root)
    stale = tuple(path for path in sorted(previously_owned - expected) if path.is_file())
    changed = sum(
        1 for path, content in pages.items() if not path.is_file() or path.read_bytes() != content
    ) + len(stale)
    manifest = (
        json.dumps(
            {
                "version": _PROJECTION_SCHEMA_VERSION,
                "projection_day": effective_day.isoformat(),
                "input_sha256": input_sha256,
                "category_count": len(category_files),
                "source_count": source_count,
                "event_count": event_count,
                "judgment_count": judgment_count,
                "relation_count": relation_count,
                "source_receipts": dict(sorted(source_receipts.items())),
                "owned_pages": [path.relative_to(root).as_posix() for path in sorted(expected)],
                "stale_pages": [path.relative_to(root).as_posix() for path in stale],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )
    return NovelWikiProjectionReport(
        len(category_files),
        source_count,
        event_count,
        judgment_count,
        relation_count,
        changed,
        pages,
        stale,
        manifest,
        project_path,
    )


def apply_novel_wiki_projection(vault: Path, report: NovelWikiProjectionReport) -> None:
    """Apply only the private Novel projection and validate the complete Wiki tree."""

    root = vault.expanduser().resolve()
    receipt = root / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    project = _canonical_projection_pages(root, {_PROJECT_CANONICAL_ID}).get(_PROJECT_CANONICAL_ID)
    if project is None or project[0] != report.project_path:
        raise WoonError("Novel project path changed after preparation; prepare again")
    project_path = report.project_path
    targets = (*report.pages.keys(), *report.stale_pages, receipt, project_path)
    snapshots = {path: path.read_bytes() if path.is_file() else None for path in targets}
    try:
        for path, content in report.pages.items():
            mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
            atomic_write(path, content, mode=mode)
        for path in report.stale_pages:
            path.unlink()
        atomic_write(receipt, report.manifest, mode=0o600)
        tree = prepare_wiki_tree_refresh(root)
        if tree.issues:
            raise WoonError(f"Novel Wiki projection failed validation: {tree.issues[0]}")
        project_content = tree.pages.get(project_path)
        if project_content is None:
            raise WoonError("Novel Wiki projection lost its project navigation owner")
        if project_path.read_bytes() != project_content:
            atomic_write(
                project_path,
                project_content,
                mode=project_path.stat().st_mode & 0o777,
            )
    except Exception:
        for path, previous in reversed(tuple(snapshots.items())):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous, mode=0o600)
        raise


def _canonical_projection_pages(
    vault: Path, canonical_ids: set[str]
) -> dict[str, tuple[Path, dict[str, object]]]:
    pages: dict[str, tuple[Path, dict[str, object]]] = {}
    for path in iter_wiki_pages(vault / "wiki"):
        metadata, _ = split_markdown(path.read_text(encoding="utf-8"))
        canonical_id = str(metadata.get("canonical_id", ""))
        if canonical_id not in canonical_ids:
            continue
        if path.resolve() != path or not path.is_relative_to(vault / "wiki"):
            raise WoonError(f"Novel canonical page must not follow a symlink: {canonical_id}")
        if canonical_id in pages:
            raise WoonError(f"Duplicate Novel canonical ID: {canonical_id}")
        if is_retired_wiki_record(metadata):
            raise WoonError(f"Novel canonical page is retired: {canonical_id}")
        pages[canonical_id] = (path, metadata)
    return pages


def _count_event_sections(novel: Path) -> int:
    ledger = novel / "work/analysis/event-evidence-ledger-2026-08-07.md"
    if not ledger.is_file():
        return 0
    return len(_NUMBERED_H2.findall(ledger.read_text(encoding="utf-8")))


def _count_judgment_sections(novel: Path) -> int:
    source = novel / "work/planning/corpus-reading-2026-08-07.md"
    if not source.is_file():
        return 0
    return len(_H2.findall(source.read_text(encoding="utf-8")))


def _relation_people(
    novel: Path,
    source_receipts: dict[str, dict[str, str]],
) -> tuple[tuple[str, str], ...]:
    source = novel / "work/people/person-link-ledger.yaml"
    if not source.is_file():
        return ()
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    people = payload.get("people", [])
    if not isinstance(people, list):
        raise WoonError("Novel person ledger people must be a list")
    people_targets = {
        "choi-woonyoung": ("최우녕", "wiki/personal/최우녕"),
        "kim-heejun": ("김희준", "wiki/personal/김희준"),
        "lee-minjeong": ("이민정", "wiki/private/이민정"),
    }
    resolved: list[tuple[str, str]] = []
    for item in people:
        if not isinstance(item, dict):
            raise WoonError("Novel person ledger entry must be a mapping")
        person_id = str(item.get("person_id", "")).strip()
        if person_id not in people_targets:
            raise WoonError(f"Novel person ledger has an unresolved person: {person_id}")
        for link in item.get("links", []):
            if not isinstance(link, dict):
                continue
            path = str(link.get("path", "")).strip()
            if path not in source_receipts:
                raise WoonError(f"Novel person link has no projected keyword page: {path}")
        resolved.append(people_targets[person_id])
    return tuple(resolved)


def _navigation_entries(path: Path) -> tuple[tuple[str, str, str], ...]:
    """Read link order and its human-authored H2 grouping axis."""

    current_group = ""
    entries: list[tuple[str, str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current_group = line.removeprefix("## ").strip()
            continue
        match = _LINK.fullmatch(line)
        if match is None:
            continue
        entries.append((current_group, match.group("label").strip(), match.group("target")))
    if len(entries) > 1 and any(not group for group, _, _ in entries):
        raise WoonError(f"Novel navigation links require H2 groups: {path}")
    return tuple((group or "원자료", label, target) for group, label, target in entries)


def _source_index_body(
    page_path: Path,
    title: str,
    groups: list[tuple[str, list[tuple[str, Path]]]],
    vault: Path,
    *,
    related_people: tuple[tuple[str, str], ...] = (),
) -> str:
    rows = [
        f"# {title}",
        "",
        "## 원자료",
        "",
        SOURCE_INDEX_START,
    ]
    for group_label, links in groups:
        rows.append(f"- {group_label}")
        for label, source in links:
            rows.append(f"  - [{label}]({_file_link(source, page_path, vault)})")
    if related_people:
        rows.append("- 작품에 연결된 인물")
        for label, target in related_people:
            rows.append(f"  - [[{target}|{label}]]")
    rows.append(SOURCE_INDEX_END)
    return "\n".join(rows).rstrip() + "\n"


def _metadata(
    *,
    title: str,
    canonical_id: str,
    parent: str,
    keyword: str,
    summary: str,
    day: date,
    node_kind: str,
    view_mode: str,
    sequence: int,
) -> dict[str, object]:
    return {
        "type": "Wiki",
        "title": title,
        "canonical_id": canonical_id,
        "record_owner": "choi-woonyoung",
        "publish": False,
        "access": "local-only",
        "status": "Active",
        "facets": ["프로젝트"],
        "knowledge_state": "확인 필요",
        "state_reason": "novel-local-source-projection",
        "state_updated": day.isoformat(),
        "summary": summary,
        "aliases": [],
        "keywords": [keyword],
        "node_kind": node_kind,
        "view_mode": view_mode,
        "updated": day.isoformat(),
        "parent": parent,
        "sequence": sequence,
        "people": [],
        "related_to": [],
        "tags": [],
    }


def _h1(path: Path) -> str:
    match = re.search(r"(?m)^# (?P<title>.+?)\s*$", path.read_text(encoding="utf-8"))
    if match is None:
        raise WoonError(f"Novel navigation page has no H1: {path}")
    return match.group("title").strip()


def _render_page(
    path: Path,
    metadata: dict[str, object],
    body: str,
    *,
    event_count: int = 0,
    preserve_dates: bool = True,
) -> bytes:
    effective_metadata = dict(metadata)
    if path.is_file():
        existing = path.read_text(encoding="utf-8")
        existing_metadata, _ = split_markdown(existing)
        history_target = existing_metadata.get("person_history_target")
        if history_target is not None:
            if not isinstance(history_target, str) or not _WIKILINK.fullmatch(history_target):
                raise WoonError("Novel person history target must be one confirmed Wiki link")
            effective_metadata["person_history_target"] = history_target
            row = f"- 실제 관계 기록\n  - {history_target}"
            if SOURCE_INDEX_START in body:
                body = body.replace(SOURCE_INDEX_START, f"{SOURCE_INDEX_START}\n{row}", 1)
            else:
                body = (
                    body.rstrip()
                    + f"\n\n## 원자료\n\n{SOURCE_INDEX_START}\n{row}\n{SOURCE_INDEX_END}\n"
                )
        if preserve_dates:
            for key in ("updated", "state_updated"):
                if key in existing_metadata:
                    effective_metadata[key] = existing_metadata[key]
        navigation_groups = existing_metadata.get("navigation_groups")
        if isinstance(navigation_groups, list) and navigation_groups:
            effective_metadata["navigation_groups"] = _reconcile_event_navigation_groups(
                path,
                str(existing_metadata.get("canonical_id", "")),
                navigation_groups,
                event_count,
            )
        rendered = render_markdown(effective_metadata, body)
        rendered = preserve_generated_wiki_views(existing, rendered)
        rendered = _preserve_novel_view_order(existing, rendered)
        rendered = _reconcile_event_navigation_body(
            rendered,
            path,
            str(existing_metadata.get("canonical_id", "")),
            event_count,
        )
    else:
        rendered = render_markdown(effective_metadata, body)
    return rendered.encode("utf-8")


def _preserve_novel_view_order(existing: str, rendered: str) -> str:
    """Keep an existing source-before-children layout stable across replay."""

    existing_source = existing.find("## 원자료")
    existing_children = existing.find(CHILDREN_START)
    if existing_source < 0 or existing_children < 0 or existing_children < existing_source:
        return rendered
    rendered_source = rendered.find("## 원자료")
    rendered_children = rendered.find(CHILDREN_START)
    if rendered_source < 0 or rendered_children < 0 or rendered_source < rendered_children:
        return rendered
    children_end = rendered.find(CHILDREN_END, rendered_children)
    if children_end < 0:
        raise WoonError("Novel Wiki child navigation markers are incomplete")
    children_end += len(CHILDREN_END)
    children_section = rendered[rendered_children:children_end].strip()
    without_children = (
        rendered[:rendered_children].rstrip() + "\n\n" + rendered[children_end:].lstrip()
    )
    source_end = without_children.find(SOURCE_INDEX_END)
    if source_end < 0:
        raise WoonError("Novel Wiki source index markers are incomplete")
    source_end += len(SOURCE_INDEX_END)
    return (
        without_children[:source_end].rstrip()
        + "\n\n"
        + children_section
        + "\n"
        + without_children[source_end:].lstrip()
    ).rstrip() + "\n"


def _reconcile_event_navigation_groups(
    hub_path: Path,
    hub_id: str,
    groups: list[object],
    event_count: int,
) -> list[object]:
    """Add source-bounded event children without preserving a stale generated index."""

    if event_count <= 0 or not hub_id.endswith("/사건-히스토리"):
        return groups
    discovered = _event_children(hub_path, hub_id, event_count)
    if not discovered:
        return groups

    normalized = copy.deepcopy(groups)
    listed = {
        child
        for group in normalized
        if isinstance(group, dict)
        for child in group.get("children", [])
        if isinstance(child, str)
    }
    missing = [(number, child[0]) for number, child in discovered.items() if child[0] not in listed]
    for number, child in missing:
        event_groups = [
            group
            for group in normalized
            if isinstance(group, dict)
            and _EVENT_GROUP.fullmatch(str(group.get("label", "")))
            and isinstance(group.get("children"), list)
        ]
        target = (
            event_groups[-1] if event_groups and len(event_groups[-1]["children"]) < 20 else None
        )
        if target is None:
            target = {"label": f"사건 {number}", "children": []}
            normalized.append(target)
        children = target["children"]
        assert isinstance(children, list)
        children.append(child)
        event_numbers = [
            int(match.group("number"))
            for value in children
            if isinstance(value, str)
            and (match := re.fullmatch(r"private/novel/events/(?P<number>\d+)", value))
        ]
        if event_numbers:
            start, end = min(event_numbers), max(event_numbers)
            target["label"] = f"사건 {start}" if start == end else f"사건 {start}–{end}"
    return normalized


def _reconcile_event_navigation_body(
    text: str,
    hub_path: Path,
    hub_id: str,
    event_count: int,
) -> str:
    """Add the same source-bounded event to the preserved visible child block."""

    discovered = _event_children(hub_path, hub_id, event_count)
    if not discovered or "<!-- woon-wiki-children:start -->" not in text:
        return text
    updated = text
    for number, (_, target, title) in discovered.items():
        if f"[[{target}|" in updated:
            continue
        marker_end = updated.find("<!-- woon-wiki-children:end -->")
        if marker_end < 0:
            raise WoonError("Novel Wiki child navigation markers are incomplete")
        headers = list(_EVENT_BODY_GROUP.finditer(updated, 0, marker_end))
        if not headers:
            raise WoonError("Novel Wiki event navigation has no event group")
        header = headers[-1]
        group_end = marker_end
        next_group = re.search(r"(?m)^- .+$", updated[header.end() : marker_end])
        if next_group is not None:
            group_end = header.end() + next_group.start()
        child_count = updated[header.end() : group_end].count("\n  - [[")
        if child_count >= 20:
            new_group = f"- 사건 {number}\n  - [[{target}|{title}]]\n"
            updated = updated[:marker_end] + new_group + updated[marker_end:]
            continue
        start = int(header.group("start"))
        end = max(int(header.group("end") or start), number)
        label = f"- 사건 {start}" if start == end else f"- 사건 {start}–{end}"
        updated = updated[: header.start()] + label + updated[header.end() :]
        delta = len(label) - (header.end() - header.start())
        group_end += delta
        row = f"  - [[{target}|{title}]]\n"
        updated = updated[:group_end].rstrip() + "\n" + row + updated[group_end:].lstrip()
    return updated.rstrip() + "\n"


def _event_children(
    hub_path: Path,
    hub_id: str,
    event_count: int,
) -> dict[int, tuple[str, str, str]]:
    if event_count <= 0 or not hub_id.endswith("/사건-히스토리"):
        return {}
    discovered: dict[int, tuple[str, str, str]] = {}
    for path in sorted(hub_path.parent.glob("event-*.md")):
        metadata, _ = split_markdown(path.read_text(encoding="utf-8"))
        canonical_id = str(metadata.get("canonical_id", "")).strip()
        parent = _WIKILINK.fullmatch(str(metadata.get("parent", "")).strip())
        match = re.fullmatch(r"private/novel/events/(?P<number>\d+)", canonical_id)
        if (
            match is None
            or parent is None
            or parent.group("target").removesuffix(".md") != f"wiki/{hub_id}/README"
        ):
            continue
        number = int(match.group("number"))
        title = str(metadata.get("title", "")).strip()
        if 1 <= number <= event_count and title:
            discovered[number] = (canonical_id, f"wiki/{hub_id}/{path.stem}", title)
    return discovered


def _slug(value: str) -> str:
    slug = _SLUG.sub("-", value.strip()).strip("-").casefold()
    if not slug:
        raise WoonError("Novel Wiki projection cannot create an empty slug")
    return slug


def _file_link(path: Path, page_path: Path, vault: Path) -> str:
    """Return an internal relative link without creating a second-vault dependency."""

    target = path.relative_to(vault)
    start = page_path.parent.relative_to(vault)
    relative = Path(os.path.relpath(target, start=start)).as_posix()
    return quote(relative, safe="/._-")


def _projection_input_sha256(
    vault: Path,
    novel: Path,
    project_title: str,
    categories: tuple[Path, ...],
) -> str:
    """Hash exactly the inputs that can change the editable Novel projection."""

    paths = {*categories}
    for category in categories:
        for match in _LINK.finditer(category.read_text(encoding="utf-8")):
            source = (category.parent / match.group("target")).resolve()
            if not source.is_relative_to(novel) or not source.is_file():
                raise WoonError(f"Novel navigation target is missing or escapes root: {source}")
            paths.add(source)
    paths.update(
        path
        for path in (
            novel / "work/analysis/event-evidence-ledger-2026-08-07.md",
            novel / "work/planning/corpus-reading-2026-08-07.md",
            novel / "work/people/person-link-ledger.yaml",
        )
        if path.is_file()
    )
    digest = hashlib.sha256()
    digest.update(b"project-title\0")
    digest.update(project_title.encode("utf-8"))
    for path in sorted(paths):
        relative = path.relative_to(vault).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _effective_projection_day(
    vault: Path,
    input_sha256: str,
    requested: date,
    *,
    former_title_inputs: Iterable[str] = (),
) -> date:
    """Keep source dates when only an explicitly retained project title changed."""

    receipt = vault / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    if not receipt.is_file():
        return requested
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"Novel Wiki projection receipt is unreadable: {error}") from error
    if (
        payload.get("input_sha256") != input_sha256
        and payload.get("input_sha256") not in former_title_inputs
    ):
        return requested
    try:
        return date.fromisoformat(str(payload["projection_day"]))
    except (KeyError, TypeError, ValueError) as error:
        raise WoonError("Novel Wiki projection receipt has an invalid projection_day") from error


def _previous_source_receipts(vault: Path) -> dict[str, dict[str, str]]:
    receipt = vault / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    if not receipt.is_file():
        return {}
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"Novel Wiki projection receipt is unreadable: {error}") from error
    records = payload.get("source_receipts", {})
    if not isinstance(records, dict):
        raise WoonError("Novel Wiki projection receipt has invalid source_receipts")
    normalized: dict[str, dict[str, str]] = {}
    for source_path, record in records.items():
        if not isinstance(source_path, str) or not isinstance(record, dict):
            raise WoonError("Novel Wiki projection receipt has invalid source_receipts")
        path = record.get("source_path")
        digest = record.get("sha256")
        if path != source_path or not isinstance(digest, str):
            raise WoonError("Novel Wiki projection receipt has invalid source_receipts")
        normalized[source_path] = {"source_path": source_path, "sha256": digest}
    return normalized


def _category_sources_changed(
    novel_root: Path,
    groups: list[tuple[str, list[tuple[str, Path]]]],
    previous: dict[str, dict[str, str]],
    current: dict[str, dict[str, str]],
) -> bool:
    if not previous:
        return True
    source_paths = {
        path.relative_to(novel_root).as_posix() for _, children in groups for _, path in children
    }
    return any(previous.get(path) != current.get(path) for path in source_paths)


def _previously_owned_pages(vault: Path, output_root: Path) -> set[Path]:
    """Return only pages explicitly owned by the current projection schema.

    Older manifests did not record ownership and therefore cannot authorize deletion.
    This keeps a projection redesign from silently removing legacy or user-owned notes.
    """

    receipt = vault / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    if not receipt.is_file():
        return set()
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"Novel Wiki projection receipt is unreadable: {error}") from error
    if payload.get("version") != _PROJECTION_SCHEMA_VERSION:
        return set()
    values = payload.get("owned_pages")
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise WoonError("Novel Wiki projection receipt has invalid owned_pages")
    owned: set[Path] = set()
    for value in values:
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md":
            raise WoonError("Novel Wiki projection receipt has an unsafe owned page")
        resolved = (vault / relative).resolve()
        if not resolved.is_relative_to(output_root):
            raise WoonError("Novel Wiki projection receipt owns a page outside its output root")
        owned.add(resolved)
    return owned
