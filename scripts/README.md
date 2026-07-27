# Scripts

이 디렉토리에는 공지 수집, 검증, Notion 동기화와 실행 상태 관리에 사용하는 모듈이 있다. 루트 `main.py`가 `scripts/main.py`를 불러와 실행한다.

## 실행 흐름

### 1. 설정과 실행 준비

`main.py`가 `.env`를 읽고 로깅, 실행 제한 시간, 중복 실행 방지와 `.runtime/` 상태 파일을 준비한다. 로컬 실행은 기본적으로 `SYNC_DRY_RUN=1`을 사용한다.

`settings.py`는 Notion API 버전, 데이터 소스, 게시판, 동기화 옵션과 속성 이름을 해석한다. `run_control.py`와 `run_lock.py`는 제한 시간, 종료 신호와 중복 실행을 처리한다.

### 2. 공지 수집과 검증

`crawler.py`가 게시판별 목록과 상세 공지를 수집한다. 서강대학교 API를 우선 사용하고 필요한 경우 HTML/HTTP 조회나 Playwright 브라우저 수집으로 보완한다.

`bbs_parser.py`는 목록 행, 상세 본문, 작성일과 첨부파일을 해석한다. `common.py`와 `utils.py`는 URL, 공지 ID, 날짜, 파일명과 본문 블록을 공통 형식으로 변환한다.

`validation.py`는 설정한 출처가 모두 수집됐는지, 공지 식별자와 URL이 일치하는지, 항목 수가 비정상적으로 줄거나 출처가 비어 있지 않은지를 검사한다. 검증에 실패한 출처는 Notion 변경 대상에서 제외한다.

### 3. 변경 계획

`sync_engine.py`가 수집 결과와 현재 Notion 페이지를 비교해 변경 계획을 만든다. 드라이런도 실제 데이터 소스와 페이지 상태를 읽어 계획을 확인하지만 페이지, 블록 또는 속성을 변경하지 않는다.

### 4. Notion 적용

`notion_client.py`는 Notion API 요청, 데이터 소스 해석, 페이지와 블록 조회, 파일 업로드, 재시도와 응답 검증을 처리한다.

`sync.py`는 페이지 검색, 속성 값, 최상위 `quote` 본문과 첨부파일 상태를 처리한다. 본문을 변경할 때는 다음 순서를 지킨다.

1. 새 최상위 `quote`와 자식 블록을 작성한다.
2. 새 블록의 구성과 본문 해시를 확인한다.
3. 적용 직전에 현재 관리 본문이 바뀌지 않았는지 다시 확인한다.
4. 새 본문을 현재 상태로 기록한 뒤 이전 관리 `quote`를 제거한다.

새 본문을 확인하기 전에 실패하면 현재 본문을 유지한다. 별도로 추가한 비인용 최상위 블록은 수정하거나 삭제하지 않으며, 내용과 구조가 같은 `quote`는 재사용한다. 관리되지 않는 최상위 `quote`가 있으면 충돌로 처리해 동기화를 중단한다.

### 5. 실행 상태

`run_state.py`는 증분 수집 기준, 이전 관측 ID, 실행 결과와 반복 실패 간격을 `.runtime/`의 JSON 파일로 관리한다. 상태 파일은 중단되거나 겹친 실행이 다음 동기화에 잘못 반영되지 않도록 검증한 뒤 갱신한다.

## 주요 파일

| 파일 | 역할 |
| --- | --- |
| `main.py` | 전체 실행과 드라이런·실제 적용 분기 |
| `models.py` | 수집·검증·동기화 공통 자료형 |
| `settings.py` | 환경 변수, 게시판과 Notion 설정 |
| `crawler.py` | API·HTTP·Playwright 수집과 첨부파일 점검 |
| `bbs_parser.py` | HTML 목록·본문·첨부파일 파싱 |
| `validation.py` | 출처 완전성과 항목 식별자 검증 |
| `sync_engine.py` | 변경 계획 생성과 적용 순서 관리 |
| `sync.py` | 페이지 속성, 첨부파일과 `quote` 본문 처리 |
| `notion_client.py` | Notion API, 데이터 소스와 파일 처리 |
| `migrate_existing_pages.py` | 명시한 기존 페이지의 검토형 메타데이터 이관 |
| `run_state.py` | 증분 수집 상태와 실행 결과 저장 |
| `run_control.py` | 종료 신호와 제한 시간 관리 |
| `run_lock.py` | 중복 실행 방지 |
| `common.py` | 여러 모듈이 공유하는 URL·행·본문 규칙 |
| `utils.py` | 날짜, 해시, 첨부파일과 Notion 블록 함수 |
| `log.py` | 로그 형식과 민감값 제거 |

## 변경 시 확인할 영역

- 수집: `crawler.py`, `bbs_parser.py`, `common.py`, `validation.py`
- Notion 요청: `notion_client.py`, `settings.py`, `tests/test_notion_protocol.py`
- 페이지와 본문: `sync.py`, `sync_engine.py`, `tests/test_sync_safety.py`
- 첨부파일과 이미지: `notion_client.py`, `utils.py`, `tests/test_attachment_*`
- 실행 상태: `run_state.py`, `run_control.py`, `run_lock.py`
- 환경 변수: `settings.py`, `run_control.py`, 루트 `README.md`, `.env.example`

설치와 검증 명령은 루트 [`README.md`](../README.md)와 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)을 기준으로 한다.
