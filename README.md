# woon-core

경로에 종속되지 않는 Python 기반 Woon 제어 도구다. 저장소와 AI 지침을 관리하고, private Markdown 정본을 MCP로 검색·갱신·복구한다.

실제 운영 대상은 macOS다. Obsidian 로컬 자동화와 POSIX 파일 권한
계약은 macOS에서 검증하며, Linux는 전체 회귀 테스트를 위한 동일 POSIX 환경으로
사용한다. Windows는 패키지 설치·정적 검사·timezone 데이터·빌드 호환성까지만
지원하고, POSIX 권한 동작은 지원 대상으로 주장하지 않는다.

## 주요 기능

- 충돌을 허용하지 않는 workspace root 탐색
- 저장소 ID와 `repo://` URI 기반 경로 해석
- 공통 정책·코드·문서·폴더 표준의 단일 정본 관리
- Codex·Claude·Copilot 지침의 결정적 생성과 drift 검사
- 운영 파일의 개인 절대경로와 토큰 예산 검사
- 한 개념당 Markdown 정본 한 편과 optimistic revision 검사
- 외부 corpus의 content-addressed catalog, 파일별 병합, resume-safe ledger
- Docling 기반 local-only 문서 변환, deterministic 정제와 terminal resolution receipt
- source·claim·page spec에서 receipt가 있는 LLM Wiki를 결정론적으로 컴파일
- 교체 가능한 document, search, history port와 local stdio MCP
- 검토한 Codex root·하위 작업의 공식 영구 삭제, 개별 응답·알림과 전체 목록 재조회 receipt

