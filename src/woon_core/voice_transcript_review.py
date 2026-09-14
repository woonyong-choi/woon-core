"""Render private diarized API responses without rewriting or dropping utterances."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from woon_core.voice_transcription import MODEL, OUTPUTS, load_parts, validate_response


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"


def review(plan_path: Path) -> tuple[str, dict[str, Any]]:
    plan, parts = load_parts(plan_path)
    if plan["model"] != MODEL:
        raise ValueError("Only actual diarized responses can be rendered here")
    root = plan_path.parent
    rows, sources, blocks = [], [], []
    speakers: dict[str, dict[str, Any]] = {}
    completed = 0
    for part_number, part in enumerate(parts, start=1):
        path = root / OUTPUTS[MODEL] / f"{part['id']}.json"
        if not path.exists():
            continue
        envelope = json.loads(path.read_text())
        if envelope["model"] != MODEL or envelope["input_sha256"] != part["sha256"]:
            raise ValueError("Response does not match the input plan")
        response = envelope["provider_response"]
        validate_response(response)
        completed += 1
        response_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        # A speaker label is local to one API response, including separate pilot runs.
        namespace = f"{envelope['request_id']}:{part['id']}"
        section = [
            f"## 구간 {part_number:03} · {timestamp(part['start_seconds'])}"
            f"–{timestamp(part['end_seconds'])}",
            "",
        ]
        turns: list[tuple[str, list[str]]] = []
        for index, segment in enumerate(response["segments"]):
            speaker = str(segment["speaker"])
            key = f"{namespace}:{speaker}"
            start = part["start_seconds"] + segment["start"]
            end = part["start_seconds"] + segment["end"]
            row = {
                "source_response": str(path.relative_to(root)),
                "source_response_sha256": response_hash,
                "segment_index": index,
                "segment_id": segment.get("id"),
                "speaker_key": key,
                "api_speaker": speaker,
                "start_seconds": start,
                "end_seconds": end,
                "text": segment["text"],
            }
            rows.append(row)
            speakers.setdefault(key, {"api_speaker": speaker, "name": None, "confirmed_by": None})
            if turns and turns[-1][0] == speaker:
                turns[-1][1].append(segment["text"])
            else:
                turns.append((speaker, [segment["text"]]))
        for speaker, utterances in turns:
            section += [f"**{speaker} :** {' '.join(utterances)}", ""]
        if not response["segments"]:
            section += ["〔API가 반환한 발화 없음〕", ""]
        blocks.extend(section)
        sources.append({"path": str(path.relative_to(root)), "sha256": response_hash})

    date = plan["jobs"][0]["recording_started_at_kst"]
    body = [
        "---",
        "publish: false",
        "access: local-only",
        "status: Review",
        f"recording_started_at: {date}",
        "---",
        "",
        f"# {date[:10]} · 화자 확인용 원문 전사",
        "",
        f"녹음 시작: {date} · 준비한 {len(parts)}개 구간 중 {completed}개 응답.",
        "",
        "API가 반환한 모든 발화를 순서 그대로 표시했습니다. 추임새·반복·잡담·"
        "불분명한 문장을 삭제하거나 교정하지 않았습니다. 자동 전사이므로 원음과 다를 수 있습니다.",
        "",
        "화자 문자는 각 구간 안에서만 유효합니다. 이름을 지정할 때는 ‘구간 001의 A = 이름’처럼 "
        "구간도 함께 알려 주세요. 구간 경계의 겹치는 음성은 그대로 남겨 두었습니다.",
        "",
        *blocks,
    ]
    ledger = {
        "publication_state": "private",
        "access": "local-only",
        "model": MODEL,
        "recording_started_at_kst": date,
        "prepared_parts": len(parts),
        "completed_parts": completed,
        "source_plan": plan_path.name,
        "sources": sources,
        "verbatim_provider_text": True,
        "direct_listening_verified": False,
        "corrections": [],
        "speaker_mapping": speakers,
        "segments": rows,
    }
    return "\n".join(body), ledger


def save_unchanged_or_new(path: Path, data: str) -> None:
    if path.exists():
        if path.read_text() != data:
            raise ValueError(f"Existing artifact differs; select a new output name: {path.name}")
        return
    with path.open("x") as stream:
        stream.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    markdown, ledger = review(args.plan)
    output = args.output.resolve()
    if not output.is_relative_to(args.plan.parent.resolve()):
        raise ValueError("Review output must remain inside the private intake")
    save_unchanged_or_new(output, markdown)
    ledger_path = output.with_suffix(".json")
    save_unchanged_or_new(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "completed_parts": ledger["completed_parts"],
                "segments": len(ledger["segments"]),
                "output": str(output),
            }
        )
    )


if __name__ == "__main__":
    main()
