"""Submit explicitly selected private audio once and retain resumable diarized responses."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

MODEL = "gpt-4o-transcribe-diarize"
PLAIN_MODEL = "gpt-transcribe"
CLASSIC_MODEL = "gpt-4o-transcribe"
TEXT_MODELS = {PLAIN_MODEL, CLASSIC_MODEL}
SETTINGS = {
    MODEL: {"response_format": "diarized_json", "chunking_strategy": "auto", "language": "ko"},
    PLAIN_MODEL: {"languages[]": "ko"},
    CLASSIC_MODEL: {"response_format": "json", "language": "ko"},
}
RATES = {MODEL: 0.006, PLAIN_MODEL: 0.0045, CLASSIC_MODEL: 0.006}
OUTPUTS = {
    MODEL: "openai-diarized-v1",
    PLAIN_MODEL: "openai-transcribed-v1",
    CLASSIC_MODEL: "openai-4o-transcribed-v1",
}
API = "https://api.openai.com/v1"
JOURNAL_LOCK = threading.Lock()


class TranscriptionError(RuntimeError):
    """A safe error message without provider text or credentials."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_key(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise TranscriptionError("Credential file permissions must be 0600")
    key = path.read_text().strip()
    if not key.startswith("sk-") or any(c.isspace() for c in key):
        raise TranscriptionError("Invalid credential format")
    return key


def request_json(request: urllib.request.Request) -> tuple[dict[str, Any], str]:
    try:
        with urllib.request.urlopen(request, timeout=900) as response:
            return json.load(response), response.headers.get("x-request-id", "")
    except urllib.error.HTTPError as error:
        # Provider error messages may echo part of a credential; never persist them.
        try:
            code = json.load(error).get("error", {}).get("code")
        except (json.JSONDecodeError, AttributeError):
            code = None
        known_codes = {"invalid_api_key", "insufficient_quota", "model_not_found"}
        detail = f" ({code})" if code in known_codes else ""
        raise TranscriptionError(f"OpenAI HTTP {error.code}{detail}; no automatic retry") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise TranscriptionError(f"Uncertain request outcome: {type(error).__name__}") from None


def check_key(key: str, model: str = MODEL) -> dict[str, Any]:
    response, request_id = request_json(
        urllib.request.Request(f"{API}/models/{model}", headers={"Authorization": f"Bearer {key}"})
    )
    return {
        "authenticated": response.get("id") == model,
        "model": model,
        "request_id": request_id,
        "audio_submitted": False,
    }


def validate_response(response: dict[str, Any], model: str = MODEL) -> None:
    if model in TEXT_MODELS:
        if not isinstance(response.get("text"), str):
            raise TranscriptionError("Response has no transcript text")
        return
    segments = response.get("segments")
    if not isinstance(segments, list):
        raise TranscriptionError("Response has no diarized segments list")
    if not segments and response.get("text", "").strip():
        raise TranscriptionError("Nonempty transcript has no diarized segments")
    for segment in segments:
        if not isinstance(segment.get("text"), str) or "speaker" not in segment:
            raise TranscriptionError("Segment lacks text or speaker")
        start, end = segment.get("start"), segment.get("end")
        if not all(isinstance(n, (int, float)) and math.isfinite(n) for n in (start, end)):
            raise TranscriptionError("Segment timestamps are invalid")
        if start < 0 or end < start:
            raise TranscriptionError("Segment timestamps are reversed")


def quality_flags(response: dict[str, Any], model: str) -> list[str]:
    flags = []
    if model == CLASSIC_MODEL and response.get("usage", {}).get("output_tokens", 0) >= 2000:
        flags.append("possible-output-truncation")
    for match in re.finditer(r"(.{1,60}?)\1{9,}", response.get("text", ""), re.DOTALL):
        if len(match.group()) >= 80:
            flags.append("repetitive-output-needs-review")
            break
    return flags


