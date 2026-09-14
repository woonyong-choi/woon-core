from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.knowledge.novel_wiki_projection import _render_page
from woon_core.knowledge.wiki_tree import (
    _render_person_history,
    preserve_generated_wiki_views,
    strip_generated_wiki_views,
)
from woon_core.knowledge.woon_wiki import preserve_managed_context
from woon_core.people.dashboard import PersonDashboardProjection


def _page(**metadata: object) -> str:
    return (
        "---\n"
        + yaml.safe_dump({"publish": False, "access": "local-only", **metadata}, allow_unicode=True)
        + "---\n\n# 기록\n\n직접 쓴 본문은 보존한다.\n"
    )


def _event(identity: str, sequence: int, people: list[str], period: str) -> str:
    return _page(
        canonical_id=identity,
        sequence=sequence,
        event_people=people,
        event_period=period,
        title="함께한 활동",
        summary="관찰과 현재 해석을 구분했다.",
        event_change="당시 해석: 관계가 달라졌다고 느꼈다.",
        updated="2026-09-08",
    )


def test_timeline_uses_confirmed_assignment_and_order_without_date_inference() -> None:
    page = _page(history_person_id="person-a")
    events = {
        "wiki/private/events/later.md": _event("event/later", 20, ["person-a"], "9월 · 추정"),
        "wiki/private/events/earlier.md": _event("event/earlier", 10, ["person-a"], "시기 미확정"),
        "wiki/private/events/other.md": _event("event/other", 1, ["person-b"], "8월"),
    }
    rendered = _render_person_history(page, events)
    assert rendered.index("earlier") < rendered.index("later")
    assert "other.md" not in rendered and "events/other" not in rendered
    assert "시기 미확정" in rendered
    assert "2026-09-08" not in rendered
    assert "직접 쓴 본문은 보존한다." in rendered
    assert "\\|사건과 근거" in rendered
    assert "당시 해석: 관계가 달라졌다고 느꼈다." in rendered
    assert _render_person_history(rendered, events) == rendered
    assert strip_generated_wiki_views(rendered) == page
    assert preserve_generated_wiki_views(rendered, page) == rendered


def test_event_correction_updates_one_row_and_shared_event_remains_single() -> None:
    page = _page(history_person_id="person-a")
    event = _event("event/shared", 1, ["person-a", "person-b"], "9월 · 추정")
    events = {"wiki/private/events/shared.md": event}
    rendered = _render_person_history(page, events)
    events["wiki/private/events/shared.md"] = event.replace("9월 · 추정", "2026-09-02")
    corrected = _render_person_history(rendered, events)
    assert "9월 · 추정" not in corrected
    assert corrected.count("events/shared") == 1
    assert (
        _render_person_history(_page(history_person_id="person-b"), events).count("events/shared")
        == 1
    )


def test_timeline_rejects_public_events_and_duplicate_identities() -> None:
    page = _page(history_person_id="person-a")
    event = _event("event/one", 1, ["person-a"], "미확정")
    with pytest.raises(WoonError, match="private scope"):
        _render_person_history(page, {"event": event.replace("publish: false", "publish: true")})
    with pytest.raises(WoonError, match="duplicate event"):
        _render_person_history(page, {"one": event, "two": event})
    with pytest.raises(WoonError, match="private/local-only"):
        _render_person_history(page.replace("publish: false", "publish: true"), {})


def test_person_base_uses_explicit_links_and_keeps_sensitive_records_out(tmp_path: Path) -> None:
    projection = PersonDashboardProjection(tmp_path)
    result = projection.refresh()
    text = (tmp_path / result.relative_path).read_text()
    spec = yaml.safe_load(text)
    assert "people.contains(this)" in spec["filters"]["and"]
    assert "record_owner == this.person_id" not in text
    assert 'file.inFolder("wiki/private/people")' in text
    assert [view["name"] for view in spec["views"]] == [
        "프로젝트·학습·자료",
        "다가오는 일정",
        "지난 일정",
    ]
    assert "file.mtime" not in text
    assert projection.refresh().changed is False


def test_novel_source_refresh_preserves_the_confirmed_person_history_entry(tmp_path: Path) -> None:
    page = tmp_path / "README.md"
    target = "[[wiki/private/people/person-a/relationships|실제 관계 기록]]"
    page.write_text(_page(person_history_target=target))
    metadata = {"title": "창작 근거", "publish": False, "access": "local-only"}
    rendered = _render_page(page, metadata, "# 창작 근거\n").decode()
    assert rendered.count(target) == 2  # Metadata and one rendered link.
    page.write_text(rendered)
    assert _render_page(page, metadata, "# 창작 근거\n").decode() == rendered


def test_compiler_context_keeps_confirmed_event_identity_and_timeline_preferences() -> None:
    existing = _page(
        event_people=["person-a"],
        event_period="시기 미확정",
        history_person_id="person-a",
        reader_navigation="sidebar-only",
        include_in_latest=False,
        navigation_groups=[{"label": "기록", "children": ["event/a"]}],
    )
    rendered = preserve_managed_context(existing, _page())
    meta = yaml.safe_load(rendered.split("---", 2)[1])
    assert meta["event_people"] == ["person-a"]
    assert meta["event_period"] == "시기 미확정"
    assert meta["history_person_id"] == "person-a"
    assert meta["reader_navigation"] == "sidebar-only"
    assert meta["include_in_latest"] is False
    assert meta["navigation_groups"][0]["children"] == ["event/a"]
