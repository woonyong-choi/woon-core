## Woon 전역 작업 계약

이 계약은 질문·답변·조사·진단·설계와 코드·문서·설정 변경에 항상 적용한다.

### 기준과 우선순위

1. 사용자의 현재 지시와 더 가까운 `AGENTS.md`를 먼저 따른다. Woon 저장소에서는 `.woon/repository.yaml`도 읽는다.
2. 절차의 단일 원본은 `repo://skills/catalog.json`과 그 catalog가 가리키는 `SKILL.md`다.
3. 같은 용도의 절차는 Woon 정본 skill, 현재 활성화된 설치 skill, provider 기본 skill 순으로 선택한다. 특정 도구가 요구하는 필수 skill은 함께 적용한다.
4. skill의 절차는 현재 사용자 지시와 실행 환경의 권한·도구 제약 안에서 적용한다. 이미 확인한 입력·결정·구체적 승인은 재사용하고, 실제 외부 호출·credential 접근이 없는 가역적 준비를 미래 단계의 승인 조건으로 막지 않는다. 승인되지 않은 외부 효과는 실행하지 않는다.
5. skill 때문에 중단하거나 추가 승인이 필요하면 해당 SKILL.md와 정확한 조항, 현재 작업에 적용되는 이유를 밝힌다. 검토 가능한 준비·검증을 먼저 완료하고 아직 허용되지 않은 행동만 확인한다.

Codex의 모든 코드 작업은 `ponytail@ponytail` full을 필수 적용한다. 자동 로드·검증·Woon 경계는 `repo://skills/skills/common/quality/references/ponytail.md`를 따른다.

### Skill 자동 선택

1. 파일 변경, 조사, 다단계 분석, 전문 문서 작성 또는 위험한 작업을 시작하기 전에 `woon resolve repo://skills/catalog.json`으로 catalog를 찾는다.
2. 요청에서 대상·행동·품질 관심사를 분리하고 각각의 핵심 명사와 원문 표기로 `name`·`description`을 찾는다. 한 검색어만으로 결론 내리지 않는다.
3. 일치하는 최소 집합을 고른다. 보통 1개, 독립 경계가 있을 때만 최대 3개를 사용한다.
4. 선택한 항목의 `path` 아래 `SKILL.md` 전체를 작업 전에 읽는다. 그 문서가 다른 skill을 함께 적용하라고 명시하면 최소 집합에 포함한다. 문서가 지시한 reference만 필요한 시점에 읽고, catalog 전체나 무관한 skill 본문을 대화 문맥에 복사하지 않는다.
5. 이미 같은 Woon 정본 skill이 활성화되어 있으면 다시 탐색하지 않는다. Woon에 맞는 skill이 없을 때만 설치된 fallback을 사용하며, 관련 없는 skill을 억지로 호출하지 않는다.
6. 선택한 skill과 이유를 작업 시작 commentary에 한 줄로 알린다. skill이 행동·중단·승인 경계를 바꾸면 그 사실도 알린다.

### Skill 자동 복구

1. 선택한 Woon 정본 skill이 현재 Codex 설치 목록에 없거나 설치본이 오래되었어도 작업을 막거나 사용자에게 설치를 떠넘기지 않는다. 이번 작업에서는 catalog가 가리키는 정본 `SKILL.md`를 직접 읽어 적용한다.
2. Woon CLI와 workspace를 사용할 수 있으면 `woon skills plan --profile personal --target codex`로 현재 설치를 확인한다. 결과가 `install`, `update`, `repair`, `unchanged`뿐이면 `woon skills install --profile personal --target codex`를 실행하고 같은 plan이 모두 `unchanged`인지 재확인한다.
3. plan에 `blocked`, `retire`, `forget`이 있으면 관리하지 않는 설치본을 덮어쓰거나 다른 skill을 자동 퇴역시키지 않는다. 정본을 직접 사용해 현재 작업은 계속하고 충돌 대상만 보고한다.
4. Git에는 머신 절대경로 대신 `repo://` 참조와 profile만 남긴다. Woon CLI, workspace 또는 registry가 전혀 없는 새 환경에서는 설치를 가장하지 말고 최초 bootstrap이 필요하다고 밝힌다.

### 작업 위임과 사용자에게 보이는 진행

- 사용자가 정한 상시·필수 고정 목록을 유지한다. 실행 중 임시·재위임 Codex 작업은 추가 고정 대상이며, 공식 adapter로 고정하고 재조회한 뒤 실행한다. 완료·인계 뒤 archive하고 고정 해제도 확인한다.
- 사이드바에 표시할 수 없는 숨은 내부 subagent를 새로 실행하지 않는다. 이미 실행 중이면 파일 쓰기의 안전한 완료점에서 보이는 부모 담당에 결과를 인계하고 정지한다. 가시성을 위한 새 사용자 작업이나 복제 wrapper를 임의로 만들지 않는다.
- 이 규칙은 담당자가 다시 위임하는 작업에도 전달한다. 사용자 채팅의 현황 보고가 내부 상태 파일 갱신보다 우선이며, 파일 기록만 남기고 사용자 보고를 생략하지 않는다.

### 모든 작업의 불변조건

- 확인한 사실, 추론, 제안, 실행하지 못한 검증을 구분한다.
- 변경 전에 실제 파일·상태·기존 규칙을 확인하고 사용자의 동시 변경을 보존한다.
- 성공 조건과 검증부터 정한다. 최소 검사·근거 재사용·완료 임시물 정리는 `repo://core/standards/code.yaml`을 따른다.
- 폴더·README는 `repo://core/docs/repository-standard.md`의 정본 링크를, 새 영어 커밋은 `repo://skills/skills/git/commit/SKILL.md`를 따른다.
- 삭제·덮어쓰기·배포·외부 전송·권한 변경은 Woon `safety` 경계를 우선 적용한다.
- 승인된 외부 앱·계정·운영 변경은 공식 MCP/API/CLI 또는 공식 UI로 수행한다. 전용 adapter 부재만으로 멈추지 않는다. 대상·결과를 기록하고 재조회하며 비공개 DB 조작이나 인증·보안 통제 우회는 하지 않는다.
- 정본과 생성물을 구분하고, 같은 규칙·문서·skill의 독립 복제본을 만들지 않는다.
- 결과를 먼저 말하고 근거를 연결한다. 검증 전에는 완료나 품질 보장을 주장하지 않는다.

짧은 작성·번역 교정도 `repo://skills/standards/writing-quality.md`의 별도 검토를 적용한다. 단순 계산·사실 단답만 catalog 생략한다.
