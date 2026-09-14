"""Command-line interface for the career application pipeline."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TextIO

from woon_core.career.current_jobs import CurrentJobs, current_job_sources
from woon_core.career.service import CareerApplicationService, CareerResult
from woon_core.errors import WoonError
from woon_core.knowledge.factory import resolve_knowledge_vault


def run_career(arguments: list[str], output: TextIO) -> None:
    if not arguments:
        raise WoonError(
            "usage: woon career <create|analyze|evaluate|approve-draft|attach-pdf|"
            "mark-reviewed|mark-ready|reopen|outcome|context|show|evidence|compose|draft|impact|"
            "jobs-prepare|jobs-base|jobs-sources>"
        )
    command, *options = arguments
    if command == "jobs-sources":
        if options:
            raise WoonError("career jobs-sources accepts no options")
        output.write(json.dumps(current_job_sources(), ensure_ascii=False, indent=2) + "\n")
        return
    values, positionals = _options(options)
    vault = (
        Path(values.pop("--vault")).expanduser()
        if "--vault" in values
        else resolve_knowledge_vault()
    )
    repositories = None
    if "--repositories" in values:
        raw = _json_input(values.pop("--repositories"))
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise WoonError("career repositories must map repository IDs to local roots")
        repositories = {key: Path(value) for key, value in raw.items()}
    if command == "jobs-prepare":
        if positionals or set(values) != {"--spec", "--now"}:
            raise WoonError("career jobs-prepare requires --spec and timezone-aware --now")
        spec = _json_input(values["--spec"])
        if not isinstance(spec, dict):
            raise WoonError("career jobs spec must be an object")
        try:
            now = datetime.fromisoformat(values["--now"])
        except ValueError as error:
            raise WoonError("career jobs --now must be an ISO datetime") from error
        jobs = CurrentJobs(vault, repositories).prepare(spec, now=now)
        output.write(json.dumps(jobs, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return
    if command == "jobs-base":
        if positionals or values:
            raise WoonError("career jobs-base accepts only --vault")
        output.write(json.dumps(CurrentJobs(vault).refresh_base(), ensure_ascii=False) + "\n")
        return
    service = CareerApplicationService(vault, repositories=repositories)
    if command == "evidence":
        if positionals or set(values) != {"--spec"}:
            raise WoonError("career evidence requires --spec")
        spec = _json_input(values["--spec"])
        if not isinstance(spec, dict):
            raise WoonError("career evidence spec must be an object")
        output.write(json.dumps(service.evidence(spec), ensure_ascii=False, indent=2) + "\n")
        return
    if command == "compose":
        _one_id(positionals, values, allowed={"--id", "--selections"}, required={"--selections"})
        selections = _json_input(values["--selections"])
        if not isinstance(selections, list) or not all(isinstance(x, dict) for x in selections):
            raise WoonError("career selections must be an array of objects")
        _result(service.compose(values["--id"], selections), output)
        return
    if command == "draft":
        _one_id(positionals, values, allowed={"--id"})
        output.write(json.dumps(service.draft(values["--id"]), ensure_ascii=False, indent=2) + "\n")
        return
    if command == "impact":
        if positionals or set(values).difference({"--id"}):
            raise WoonError("career impact accepts optional --id")
        impacted = service.impact([values["--id"]] if "--id" in values else None)
        output.write(json.dumps(impacted, ensure_ascii=False, indent=2) + "\n")
        return
    if command == "create":
        required = {"--id", "--company", "--role", "--jd"}
        if (
            positionals
            or not required.issubset(values)
            or set(values).difference(required | {"--deadline"})
        ):
            raise WoonError(
                "career create requires --id --company --role --jd and optional --deadline"
            )
        result = service.create(
            application_id=values["--id"],
            company=values["--company"],
            role=values["--role"],
            jd_path=Path(values["--jd"]),
            deadline=values.get("--deadline"),
        )
        _result(result, output)
        return
    if command == "analyze":
        _one_id(positionals, values, allowed={"--id", "--max-requirements"})
        count = int(values.get("--max-requirements", "12"))
        _result(service.analyze(values["--id"], max_requirements=count), output)
        return
    if command == "evaluate":
        _one_id(positionals, values, allowed={"--id", "--matrix"}, required={"--matrix"})
        try:
            matrix = json.loads(Path(values["--matrix"]).expanduser().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise WoonError(f"career matrix could not be read: {error}") from error
        if not isinstance(matrix, list) or not all(isinstance(item, dict) for item in matrix):
            raise WoonError("career matrix must be a JSON array of objects")
        _result(service.evaluate(values["--id"], matrix), output)
        return
    if command in {"approve-draft", "mark-reviewed", "mark-ready"}:
        _one_id(positionals, values, allowed={"--id", "--confirmed"}, required={"--confirmed"})
        confirmed = _confirmed(values["--confirmed"])
        action = {
            "approve-draft": service.approve_draft,
            "mark-reviewed": service.mark_reviewed,
            "mark-ready": service.mark_ready,
        }[command]
        _result(action(values["--id"], confirmed=confirmed), output)
        return
    if command == "attach-pdf":
        required = {"--pdf", "--kind"}
        _one_id(
            positionals,
            values,
            allowed={"--id", "--pdf", "--kind", "--confirmed"},
            required=required,
        )
        _result(
            service.attach_pdf(
                values["--id"],
                Path(values["--pdf"]),
                kind=values["--kind"],
                confirmed=_confirmed(values.get("--confirmed", "false")),
            ),
            output,
        )
        return
    if command == "outcome":
        _one_id(
            positionals,
            values,
            allowed={
                "--id",
                "--outcome",
                "--confirmed",
                "--occurred-on",
                "--evidence-kind",
                "--evidence-summary",
                "--evidence-subject",
                "--evidence-sender",
                "--evidence-locator",
                "--received-at",
            },
            required={
                "--outcome",
                "--confirmed",
                "--occurred-on",
                "--evidence-kind",
                "--evidence-summary",
            },
        )
        evidence = {
            key.removeprefix("--evidence-").replace("-", "_"): value
            for key, value in values.items()
            if key.startswith("--evidence-")
        }
        if "--received-at" in values:
            evidence["received_at"] = values["--received-at"]
        _result(
            service.outcome(
                values["--id"],
                values["--outcome"],
                confirmed=_confirmed(values["--confirmed"]),
                occurred_on=values["--occurred-on"],
                evidence=evidence,
            ),
            output,
        )
        return
    if command == "reopen":
        required = {"--state", "--reason", "--confirmed"}
        _one_id(
            positionals,
            values,
            allowed={"--id", "--state", "--reason", "--confirmed"},
            required=required,
        )
        _result(
            service.reopen(
                values["--id"],
                state=values["--state"],
                reason=values["--reason"],
                confirmed=_confirmed(values["--confirmed"]),
            ),
            output,
        )
        return
    if command == "context":
        _one_id(positionals, values, allowed={"--id", "--max-items"})
        bundle = service.context(values["--id"], max_items=int(values.get("--max-items", "12")))
        output.write(
            json.dumps(bundle.to_record(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        return
    if command == "show":
        _one_id(positionals, values, allowed={"--id"})
        output.write(
            json.dumps(service.show(values["--id"]), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        )
        return
    raise WoonError(f"unknown career command {command!r}")


def _options(arguments: list[str]) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    positionals: list[str] = []
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if not option.startswith("--"):
            positionals.append(option)
            index += 1
            continue
        if index + 1 >= len(arguments) or option in values:
            raise WoonError(f"{option} requires exactly one value")
        values[option] = arguments[index + 1]
        index += 2
    return values, positionals


def _json_input(value: str) -> object:
    try:
        return json.loads(Path(value).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WoonError(f"career input could not be read: {error}") from error


def _one_id(
    positionals: list[str],
    values: dict[str, str],
    *,
    allowed: set[str],
    required: set[str] | None = None,
) -> None:
    needed = {"--id", *(required or set())}
    if positionals or not needed.issubset(values) or set(values).difference(allowed):
        raise WoonError(f"career command requires {' '.join(sorted(needed))}")


def _confirmed(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise WoonError("career confirmation must be true or false")


def _result(result: CareerResult, output: TextIO) -> None:
    output.write(
        f"status: ok\napplication_id: {result.application_id}\nstate: {result.state}\n"
        f"changed: {str(result.changed).lower()}\nrecord: {result.relative_path}\n"
    )
