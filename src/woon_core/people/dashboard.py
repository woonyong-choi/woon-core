"""Generate the shared Obsidian Base embedded by every person entity page."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from woon_core.errors import WoonError
from woon_core.io import atomic_write
from woon_core.people.records import RECORD_KINDS

PERSON_DASHBOARD_BASE_RELATIVE_PATH = "inbox/person-indexed-docs.base"
PRIVATE_RECORDS_BASE_RELATIVE_PATH = "inbox/private-linked-records.base"
_PROJECTION_MARKER = "# woon_projection: person-dashboard-base\n"
_PRIVATE_MARKER = "# woon_projection: private-linked-records-base\n"
_READONLY_FILE_MODE = 0o400

_RECORD_ROLE_LABELS = {
    "related-record": "사용자 연결 · 화자 증거 아님",
    "speaker": "확인된 화자",
    "participant": "참석자",
    "organizer": "주최자",
    "author": "저자",
    "source-provider": "자료 제공자",
    "interviewee": "면접·인터뷰 대상",
    "collaborator": "협업자",
    "reviewer": "검토자",
    "subject": "자료의 대상",
    "mentioned": "언급된 인물",
}

_LEGACY_BASE = """filters:
  and:
    - file.ext == "md"
    - file.path != this.file.path
    - or:
        - people.contains(this)
        - record_owner == this.person_id
views:
  - type: table
    name: "최근 색인 문서"
    limit: 30
    order:
      - file.name
      - title
      - type
      - status
      - record_owner
      - person_roles
      - attributions
      - parent
      - file.ctime
      - file.mtime
    sort:
      - property: file.ctime
        direction: DESC
      - property: file.mtime
        direction: DESC
