import io
import json
import urllib.error
import urllib.request

import pytest

from woon_core import voice_transcription as vt


def part(tmp_path):
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"test-audio")
    return {
        "id": "recording-000",
        "source_id": "recording",
        "input": audio,
        "sha256": vt.digest(audio),
        "estimated_usd": 0.01,
        "start_seconds": 1200,
        "diary_date": "2026-05-16",
        "recording_started_at_kst": "2026-05-16T01:00:00+09:00",
    }


def response():
    return {"text": "Hello", "segments": [{"start": 0, "end": 1, "speaker": "A", "text": "Hello"}]}


def test_completed_response_is_reused_after_missing_completion_receipt(tmp_path, monkeypatch):
    item = part(tmp_path)
    monkeypatch.setattr(vt, "request_json", lambda request: (response(), "request-id"))
    vt.transcribe_part(item, {"response_format": "diarized_json"}, "sk-test-secret", tmp_path)
    journal = tmp_path / "requests.jsonl"
    journal.write_text(journal.read_text().splitlines()[0] + "\n")
    assert vt.unfinished([item], tmp_path) == []
    saved = json.loads((tmp_path / "recording-000.json").read_text())
    assert saved["diary_date"] == "2026-05-16" and saved["start_seconds"] == 1200
    assert "sk-test-secret" not in journal.read_text()


def test_uncertain_request_is_not_sent_twice(tmp_path, monkeypatch):
    item = part(tmp_path)

    def fail(request):
        raise vt.TranscriptionError("Uncertain outcome")

    monkeypatch.setattr(vt, "request_json", fail)
    with pytest.raises(vt.TranscriptionError):
        vt.transcribe_part(item, {}, "sk-test-secret", tmp_path)
    with pytest.raises(vt.TranscriptionError, match="not automatic"):
        vt.unfinished([item], tmp_path)


def test_changed_input_cannot_reuse_old_result(tmp_path):
    item = part(tmp_path)
    vt.save_new_json(
        tmp_path / "recording-000.json",
        {"input_sha256": "different", "model": vt.MODEL, "provider_response": response()},
    )
    with pytest.raises(vt.TranscriptionError, match="does not match"):
        vt.unfinished([item], tmp_path)


def test_invalid_diarization_does_not_count_as_complete():
    with pytest.raises(vt.TranscriptionError):
        vt.validate_response({"text": "unattributed", "segments": []})
    with pytest.raises(vt.TranscriptionError):
        vt.validate_response({"segments": [{"text": "x", "speaker": "A", "start": 2, "end": 1}]})


def test_credentials_require_private_permissions(tmp_path):
    path = tmp_path / "key"
    path.write_text("sk-test-secret")
    path.chmod(0o644)
    with pytest.raises(vt.TranscriptionError, match="0600"):
        vt.read_key(path)
    path.chmod(0o600)
    assert vt.read_key(path) == "sk-test-secret"


def test_provider_error_does_not_echo_credential(monkeypatch):
    body = json.dumps({"error": {"code": "invalid_api_key", "message": "sk-test-secret"}})

    def deny(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", {}, io.BytesIO(body.encode())
        )

    monkeypatch.setattr(urllib.request, "urlopen", deny)
    with pytest.raises(vt.TranscriptionError) as error:
        vt.check_key("sk-test-secret")
    assert "invalid_api_key" in str(error.value)
    assert "sk-test-secret" not in str(error.value)


@pytest.mark.parametrize("model", sorted(vt.TEXT_MODELS))
def test_plain_transcript_is_separate_and_does_not_invent_speakers(tmp_path, monkeypatch, model):
    item = part(tmp_path)
    plain = {"text": "A complete transcript.", "languages": [{"code": "ko"}]}
    monkeypatch.setattr(vt, "request_json", lambda request: (plain, "plain-request"))
    vt.transcribe_part(item, vt.SETTINGS[model], "sk-test-secret", tmp_path, model)
    assert vt.unfinished([item], tmp_path, model) == []
    saved = json.loads((tmp_path / "recording-000.json").read_text())
    assert saved["provider_response"] == plain
    with pytest.raises(vt.TranscriptionError):
        vt.unfinished([item], tmp_path, vt.MODEL)


def test_truncated_saved_text_cannot_be_silently_reused(tmp_path):
    item = part(tmp_path)
    vt.save_new_json(
        tmp_path / "recording-000.json",
        {
            "input_sha256": item["sha256"],
            "model": vt.CLASSIC_MODEL,
            "provider_response": {"text": "잘린 문장", "usage": {"output_tokens": 2048}},
        },
    )
    with pytest.raises(vt.TranscriptionError, match="quality review"):
        vt.unfinished([item], tmp_path, vt.CLASSIC_MODEL)


def test_repeated_hallucination_is_flagged_but_normal_repetition_is_not():
    assert vt.quality_flags({"text": "비싼 거 아니야? " * 30}, vt.CLASSIC_MODEL)
    assert not vt.quality_flags({"text": "맞아요. 맞아요. 산책했어요."}, vt.CLASSIC_MODEL)


def test_explicit_verbatim_review_retains_flagged_response_without_resubmission(tmp_path):
    item = part(tmp_path)
    raw = response()
    raw["text"] = "반복 문장입니다. " * 30
    vt.save_new_json(
        tmp_path / "recording-000.json",
        {
            "input_sha256": item["sha256"],
            "model": vt.MODEL,
            "provider_response": raw,
        },
    )
    with pytest.raises(vt.TranscriptionError, match="quality review"):
        vt.unfinished([item], tmp_path)
    assert vt.unfinished([item], tmp_path, review_only=True) == []
    assert vt.quality_flags(raw, vt.MODEL) == ["repetitive-output-needs-review"]
