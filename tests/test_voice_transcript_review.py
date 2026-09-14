import json

import pytest

from woon_core import voice_transcript_review as review
from woon_core import voice_transcription as vt


def test_every_utterance_and_request_local_speaker_survives(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"fixture")
    part = {
        "path": "audio.wav",
        "duration_seconds": 120,
        "start_seconds": 0,
        "end_seconds": 120,
        "sha256": vt.digest(audio),
    }
    plan = {
        "publication_state": "private",
        "model": vt.MODEL,
        "request_settings": vt.SETTINGS[vt.MODEL],
        "jobs": [
            {
                "observation_id": "recording",
                "diary_date": "2026-05-16",
                "recording_started_at_kst": "2026-05-16T01:21:27+09:00",
                "parts": [part, {**part, "start_seconds": 118, "end_seconds": 238}],
            }
        ],
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    output = tmp_path / vt.OUTPUTS[vt.MODEL]
    output.mkdir()
    texts = [" 어... 어...", "맞아. 맞아.", "〔잘 모름〕", "잡담도 남겨.\n응."]
    for index in range(2):
        envelope = {
            "model": vt.MODEL,
            "input_sha256": part["sha256"],
            "request_id": f"request-{index}",
            "provider_response": {
                "text": "".join(texts),
                "segments": [
                    {"id": f"seg_{i}", "speaker": "A", "start": i, "end": i + 1, "text": text}
                    for i, text in enumerate(texts)
                ],
            },
        }
        (output / f"recording-{index:03}.json").write_text(json.dumps(envelope))
    markdown, ledger = review.review(plan_path)
    assert [s["text"] for s in ledger["segments"]] == texts * 2
    assert len(ledger["speaker_mapping"]) == 2
    assert all(value["name"] is None for value in ledger["speaker_mapping"].values())
    assert ledger["segments"][4]["start_seconds"] == 118
    assert all(markdown.count(text) == 2 for text in texts)
    assert review.review(plan_path) == (markdown, ledger)


def test_review_never_overwrites_user_changes(tmp_path):
    path = tmp_path / "review.md"
    review.save_unchanged_or_new(path, "original")
    review.save_unchanged_or_new(path, "original")
    with pytest.raises(ValueError, match="Existing artifact differs"):
        review.save_unchanged_or_new(path, "changed")
    assert path.read_text() == "original"
