from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from woon_core.errors import WoonError
from woon_core.people.dashboard import (
    PRIVATE_RECORDS_BASE_RELATIVE_PATH,
    PersonDashboardProjection,
    is_core_private_records_base,
)
from woon_core.people.records import (
    resolve_record_metadata,
    validate_record_collection,
    validate_record_metadata_update,
)


def _record() -> dict[str, object]:
    return {
        "recording_id": "voice-001",
        "record_kind": "recording",
        "access": "local-only",
        "publish": False,
        "publication_state": "private",
        "title": "A conversation",
        "recorded_at": "2026-09-09T12:30:00+09:00",
        "candidate_person_ids": ["lee-minjeong"],
        "unresolved_speaker_count": 2,
        "event_people": ["lee-minjeong"],
        "event_period": "Occurrence date unknown",
        "custom_note": "Keep this annotation",
    }


def _relation() -> dict[str, object]:
    return {
        "person_id": "lee-minjeong",
        "person_path": "wiki/personal/minjeong.md",
        "role": "related-record",
        "basis": "user-selected-recording-collection",
        "evidence": "The user selected this collection as related material, not speaker evidence.",
    }


def test_full_renderer_roundtrip_preserves_records_without_promoting_candidates() -> None:
    previous = _record()
    before = deepcopy(previous)
    first = resolve_record_metadata(
        previous, {"title": "Corrected title"}, confirmed_relations=[_relation()]
    )
    second = resolve_record_metadata(
        first, {"title": "Corrected title"}, confirmed_relations=[_relation()]
    )
    assert first == second and previous == before
    assert first["candidate_person_ids"] == ["lee-minjeong"]
    assert first["unresolved_speaker_count"] == 2
    assert first["event_period"] == before["event_period"]
    assert "occurred_on" not in first and "Date" not in first
    assert first["custom_note"] == before["custom_note"]
    assert len(first["people"]) == len(first["person_roles"]) == 1
    assert first["person_roles"][0]["role"] == "related-record"
    validate_record_metadata_update(first, second)


@pytest.mark.parametrize(
    "change",
    [
        {"candidate_person_ids": ["[[wiki/personal/minjeong]]"]},
        {"candidate_person_ids": ["lee-minjeong", "lee-minjeong"]},
        {"unresolved_speaker_count": -1},
        {"unresolved_speaker_count": True},
        {"recorded_at": "2026-09-09T12:30:00"},
        {"recorded_at": "2026-02-30"},
        {"record_kind": "person"},
        {"publish": True},
        {"publication_state": "public"},
        {"recording_id": "a-new-identity"},
    ],
)
def test_resolver_rejects_identity_privacy_and_uncertainty_corruption(change: dict) -> None:
    with pytest.raises(WoonError):
        resolve_record_metadata(_record(), change)


def test_collection_relation_cannot_become_a_speaker_and_conflicting_evidence_survives() -> None:
    with pytest.raises(WoonError, match="related-record only"):
        resolve_record_metadata(
            _record(),
            {},
            confirmed_relations=[
                _relation() | {"role": "speaker"},
            ],
        )
    with pytest.raises(WoonError, match="candidate identity"):
        resolve_record_metadata(
            _record(),
            {},
            confirmed_relations=[
                _relation() | {"basis": "context-inferred"},
            ],
        )
    resolved = resolve_record_metadata(_record(), {}, confirmed_relations=[_relation()])
    with pytest.raises(WoonError, match="review existing evidence"):
        resolve_record_metadata(
            resolved,
            {},
            confirmed_relations=[
                _relation() | {"evidence": "An unreviewed replacement"},
            ],
        )


def test_native_replacement_requires_preservation_and_selected_batch_has_unique_ids() -> None:
    record = _record()
    with pytest.raises(WoonError, match="metadata omitted"):
        validate_record_metadata_update(
            record, {key: value for key, value in record.items() if key != "candidate_person_ids"}
        )
    updated = resolve_record_metadata(record, {"candidate_person_ids": [], "title": "Updated"})
    validate_record_metadata_update(record, updated)
    validate_record_collection([("private/a.md", record)])
    with pytest.raises(WoonError, match="identity repeats"):
        validate_record_collection([("private/a.md", record), ("private/b.md", updated)])
    # Ordinary entities need no record_kind or manufactured date/record ID.
    assert resolve_record_metadata({"person_id": "lee-minjeong"}, {"title": "Person"}) == {
        "person_id": "lee-minjeong",
        "title": "Person",
    }


def test_existing_dates_and_fractional_sequence_survive_a_native_roundtrip() -> None:
    original = _record() | {
        "record_kind": "experience",
        "occurred_on": "2024-03-01",
        "started_on": "2024-02-29",
        "ended_on": "2024-03-02",
        "Date": "2026-10-01",
        "Time": "10:30",
        "sequence": 1.5,
    }
    regenerated = resolve_record_metadata(original, {"title": "Revised body title"})
    for key in ("occurred_on", "started_on", "ended_on", "Date", "Time", "sequence"):
        assert regenerated[key] == original[key]
        with pytest.raises(WoonError, match="metadata omitted"):
            validate_record_metadata_update(
                original, {field: value for field, value in regenerated.items() if field != key}
            )
    validate_record_metadata_update(original, regenerated)


