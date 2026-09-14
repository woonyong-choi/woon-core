# 현재 채용 공고

매일 담당 작업은 공식 회사 목록과 각 공고를 직접 읽고, 공유 Wiki writer는 검토한 결과만 반영한다. 조회 기능은 계정·메일·Calendar 없이 실행되며 지원을 제출하지 않는다.

## 지속 입력

`woon career jobs-sources`가 읽는 `src/woon_core/career/current-job-sources.yaml`은 초기 회사 목록의 단일 설정이다. 공고가 모두 마감되어도 회사 목록은 남는다. 이 설정은 전체 시장 조사 범위나 모집 중임을 보증하지 않는다. 탐색 범위를 넓힐 때 검증한 회사 목록을 이 설정에 추가한다.

개별 공고의 정본은 `wiki/personal/career/job-search/job-<identity-hash>.md`의 metadata다. 공식 URL·확인일·조건·개인 판정은 이 페이지를 재조회한다. 이전 실행의 임시 JSON이나 대화 요약을 새로운 근거로 쓰지 않는다.

## 검토 입력

`jobs-prepare`의 JSON은 `schema_version: 1`, `records`, 선택적인 `previous_ids`와 `scope_complete`를 받는다. `previous_ids`는 이번에 확인할 범위에 속한 기존 producer ID만 넣는다. 일부 출처에 접근하지 못했다면 `scope_complete`를 true로 만들지 않는다.

각 모집 중인 record에는 다음 필드를 둔다.

- 식별: `company`, `team`, `role`, `location`, `official_url`, `list_url`. 팀 미기재는 그대로 표시하며 회사 주소로 직무 근무지를 확정하지 않는다.
- 관찰: `posting_state: open`, `detail_verified`, `list_verified`, timezone이 있는 `detail_checked_at`, `list_checked_at`. HTTP 성공만으로 true를 주지 않는다.
- 범위: `in_scope`, 제외라면 `scope_reason`. `deadline`은 명시된 시각과 timezone, 미기재이면 null이다. `posted_at`도 모르면 null이다.
- 조건과 설명: `employment`, `experience`, `education`, `conditions`, `work`, `fit`, `gap`, 선택 `process`.
- 기업 근거: `company_basis`, `company_basis_url`. 투자·상장·인수 확인은 직무 적합도와 별도로 한다.
- `checks`: `company`, `implementation`, `experience`, `location`, `employment`, `education`, `availability` 각각에 `state: verified|unknown|failed`와 `basis`를 둔다. 다른 필수 제약도 같은 형식으로 추가할 수 있다.
- `requirements`: JD 필수 항목별 `requirement`, `classification: verified|adjacent|gap|unknown`, `ownership`, `rationale`. verified에는 `career evidence`로 고정한 `evidence_refs`가 필요하다. 개인·팀·종료 후 개인 확장을 구분하고 생성 이력서·지원 기록·공고 검토를 개인 근거로 재사용하지 않는다. 필수 목록 전체를 검토했을 때만 `requirements_complete: true`다.

모든 필수 역량과 hard constraint가 확인되어야 추천한다. 인접 경험·일정 미확인 등은 검토이지 지원 가능 확정이 아니다. 이 생산기는 검토 구조와 근거 revision을 검증하며 채용 가능성이나 합격 확률을 계산하지 않는다.

## 매일 인계

1. `jobs-sources`와 현재 Wiki leaf를 조회하고 공식 목록·원문을 직접 연다. 삭제·마감은 근거를 확인하며 차단·빈 본문을 삭제로 해석하지 않는다.
2. 위 형식의 검토 입력을 임시 파일로 만들고 `woon career jobs-prepare --spec /path/to/review.json --now 2026-09-11T08:00:00+09:00 --vault /path/to/vault`를 실행한다. `--now`에는 실제 실행 시각을 쓴다.
3. 단일 Wiki writer가 `pages`와 `remove_from_current`를 현재 revision에 대조한다. source·claim·page·receipt transaction으로 반영하며 `semantic_sha256`가 같으면 기존 본문 source·claim을 유지하고 확인 metadata만 갱신한다. source 이력은 보존하되 현재 트리에 제외·마감 archive를 만들지 않는다.
4. writer 창에서 `woon career jobs-base --vault /path/to/vault`를 실행한다. Base 하나는 항상 producer가 소유한다. compile audit·knowledge audit·reindex·대상 재조회와 URL 확인 뒤 결과를 보고한다. 확인하지 못한 실제 화면은 데이터 검증과 구분한다.

동일 다섯 식별 값은 하나로 합치며 충돌한 중복 입력은 거부한다. 36시간 지난 관찰과 확인된 마감 시각은 producer와 Base 양쪽에서 현재 결과를 차단한다. 실패한 조회의 관찰 시간을 새 시각으로 바꾸지 않는다. 완료한 임시 입력·출력은 더 이상 복구·후속 적용에 필요하지 않으면 정리하고 매일 별도 tracker를 누적하지 않는다. 사용자가 선택한 공고만 기존 지원 파이프라인으로 승격한다. 예약·담당 작업의 연결은 Master가 소유하며 이 명령은 자동화를 생성하지 않는다.
