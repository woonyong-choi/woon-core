"""Create a minimal passing writing-review v2 request and result."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


root = Path(sys.argv[1]).resolve()
root.mkdir(parents=True, exist_ok=True)
document = root / "document.md"
source = root / "source.md"
standard = root / "standard.md"
document.write_text("# 설명\n\n입력이 없으면 결과는 0이다.\n", encoding="utf-8")
source.write_text("원문은 입력이 없을 때 결과가 0이라고 설명한다.\n", encoding="utf-8")
standard.write_text("근거와 표현을 분리해 검토한다.\n", encoding="utf-8")

request = {
    "version": 2,
    "profile": "technical-learning",
    "purpose": "짧은 기술 설명을 검증한다.",
    "audience": "학습자",
    "visibility": "local-only",
    "edit_scope": "본문 한 문장",
    "revision_attempt": 0,
    "preserved_elements": ["제목"],
    "completion_conditions": ["별도 검토 통과"],
    "document": {
        "id": "demo-document",
        "path": str(document),
        "sha256": sha256(document),
        "revision": "r1",
    },
    "standard": {
        "path": str(standard),
        "sha256": sha256(standard),
        "revision": "r1",
    },
    "references": [
        {
            "id": "source-1",
            "role": "fact",
            "locator": "demo://source",
            "revision": "r1",
            "path": str(source),
            "sha256": sha256(source),
        }
    ],
    "claims": [
        {
            "id": "claim-1",
            "kind": "fact",
            "core": True,
            "body_anchor": "입력이 없으면 결과는 0이다.",
            "evidence": [
                {
                    "reference_id": "source-1",
                    "anchor": "입력이 없을 때 결과가 0",
                    "relation": "condition-result",
                }
            ],
        }
    ],
    "writer": {
        "provider": "example",
        "model": "writer",
        "tool": "visible-task",
        "run_id": "writer-run-1",
    },
}
request_path = root / "request.json"
write_json(request_path, request)

document_anchor = "입력이 없으면 결과는 0이다."
source_evidence = [
    {
        "reference_id": "source-1",
        "anchor": "입력이 없을 때 결과가 0",
        "relation": "condition-result",
    }
]
criterion_reviews = []
for criterion in (
    "semantic-fidelity",
    "preservation",
    "style",
    "factual-accuracy",
    "relation-accuracy",
    "evidence-boundary",
):
    criterion_reviews.append(
        {
            "id": criterion,
            "status": "pass",
            "reason": "현재 문장과 근거에서 확인했다.",
            "document_anchor": document_anchor,
            "evidence": source_evidence
            if criterion
            in {
                "factual-accuracy",
                "relation-accuracy",
                "evidence-boundary",
            }
            else [],
        }
    )

review = {
    "version": 2,
    "document_id": "demo-document",
    "request_sha256": sha256(request_path),
    "document_sha256": sha256(document),
    "final_document_sha256": sha256(document),
    "standard_sha256": sha256(standard),
    "revision_attempt": 0,
    "reviewer": {
        "provider": "example",
        "model": "reviewer",
        "tool": "visible-task",
        "run_id": "reviewer-run-1",
    },
    "criterion_reviews": criterion_reviews,
    "claim_reviews": [
        {
            "id": "claim-1",
            "status": "pass",
            "reason": "주장과 출처가 일치한다.",
            "document_anchor": document_anchor,
            "evidence": source_evidence,
        }
    ],
    "hard_failures": [],
    "verdict": "passed",
}
write_json(root / "review.json", review)
print(root)