Codex 작업 삭제는 `python -m woon_core.environment.codex_thread_review plan`으로 준비하고
`python -m woon_core.environment.codex_thread_delete`에서 exact plan/review hash와 전체 내용
검토, 보호 ID, 최신 실제 Desktop 근거를 확인한다. 기본은 preview이며 명시적 `--apply`만
공식 삭제를 호출한다. [입력 schema와 실행 절차](repo://skills/skills/common/safety/references/codex-thread-delete.md)를
따르며, 부분 실패나 불확실한 삭제는 자동 재시도하지 않는다.

## 설치

Python 3.12 이상과 `uv`를 사용한다. GitHub 저장소에서 CLI와 MCP를 설치한다.

```bash
uv tool install git+https://github.com/woonyong-kr/woon-core.git
```

개발 checkout에서는 `uv sync --all-extras --dev`를 사용한다.

## 사용법

```bash
woon init --root /path/to/woon
woon doctor
woon repo sync
woon context generate --all
woon context check --all
woon skills validate --profile core
woon skills plan --profile core,python --target codex
woon skills eval-routing --executor all --repeat 3
woon knowledge compile-audit --vault /path/to/woon-knowledge
woon knowledge compile --vault /path/to/woon-knowledge
woon knowledge book-intake-audit --manifest official-books --vault /path/to/woon-knowledge
woon knowledge book-coverage-audit --vault /path/to/woon-knowledge
woon knowledge book-promote --input /path/to/verified-book-pages.json --vault /path/to/woon-knowledge
woon knowledge book-promote-retire --input /path/to/atomic-book-update.json --vault /path/to/woon-knowledge
woon knowledge book-rights-demote --input /path/to/book-rights-demotion.json --vault /path/to/woon-knowledge
woon knowledge apply-compiled-transaction --input /path/to/catalog-transaction.json --vault /path/to/woon-knowledge
woon knowledge index --vault /path/to/woon-knowledge
woon knowledge search '검색어' --vault /path/to/woon-knowledge
woon knowledge configure-navigation --vault /path/to/woon-knowledge --graph-colors
woon knowledge learning-checkpoint --canonical-id personal/topic --unit '현재 범위' \
  --status partial --evidence '실제 실행 근거' --unstable '남은 오류' \
  --next-question '다음에 자료 없이 답할 질문' --recorded-on 2026-08-29 \
  --expected-revision <현재-revision> --vault /path/to/woon-knowledge
woon knowledge source-plan --source /path/to/source --source-name source --vault /path/to/woon-knowledge
woon knowledge source-audit --source /path/to/source --source-name source --vault /path/to/woon-knowledge
woon knowledge document-intake --source /path/to/document.docx --vault /path/to/woon-knowledge
woon knowledge document-resolve --decision /path/to/decision.json --vault /path/to/woon-knowledge
woon knowledge document-audit --vault /path/to/woon-knowledge
woon career create --id company-role-2026 --company Company --role Role --jd /path/to/jd.pdf --vault /path/to/woon-knowledge
woon career analyze --id company-role-2026 --vault /path/to/woon-knowledge
woon career context --id company-role-2026 --vault /path/to/woon-knowledge
```

검토한 private PDF·HTML의 파일명과 활성 연결을 함께 바꿀 때는
`KnowledgeService.apply_wiki_restructure_transaction`의 선택 인자
`resource_renames`와 `resource_reference_writes`를 사용한다.
`ResourceFileRename`은 기존·대상 상대 경로와 원본 SHA-256을 받으며 같은 폴더·확장자와
원문 bytes·권한을 보존한다. 대상 파일이 이미 있거나 검토한 입력이 바뀌면 거절한다.
`resource_reference_writes`는 기존 `ManualWikiWrite` 형식으로 `catalog/sources/`의
정확한 locator 치환만 받는다. compiler 본문은 기존 compiler transaction,
native 연결은 기존 manual write로 함께 반영한다. 과거 provenance·receipt는 수정하지
않으며 실패하면 기존 transaction과 함께 복구하고, 도중의 외부 파일 변경은 보존해 보고한다.
기존 curated 본문을 후속 source로 바꾸는 경우에는 compiler transaction의
`curated_successor_page_ids`에 해당 page ID를 명시하고 catalog revision과 page spec hash를
고정한다. 해당 페이지가 이전 source·claim을 단독 소유할 때만 기존 curation 절차로
상태와 `superseded_by`를 갱신한다. 이전 본문·ID와 다른 의존성은 보존한다.

Graph 분야색을 탐색 순서와 무관하게 유지하려면 Vault의
`config/obsidian-navigation.json` 안 `graph.category_colors`에 현재 선언된 분야의
정확한 `canonical_id`를 키로, RGB 정수 `0..16777215`를 값으로 기록한다.
기존 `--graph-colors` plan의 legend는 category 경로와 canonical ID, RGB를 함께 제공한다.
명시한 값은 기본 palette보다 우선하며 하위 문서는 실제 parent로 그 색을 상속한다.
없는 ID·중복 ID·현재 category가 아닌 ID와 정수가 아닌 값은 적용 전에 차단한다.
하위 분야로 옮긴 항목은 별도 색 선언에서 빼고 부모색을 상속하게 한다.
비공개·책·원자료 분류 우선순위는 유지하며 `category_colors` 자체는 `graph.json`에 쓰지 않는다.
실제 적용은 기존 `--graph-colors --apply`의 colorGroups 전용 backup·receipt·재조회 경로를 따른다.

Runnable 원격 실행만 먼저 차단하려면 다음 설정 adapter를 사용한다. 변경은
`remoteExecutionEnabled: false` 한 필드이며 기존 legacy/path·local·pairing 설정을 보존한다.

```bash
woon knowledge obsidian-plugin disable-runnable-remote-execution --vault /path/to/vault
woon knowledge obsidian-plugin disable-runnable-remote-execution --vault /path/to/vault \
  --apply --expected-settings-sha256 <preview-before-sha256>
```

이 경로는 backup·동시 변경 검사·rollback·receipt·disk 재조회를 수행하며 앱과 runner는
호출하지 않는다. 이미 차단된 설정의 bytes도 그대로 보존한다. 실행 중인 plugin의 정책은
설정 receipt만으로 확인되지 않는다. 설치·화면 담당이 정본
[runtime 반영 절차](repo://skills/skills/knowledge/obsidian-plugin/references/runnable-remote-policy.md)의
공식 CLI `loadSettings()`와 별도 읽기 전용 재조회를 수행해 runtime receipt를 연결한다.
companion 배포·pairing·책 코드 실행은 별도 범위다.

승인된 로컬 연결에는 `start-runnable-companion`과 `pair-runnable-companion`을 사용한다.
관리 프로세스 교체는 `stop-runnable-companion --pid <exact-pid> --docker-cli /path/to/docker`로
기존 시작 receipt와 요청·컨테이너 부재를 확인한 뒤 종료한다. 설정·token·기존 state를 보존하며
새 artifact 정책을 고정한 뒤 start한다. 담당자는 교체 동안 새 Run을 보류한다.
각 action은 preview의 `expected_state`로 apply를 고정한다. 시작은 등록 release hash의
artifact와 이미 설치된 Node·로컬 Docker·image를 확인하고 17171 관리 프로세스만 다룬다.
pairing은 공식 Obsidian CLI와 공개 SecretStorage API를 사용하며 remote=false와 기존
다른 설정을 보존한다. `config/runnable-companion.yaml`의 `release`·`artifact_sha256`은
companion을 고정하고, 선택적인 `approved_plugin_versions`는 pairing을 허용할 정확한
plugin 버전 목록이다. 필드가 없는 기존 정책은 `release` 하나만 허용한다. 실제 설치된
허용 버전을 로드된 plugin과 정확히 비교하며 companion hash·기존 token은 유지한다.
`runnable-companion-status`는 소유한 프로세스의 authenticated
capabilities만 재조회하며 소스 실행·image pull·public gateway 변경을 하지 않는다.
실행 명령과 실패 복구·receipt 경계는 정본
[관리 companion 연결](repo://skills/skills/knowledge/obsidian-plugin/references/runnable-companion.md)을
따른다. 성공한 연결은 실제 sample 실행이나 화면 반영의 증거와 구분한다.
시작 CLI는 Volta 등의 shim을 실제 Node executable로 해석해 경로와 hash를 고정한다.
시작 실패에는 attempt와 남은 listener를 기록한다. 관리 state 없이 orphan이 남으면
`recover-runnable-companion-orphan --pid <exact-pid>`의 preview/apply로 검토한 PID만
종료하고 process·port를 재조회한다. 기존 config·settings와 다른 서비스를 변경하지 않는다.

Graph 설정을 적용한 뒤 실행 중인 Vault 창을 reload해야 할 때는 기존 navigation
adapter의 별도 모드를 사용한다. 이미 열린 대상 Vault의 화면을 담당하는 작업만 실행하며,
편집 중에는 실행하지 않는다. [Obsidian CLI](https://help.obsidian.md/cli)는 앱이 닫혀 있으면
앱을 시작하므로 준비 조회도 화면 담당의 범위에서 수행한다.

```bash
woon knowledge configure-navigation --vault /path/to/vault --runtime-reload \
  --obsidian-cli /absolute/path/to/obsidian-cli --vault-name 'Vault name'
# 준비 결과의 expected_state를 사용한다. 설정 적용과 reload는 별도 호출이다.
woon knowledge configure-navigation --vault /path/to/vault --runtime-reload \
  --obsidian-cli /absolute/path/to/obsidian-cli --vault-name 'Vault name' \
  --apply --expected-state <준비-결과의-digest>
```

adapter는 `vault=...`를 CLI 명령 앞에 놓고 공개 API로 Vault 이름과 실제 경로,
탭 배치·활성 문서·편집 내용, Graph 설정과 기존 receipt를 대조한다. 저장되지 않은
Markdown은 자동 저장하지 않으며, 준비 이후 상태가 달라지면 reload를 실행하지 않는다.
공개 `WorkspaceLeaf.isDeferred`와 view state가 확인된 지연 로딩 읽기 탭은 같은 파일·읽기
모드·안정된 디스크 내용으로 대조한다. 지연 로딩된 편집 모드, 확인 불가능한 버퍼와
미저장 내용은 계속 차단하며 검증을 위해 탭을 강제로 로딩하지 않는다.
지원되지 않는 편집 상태도 원인을 반환한다. 공개 layout 저장 후 reload를 한 번 실행하고
새 창의 `performance.timeOrigin`과 문서/layout 준비 상태를 확인하고, 1초 간격의 두
관측에서 같은 탭·활성 문서·설정을 확인한다. 옛 창의 응답은 reload 증거로 인정하지 않는다.
창 재연결 조회만 최대 3회 수행하며 준비되지 않은 eval JSON도 이 범위에서 재조회한다.
reload 자체는 재시도하지 않는다. Homepage 등 시작
플러그인이 탭을 교체하면 보존 실패로 반환하며 설정이나 탭을 임의로 복원하지 않는다.
이 검증은 관측 구간 이후의 플러그인 동작까지 보증하지 않는다.
성공한 기계 검증은 기존 settings-receipts의 `navigation-runtime.json`에 유지한다.
별도 `navigation-runtime-attempt.json`에는 최신 시도의 준비·layout 저장·reload 요청·CLI
반환·새 window epoch·postcheck 단계를 남긴다. 실패하면 그 단계와 확인된 epoch를 기록하고
이전 성공 기록은 보존한다. CLI 반환만으로 실제 reload 완료를 단정하지 않으며,
새 창을 확인한 뒤 발생한 오류를 reload 전 차단으로 보고하지 않는다.
이는 `pending-visual-verification` 상태의 기계 검증 기록이며 실제 Graph 화면 확인을
대신하지 않는다. 공개 view state에서 색상을 읽지 못하면 `public-view-state-unavailable`로
표시한다. 플러그인 설치 receipt로 native Graph 반영을 판정하지 않는다.
현재 계약은 `runtime_contract_version: 3`이다. 단계 기록과 deferred 읽기 탭 판정을 추가했으며,
서로 다른 전후 window epoch가 없는 과거 기록은 새 창에서 탭 보존을 검증한 근거로 사용하지 않는다.

reload 요청 후 재연결 확인이 실패했다면, 화면 담당은 실패한
`navigation-runtime-attempt.json`의 SHA-256을 확인하고 읽기 전용 재연결을 실행할 수 있다.

```bash
woon knowledge configure-navigation --vault /path/to/vault --runtime-reconnect \
  --obsidian-cli /absolute/path/to/obsidian-cli --vault-name 'Vault name' \
  --expected-attempt <실패한-attempt-파일의-sha256>
```

이 모드는 eval 조회만 수행하며 layout 저장·reload·설정 및 receipt 쓰기는 하지 않는다.
새 window epoch와 1초 간격의 두 관측에서 현재 session·설정이 유지되는지 확인한다.
새 시도 기록에 보존된 baseline이 있으면 reload 전 session·설정 receipt까지 대조한다.
baseline이 없는 기존 v3 실패 기록은 `observed-only`와
`pre_reload_preservation: unverified-missing-baseline`을 반환한다. 이를 과거 reload의
보존 검증 성공으로 승격하지 않는다. 결과는 계속 실제 Graph 화면 확인과 구분하며,
`--apply`·`--runtime-reload`·`--graph-colors`와 함께 호출할 수 없다.

`apply-compiled-transaction`은 catalog·page spec·문서 revision이 고정된 단일 기존 페이지의
`source-body` 갱신에서 현재 receipt와 목차 블록을 재사용한다. 제목·identity·경로·부모·공개
범위·navigation metadata와 본문의 wikilink가 같아야 하며 summary·updated만 함께 바꿀 수
있다. 독립 reader를 여는 일반 Markdown 링크는 호출자가 Vault 내부의 실제 파일과 private
경계를 확인한다. 무관한 기존 하위 트리를 다시 쓰지 않으며 compiler 검증·audit 차이 검사·
원자적 rollback은 유지한다. 구조 변경, 여러 페이지 변경, 퇴역, 명시적 전체 tree refresh에는
기존 트리 검증이 계속 적용된다. 이 경로의 통과는 기존 하위 트리 오류가 해결됐다는 뜻이 아니다.
기존 native 입구로 navigation-only hub를 병합할 때는 같은 mixed transaction의
`ManualWikiWrite`가 동일 canonical ID와 전후 hash를 증명해야 한다. native 소유권을
유지하고, 퇴역 영수증은 당시 쓰기 hash와 보존한 원자료·claim hash를 기록한다.
후속 감사는 native ID의 유일한 정본과 보존 원자료를 확인하므로 문서의 정상적인 이동·편집은
허용하되 대상의 소실·퇴역·compiler 소유권 전환은 자동으로 승인하지 않는다.
기존 `toc-only` 입구도 같은 조건에서 단일 일반 Markdown 링크를 추가할 수 있다. 비공개
book 입구는 H2와 불릿 Markdown 링크로만 구성한 목차도 허용한다. 모든 대상은 Vault의
`private/` 아래 실제 Markdown 파일이어야 하며 절대경로·anchor·symlink는 거부한다.
이 예외로 책 본문을 승격하지 않는다. 책 tree 검증도 같은 target 검증을 사용한다. 기존 목차의
유무와 관계없이 부모·identity·private 경계 검증은 유지하며 source coverage로 계산하지 않는다.

`lifecycle_status`의 확인된 종료 상태(`completed`, `cancelled`, `archived`)와 종료 날짜는
독립적이다. 날짜를 모르면 `ended_on`·`occurred_on`을 생략하거나 null로 두며, 화면에는
`종료됨(종료일 미상)`을 표시한다. 시험일·문서 수정일을 종료일로 추정하지 않는다.
열린 상태의 `ended_on`, 역전된 날짜, `occurred_on`과 기간의 혼용은 계속 거부한다.

`book-promote`와 `book-promote-retire` 입력은 현재 `payload_schema_version`과
hash-pinned `book_contract`, 명시적 `coverage_manifest.mode`를 반드시 포함한다. 전권이
schema v2로 검증된 경우에만 `replace`를 사용한다. 한 장만 검증됐고 기존 전권 manifest의
나머지 장이 아직 legacy·pending이면 `merge-scope`를 사용한다. 이 모드는 전권 manifest의
경로와 SHA-256을 고정한 채 `catalog/book-coverage-scopes/<book>/<scope>.json`만 원자적으로
생성·갱신한다. 전권 파일은 byte-for-byte 유지되고, scoped audit 0건만 해당 장의 완료
근거가 된다. 장이 별도 페이지 없이 책의 navigation-group-heading으로 표현된 경우에도,
고정된 전권 구조 증거가 해당 장의 자식 전체와 원문 순서를 유일하게 입증하면 같은 scope를
사용할 수 있다. 일부 자식만 포함하거나 다른 장의 자식을 섞은 scope는 거부한다. 전권 audit는
나머지 장을 `pending_books`로 계속 보고하며 책 전체 완료로
승격하지 않는다. `book-promote-retire`에서 `apply: false`는 revision·hash·scope 경계를
검사하는 read-only preflight이고, 동일 payload를 `apply: true`로 바꿔야 실제 writer가
compiler·tree·scope fragment·검색 index를 하나의 rollback 경계에서 갱신한다.
승인된 전체 본문이 기존 curated revision을 대체하면, writer는 URL 인코딩을 해석한
동일 페이지 정체성과 실제 근거를 확인해 과거 source·claim을 후속 verified revision으로
연결한다. 과거 본문은 보존하며, 다른 페이지나 남은 accepted claim이 공유하는 근거는
활성 상태로 유지한다. 누락된 근거를 만들어 넣거나 출처 정합성 검사를 생략하지 않는다.
`book-promote`와 `book-promote-retire`는 같은 optional `staged_assets` 계약을 사용한다.
이미 존재하는 archive 경로는 staged SHA-256과 현재 bytes가 동일할 때만 idempotent하게
재사용하며, 서로 다른 bytes로의 암묵적 덮어쓰기는 거부한다. 새 asset을 설치한 뒤 후속
검증이 실패하면 transaction snapshot에서 원래 asset 상태까지 복원한다.
퇴역 page의 기존 도판을 새 archive 경로로 옮겨야 할 때만
`retirement_image_replacements`에 page별 `old_target: new_target`을 명시한다. 이 예외는
현재·대체 coverage inventory와 실제 archive SHA-256이 모두 일치하고, 기존 본문에서
정확히 한 번 나타나는 Markdown image target만 바뀔 때 허용된다. 일반 본문 차이는 계속
거부한다.

```json
{
  "mode": "merge-scope",
  "relative_path": "catalog/book-coverage-scopes/book-slug/chapter-02.json",
  "expected_sha256": null,
  "base_relative_path": "catalog/book-coverage/book-slug.json",
  "base_expected_sha256": "<current-full-manifest-sha256>",
  "scope_root_id": "books/book-slug/chapter-02",
  "replacement": {
    "schema_version": 2,
    "book_id": "books/book-slug",
    "coverage_scope": {
      "root_id": "books/book-slug/chapter-02",
      "base_relative_path": "catalog/book-coverage/book-slug.json",
      "base_sha256": "<current-full-manifest-sha256>"
    }
  }
}
```

schema v5 이하 payload와 `coverage_manifest.mode`가 없는 implicit full replacement는
fail-closed한다. 기존 payload를 hash만 바꿔 재사용하지 말고 현재 contract로 다시 생성한다.
현재 book contract는 원문의 claim·example·caution·figure·code를 semantic unit으로
전수 inventory하고 stable locator·source hash·exact-one leaf assignment를 요구한다. non-code는
실제 reader body의 unique exact span 또는 hash-pinned figure delivery를 가리켜야 하며, node별
coverage count는 모든 element assignment에서 파생된다. count-only manifest나 과거 schema의
payload는 승격 전에 거부된다.
source structure inventory는 저자·역자 서문과 소개, 본문, 부록, 참고문헌, 찾아보기를
원문 순서로 전수 분류하며 의미 있는 front/back matter와 부록을 canonical leaf로 요구한다.
`source-landed`에서 `translated`로 검토할 때 잘못된 실행 분류만 정정하려면
`coverage_manifest.runnable_support_corrections`를 명시한다. 이 선택 객체는
`expected_source_elements_sha256`(기존 배열의 canonical JSON SHA-256),
`evidence_relative_path`(Vault 안의 분류 증거 JSON), `evidence_sha256`, `items`를 받는다.
각 item은 `element_id`, `block_id`, `owner_id`, `field: runnable_support`,
`before: supported`, `after: static-exception`, `source_sha256`, `reason`을 고정한다.
실제 변경과 items가 정확히 일치하고, hash가 같은 local 증거의 block·owner·static 판정·이유가
일치할 때만 허용한다. 원문 배열의 나머지 필드·순서·ID·locator·SHA와 전체 leaf 소유권은
그대로 유지해야 한다. 기존 scope/base 파일 hash, static 원문 fence·이유·근거와
독립 실행 예제의 검증은 계속 적용한다. 증거 없는 분류 변경이나 검증 완료의 소급 주장은
허용하지 않으며 이 경로는 실제 컴파일·coverage·index 검증을 생략하지 않는다.

번역된 reader에 검토한 보충 실행을 붙일 때는
`KnowledgeService.apply_compiled_wiki_transaction`의
`CompiledWikiTransaction.coverage_manifest`에 기존 `merge-scope` update를 함께 전달한다.
기존 `woon knowledge apply-compiled-transaction --input transaction.json --vault /path/to/vault`
명령도 같은 선택 필드와 `expected_page_spec_sha256`를 받는다.
catalog revision·page revision·page spec·scope/base SHA-256을 모두 고정하며,
기존 scope에서는 `supplemental_runnables` 목록만 뒤에 추가할 수 있다.
원문 inventory·assignment·`runnable.expected/verified`·phase evidence는 변하지 않는다.
새 source(`curated-wiki`, `local-only`, `compiled`)와 claim(`curated-document`, `accepted`)은
같은 transaction의 upsert로 등록한다. 페이지의 기존 source/claim 의존성을 보존하고
새 ID를 뒤에 추가하며, `render.kind: source-body`와 기존 `render.source_id`를 유지한 채
`render.supplemental_claim_ids`에 보충 claim ID를 순서대로 추가한다.
컴파일러는 원문 source body 뒤에 해당 claim 본문을 합성한다. source-body 전체를 새 원문으로
등록하거나 보충 본문을 먼저 쓰는 중간 적용은 필요하지 않다.

각 `supplemental_runnables` 항목은 다음 필드를 정확히 지정한다.

```json
{
  "owner_id": "books/example/chapter-01",
  "source_id": "source://curated-wiki/books/example/chapter-01/<body-revision>",
  "source_record_sha256": "<canonical-JSON-source-record-SHA256>",
  "claim_id": "claim://curated-wiki/books/example/chapter-01/<body-revision>-supplement",
  "claim_record_sha256": "<canonical-JSON-claim-record-SHA256>",
  "run_language": "run-kotlin",
  "run_block_index": 7,
  "code_sha256": "<exact-fence-body-SHA256>",
  "verification_evidence": "private/knowledge/local-only/example/supplement-execution.json",
  "verification_sha256": "<wrapper-file-SHA256>",
  "verification_case": "GetterField"
}
```

record hash는 UTF-8 JSON의 `ensure_ascii=False, sort_keys=True, separators=(",", ":")`로
계산한다. `run_block_index`는 합성된 reader에서 같은 language fence의 1-based 순번이다.
code hash는 fence 내부의 정확한 UTF-8 bytes를 사용하며 종료 fence 직전 LF도 포함한다.
원문 또는 보충 assignment에 정확히 한 번 연결되지 않은 run, 둘에 중복 연결된 run,
accepted source/claim에 없는 코드, reader 코드 hash 불일치는 실패한다.

wrapper는 `schema_version: 1`, `provider: local`, `external_transmission: false`,
Vault-relative `execution_receipt_relative_path`, `execution_receipt_sha256`를 가진다.
설명용 `verification`은 선택 사항이다. 원래 receipt bytes는 바꾸지 않고 wrapper로 고정한다.
실행 receipt의 `cases`에서 `key == verification_case`가 하나여야 하며
`code_sha256`, 정수 `compile_exit: 0`, `run_exit: 0`, `stdout`과 `stdout_sha256`를 대조한다.
의도된 컴파일 실패는 성공 run이 아니다. 두 증거 파일은 Vault 내부의 실제 파일이어야 하고
symlink·원격 실행·외부 전송 기록은 거부한다. 이 검사는 코드를 재실행하지 않는다.
선택한 scope 검사 또는 index 갱신 실패 시 source·claim·page·scope·receipt를 함께 복원한다.
다른 scope의 기존 오류를 새 scope의 성공으로 간주하거나 이 경로로 전체 phase를 올리지 않는다.

Markdown 본문의 설명 표식은 밝은 원형 숫자 `①`부터 `⑩`까지 사용하며, 낮은 대비의
검은 원형 숫자 `❶`부터 `❿`까지가 남은 payload는 승격 전에 거부한다. 원본 figure asset은
변형하지 않는다.
번호 section은 descendant wrapper가 아니라 Map H2 group이고, 퇴역 wrapper prose는 첫 terminal
leaf의 exact relocated span evidence로 보존한다.
승격할 기존 page, coverage manifest, retire할 wrapper의 현재 revision·hash를 다시 확인한 뒤
compiler input·generated output·coverage manifest·검색 index를 하나의 rollback 경계에서
갱신한다. 본문과 coverage를 따로 쓰거나 `book-promote`와 retirement를 분리해 중간 tree를
노출하지 않는다.

`apply-compiled-transaction` 입력은 `apply`, `expected_revisions`, `sources_upsert`,
`claims_upsert`, `pages_upsert`, `curations_upsert`만 받는다. `apply`는 `true`여야 하고,
기존 page는 현재 Markdown SHA-256, 신규 page는 `null` revision과 실제 부재를 요구한다.
명령은 compiler catalog 입력, generated output, compile audit와 검색 index를 하나의 exclusive
lock과 rollback 경계에서 갱신하며 source·claim ID의 비동일 충돌을 거부한다.

### 명시적 반복 기록 삭제

`woon tasks preview-deletion --ids routine-one,routine-two --vault /path/to/vault`는 정확한 routine ID의 정의와 관리행(완료 포함), 파일 revision, 제거 후 비는 날짜 후보를 반환한다. 사용자 삭제 요청에 해당하는 결과를 검토한 뒤 `woon tasks delete-recurring --request /path/to/request.json --vault /path/to/vault`로 적용한다.

request는 `task_ids` 배열, `expected_revisions` 경로→SHA-256/null 객체, 검토한 `empty_daily_paths` 배열, `review_reference` 문자열만 받는다. preview의 집계 필드는 요청에서 제외한다. 사용자 본문이나 다른 ID가 남는 날짜는 삭제할 수 없고, 이미 없어진 정의는 재생성하지 않는다. 같은 요청의 pending receipt는 확인된 before/after hash에서만 재개하며, revision 불일치는 쓰기와 완료 receipt를 차단한다. 완료 후 최소 삭제 receipt 하나를 기존 Tasks runtime에 남기고 해당 ID의 과거 materialization 항목을 제거한다. 연결된 별도 목표와 외부 일정은 이 명령의 대상이 아니다.

## 지원 파이프라인

`woon career`는 지원 하나를 `wiki/personal/career/applications/<id>.md` 한 편에서 관리한다. JD와 PDF는 private source로 hash를 보존하며, 별도 JSON tracker나 context 저장소를 만들지 않는다.

- `analyze`: JD 문장과 기존 Wiki를 대조하되 결과를 사람 검토 전 후보로만 둔다.
- `evaluate`: 사람이 검토한 `verified`·`adjacent`·`gap` 판정과 Wiki 근거를 기록한다.
- `approve-draft` → `attach-pdf --kind draft` → `mark-reviewed` → `mark-ready`: 명시 확인을 거쳐 초안을 제출 가능 상태로 올린다.
- `attach-pdf --kind submitted --confirmed true`: 검증된 실제 제출 PDF 복사와 지원 상태 변경을 같은 잠금·복구 경계에서 수행한다.
- `outcome --confirmed true`: 제출 뒤 면접·합격·불합격·철회·종료 결과를 기록한다.
- `context`: 현재 Wiki 검색 결과를 제한된 크기로 조립해 출력할 뿐 저장하지 않는다.

PDF 렌더러는 각 문서 저장소가 소유한다. Career pipeline은 렌더러가 만든 PDF를 읽어 페이지와 hash를 검증한 뒤 지원 기록과 함께 보존하며, 자동 지원·메일 전송·공개 게시를 수행하지 않는다.

현재 공고는 지원 기록과 분리한다. `woon career jobs-prepare --spec /path/to/review.json --now 2026-09-11T08:00:00+09:00 --vault /path/to/vault`는 공식 목록·원문을 직접 확인한 검토 입력에서 현재 leaf와 Bases 입력을 산출한다. `schema_version: 1`, `records`를 받고 회사·팀·직무·근무지·공식 URL로 중복을 합치며, 충돌 중복은 거부한다. 모집 미확인·마감·36시간 지난 확인·기업 기준 미확인·필수 조건 불충족은 현재 목록에서 제외한다. 개인 필수 역량은 기존 `career evidence`의 revision 고정 근거와 기여 구분이 모두 검증되어야 추천하며, 미확인 조건은 검토로 남긴다.

공식 재조회는 매일 담당 작업이 수행한다. HTTP 접근만으로 모집·지원 가능을 추론하지 않는다. 출력의 `pages`는 공유 Wiki writer가 source·claim·page·receipt로 반영하고, `semantic_sha256`가 같으면 본문 source·claim을 재생성하지 않는다. 확인 시점 metadata는 동일 원자 writer에서 갱신한다. `previous_ids`는 이 producer의 현재 ID만 받으며 부분 조회는 관찰하지 않은 공고의 삭제 근거가 아니다. `scope_complete: true`일 때만 현재 범위 전체의 누락을 제거 대상으로 반환한다. 제외·검색 로그·일일 사본은 Wiki에 쓰지 않는다.

writer 창에서 `woon career jobs-base --vault /path/to/vault`를 실행하면 `inbox/career/current-jobs.base` 하나를 갱신한다. formula 전용 셀은 compiler metadata를 직접 편집하지 않으며 화면에서도 마감·확인 유효기간을 검사한다. 기존 `personal/career/job-search`에서 이 Base를 연다. 이 기능은 지원 정본·private JD를 만들거나 계정 인증·메일·Calendar·지원 제출·예약 설정을 변경하지 않는다.

지속 운영의 공식 회사 목록은 `woon career jobs-sources`로 조회한다. 개별 공고와 개인 검토의 정본은 현재 Wiki leaf metadata이고 임시 JSON이 아니다. 입력 필드와 매일 writer 인계 절차는 [현재 채용 공고](docs/career-current-jobs.md)를 따른다.

`skills eval-routing`은 같은 catalog·prompt·JSON schema로 Codex와 Claude를 각각 격리 실행합니다. 특정 실행기만 검사하려면 `--executor codex` 또는 `--executor claude`를 사용합니다. `installable: false`인 평가 전용 profile은 validate와 routing에는 사용할 수 있지만 target plan·install은 거부됩니다.

문서 변환 설치·model cache·privacy·rollback 운영 계약은 [Docling document intake](docs/docling-document-intake.md)를 따른다. 동일 bytes는 content candidate 하나로 합치고 locator별 observation을 분리한다. 변환 결과는 Wiki가 아니며 기존 정본 검색과 의미 정리를 거쳐 `integrated`, `duplicate`, `discarded`, 예외적인 `user-action-required` 중 하나의 terminal receipt로 끝나야 한다. 현재 권한으로 판단 가능한 중복·저가치·명확한 통합을 Review 적체로 남기지 않는다.

### 명시적 Inbox 처리

`woon knowledge intake ACTION --request /path/to/request.json --vault /path/to/vault`와 `woon_knowledge_intake(action, request)`는 같은 계약을 사용한다. 사용자가 선택한 의미 단위를 먼저 `register`하고, 기존 Wiki writer의 결과를 `prepare`·검증·`complete`로 연결한다. 기존 진행 작업을 소급 등록하거나 자동화를 재개하지 않는다. 이 인터페이스는 Wiki 본문·원증거·외부 서비스를 직접 수정하지 않는다.

`Archived`인 종료 지원·사건 기록도 유효한 정본으로 편입할 수 있다. 의미상 퇴역한
`status: Retired` 또는 `knowledge_state: 폐기됨`은 완료 대상에서 제외한다.

| Action | JSON request 필드 | 결과와 쓰기 |
|---|---|---|
| `register` | `sources`, `title`, `summary`, `explicit_request: true`; 선택 `keywords`, `expected_input_revision` | 안정된 `intake_id`, `input_revision`, `card.path`를 반환하고 미처리 card 하나를 생성·갱신 |
| `status` | `intake_id` | 현재 상태·정본 결과·현재 결과 revision 조회 |
| `prepare` | `intake_id`, `input_revision`, `targets` | 정확한 결과 계획을 고정하고 `write_required` 반환 |
| `complete` | `intake_id`, `input_revision`, `reviewed_revisions` | 실제 결과 hash 검증 후 생성된 미수정 card만 제거 |
| `document-status` | `candidate_id` | 추출물 삭제 후에도 Docling terminal 영수증 조회 |
| `document-cleanup` | `candidate_id`, `expected_resolution_sha256` | 종결된 Docling 변환의 hash가 일치하는 파일만 제거; 사용자 추가 파일은 보존하며 중단 |
| `source-body-plan` | `source_ids`, `protected_source_ids`, `reviewed_successors`, `review_reference` | 공개 생성 문서의 과거 source 본문 축소 계획만 반환; catalog 쓰기 없음 |
| `review-status` | `relative_path`, `source_sha256` | 삭제한 기존 Review 카드의 완료 상태 조회 |
| `review-complete` | 위 필드와 `disposition: integrated\|obsolete`, `review_reference` | 실제 의미 보존·전제 소멸을 검토한 정확한 card만 제거하고 동일 입력의 재생성을 억제 |

`sources`의 각 항목은 `{source_id, selection, revision, locator}`다. 담당자가 확인한 원자료 ID와 안정된 선택 범위가 identity이며 `revision`은 선택 입력의 SHA-256이다. 제목·요약·locator 수정은 ID를 바꾸지 않고 입력 내용 변경은 이전 `input_revision`을 요구한다. 제목은 48자, 요약은 280자, keyword는 최대 8개다. source hash는 담당자가 실제 입력에서 검증해야 하며 등록만으로 원문 확인을 주장하지 않는다.

`targets`의 각 항목은 `{canonical_id, path, before_revision, after_revision}`다. 경로는 Vault 상대 Wiki Markdown, revision은 전체 파일 bytes의 SHA-256이고 새 파일의 `before_revision`은 `null`이다. `reviewed_revisions`는 targets 순서의 `after_revision` 배열로, 실제 의미 보존을 확인한 담당자의 검토 표시다. 제목 일치·hub 링크·옛 완료 문장은 의미 검토를 대신하지 않는다. `prepare`가 `write_required: false`이면 writer를 다시 호출하지 않는다. CLI의 prepare→기존 writer는 그 writer의 잠금·revision 검증을 유지한다. Python에서는 `woon_core.knowledge.intake.run_intake(..., targets=..., writer=..., reviewed_revisions=...)`가 동일 intake callback도 직렬화한다. content 승인 ID가 필요한 기존 writer에는 기존 승인 ID를 전달하며 Inbox ID로 대체하지 않는다.

처리 상태는 `registering → pending → prepared → cleanup-pending → complete`다. 중단 뒤 `status`로 같은 ID를 다시 열어 실제 bytes와 계획을 대조한다. 현재 정본의 후속 편집은 결과의 `current_revision`으로 보여 준다. 완료 후에는 최소 입력 identity·revision·정본 참조만 유지하며 별도 History 문서나 원문·교정본 쌍을 생성하지 않는다. 원자료 교정·보존의 절차 정본은 [archive skill](repo://skills/skills/knowledge/archive/SKILL.md)이다.

source 본문 축소는 최대 24개의 명시적 source ID와 `{source_id, normalized_sha256}` 형식의 검토한 현재 successor를 source ID별 `reviewed_successors`에 요구한다. `protected_source_ids`에는 현재 담당·복구 범위의 보호 ID를 전달한다. 현재 page·accepted claim·미완료 검토가 참조하는 source, private·원증거·책·보호 source는 거부한다. 결과의 `replacement`에는 본문 대신 원래 source identity·hash·후속 참조와 `body_retention` 표식이 남는다. 삭제된 bytes를 복원할 수 있다는 뜻이 아니다. 실제 적용은 공유 catalog writer가 잠금 안에서 `expected_catalog_sha256`와 record hash를 재확인하고 기존 transaction·audit으로 수행한다.

스킬 설치 경로는 Woon 전용 직접 경로가 가장 우선합니다. 설정하지 않으면 각 executor의 표준 home 아래 `skills`를 사용하고, 표준 home도 없으면 사용자 기본 경로를 사용합니다.

| target | 직접 경로 | executor home | 기본 경로 |
|---|---|---|---|
| Codex | `WOON_CODEX_SKILLS_HOME` | `CODEX_HOME/skills` | `~/.codex/skills` |
| Claude | `WOON_CLAUDE_SKILLS_HOME` | `CLAUDE_CONFIG_DIR/skills` | `~/.claude/skills` |

격리된 plan·install 검증은 임시 executor home을 명시합니다.

```bash
CODEX_HOME=/tmp/woon-codex-eval woon skills plan --profile learning --target codex
CLAUDE_CONFIG_DIR=/tmp/woon-claude-eval woon skills plan --profile learning --target claude
```

root 후보가 서로 다르면 임의로 선택하지 않고 실패한다. `--root`, `WOON_HOME`, platform config, 상위 `.woon-root`, 기본 workspace 순서로 확인한다.

교차 저장소 참조는 안정적인 URI를 사용한다.

```text
repo://knowledge/wiki/os/page-fault.md
```

공유 registry에는 Git URL과 상대 폴더만 기록한다. 머신 경로는 local Woon config에만 저장하고 Git에 커밋하지 않는다.
`base: workspace-parent`는 Woon과 나란한 작업 폴더를 기준으로 삼는다. Obsidian 플러그인은 이 기준의 `OSS/obsidian/`에 모으며, `directory`의 절대경로와 `..`는 계속 금지한다.

## 정본 지식 MCP

`woon-knowledge-mcp`는 client가 stdio 연결을 유지하는 동안에만 실행되며 background daemon을 만들지 않는다. `WOON_KNOWLEDGE_ROOT`에 private vault를 지정한다.

Codex에는 설치된 실행 파일과 vault를 한 번 등록한다.

```bash
codex mcp add woon-knowledge \
  --env WOON_KNOWLEDGE_ROOT=/path/to/woon-knowledge \
  -- "$(uv tool dir --bin)/woon-knowledge-mcp"
```

등록 뒤 새 Codex 작업에서 `woon_knowledge_search`를 사용할 수 있다. 설정 확인과 제거는 각각 `codex mcp get woon-knowledge`, `codex mcp remove woon-knowledge`다.

제공 도구는 정본 검색·전체 읽기·대화 병합·학습 체크포인트·LLM Wiki compile/receipt audit·index rebuild·audit·Git history·확인된 복구다. 학습 체크포인트는 최신 revision을 요구하며 compiler-owned page는 새 curated source·claim·receipt로, 그 밖의 canonical page는 YAML을 보존하는 body writer로 갱신한다. 같은 개념의 블로그, 기술문서, AI 전용 변형은 생성하지 않는다.

IDE 설정은 같은 바이너리에서 관리한다.

```bash
woon env doctor --all
woon env plan --all
woon env generate
woon env apply --all
woon env verify --all
```

## 저장소 구성

- `woon-core`: policy and orchestration
- `woon-skills`: skill catalog, profiles, locks, and conflicts
- `woon-env`: deterministic IDE configuration
- `woon-knowledge`: private durable knowledge
- `woon-site`: private publishing source
- `woonyong-kr`: generated GitHub profile output
- `woonyong-kr.github.io`: protected Pages output

두 출력 저장소는 GitHub가 요구하는 이름을 유지하며 rename 대상이 아니다.

## 기여

변경 전 [저장소 표준](docs/repository-standard.md)을 확인하고 [최소 검증 원칙](standards/code.yaml)에 따라 관련 검사만 선택한다. 생성 지침 비교는 `woon context check core --artifacts-only`, 전체 경로 감사가 필요한 변경은 `woon context check core`로 확인한다.
