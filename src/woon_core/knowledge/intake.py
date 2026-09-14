"""Explicit Inbox requests and crash recovery around the existing Wiki writers.

An intake identifies selected source material, not its generated title, a Docling
conversion, or an approval. Writers retain their own authorization, privacy,
revision and semantic-review contracts. This module never writes Wiki content.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from woon_core.errors import WoonError
from woon_core.io import atomic_write, encode_json, exclusive_file_lock
from woon_core.knowledge.second_brain_candidates import render_intake_candidate
from woon_core.knowledge.wiki_tree import split_markdown
from woon_core.knowledge.woon_wiki import is_retired_wiki_record

_ID = re.compile(r"intake-[0-9a-f]{32}")
_SHA = re.compile(r"[0-9a-f]{64}")
_RUNTIME = Path(".local/woon-knowledge/document-intake/requests")
_INBOX = Path("inbox/capture")


@dataclass(frozen=True, slots=True)
class IntakeSource:
    """An owner-verified source and stable selection; revision is its input hash."""

    source_id: str
    selection: str
    revision: str
    locator: str


@dataclass(frozen=True, slots=True)
class IntakeTarget:
    """Exact, semantically reviewed output of an existing Wiki writer."""

    canonical_id: str
    path: str
    before_revision: str | None
    after_revision: str


def register_intake(
    vault: Path,
    *,
    sources: tuple[IntakeSource, ...],
    title: str,
    summary: str,
    keywords: tuple[str, ...] = (),
    explicit_request: bool = False,
    expected_input_revision: str | None = None,
) -> dict[str, Any]:
    """Register one requested meaning unit, reusing its card and destination.

    No source is copied or corrected here. A changed source revision requires
    the caller's previously observed input revision, just like a Wiki edit.
    """
    if explicit_request is not True:
        raise WoonError("intake requires an explicit request to organize selected material")
    ordered = _sources(sources)
    identity = [{"source_id": s.source_id, "selection": s.selection} for s in ordered]
    intake_id = "intake-" + _digest(identity)[:32]
    revision = _digest(
        [{**i, "revision": s.revision} for i, s in zip(identity, ordered, strict=True)]
    )
    _label(title, "title", 48)
    _label(summary, "summary", 280)
    if len(keywords) > 8:
        raise WoonError("intake needs at most eight candidate keywords")
    for keyword in keywords:
        _label(keyword, "keyword", 48)
    with _locked(vault, intake_id) as (root, path):
        previous = _load(path) if path.exists() else None
        if previous and previous["input_revision"] == revision:
            if previous["state"] not in {"pending", "registering"}:
                return _result(root, previous, replayed=True)
        elif previous:
            if previous["state"] not in {"pending", "complete"}:
                raise WoonError("finish the existing intake revision before replacing its input")
            if expected_input_revision != previous["input_revision"]:
                raise WoonError("intake input changed; supply its current input revision")
        elif expected_input_revision is not None:
            raise WoonError("intake does not have the expected input revision")

        content = render_intake_candidate(
            intake_id=intake_id,
            title=title,
            summary=summary,
            source_locators=tuple(s.locator for s in ordered),
            keywords=keywords,
        ).encode()
        card = previous.get("card") if previous else None
        if card:
            destination = _inside(root, card["path"])
            actual = _hash(destination) if destination.is_file() else None
            recovering = previous is not None and previous["state"] == "registering"
            if recovering and hashlib.sha256(content).hexdigest() != card["sha256"]:
                raise WoonError("finish interrupted Inbox registration before editing its title")
            acceptable = {card["sha256"]}
            if recovering:
                assert previous is not None
                acceptable.add(previous.get("card_before_sha256"))
            if actual not in acceptable:
                raise WoonError("Inbox card was edited; preserve it and reconcile before retry")
        else:
            destination = _card_path(root, title)
        _mkdir(root, destination.parent)
        # Store the expected card before creating it so an interrupted initial
        # registration can be recovered without choosing a second filename.
        value: dict[str, Any] = {
            "version": 1,
            "intake_id": intake_id,
            "input_revision": revision,
            "sources": [asdict(s) for s in ordered],
            "state": "registering",
            "card": {
                "path": destination.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
            "destinations": previous.get("destinations", []) if previous else [],
        }
        if (
            previous
            and previous["state"] == "pending"
            and previous["input_revision"] == revision
            and destination.read_bytes() == content
        ):
            return _result(root, previous, replayed=True)
        value["card_before_sha256"] = _hash(destination) if destination.is_file() else None
        atomic_write(path, encode_json(value), mode=0o600)
        atomic_write(destination, content, mode=0o600)
        value["state"] = "pending"
        value.pop("card_before_sha256")
        atomic_write(path, encode_json(value), mode=0o600)
        return _result(root, value, replayed=False)


def read_intake(vault: Path, intake_id: str) -> dict[str, Any]:
    """Read the minimal current record; no chat history or candidate body is needed."""
    root = vault.expanduser().resolve()
    path = _state_path(root, intake_id)
    return _result(root, _load(path), replayed=True)


def prepare_intake(
    vault: Path,
    intake_id: str,
    input_revision: str,
    targets: tuple[IntakeTarget, ...],
) -> dict[str, Any]:
    """Bind reviewed target hashes before writing, or recover a finished write.

    The caller runs its existing writer only when ``write_required`` is true.
    A mixed partial batch must be recovered by that writer, never overwritten.
    """
    if not targets or len(targets) > 24:
        raise WoonError("intake requires one to twenty-four reviewed targets")
    records = [asdict(t) for t in targets]
    if len({t.path for t in targets}) != len(targets):
        raise WoonError("intake targets must have distinct paths")
    with _locked(vault, intake_id) as (root, path):
        value = _load(path)
        _revision(value, input_revision)
        if value["state"] == "registering":
            raise WoonError("finish Inbox registration before preparing the writer")
        for target in targets:
            _target(root, asdict(target))
        destinations = [{"canonical_id": t.canonical_id, "path": t.path} for t in targets]
        if value.get("destinations") and value["destinations"] != destinations:
            raise WoonError("intake destination changed; reconcile the existing identity first")
        if value["state"] == "complete":
            return {**_result(root, value, replayed=True), "write_required": False}
        if value.get("targets") and value["targets"] != records:
            raise WoonError("intake already has another prepared writer plan")
        states = [_target_state(root, t) for t in records]
        if "drift" in states or ("before" in states and "after" in states):
            raise WoonError("intake target changed or is partially written; recover the writer")
        if value["state"] == "pending":
            _verify_card(root, value)
            value.update(state="prepared", targets=records, destinations=destinations)
            atomic_write(path, encode_json(value), mode=0o600)
        return {**_result(root, value, replayed=True), "write_required": "before" in states}


def complete_intake(
    vault: Path,
    intake_id: str,
    input_revision: str,
    *,
    reviewed_revisions: tuple[str, ...],
) -> dict[str, Any]:
    """Verify reviewed bytes, then remove only the consumed, unedited AI card.

    ``reviewed_revisions`` attests the caller checked meaning in the actual
    target contents. Title matches, hub existence and old completion prose do
    not establish semantic preservation. Deleting the card is not source backup.
    """
    with _locked(vault, intake_id) as (root, path):
        value = _load(path)
        _revision(value, input_revision)
        if value["state"] == "complete":
            return _result(root, value, replayed=True)
        targets = value.get("targets", [])
        if not targets or tuple(t["after_revision"] for t in targets) != reviewed_revisions:
            raise WoonError("intake requires semantic review of the exact prepared outputs")
        if any(_target_state(root, t) not in {"after", "unchanged"} for t in targets):
            raise WoonError("intake output has not been verified; keep the Inbox item")
        value["state"] = "cleanup-pending"
        atomic_write(path, encode_json(value), mode=0o600)
        card_path = _inside(root, value["card"]["path"])
        if card_path.exists():
            _verify_card(root, value)
            card_path.unlink()
        value["state"] = "complete"
        value["results"] = [
            {"canonical_id": t["canonical_id"], "path": t["path"], "revision": t["after_revision"]}
            for t in targets
        ]
        value.pop("card", None)
        value.pop("targets", None)
        atomic_write(path, encode_json(value), mode=0o600)
        return _result(root, value, replayed=False)


def run_intake(
    vault: Path,
    intake_id: str,
    input_revision: str,
    *,
    targets: tuple[IntakeTarget, ...],
    writer: Callable[[], object],
    reviewed_revisions: tuple[str, ...],
) -> dict[str, Any]:
    """Run one existing, revision-guarded writer and finish the same authorized request."""
    if tuple(t.after_revision for t in targets) != reviewed_revisions:
        raise WoonError("intake requires semantic review before invoking its writer")
    root = vault.expanduser().resolve()
    state = _state_path(root, intake_id)
    _load(state)
    # Serialize callbacks for this intake as well as the state transitions.
    # The writer still owns revision guards against other, unrelated writers.
    with exclusive_file_lock(state.with_suffix(".writer.lock")):
        plan = prepare_intake(vault, intake_id, input_revision, targets)
        if plan["write_required"]:
            writer()
        return complete_intake(
            vault,
            intake_id,
            input_revision,
            reviewed_revisions=reviewed_revisions,
        )


def _sources(sources: tuple[IntakeSource, ...]) -> tuple[IntakeSource, ...]:
    if not sources or len(sources) > 24:
        raise WoonError("intake needs one to twenty-four explicit source selections")
    for source in sources:
        _label(source.source_id, "source_id", 240)
        _label(source.selection, "selection", 240)
        _label(source.locator, "locator", 1024)
        if not _SHA.fullmatch(source.revision):
            raise WoonError("intake source revision must be SHA-256")
    keys = [(s.source_id, s.selection) for s in sources]
    if len(set(keys)) != len(keys):
        raise WoonError("intake source selections must be unique")
    return tuple(sorted(sources, key=lambda s: (s.source_id, s.selection)))


def _label(value: str, name: str, limit: int) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > limit
        or any(ord(c) < 32 for c in value)
    ):
        raise WoonError(f"intake {name} must be a bounded single-line value")


def _digest(value: object) -> str:
    return hashlib.sha256(encode_json(value)).hexdigest()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise WoonError("intake path must stay inside its vault")
    path = root / candidate
    current = root
    for part in candidate.parts:
        current /= part
        if current.is_symlink():
            raise WoonError("intake refuses symlink paths")
    return path


def _mkdir(root: Path, directory: Path) -> None:
    current = root
    for part in directory.relative_to(root).parts:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise WoonError("intake runtime must use regular directories")
        current.mkdir(exist_ok=True, mode=0o700)


def _state_path(root: Path, intake_id: str) -> Path:
    if not _ID.fullmatch(intake_id):
        raise WoonError("invalid intake ID; conversion and approval IDs are different")
    return _inside(root, (_RUNTIME / f"{intake_id}.json").as_posix())


@contextmanager
def _locked(vault: Path, intake_id: str) -> Iterator[tuple[Path, Path]]:
    root = vault.expanduser().resolve()
    if not root.is_dir():
        raise WoonError("intake vault does not exist")
    path = _state_path(root, intake_id)
    _mkdir(root, path.parent)
    # Registration chooses a readable, collision-free card path across requests.
    with exclusive_file_lock(path.parent / "requests.lock"):
        yield root, path


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise WoonError("intake is missing; register the explicit input first") from error
    if (
        not isinstance(value, dict)
        or value.get("version") != 1
        or value.get("intake_id") != path.stem
        or not _SHA.fullmatch(str(value.get("input_revision", "")))
        or value.get("state")
        not in {
            "registering",
            "pending",
            "prepared",
            "cleanup-pending",
            "complete",
        }
    ):
        raise WoonError("invalid intake state")
    if value["state"] != "complete":
        card = value.get("card")
        if (
            not isinstance(card, dict)
            or not isinstance(card.get("path"), str)
            or not Path(card["path"]).is_relative_to(_INBOX)
            or ".." in Path(card["path"]).parts
            or not _SHA.fullmatch(str(card.get("sha256", "")))
        ):
            raise WoonError("invalid intake card boundary")
    else:
        results = value.get("results")
        if (
            not isinstance(results, list)
            or not results
            or any(
                not isinstance(r, dict)
                or not isinstance(r.get("path"), str)
                or not isinstance(r.get("canonical_id"), str)
                or not _SHA.fullmatch(str(r.get("revision", "")))
                for r in results
            )
        ):
            raise WoonError("invalid intake terminal results")
    return value


def _revision(value: dict[str, Any], expected: str) -> None:
    if value["input_revision"] != expected:
        raise WoonError("intake input revision changed; reread before writing")


def _card_path(root: Path, title: str) -> Path:
    stem = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", title).strip("-_") or "검토"
    for number in range(1, 10_000):
        name = stem if number == 1 else f"{stem}-{number}"
        path = _inside(root, (_INBOX / f"{name}.md").as_posix())
        if not path.exists():
            return path
    raise WoonError("cannot choose an unused Inbox path")


def _verify_card(root: Path, value: dict[str, Any]) -> None:
    card = value.get("card", {})
    relative = card.get("path", "")
    path = _inside(root, relative)
    if (
        not Path(relative).is_relative_to(_INBOX)
        or not path.is_file()
        or _hash(path) != card.get("sha256")
    ):
        raise WoonError("Inbox card was edited; preserve it and reconcile before cleanup")


def _target(root: Path, record: dict[str, Any]) -> Path:
    path = _inside(root, record["path"])
    if (
        not Path(record["path"]).is_relative_to("wiki")
        or path.suffix != ".md"
        or Path(record["path"]).is_relative_to("wiki/private/_sources")
        or not _SHA.fullmatch(record["after_revision"])
        or (record["before_revision"] is not None and not _SHA.fullmatch(record["before_revision"]))
    ):
        raise WoonError("intake needs an exact Wiki target and before/after hashes")
    _label(record["canonical_id"], "canonical_id", 512)
    return path


def _target_state(root: Path, record: dict[str, Any]) -> str:
    path = _target(root, record)
    actual = _hash(path) if path.is_file() else None
    if actual == record["after_revision"]:
        header, _ = split_markdown(path.read_text())
        if header.get("canonical_id") != record["canonical_id"] or is_retired_wiki_record(header):
            raise WoonError("intake target identity is missing or retired")
        return "unchanged" if actual == record["before_revision"] else "after"
    if actual == record["before_revision"]:
        if path.is_file():
            header, _ = split_markdown(path.read_text())
            if header.get("canonical_id") != record["canonical_id"]:
                raise WoonError("intake target identity changed before writing")
        return "before"
    return "drift"


def _result(root: Path, value: dict[str, Any], *, replayed: bool) -> dict[str, Any]:
    result = dict(value)
    result["replayed"] = replayed
    result["receipt"] = (_RUNTIME / f"{value['intake_id']}.json").as_posix()
    if value["state"] == "complete":
        result["results"] = []
        for target in value.get("results", []):
            path = _inside(root, target["path"])
            header, _ = split_markdown(path.read_text()) if path.is_file() else ({}, "")
            result["results"].append(
                {
                    **target,
                    "available": (
                        header.get("canonical_id") == target["canonical_id"]
                        and not is_retired_wiki_record(header)
                    ),
                    "current_revision": _hash(path) if path.is_file() else None,
                }
            )
    return result
