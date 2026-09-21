# Evidence-bound writing quality

`woon_core.writing_quality` validates writing without starting a hidden writer or reviewer.
The writer creates a version 2 request, a separate visible task or human creates the review,
and Core checks the current bytes before accepting the result. The existing Wiki
`quality-review-plan` pipeline remains a version 1 Wiki-only acceptance path. It can pass that
existing Wiki gate, but it does not satisfy the version 2 cross-profile contract.

## Review boundary

Run the provider-neutral evaluator after the separate review is complete:

```bash
woon knowledge evaluate-writing-review \
  --request /path/to/writing-review-request.json \
  --review /path/to/writing-review-result.json
```

The request uses `version: 2` and declares:

- `profile`: `translation`, `technical-learning`, `research-report`, `general-guidance`,
  `personal-record`, or `fiction`
- `purpose`, `audience`, `visibility`, `edit_scope`, `preserved_elements`, and
  `completion_conditions`
- `revision_attempt` from 0 to 2; later attempts bind the immediately preceding `prior_request`
  and non-passing `prior_review` artifacts
- current `document` (`id`, `path`, `sha256`, and `revision`) and `standard` (`path`, `sha256`,
  and `revision`) records
- `references` with `id`, `role` (`fact` or `style`), `locator`, `revision`, `path`, and `sha256`
- typed `claims` with `id`, `kind`, `core`, exact `body_anchor`, and source evidence
- optional `applied_rules` (`id` and `version`) plus `policy_path` and `policy_revision`; Core
  requires each exact rule set to be currently adopted for the request profile
- writer `provider`, `model`, `tool`, and `run_id`

The result uses `version: 2`, binds the document ID and request/document/final-document/standard hashes, records
the separate reviewer identity and revision attempt, and supplies exact `criterion_reviews` and
`claim_reviews`. Each item has `status` (`pass`, `fail`, `unknown`, or `not-applicable`), `reason`,
an exact document anchor when applicable, and source evidence entries with `reference_id`, exact
`anchor`, and one of these relations:

Required criterion IDs are exactly `COMMON_CRITERIA | PROFILE_CRITERIA[profile]`. The
[Core constants](../src/woon_core/writing_quality.py) and
[executable demo](examples/writing-quality-v2-demo.py) are the canonical schema references.

`premise-conclusion`, `condition-result`, `coreference`, `cause-change`, `concept-example`,
`code-output-explanation`, or `question-answer`.

A pass cannot use a missing anchor or a style reference as factual evidence. Required claims
cannot be `not-applicable`. A hard failure, failed item, core unknown, stale byte hash, third
revision, or top-level verdict inconsistent with the item results prevents acceptance. Short text
has no length penalty. Fiction uses internal consistency instead of factual-accuracy criteria.
Version 1 reviews are readable only as incomplete legacy results.

A non-core claim may remain `unknown` only with top-level `verdict: qualified`. Core returns
`passed: true`, `state: qualified`, and `qualified: true` while preserving the item in `unknowns`;
consumers must not present that state as an unqualified pass.

Create and evaluate a complete local sample:

```bash
demo=/tmp/woon-writing-quality-v2
.venv/bin/python docs/examples/writing-quality-v2-demo.py "$demo"
.venv/bin/woon knowledge evaluate-writing-review \
  --request "$demo/request.json" \
  --review "$demo/review.json"
```

For a Wiki document, keep the existing `source -> accepted claim -> page spec -> compiled
Markdown -> receipt` ownership chain. Point the v2 request at the compiled Markdown and accepted
source files after the producer has written them. A separate visible reviewer creates the result;
the v2 pass accompanies the compiler receipt and does not replace it or authorize editing generated
Markdown directly.

Writer/reviewer run IDs and model names are declared metadata. Core rejects an identical run ID
and reports whether the declared provider/model strings differ, but it does not claim statistical
independence or prove that two external calls occurred. Set `reviewer.run_id` to the actual reviewer
task/run ID; never copy an example or another task's ID. A requested model name is declared metadata,
not runtime attestation; record attestation separately only when it was observed.

## Learned-rule gate and recovery

```bash
woon knowledge evaluate-writing-rule --candidate /path/to/rule-candidate.json

woon knowledge adopt-writing-rule \
  --policy /path/to/writing-policy.json \
  --candidate /path/to/rule-candidate.json \
  --expected-policy-sha256 <current-policy-sha256>

woon knowledge register-writing-rule-application \
  --policy /path/to/writing-policy.json \
  --request /path/to/writing-review-request.json \
  --expected-policy-sha256 <current-policy-sha256>

woon knowledge rollback-writing-rule \
  --policy /path/to/writing-policy.json \
  --rule-id <rule-id> \
  --expected-policy-sha256 <current-policy-sha256>
```

A rule records its ID/version, instruction, source observations, profiles, applicability,
exclusions, expected effect, counterexamples, state, evaluation revision, and
`invariant_effect: preserve-only`. Each profile must independently have at least 20 unique real
cases, at least three sources or tasks, at least 12 wins, only ties otherwise, no loss/unknown,
no leakage, no hard failure/core unknown/disagreement/preference conflict, and all fixed controls
passing. Baseline and candidate bind the same provider/model/tool/input while their input,
outputs, source, and review artifacts are checked by path and SHA-256. Exact relation anchors must
exist in the candidate and source bytes. Synthetic fixtures cannot be adopted.

Each review artifact is JSON that binds its case/profile, source/task IDs,
input/baseline/candidate/source hashes, outcome, fixed controls, failure and disagreement flags,
evidence, reason, and reviewer identity. Baseline, candidate, and reviewer run IDs must be distinct.
Core reads the decision from that artifact and rejects conflicting case metadata; an arbitrary
non-empty review file is not a valid label.

Adoption, application registration, and rollback lock the policy file, compare the expected policy
hash inside the lock, write atomically, and retain the previous rule. A request binds the adopted
rule version and profile; registration records that request, document ID, and document revision. Rollback returns
both initially declared and subsequently registered affected document IDs. Core can verify
declared artifact bytes, hashes, source-unit IDs, case separation, and review provenance. It cannot
prove semantic independence, honest human labeling, or held-out first exposure from metadata
alone; those remain reviewer and workflow evidence requirements.