def test_evidence_full_render_preserves_opted_in_record_metadata() -> None:
    from woon_core.knowledge.wiki_tree import split_markdown
    from woon_core.knowledge.woon_wiki import preserve_managed_context

    previous = _record() | {"canonical_id": "private/record", "occurred_on": "2024-03-01"}
    desired = {
        "canonical_id": "private/record",
        "title": "Revised record",
        "access": "local-only",
        "publish": False,
    }

    def render(meta: dict) -> str:
        return "---\n" + yaml.safe_dump(meta) + "---\n\n# " + meta["title"] + "\n\nBody.\n"

    original = render(previous)
    merged = preserve_managed_context(original, render(desired))
    result = split_markdown(merged)[0]
    for field in ("record_kind", "candidate_person_ids", "occurred_on", "recorded_at"):
        assert result[field] == previous[field]
    assert preserve_managed_context(merged, render(desired)) == merged
    # Existing explicit-clear semantics remain; ordinary entities do not acquire a record schema.
    explicit = split_markdown(
        preserve_managed_context(
            original,
            render(desired | {"candidate_person_ids": [], "occurred_on": None}),
        )
    )[0]
    assert explicit["candidate_person_ids"] == [] and explicit["occurred_on"] is None
    ordinary = split_markdown(preserve_managed_context("", render(desired)))[0]
    assert "record_kind" not in ordinary and "candidate_person_ids" not in ordinary


def test_private_base_is_formula_only_host_guarded_and_keeps_general_query(tmp_path: Path) -> None:
    producer = PersonDashboardProjection(tmp_path)
    normal = producer.refresh()
    normal_bytes = (tmp_path / normal.relative_path).read_bytes()
    first = producer.refresh_private_records()
    path = tmp_path / first.relative_path
    parsed = yaml.safe_load(path.read_text())
    assert first.changed and not producer.refresh_private_records().changed
    assert is_core_private_records_base(path)
    assert (tmp_path / normal.relative_path).read_bytes() == normal_bytes
    assert path.stat().st_mode & 0o777 == 0o400
    filters = parsed["filters"]["and"]
    assert 'this.access == "local-only"' in filters and "this.publish == false" in filters
    assert 'access == "local-only"' in filters and "publish == false" in filters
    assert any('this.file.path.startsWith("wiki/private/")' in f for f in filters)
    assert all(key.startswith("formula.") for key in parsed["properties"])
    assert all(key.startswith("formula.") for view in parsed["views"] for key in view["order"])
    related, unknown, review, details = parsed["views"]
    assert related["name"] == "관련 기록"
    assert related["order"] == [
        "formula." + key for key in ("moment", "record", "kind", "role", "state")
    ]
    assert "candidate_person_ids" not in related["filters"]
    assert "candidate_person_ids" in review["filters"]
    dates = ("occurred_on", "started_on", "ended_on", "Date")
    assert all(key in unknown["filters"] for key in dates)
    assert len(details["order"]) > len(related["order"])
    formula = parsed["formulas"]
    assert "value.person == this" in formula["role"]
    assert "value.person_id == if(this.history_person_id" in formula["role"]
    assert '.map(if(value.role == "related-record"' in formula["role"]
    assert 'value.basis == "explicit-event-people"' in formula["role"]
    assert 'value.basis == "user-selected-recording-collection"' in formula["role"]
    assert "review_status" in formula["state"] and "audio_verified" in formula["state"]
    assert 'record_kind == "application"' in formula["state"]
    assert 'application_state == "rejected", "불합격"' in formula["state"]
    assert '" · 문서 · " + if(status' in formula["state"]
    assert all(key in formula["moment"] for key in dates)
    assert "file.mtime" not in path.read_text() and "file.ctime" not in path.read_text()


@pytest.mark.parametrize("user_content", ["views: []\n# user query\n", ""])
def test_private_base_never_overwrites_user_query_or_follows_symlink(
    tmp_path: Path,
    user_content: str,
) -> None:
    path = tmp_path / PRIVATE_RECORDS_BASE_RELATIVE_PATH
    path.parent.mkdir()
    path.write_text(user_content)
    with pytest.raises(WoonError, match="not a Core projection"):
        PersonDashboardProjection(tmp_path).refresh_private_records()
    assert path.read_text() == user_content
    path.unlink()
    target = tmp_path / "personal.base"
    target.write_text("private query")
    path.symlink_to(target)
    with pytest.raises(WoonError, match="symlinks"):
        PersonDashboardProjection(tmp_path).refresh_private_records()
    assert target.read_text() == "private query"


def test_private_recording_dql_rejects_foreign_or_unbounded_scopes() -> None:
    from woon_core.people.dashboard import render_private_recording_tables

    approved = {
        "host_path": "wiki/private/person.md",
        "person_id": "person",
        "source_folder": "private/knowledge/voice-memos/recordings/2026",
    }
    text = render_private_recording_tables(**approved)
    assert text.count("```dataview\n") == 4 and "dataviewjs" not in text
    assert 'this.file.path = "wiki/private/person.md"' in text
    assert 'this.person_id = "person"' in text
    assert 'this.access = "local-only" AND this.publish = false' in text
    assert 'access = "local-only" AND publish = false' in text
    for key, value in (
        ("host_path", "wiki/personal/person.md"),
        ("host_path", "wiki/private/../personal/person.md"),
        ("person_id", 'person" OR true'),
        ("source_folder", "private"),
        ("source_folder", "private/knowledge/voice-memos/intake/2026"),
    ):
        with pytest.raises(WoonError, match="exact private host"):
            render_private_recording_tables(**{**approved, key: value})
