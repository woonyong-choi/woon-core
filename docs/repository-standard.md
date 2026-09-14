# 저장소 표준

모든 Woon 편집 정본 저장소는 같은 제어 표면을 갖는다.

```text
README.md       사람이 읽는 진입점
.woon/
└── repository.yaml  저장소 ID, 정책, 표준과 검증 목록
AGENTS.md       생성된 Codex 호환 지침
CLAUDE.md       생성된 Claude 지침
.github/        생성된 Copilot 지침과 repository workflow
docs/           오래 유지할 아키텍처와 운영 설명
src/            언어·framework 구조에 맞는 실행 코드
tests/          실행 코드의 자동 검증
```

`src/`와 `tests/`는 실행 코드가 있는 저장소에만 둔다. 지식·설정·배포 산출물 저장소에는 억지로 만들지 않는다. domain 폴더는 실제로 필요할 때만 추가하며 빈 framework 폴더와 복사된 정책 문서는 허용하지 않는다.

## 공통 기준

폴더 역할·이름과 도구별 예외는 [repository-layout](../standards/repository-layout.yaml), 필요한 최소 테스트와 완료 임시물 정리는 [code](../standards/code.yaml), 짧은 README는 [documentation](../standards/documentation.yaml)이 소유한다. 새 커밋 메시지는 [Commit](repo://skills/skills/git/commit/SKILL.md)을 따른다. 이 문서에 독립된 컨벤션을 복제하지 않는다.

`woon context check`는 등록된 모든 저장소를 순회하며 이 규칙을 검사한다. 표시용 문서 제목과 frontmatter 값은 폴더 이름이 아니므로 원문의 언어를 유지한다.

## 정본 소유권

- 공통 동작: `repo://core/policies/`, `repo://core/standards/`
- 저장소별 선택: 각 저장소의 `.woon/repository.yaml`
- 머신별 값: Git에서 제외한 `*.local.yaml`
- 생성 지침: compiler 출력이며 직접 편집 금지
- 저장소별 아키텍처: 필요할 때만 local `docs/architecture.md`

## 변경 gate

1. 파일을 rename하거나 제거하기 전에 참조를 검색한다.
2. 정본만 편집한다.
3. 생성 로직을 바꾼 경우에만 결정성을 확인하며 기존의 동일 입력·환경 통과 근거를 재사용한다.
4. `repo://core/standards/code.yaml`에 따라 변경 영향에 맞는 최소 검증을 선택한다. 생성 지침만 대조할 때는 `woon context check <repo-id> --artifacts-only`를 사용하며 경로 감사 통과로 해석하지 않는다.
5. 폴더·경로 변경이 있을 때 `woon context check <repo-id>`로 경로 감사를 포함한다.
6. 최종 diff에서 관련 없는 변경과 하드코딩된 local 값을 확인한다.