"""


@dataclass(frozen=True, slots=True)
class PersonDashboardProjectionResult:
    """One deterministic refresh of the shared person projection."""

    changed: bool
    relative_path: str


class PersonDashboardProjection:
    """Own a semantic, date-aware view over explicit person relationships."""

    def __init__(self, vault: Path) -> None:
        self._vault = vault.expanduser().resolve()
        self._path = self._vault / PERSON_DASHBOARD_BASE_RELATIVE_PATH

    def refresh(self) -> PersonDashboardProjectionResult:
        """Create or refresh the Base without overwriting an unknown user file."""

        content = _render_base()
        previous = self._path.read_text(encoding="utf-8") if self._path.exists() else ""
        if previous and _PROJECTION_MARKER not in previous and previous != _LEGACY_BASE:
            raise WoonError(
                "person dashboard Base is not the known legacy view or a Core projection"
            )
        changed = previous != content
        if changed:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(self._path, content.encode("utf-8"), mode=_READONLY_FILE_MODE)
        elif self._path.stat().st_mode & 0o777 != _READONLY_FILE_MODE:
            self._path.chmod(_READONLY_FILE_MODE)
            changed = True
        return PersonDashboardProjectionResult(
            changed=changed,
            relative_path=PERSON_DASHBOARD_BASE_RELATIVE_PATH,
        )

    def refresh_private_records(self) -> PersonDashboardProjectionResult:
        """Refresh the opt-in private view without changing the general person Base.

        The caller owns the Vault writer window. The generated view contains no
        record inventory or copied source bodies; it queries explicit metadata.
        """
        path = self._vault / PRIVATE_RECORDS_BASE_RELATIVE_PATH
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise WoonError("private record Base must not follow symlinks")
        previous = path.read_text(encoding="utf-8") if path.exists() else ""
        if path.exists() and not previous.startswith(_PRIVATE_MARKER):
            raise WoonError("private record Base is not a Core projection")
        content = _render_private_base()
        changed = content != previous
        if changed:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content.encode("utf-8"), mode=_READONLY_FILE_MODE)
        elif path.stat().st_mode & 0o777 != _READONLY_FILE_MODE:
            path.chmod(_READONLY_FILE_MODE)
            changed = True
        if path.read_text(encoding="utf-8") != content:
            raise WoonError("private record Base verification failed")
        return PersonDashboardProjectionResult(changed, PRIVATE_RECORDS_BASE_RELATIVE_PATH)


def is_core_person_dashboard_base(path: Path) -> bool:
    """Return whether a Base is the exact deterministic Core projection."""

    return path.is_file() and path.read_text(encoding="utf-8") == _render_base()


def is_core_private_records_base(path: Path) -> bool:
    """Return whether a Base matches the single private record producer."""
    return path.is_file() and path.read_text(encoding="utf-8") == _render_private_base()


def _record_role_label(variable: str, *, dql: bool = False) -> str:
    """Keep relationship meaning identical in the two declarative view dialects."""
    choose, equal = ("choice", "=") if dql else ("if", "==")
    result = f"{variable}.role"
    for role, label in reversed(tuple(_RECORD_ROLE_LABELS.items())):
        if role == "related-record":
            display = (
                f'{choose}({variable}.basis {equal} "explicit-event-people", '
                '"사건 관련 인물 · 참석자 증거 아님", '
                f'{choose}({variable}.basis {equal} "user-selected-recording-collection", '
                '"사용자 연결 · 화자 증거 아님", "관련 자료 · 화자·참석자 증거 아님"))'
            )
        else:
            display = json.dumps(label, ensure_ascii=False)
        result = f'{choose}({variable}.role {equal} "{role}", {display}, {result})'
    return result


def _recording_state(*, dql: bool = False) -> str:
    choose, equal = ("choice", "=") if dql else ("if", "==")
    candidates = (
        "length(ldefault(candidate_person_ids, [])) > 0 OR unresolved_speaker_count > 0"
        if dql
        else "list(candidate_person_ids).filter(value).length > 0 || unresolved_speaker_count > 0"
    )
    return (
        f"{choose}(review_status, review_status, {choose}(full_text_reviewed {equal} true, "
        '"전문 대조 완료", "교정 상태 미확인")) + '
        f'{choose}({candidates}, " · 화자 확인 필요", "") + '
        f'{choose}(audio_verified {equal} true, " · 원음 대조 완료", '
        f'{choose}(audio_verified {equal} false, " · 원음 대조 미완료", " · 원음 대조 미확인"))'
    )


def render_private_recording_tables(*, host_path: str, person_id: str, source_folder: str) -> str:
    """Render DQL for an approved private host's existing recording readers.

    Native Bases skip Obsidian excluded files. Dataview's declarative index can
    query their metadata without changing those exclusions or enabling JS.
    Embed this generated block in the host itself: DQL's `this` is that document.
    The caller owns the exact host transaction and source metadata validation.
    """
    if (
        not re.fullmatch(r"wiki/private/[^\n]+\.md", host_path)
        or any(part in {"", ".", ".."} for part in host_path.split("/"))
        or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", person_id)
        or not re.fullmatch(r"private/knowledge/voice-memos/recordings/\d{4}", source_folder)
    ):
        raise WoonError("private recording table requires an exact private host and reader scope")

    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)

    relation = (
        "length(host_roles) > 0 OR contains(ldefault(people, []), this.file.link) OR "
        "contains(ldefault(related_to, []), this.file.link) OR "
        "contains(ldefault(event_people, []), this.person_id)"
    )
    candidates = "contains(ldefault(candidate_person_ids, []), this.person_id)"
    needs_review = "length(ldefault(candidate_person_ids, [])) > 0 OR unresolved_speaker_count > 0"
    known_time = "recorded_at OR occurred_on OR started_on OR ended_on OR Date"
    moment = (
        'choice(recorded_at, "녹음 · " + string(recorded_at), '
        'choice(occurred_on, "발생 · " + string(occurred_on), '
        'choice(started_on, "기간 · " + string(started_on) + '
        'choice(ended_on, " → " + string(ended_on), ""), '
        'choice(ended_on, "종료 · " + string(ended_on), '
        'choice(Date, "원본 날짜 · " + string(Date), '
        'choice(event_period, "시기 · " + event_period, "날짜 미상"))))))'
    )
    role = (
        f"choice(length(host_roles) > 0, join(unique(map(host_roles, (role) => "
        f'{_record_role_label("role", dql=True)})), " · "), '
        "choice(contains(ldefault(event_people, []), this.person_id), "
        '"사건 관련 인물 · 참석자 증거 아님", '
        f'choice({relation}, "관련 맥락", "화자 후보 · 미확정")))'
    )
    columns = {
        "moment": (moment, "시점"),
        "record": ("file.link", "기록"),
        "kind": ('"녹음"', "종류"),
        "role": (role, "관계 역할"),
        "state": (_recording_state(dql=True), "상태"),
        "period": ("event_period", "발생 시기·불확실성"),
        "sequence": ("sequence", "확인된 순서"),
        "recorded": ("recorded_at", "녹음일"),
        "candidates": ("candidate_person_ids", "화자 후보 ID"),
        "unresolved": ("unresolved_speaker_count", "미확인 화자 수"),
        "occurred": ("occurred_on", "발생일"),
        "started": ("started_on", "시작일"),
        "ended": ("ended_on", "종료일"),
        "calendar": ("Date", "Calendar 날짜"),
    }
    shared = (
        f"FROM {quote(source_folder)}\n"
        f"WHERE this.file.path = {quote(host_path)} AND this.person_id = {quote(person_id)}\n"
        'AND this.access = "local-only" AND this.publish = false\n'
        'AND (this.publication_state = null OR this.publication_state = "private")\n'
        'AND access = "local-only" AND publish = false\n'
        'AND (publication_state = null OR publication_state = "private")\n'
        'AND record_kind = "recording" AND typeof(recording_id) = "string" '
        "AND length(recording_id) > 0\n"
        "FLATTEN [filter(ldefault(person_roles, []), (role) => "
        "role.person = this.file.link OR role.person_id = this.person_id)] AS host_roles\n"
    )
    views = (
        ("관련 기록", relation, ("moment", "record", "kind", "role", "state")),
        (
            "날짜 미상",
            f"({relation}) AND !({known_time})",
            ("record", "kind", "period", "sequence", "role"),
        ),
        (
            "화자 확인 필요",
            f"(({relation}) OR {candidates}) AND ({needs_review})",
            ("record", "recorded", "candidates", "unresolved", "state"),
        ),
        (
            "날짜 상세",
            relation,
            ("record", "occurred", "started", "ended", "recorded", "calendar", "period"),
        ),
    )
    blocks = ["<!-- woon-private-recording-tables:start -->"]
    for index, (name, predicate, fields) in enumerate(views):
        table = ",\n".join(f"{columns[field][0]} AS {quote(columns[field][1])}" for field in fields)
        query = f"TABLE WITHOUT ID\n{table}\n{shared}WHERE {predicate}\nSORT "
        query += "default(recorded_at, default(occurred_on, default(started_on, "
        query += "default(ended_on, Date)))) DESC, sequence ASC, file.path ASC"
        block = f"```dataview\n{query}\n```"
        if index:
            block = f"> [!info]- {name}\n>\n" + "\n".join(
                "> " + line if line else ">" for line in block.splitlines()
            )
        blocks.append(block)
    blocks.append("<!-- woon-private-recording-tables:end -->")
    return "\n\n".join(blocks) + "\n"


def _render_private_base() -> str:
    """Use formula columns only; chmod alone cannot prevent cell metadata edits."""
    host_person = "if(this.history_person_id, this.history_person_id, this.person_id)"
    host_roles = (
        "list(person_roles).filter(value).filter(value.person == this || "
        f"({host_person} && value.person_id == {host_person}))"
    )
    event_relation = f"({host_person} && list(event_people).contains({host_person}))"
    relation = (
        "list(people).contains(this) || list(related_to).contains(this) || "
        f"{event_relation} || {host_roles}.length > 0"
    )
    candidates = f"({host_person} && list(candidate_person_ids).contains({host_person}))"
    known_time = (
        '(record_kind == "recording" && recorded_at) || occurred_on || '
        "started_on || ended_on || Date"
    )
    moment = (
        'if(record_kind == "recording" && recorded_at, "녹음 · " + recorded_at, '
        'if(occurred_on, "발생 · " + occurred_on, '
        'if(started_on, "기간 · " + started_on + if(ended_on, " → " + ended_on, ""), '
        'if(ended_on, "종료 · " + ended_on, '
        'if(Date, if(type == "calendar-event", "예정 · ", "원본 날짜 · ") + Date, '
        'if(event_period, "시기 · " + event_period, "날짜 미상"))))))'
    )
    role_label = _record_role_label("value")
    # Career service imports KnowledgeService, so defer labels until rendering.
    from woon_core.career.service import STATE_LABELS

    application_label = 'if(application_state, application_state, "미확인")'
    for state, label in reversed(tuple(STATE_LABELS.items())):
        application_label = f'if(application_state == "{state}", "{label}", {application_label})'
    columns = {
        "moment": (moment, "시점"),
        "record": ("file.asLink(if(title, title, file.name))", "기록"),
        "kind": ("record_kind", "종류"),
        "role": (
            f'if({host_roles}.length > 0, {host_roles}.map({role_label}).unique().join(" · "), '
            f'if({event_relation}, "사건 관련 인물 · 참석자 증거 아님", '
            f'if(({relation}), "관련 맥락", "화자 후보 · 미확정")))',
            "관계 역할",
        ),
        "state": (
            f'if(record_kind == "recording", {_recording_state()}, '
            f'if(record_kind == "application", "지원 · " + {application_label} + '
            '" · 문서 · " + if(status, status, "미확인"), '
            'if(status, status, "상태 미상")))',
            "상태",
        ),
        "period": ("event_period", "발생 시기·불확실성"),
        "occurred": ("occurred_on", "발생일"),
        "started": ("started_on", "시작일"),
        "ended": ("ended_on", "종료일"),
        "recorded": ("recorded_at", "녹음일"),
        "calendar_date": ("Date", "Calendar 날짜"),
        "sequence": ("sequence", "확인된 순서"),
        "candidate_ids": ("candidate_person_ids", "화자 후보 ID"),
        "unresolved": ("unresolved_speaker_count", "미확인 화자 수"),
        "sort_time": (
            'if(record_kind == "recording" && recorded_at, date(recorded_at), '
            "if(occurred_on, date(occurred_on), if(started_on, date(started_on), "
            "if(ended_on, date(ended_on), if(Date, date(Date), null)))))",
            "시점 정렬",
        ),
        "date_context": (
            'if(record_kind == "recording" && recorded_at, "녹음 시점", '
            'if(occurred_on, "발생 시점", if(started_on || ended_on, "활동 기간", '
            'if(Date, if(type == "calendar-event", "예정 시점", "원본 날짜"), '
            '"날짜 미상 · 시기 원문 참고"))))',
            "날짜 기준",
        ),
    }

    def view(name: str, filters: str, fields: list[str]) -> dict[str, object]:
        return {
            "type": "table",
            "name": name,
            "filters": filters,
            "groupBy": {"property": "formula.date_context", "direction": "ASC"},
            "order": ["formula." + field for field in fields],
            "sort": [
                {"property": "formula." + field, "direction": direction}
                for field, direction in (("sort_time", "DESC"), ("sequence", "ASC"))
            ],
        }

    data = {
        "filters": {
            "and": [
                'file.ext == "md"',
                "file.path != this.file.path",
                'this.access == "local-only"',
                "this.publish == false",
                '(!this.publication_state || this.publication_state == "private")',
                '(this.file.path.startsWith("private/") || '
                'this.file.path.startsWith("wiki/private/") || '
                'this.publication_state == "private")',
                'access == "local-only"',
                "publish == false",
                '(!publication_state || publication_state == "private")',
                '(file.inFolder("private") || file.inFolder("wiki/private") || '
                'publication_state == "private")',
                f"{list(RECORD_KINDS)!r}.contains(record_kind)",
                "(canonical_id || recording_id || record_id)",
            ]
        },
        "formulas": {key: value for key, (value, _label) in columns.items()},
        "properties": {
            "formula." + key: {"displayName": label} for key, (_value, label) in columns.items()
        },
        "views": [
            view("관련 기록", relation, ["moment", "record", "kind", "role", "state"]),
            view(
                "날짜 미상",
                f"({relation}) && !({known_time})",
                ["record", "kind", "period", "sequence", "role"],
            ),
            view(
                "화자 확인 필요",
                f"(({relation}) || {candidates}) && "
                "(list(candidate_person_ids).filter(value).length > 0 || "
                "unresolved_speaker_count > 0)",
                ["record", "recorded", "candidate_ids", "unresolved", "state"],
            ),
            view(
                "날짜 상세",
                relation,
                ["record", "occurred", "started", "ended", "recorded", "calendar_date", "period"],
            ),
        ],
    }
    return _PRIVATE_MARKER + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)


def _render_base() -> str:
    """Render one person-relative dashboard with explicit time semantics."""

    return """# woon_projection: person-dashboard-base
