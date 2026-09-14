# Private audio transcription adapters

The operating procedure is owned by `repo://skills/skills/knowledge/voice-transcription`.
`woon_core.voice_transcription` submits prepared audio manifests; `woon_core.voice_transcript_review`
renders saved diarized responses for review. Neither adapter promotes content into canonical Wiki pages.

## Transcription

```bash
python -m woon_core.voice_transcription --run \
  --plan <private-intake>/plan.json \
  --key-file <private-credential-file> \
  --limit-parts 1 --estimated-budget-usd 1 --workers 1
```

The plan owns the model, exact request settings, source dates, prepared intervals and input hashes.
Completed responses and the request journal remain in the model-specific directory beside the plan.
Re-running skips matching completed responses and refuses unresolved earlier requests.
By default, flagged output stops further scheduling and requires review before resumption.

For an explicitly requested complete, unedited diarization review, the plan may set
`review_only_include_flagged_responses: true`. Passing `--verbatim-review-only` then retains and reuses
flagged responses while completing the requested inputs. This mode requires
`gpt-4o-transcribe-diarize`; flags remain in the journal and do not become quality acceptance.
HTTP failures, invalid response structure, hash mismatches and unresolved requests still stop execution.

## Verbatim review

```bash
python -m woon_core.voice_transcript_review \
  --plan <private-intake>/plan.json \
  --output <private-intake>/speaker-transcript.md
```

The Markdown displays all returned segments in source order using `A :` / `B :` labels, with input
intervals as section headings. Consecutive segments with the same API label within one response share
one display block. It does not rewrite text, remove repetitions or merge speaker identities.
The companion JSON preserves every segment, exact API text, timestamps, source response hashes and
request-local speaker keys. Actual names remain unset until confirmed. Incomplete input coverage is
reported as a completed/prepared count. Identical re-renders are no-ops; changed output requires a new
filename so that an existing review is never silently overwritten.
