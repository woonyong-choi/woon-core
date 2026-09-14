import json
from datetime import date
from pathlib import Path

import pytest

from woon_core.errors import WoonError
from woon_core.knowledge.novel_wiki_projection import (
    _render_page,
    apply_novel_wiki_projection,
    prepare_novel_wiki_projection,
)
from woon_core.knowledge.wiki_tree import render_markdown, split_markdown


def _page(
    path: Path,
    *,
    title: str,
    canonical_id: str,
    node_kind: str,
    parent: str | None,
    entity_kind: str | None = None,
    entity_section: str | None = None,
    sequence: int | None = None,
    navigation_groups: str = "",
    body: str = "",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_row = f"parent: '{parent}'\n" if parent else ""
    entity_row = f"entity_kind: {entity_kind}\n" if entity_kind else ""
    entity_section_row = f"entity_section: {entity_section}\n" if entity_section else ""
    lifecycle_row = (
        "lifecycle_status: active\n"
        if entity_kind in {"project", "person", "career", "application"}
        else ""
    )
    sequence_row = f"sequence: {sequence}\n" if sequence is not None else ""
    path.write_text(
        "---\n"
        f"type: Wiki\ntitle: {title}\ncanonical_id: {canonical_id}\n"
        f"node_kind: {node_kind}\n{entity_row}{entity_section_row}{lifecycle_row}"
        f"{sequence_row}{navigation_groups}{parent_row}"
        f"keywords:\n- {title}\naliases: []\nview_mode: tree\n"
        "updated: 2026-08-25\nsummary: 테스트 문서다.\n"
        "knowledge_state: 확인 필요\n---\n\n"
        f"# {title}\n\n{body}".rstrip()
        + "\n",
        encoding="utf-8",
    )


def test_render_preserves_existing_human_reviewed_navigation_groups(tmp_path: Path) -> None:
    path = tmp_path / "hub.md"
    _page(
        path,
        title="기존 허브",
        canonical_id="private/novel/existing",
        node_kind="hub",
        parent=None,
        navigation_groups=(
            "navigation_groups:\n"
            "- label: 선형 탐색\n"
            "  children:\n"
            "  - private/novel/existing/first\n"
        ),
        body=(
            "## 원자료\n\n"
            "<!-- woon-wiki-source-index:start -->\n"
            "- [원자료](../../_sources/novel/source.md)\n"
            "<!-- woon-wiki-source-index:end -->\n\n"
            "## 하위 키워드\n\n"
            "<!-- woon-wiki-children:start -->\n"
            "- 기존 링크\n"
            "<!-- woon-wiki-children:end -->\n"
        ),
    )

    rendered = _render_page(
        path,
        {
            "type": "Wiki",
            "title": "갱신된 허브",
            "canonical_id": "private/novel/existing",
            "node_kind": "hub",
        },
        (
            "# 갱신된 허브\n\n"
            "## 원자료\n\n"
            "<!-- woon-wiki-source-index:start -->\n"
            "- [원자료](../../_sources/novel/source.md)\n"
            "<!-- woon-wiki-source-index:end -->\n"
        ),
    ).decode("utf-8")

    assert "navigation_groups:" in rendered
    assert "label: 선형 탐색" in rendered
    assert "private/novel/existing/first" in rendered
    assert rendered.index("## 원자료") < rendered.index("<!-- woon-wiki-children:start -->")


def test_render_reconciles_source_bounded_event_child_and_replays(tmp_path: Path) -> None:
    path = tmp_path / "wiki/private/novel/사건-히스토리/README.md"
    existing_children = "".join(
        f"  - private/novel/events/{number:02d}\n" for number in range(13, 26)
    )
    _page(
        path,
        title="소설 · 사건·히스토리",
        canonical_id="private/novel/사건-히스토리",
        node_kind="hub",
        parent="[[wiki/personal/projects/novel|소설 집필]]",
        navigation_groups=(
            f"navigation_groups:\n- label: 사건 13–25\n  children:\n{existing_children}"
        ),
        body=(
            "## 하위 키워드\n\n"
            "<!-- woon-wiki-children:start -->\n"
            "- 사건 13–25\n"
            "<!-- woon-wiki-children:end -->\n"
        ),
    )
    _page(
        path.parent / "event-26.md",
        title="소설 사건 26",
        canonical_id="private/novel/events/26",
        node_kind="detail",
        parent="[[wiki/private/novel/사건-히스토리/README|소설 · 사건·히스토리]]",
    )
    metadata = {
        "type": "Wiki",
        "title": "소설 · 사건·히스토리",
        "canonical_id": "private/novel/사건-히스토리",
        "node_kind": "hub",
    }

    rendered = _render_page(
        path,
        metadata,
        (
            "# 소설 · 사건·히스토리\n\n"
            "## 원자료\n\n"
            "<!-- woon-wiki-source-index:start -->\n"
            "- [사건 장부](../../_sources/novel/events.md)\n"
            "<!-- woon-wiki-source-index:end -->\n"
        ),
        event_count=26,
    )

    text = rendered.decode("utf-8")
    assert "label: 사건 13–26" in text
    assert text.count("private/novel/events/26") == 1
    assert text.index("<!-- woon-wiki-children:start -->") < text.index("## 원자료")
    path.write_bytes(rendered)
    replay = _render_page(
        path,
        metadata,
        (
            "# 소설 · 사건·히스토리\n\n"
            "## 원자료\n\n"
            "<!-- woon-wiki-source-index:start -->\n"
            "- [사건 장부](../../_sources/novel/events.md)\n"
            "<!-- woon-wiki-source-index:end -->\n"
        ),
        event_count=26,
    )
    assert replay == rendered


def test_render_does_not_add_event_beyond_source_ledger_count(tmp_path: Path) -> None:
    path = tmp_path / "wiki/private/novel/사건-히스토리/README.md"
    _page(
        path,
        title="소설 · 사건·히스토리",
        canonical_id="private/novel/사건-히스토리",
        node_kind="hub",
        parent="[[wiki/personal/projects/novel|소설 집필]]",
        navigation_groups=(
            "navigation_groups:\n- label: 사건 25\n  children:\n  - private/novel/events/25\n"
        ),
    )
    _page(
        path.parent / "event-26.md",
        title="소설 사건 26",
        canonical_id="private/novel/events/26",
        node_kind="detail",
        parent="[[wiki/private/novel/사건-히스토리/README|소설 · 사건·히스토리]]",
    )

    rendered = _render_page(
        path,
        {
            "type": "Wiki",
            "title": "소설 · 사건·히스토리",
            "canonical_id": "private/novel/사건-히스토리",
            "node_kind": "hub",
        },
        "# 소설 · 사건·히스토리\n",
        event_count=25,
    ).decode("utf-8")

    assert "private/novel/events/26" not in rendered


def test_projects_every_novel_navigation_source_into_private_wiki_and_replays(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    novel = vault / "wiki/private/_sources/novel"
    _page(
        vault / "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
    )
    _page(
        vault / "wiki/projects.md",
        title="프로젝트",
        canonical_id="projects",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
        sequence=1,
    )
    _page(
        vault / "wiki/personal/projects/(미정)소설-집필.md",
        title="(미정)소설 집필",
        canonical_id="personal/projects/private-novel-writing",
        node_kind="entity",
        parent="[[wiki/projects|프로젝트]]",
        entity_kind="project",
        body="현재 목표와 집필 기준을 관리한다.",
        navigation_groups=(
            "navigation_groups:\n"
            "- label: 작품 탐색\n"
            "  children:\n"
            "  - private/novel/장면-원고\n"
            "  - private/novel/집필-계획\n"
            "  - private/novel/인물\n"
        ),
    )
    _page(
        vault / "wiki/private/이민정.md",
        title="이민정",
        canonical_id="private/이민정",
        node_kind="entity",
        parent="[[wiki/README|Wiki]]",
        entity_kind="person",
        sequence=2,
        body="소설에 연결된 인물이다.",
    )
    source = novel / "vault-source/scene.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 장면 원본\n", encoding="utf-8")
    navigation = novel / "work/navigation/장면-원고.md"
    navigation.parent.mkdir(parents=True)
    navigation.write_text(
        "# 장면·원고\n\n- [첫 장면](../../vault-source/scene.md)\n",
        encoding="utf-8",
    )
    (navigation.parent / "집필-계획.md").write_text("# 집필 계획\n", encoding="utf-8")
    (navigation.parent / "인물.md").write_text("# 인물\n", encoding="utf-8")
    planning = novel / "work/planning/corpus-reading-2026-08-07.md"
    planning.parent.mkdir(parents=True)
    planning.write_text(
        "# 집필 판단\n\n## 다음 집필 순서\n\n첫 장면을 다듬는다.\n",
        encoding="utf-8",
    )
    people = novel / "work/people/person-link-ledger.yaml"
    people.parent.mkdir(parents=True)
    people.write_text(
        "people:\n- person_id: lee-minjeong\n  links:\n  - path: vault-source/scene.md\n",
        encoding="utf-8",
    )

    first = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 25))
    apply_novel_wiki_projection(vault, first)
    replay = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 26))

    project = (vault / "wiki/personal/projects/(미정)소설-집필.md").read_text(encoding="utf-8")
    scene_hub = (vault / "wiki/private/novel/장면-원고/README.md").read_text(encoding="utf-8")
    people_hub = (vault / "wiki/private/novel/인물/README.md").read_text(encoding="utf-8")
    assert first.category_count == 3
    assert first.source_count == 1
    assert first.judgment_count == 1
    assert first.relation_count == 1
    assert "[[wiki/private/novel/장면-원고/README|소설 · 장면·원고]]" in project
    assert "summary: (미정)소설의 장면·원고 키워드다." in (
        vault / "wiki/private/novel/장면-원고/README.md"
    ).read_text(encoding="utf-8")
    assert "[첫 장면](../../_sources/novel/vault-source/scene.md)" in scene_hub
    assert "[[wiki/private/이민정|이민정]]" in people_hub
    assert not (vault / "wiki/private/novel/장면-원고/01-첫-장면.md").exists()
    assert not (vault / "wiki/private/novel/집필-계획/judgment-01-다음-집필-순서.md").exists()
    assert not (vault / "wiki/private/novel/인물/lee-minjeong.md").exists()
    assert replay.changed_count == 0

    legacy_page = vault / "wiki/private/novel/인물/legacy-detail.md"
    _page(
        legacy_page,
        title="보존할 기존 상세 기록",
        canonical_id="private/novel/인물/legacy-detail",
        node_kind="topic",
        parent="[[wiki/private/novel/인물/README|소설 · 인물]]",
    )
    receipt = vault / ".local/woon-knowledge/novel-wiki-projection/manifest.json"
    legacy_manifest = json.loads(receipt.read_text(encoding="utf-8"))
    legacy_manifest["version"] = 2
    legacy_manifest.pop("owned_pages")
    receipt.write_text(json.dumps(legacy_manifest), encoding="utf-8")

    migration = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 26))
    assert legacy_page not in migration.stale_pages
    apply_novel_wiki_projection(vault, migration)
    assert legacy_page.is_file()

    source.write_text("# 장면 원본\n\n변경됨\n", encoding="utf-8")
    changed = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 26))
    assert changed.changed_count == 1
    assert b'"projection_day": "2026-08-26"' in changed.manifest

    old_project = vault / "wiki/personal/projects/(미정)소설-집필.md"
    new_project = old_project.with_name("(미정)소설.md")
    old_project.rename(new_project)
    unchanged_pages = {path: path.read_bytes() for path in changed.pages}
    unchanged_receipt = receipt.read_bytes()
    with pytest.raises(WoonError, match="path changed after preparation"):
        apply_novel_wiki_projection(vault, changed)
    assert unchanged_pages == {path: path.read_bytes() for path in changed.pages}
    assert receipt.read_bytes() == unchanged_receipt
    new_project.rename(old_project)
    apply_novel_wiki_projection(vault, changed)
    old_project.rename(new_project)

    project_metadata, project_body = split_markdown(new_project.read_text(encoding="utf-8"))
    project_metadata["title"] = "(미정)소설"
    project_metadata["aliases"] = ["(미정)소설 집필"]
    new_project.write_text(
        render_markdown(
            project_metadata, project_body.replace("# (미정)소설 집필", "# (미정)소설")
        ),
        encoding="utf-8",
    )
    for hub in first.pages:
        metadata, body = split_markdown(hub.read_text(encoding="utf-8"))
        metadata["parent"] = "[[wiki/personal/projects/(미정)소설|(미정)소설]]"
        hub.write_text(render_markdown(metadata, body), encoding="utf-8")
    old_hub = vault / "wiki/private/novel/장면-원고/README.md"
    new_hub = old_hub.with_name("원고.md")
    old_hub.rename(new_hub)
    metadata, body = split_markdown(new_hub.read_text(encoding="utf-8"))
    metadata.update(title="원고", aliases=["장면과 원고"], keywords=["원고"], summary="확정한 설명")
    new_hub.write_text(
        render_markdown(metadata, body.replace("# 소설 · 장면·원고", "# 원고")), encoding="utf-8"
    )
    source_before = source.read_bytes()
    renamed = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 27))
    assert json.loads(renamed.manifest)["projection_day"] == "2026-08-26"
    apply_novel_wiki_projection(vault, renamed)
    replay = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 27))
    assert replay.changed_count == 0
    assert not old_project.exists()
    assert not old_hub.exists()
    metadata, body = split_markdown(new_hub.read_text(encoding="utf-8"))
    assert metadata["title"] == "원고"
    assert metadata["aliases"] == ["장면과 원고"]
    assert metadata["keywords"] == ["원고"]
    assert metadata["summary"] == "확정한 설명"
    assert str(metadata["updated"]) == "2026-08-26"
    assert body.startswith("# 원고\n")
    assert "[[wiki/private/novel/장면-원고/원고|원고]]" in new_project.read_text(encoding="utf-8")
    assert source.read_bytes() == source_before

    source.write_bytes(source_before + "\n추가 원자료\n".encode())
    changed_source = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 28))
    assert json.loads(changed_source.manifest)["projection_day"] == "2026-08-28"

    duplicate = new_project.with_name("중복.md")
    duplicate.write_bytes(new_project.read_bytes())
    with pytest.raises(WoonError, match="Duplicate Novel canonical ID"):
        prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 27))