filters:
  and:
    - file.ext == "md"
    - file.path != this.file.path
    - people.contains(this)
    - '!file.inFolder("private")'
    - '!file.inFolder("wiki/private/novel")'
    - '!file.inFolder("wiki/private/people")'
    - '!file.inFolder("wiki/private/_sources")'
properties:
  title:
    displayName: "문서"
  Date:
    displayName: "날짜"
  Time:
    displayName: "시간"
  Start Date:
    displayName: "시작"
  End Date:
    displayName: "종료"
  Category:
    displayName: "분류"
  type:
    displayName: "종류"
  status:
    displayName: "상태"
views:
  - type: table
    name: "프로젝트·학습·자료"
    limit: 40
    filters:
      and:
        - type != "calendar-event"
    order:
      - title
      - type
      - status
      - parent
    sort:
      - property: title
        direction: ASC
  - type: table
    name: "다가오는 일정"
    limit: 30
    filters:
      and:
        - type == "calendar-event"
        - Date >= today()
    order:
      - title
      - Date
      - Time
      - Category
    sort:
      - property: Date
        direction: ASC
      - property: Start Date
        direction: ASC
  - type: table
    name: "지난 일정"
    limit: 40
    filters:
      and:
        - type == "calendar-event"
        - Date < today()
    order:
      - title
      - Date
      - Time
      - Category
    sort:
      - property: Date
        direction: DESC
      - property: Start Date
        direction: DESC
"""
