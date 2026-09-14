"""JSON interfaces for explicit Inbox requests and document terminal cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TextIO

from woon_core.errors import WoonError
from woon_core.knowledge.document_resolution import (
    cleanup_document_candidate,
    read_document_resolution,
)
from woon_core.knowledge.factory import resolve_knowledge_vault
from woon_core.knowledge.intake import (
    IntakeSource,
    IntakeTarget,
    complete_intake,
    prepare_intake,
    read_intake,
    register_intake,
)
from woon_core.knowledge.review_resolution import complete_review, read_review_resolution
from woon_core.knowledge.source_compaction import plan_source_body_compaction


def execute_intake_request(vault: Path, action: str, request: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a bounded typed request; writing Wiki content remains the owner's job."""
    fields = {
        "register": (
            {"sources", "title", "summary", "explicit_request"},
            {"keywords", "expected_input_revision"},
        ),
        "status": ({"intake_id"}, set()),
        "prepare": ({"intake_id", "input_revision", "targets"}, set()),
        "complete": ({"intake_id", "input_revision", "reviewed_revisions"}, set()),
        "document-status": ({"candidate_id"}, set()),
        "document-cleanup": ({"candidate_id", "expected_resolution_sha256"}, set()),
        "source-body-plan": (
            {"source_ids", "protected_source_ids", "reviewed_successors", "review_reference"},
            set(),
        ),
        "review-status": ({"relative_path", "source_sha256"}, set()),
        "review-complete": (
            {"relative_path", "source_sha256", "disposition", "review_reference"},
            set(),
        ),
    }
    if action not in fields or not isinstance(request, dict):
        raise WoonError("unsupported intake action or request")
    required, optional = fields[action]
    if not required.issubset(request) or set(request) - required - optional:
        raise WoonError(f"intake {action} request has missing or unsupported fields")
    try:
        if action == "register":
            sources = tuple(IntakeSource(**s) for s in request["sources"])
            return register_intake(
                vault,
                **{
                    **request,
                    "sources": sources,
                    "keywords": tuple(request.get("keywords", [])),
                },
            )
        if action == "status":
            return read_intake(vault, request["intake_id"])
        if action == "prepare":
            return prepare_intake(
                vault,
                request["intake_id"],
                request["input_revision"],
                tuple(IntakeTarget(**t) for t in request["targets"]),
            )
        if action == "complete":
            return complete_intake(
                vault,
                request["intake_id"],
                request["input_revision"],
                reviewed_revisions=tuple(request["reviewed_revisions"]),
            )
        if action == "document-status":
            return read_document_resolution(vault, request["candidate_id"])
        if action == "review-status":
            return read_review_resolution(vault, **request) or {"state": "unresolved"}
        if action == "review-complete":
            return complete_review(vault, **request)
        if action == "source-body-plan":
            return plan_source_body_compaction(
                vault,
                **{
                    **request,
                    "source_ids": tuple(request["source_ids"]),
                    "protected_source_ids": tuple(request["protected_source_ids"]),
                },
            )
        return cleanup_document_candidate(vault, **request)
    except (TypeError, KeyError, AttributeError) as error:
        raise WoonError(f"intake {action} request has invalid field types") from error


def run_intake_command(arguments: list[str], output: TextIO) -> None:
    """Run ``woon knowledge intake ACTION --request FILE [--vault PATH]``."""
    if not arguments:
        raise WoonError("knowledge intake requires register, status, prepare or complete")
    action, *options = arguments
    values: dict[str, str] = {}
    while options:
        option, *options = options
        if option not in {"--request", "--vault"} or option in values or not options:
            raise WoonError("intake requires --request FILE and optional --vault PATH")
        values[option], *options = options
    if "--request" not in values:
        raise WoonError("intake requires --request FILE")
    try:
        request = json.loads(Path(values["--request"]).expanduser().read_text())
    except (OSError, ValueError) as error:
        raise WoonError("intake request must be a readable JSON file") from error
    vault = Path(values["--vault"]) if "--vault" in values else resolve_knowledge_vault()
    result = execute_intake_request(vault, action, request)
    print(json.dumps(result, ensure_ascii=False, indent=2), file=output)