def test_groups_large_event_timeline_into_linear_stages(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    novel = vault / "wiki/private/_sources/novel"
    _page(
        vault / "wiki/README.md",
        title="Wiki",
        canonical_id="README",
        node_kind="root",
        parent=None,
    )
    _page(
        vault / "wiki/projects.md",
        title="프로젝트",
        canonical_id="projects",
        node_kind="hub",
        parent="[[wiki/README|Wiki]]",
    )
    _page(
        vault / "wiki/personal/projects/(미정)소설-집필.md",
        title="(미정)소설 집필",
        canonical_id="personal/projects/private-novel-writing",
        node_kind="entity",
        parent="[[wiki/projects|프로젝트]]",
        entity_kind="project",
        body="현재 사건 구조를 선형으로 관리한다.",
    )
    navigation = novel / "work/navigation/사건-히스토리.md"
    navigation.parent.mkdir(parents=True)
    navigation.write_text(
        "# 사건·히스토리\n\n"
        "## 사건 장부\n\n"
        "- [사건 근거 장부](../analysis/event-evidence-ledger-2026-08-07.md)\n",
        encoding="utf-8",
    )
    ledger = novel / "work/analysis/event-evidence-ledger-2026-08-07.md"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "# 사건\n\n" + "\n".join(f"## {number}. 사건 {number}\n\n근거" for number in range(1, 26)),
        encoding="utf-8",
    )

    report = prepare_novel_wiki_projection(vault, novel, projection_day=date(2026, 8, 25))
    apply_novel_wiki_projection(vault, report)

    hub = (vault / "wiki/private/novel/사건-히스토리/README.md").read_text(encoding="utf-8")
    assert report.event_count == 25
    assert "- 사건 장부" in hub
    assert "[사건 근거 장부]" in hub
    assert not (vault / "wiki/private/novel/사건-히스토리/event-01.md").exists()