def multipart(path: Path, settings: dict[str, str], model: str = MODEL) -> tuple[bytes, str]:
    media_types = {".m4a": "audio/mp4", ".wav": "audio/wav"}
    extension = path.suffix.lower()
    if extension not in media_types:
        raise TranscriptionError("Unsupported prepared audio format")
    boundary = f"woon-{uuid.uuid4().hex}"
    pieces = []
    for name, value in {"model": model, **settings}.items():
        pieces.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
            f"\r\n\r\n{value}\r\n".encode()
        )
    pieces.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="audio{extension}"\r\nContent-Type: {media_types[extension]}\r\n\r\n'.encode()
    )
    pieces += [path.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
    return b"".join(pieces), f"multipart/form-data; boundary={boundary}"


def append_receipt(path: Path, record: dict[str, Any]) -> None:
    with JOURNAL_LOCK, path.open("a") as stream:
        stream.write(json.dumps({"time": time.time(), **record}, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def save_new_json(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    # Exclusive creation protects a prior response even across interrupted invocations.
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def load_parts(plan_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    plan = json.loads(plan_path.read_text())
    model = plan.get("model")
    if model not in SETTINGS or plan.get("publication_state") != "private":
        raise TranscriptionError("Expected a private supported transcription input plan")
    settings = plan.get("request_settings", {})
    if settings != SETTINGS[model]:
        raise TranscriptionError("Unexpected request settings")
    root = plan_path.parent.resolve()
    parts = []
    seen = set()
    for job in plan["jobs"]:
        for index, part in enumerate(job["parts"]):
            path = (root / part["path"]).resolve()
            duration = part["duration_seconds"]
            if (
                not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration <= 0
            ):
                raise TranscriptionError("Input duration must be positive and finite")
            if not path.is_relative_to(root) or path.stat().st_size >= 25_000_000:
                raise TranscriptionError("Input is outside intake or exceeds upload limit")
            if digest(path) != part["sha256"]:
                raise TranscriptionError("Prepared audio hash changed")
            part_id = f"{job['observation_id']}-{index:03}"
            if part_id in seen:
                raise TranscriptionError("Duplicate part ID")
            seen.add(part_id)
            parts.append(
                {
                    **part,
                    "id": part_id,
                    "source_id": job["observation_id"],
                    "recording_started_at_kst": job["recording_started_at_kst"],
                    "diary_date": job["diary_date"],
                    "input": path,
                    "estimated_usd": duration / 60 * RATES[model],
                }
            )
    return plan, parts


def unfinished(
    parts: list[dict[str, Any]], output: Path, model: str = MODEL, *, review_only: bool = False
) -> list[dict[str, Any]]:
    journal = output / "requests.jsonl"
    attempted = set()
    if journal.exists():
        for line in journal.read_text().splitlines():
            event = json.loads(line)
            if event["event"] == "attempt":
                attempted.add(event["part_id"])
    pending = []
    for part in parts:
        saved = output / f"{part['id']}.json"
        if saved.exists():
            envelope = json.loads(saved.read_text())
            if envelope["input_sha256"] != part["sha256"] or envelope["model"] != model:
                raise TranscriptionError("Existing result does not match selected input")
            validate_response(envelope["provider_response"], model)
            if quality_flags(envelope["provider_response"], model) and not review_only:
                raise TranscriptionError(f"Saved result {part['id']} requires quality review")
            continue
        if part["id"] in attempted:
            raise TranscriptionError(
                f"Unresolved prior request {part['id']}; retry is not automatic"
            )
        pending.append(part)
    return pending


def transcribe_part(
    part: dict[str, Any], settings: dict[str, str], key: str, output: Path, model: str = MODEL
) -> dict[str, Any]:
    body, content_type = multipart(part["input"], settings, model)
    journal = output / "requests.jsonl"
    append_receipt(
        journal,
        {
            "event": "attempt",
            "part_id": part["id"],
            "input_sha256": part["sha256"],
            "model": model,
            "estimated_usd": part["estimated_usd"],
        },
    )
    try:
        response, request_id = request_json(
            urllib.request.Request(
                f"{API}/audio/transcriptions",
                data=body,
                method="POST",
                headers={"Authorization": f"Bearer {key}", "Content-Type": content_type},
            )
        )
        envelope = {
            "model": model,
            "part_id": part["id"],
            "source_id": part["source_id"],
            "input_sha256": part["sha256"],
            "request_id": request_id,
            "start_seconds": part["start_seconds"],
            "diary_date": part["diary_date"],
            "recording_started_at_kst": part["recording_started_at_kst"],
            "provider_response": response,
        }
        destination = output / f"{part['id']}.json"
        save_new_json(destination, envelope)
        reread = json.loads(destination.read_text())
        validate_response(reread["provider_response"], model)
        result = {
            "event": "complete",
            "part_id": part["id"],
            "request_id": request_id,
            "result_sha256": digest(destination),
            "segments": len(response.get("segments", [])),
            "text_chars": len(response.get("text", "")),
            "detected_languages": response.get("languages"),
            "usage": response.get("usage"),
            "estimated_usd": part["estimated_usd"],
            "quality_flags": quality_flags(response, model),
        }
        append_receipt(journal, result)
        return result
    except Exception as error:
        append_receipt(
            journal,
            {
                "event": "failed-or-uncertain",
                "part_id": part["id"],
                "error_type": type(error).__name__,
            },
        )
        raise


def run(
    plan_path: Path, key: str, limit: int, budget: float, workers: int, *, review_only: bool = False
) -> None:
    plan, parts = load_parts(plan_path)
    model = plan["model"]
    if review_only and (
        model != MODEL or plan.get("review_only_include_flagged_responses") is not True
    ):
        raise TranscriptionError("Verbatim review requires an explicitly marked diarization plan")
    if sum(p["estimated_usd"] for p in parts) > budget:
        raise TranscriptionError("Full selected pass exceeds the estimated budget")
    output = plan_path.parent / OUTPUTS[model]
    output.mkdir(exist_ok=True, mode=0o700)
    with (output / ".run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise TranscriptionError("Another transcription process is running") from None
        pending = iter(unfinished(parts, output, model, review_only=review_only)[:limit])
        failures: list[Exception] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            active: set[Future[dict[str, Any]]] = set()
            while True:
                while len(active) < workers and not failures:
                    part = next(pending, None)
                    if part is None:
                        break
                    active.add(
                        pool.submit(
                            transcribe_part, part, plan["request_settings"], key, output, model
                        )
                    )
                if not active:
                    break
                done, active = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        result = future.result()
                        print(json.dumps(result), flush=True)
                        if result.get("quality_flags") and not review_only:
                            failures.append(
                                TranscriptionError(
                                    "Saved response requires quality review; scheduling stopped"
                                )
                            )
                    except Exception as error:
                        failures.append(error)
        if failures:
            raise failures[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--run", action="store_true")
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--model", choices=tuple(SETTINGS), default=MODEL)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--limit-parts", type=int, default=1)
    parser.add_argument("--estimated-budget-usd", type=float, default=20)
    parser.add_argument("--workers", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument(
        "--verbatim-review-only",
        action="store_true",
        help="Retain flagged diarized responses for full-text review, without quality acceptance",
    )
    args = parser.parse_args()
    try:
        key = read_key(args.key_file)
        if args.check:
            print(json.dumps(check_key(key, args.model)))
        elif args.plan is None or args.limit_parts < 1:
            raise TranscriptionError("A plan and positive part limit are required")
        else:
            run(
                args.plan,
                key,
                args.limit_parts,
                args.estimated_budget_usd,
                args.workers,
                review_only=args.verbatim_review_only,
            )
    except TranscriptionError as error:
        print(json.dumps({"error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
