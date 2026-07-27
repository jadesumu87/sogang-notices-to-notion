# Sogang Notices to Notion

서강대학교 장학공지와 학사공지를 수집해 Notion 데이터베이스로 동기화하는 CLI 프로젝트다. 공지 목록과 상세 본문, 첨부파일을 수집하고 현재 페이지와 비교한 뒤 검증을 통과한 변경만 반영한다.

## 주요 기능

- 장학공지(`141`)와 학사공지(`2`)를 기본 수집 대상으로 사용한다.
- 서강대학교 API를 우선 사용하고 필요한 경우 HTML/HTTP 조회와 Playwright 브라우저 수집으로 보완한다.
- 출처별 수집 완전성, 공지 식별자, 본문과 첨부파일 상태를 확인한 뒤 Notion에 반영한다.
- 로컬 실행은 기본적으로 드라이런이며 Notion 데이터를 변경하지 않는다.
- Notion 본문은 페이지 최상위의 단일 `quote` 블록으로 유지한다.
- 동기화 대상이 아닌 수동 최상위 블록은 수정하거나 삭제하지 않는다.

## 프로젝트 구조

```text
.
├─ .github/
│  └─ workflows/
│     ├─ ci.yml                   # 코드와 테스트 검증
│     └─ crawler.yml              # 정기 및 수동 동기화
├─ scripts/
│  ├─ main.py                     # 실행 흐름
│  ├─ crawler.py                  # 공지 수집
│  ├─ bbs_parser.py               # HTML·본문·첨부파일 파싱
│  ├─ notion_client.py            # Notion API와 파일 처리
│  ├─ sync.py                     # 페이지와 본문 동기화
│  ├─ sync_engine.py              # 변경 계획과 적용
│  ├─ migrate_existing_pages.py   # 기존 페이지 검토형 이관
│  └─ ...                         # 상태, 검증, 잠금, 공통 모델
├─ tests/                         # 회귀 테스트
├─ .env.example
├─ .python-version
├─ main.py                        # CLI 진입점
├─ pyproject.toml
├─ requirements.lock              # 실행 의존성 잠금
└─ requirements-ci.lock           # 검증 의존성 잠금
```

루트 `main.py`가 CLI 진입점을 제공하고 실제 구현은 `scripts/`에 있다. 모듈별 역할은 [`scripts/README.md`](scripts/README.md)에서 확인할 수 있다.

## 요구 사항

- Python `3.13.14`
- macOS 또는 Linux (`fcntl` 기반 실행 잠금)
- Notion 통합 토큰과 대상 데이터베이스 ID
- Playwright용 Chromium

로컬과 GitHub Actions 모두 Python `3.13.14`를 기준으로 검증한다.

## 설치

가상환경을 만들고 활성화한다.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python --version
```

실행 의존성과 Chromium을 설치한다.

```bash
python -m pip install --require-hashes -r requirements.lock
python -m pip check
python -m playwright install chromium
```

테스트와 정적 검사를 실행하려면 검증 의존성도 설치한다.

```bash
python -m pip install --require-hashes -r requirements-ci.lock
python -m pip check
```

해시가 포함된 `requirements.lock`과 `requirements-ci.lock`이 재현 가능한 설치 기준이다.

## Notion 준비

1. Notion 통합을 만들고 대상 데이터베이스를 해당 통합과 공유한다.
2. `.env.example`을 `.env`로 복사한다.
3. `NOTION_TOKEN`과 `NOTION_DB_ID`를 입력한다.
4. 데이터베이스에 데이터 소스가 여러 개라면 `NOTION_DATA_SOURCE_ID`도 입력한다.

```bash
cp .env.example .env
```

```env
NOTION_TOKEN=your_notion_token
NOTION_DB_ID=your_database_id
NOTION_DATA_SOURCE_ID=
SYNC_DRY_RUN=1
```

지원하는 Notion API 버전은 `2026-03-11`이다. 대상 데이터베이스에 데이터 소스가 하나면 자동으로 선택하고, 여러 개면 `NOTION_DATA_SOURCE_ID`로 대상을 지정한다.

`.env`는 Git에서 제외된다. 실제 토큰이나 데이터베이스 ID를 예제 파일, 로그 또는 이슈에 기록하지 않는다.

## 로컬 실행

로컬 기본 실행은 현재 Notion 상태를 읽고 변경 계획을 계산하지만 데이터를 쓰지 않는 드라이런이다.

```bash
python main.py
```

드라이런을 명시하려면 다음과 같이 실행한다.

```bash
SYNC_DRY_RUN=1 python main.py
```

사용 가능한 인수는 `python main.py --help`로 확인한다.

로컬 HTML 파일은 단일 출처의 목록 파싱을 진단할 때 사용할 수 있다. 상세 페이지까지 검증하지 않으므로 이 실행은 Notion을 변경하지 않고 안전 검사 실패로 종료되는 것이 정상이다. `141`은 HTML과 맞는 게시판 ID로 바꾼다.

```bash
BBS_CONFIG_FKS=141 SYNC_DRY_RUN=1 HTML_PATH=/path/to/notice.html python main.py
```

## Notion 스키마

동기화 대상 데이터 소스는 다음 속성과 타입을 사용한다.

| 속성 | 타입 |
| --- | --- |
| `공지사항` | `title` |
| `TOP` | `checkbox` |
| `작성일` | `date` |
| `작성자` | `select` |
| `URL` | `url` |
| `유형` | `select` |
| `분류` | `select` |
| `조회수` | `number` |
| `첨부파일` | `files` |
| `본문 해시` | `rich_text` |
| `본문 미디어 상태` | `rich_text` |
| `첨부 상태` | `rich_text` |
| `동기화 소유자` | `rich_text` |
| `출처 ID` | `rich_text` |
| `공지 ID` | `rich_text` |
| `본문 세대` | `rich_text` |
| `동기화 상태` | `rich_text` |
| `작업 ID` | `rich_text` |

일반 실행과 드라이런은 스키마를 자동으로 변경하지 않는다. 필요한 속성이 없거나 제목 속성 이름이 다르면 `crawler.yml`의 수동 실행에서 `dry_run=false`, `schema_migration=true`를 선택해 스키마를 갱신한 뒤 일반 동기화를 실행한다.

본문과 동기화 상태를 관리하는 속성은 기본 Notion 뷰에서 숨길 수 있으며 직접 수정하지 않는 것을 권장한다.

### 기존 페이지 이관

새 동기화 속성을 추가하기 전에 만들어진 공지 페이지는 자동으로 소유권을 가져오지 않는다. 이관이 필요하면 먼저 스키마 마이그레이션을 완료하고, 이관할 페이지 ID를 명시해 읽기 전용 계획을 만든다. 여러 페이지는 `--page-id`를 반복해 지정한다.

```bash
mkdir -p .migration
python scripts/migrate_existing_pages.py \
  --data-source-id your_data_source_id \
  --page-id your_page_id \
  --output .migration/existing-pages.json
```

계획 파일의 페이지 제목, 공식 URL, 페이지 ID와 `confirmation` 값을 검토한 뒤에만 적용한다.

```bash
python scripts/migrate_existing_pages.py \
  --apply \
  --plan .migration/existing-pages.json \
  --confirm "계획 파일의 confirmation 값" \
  --allow-write
```

이 도구는 지정한 페이지의 동기화 메타데이터만 변경한다. 페이지 본문, 수동 블록, 첨부파일, `TOP`, 아이콘은 변경하지 않으며 계획 이후 페이지·본문·스키마가 달라지거나 URL·본문 소유권이 모호하면 적용을 중단한다. 이전 동기화 표식이 있는 `quote`는 수동 블록과 함께 있어도 이관할 수 있다. 표식이 없는 `quote`는 유일한 최상위 블록일 때만 이관한다. `.migration/`은 Git에서 제외된다.

## 본문 구조와 업데이트 방식

- 관리되는 공지 본문은 페이지 최상위의 단일 `quote` 블록에 표시한다.
- 긴 본문과 이미지는 해당 `quote`의 자식 블록으로 저장한다.
- 업데이트할 때 새 `quote`와 자식 블록을 먼저 작성하고 내용과 구조를 확인한다.
- 확인이 끝난 뒤에만 이전에 관리하던 `quote`를 제거한다.
- 새 본문을 확인하기 전에 실패하면 현재 본문을 그대로 유지한다.
- 별도로 추가한 비인용 최상위 블록은 수정하거나 삭제하지 않는다.
- 관리되지 않는 최상위 `quote`가 있으면 어느 본문인지 임의로 판단하지 않고 동기화를 중단한다.
- 내용과 구조가 같은 `quote`는 다시 만들지 않고 재사용한다.

## 주요 환경 변수

자주 사용하는 설정 예시는 [`.env.example`](.env.example)에 있다.

| 변수 | 필수 여부 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `NOTION_TOKEN` | 필수 | 없음 | Notion 통합 토큰 |
| `NOTION_DB_ID` | 필수 | 없음 | 대상 Notion 데이터베이스 ID |
| `NOTION_DATA_SOURCE_ID` | 조건부 | 자동 선택 | 데이터 소스가 여러 개일 때 대상 ID |
| `NOTION_SCHEMA_MIGRATION` | 선택 | `0` | 명시적 스키마 변경 허용 |
| `NOTION_SCHEMA_MIGRATION_ONLY` | 선택 | `0` | 스키마 변경만 실행하고 종료 |
| `SYNC_DRY_RUN` | 선택 | `1` | Notion 쓰기 없이 변경 계획만 검사 |
| `BBS_CONFIG_FKS` | 선택 | `141,2` | 수집할 게시판 ID 목록 |
| `BBS_CONFIG_CLASSIFY` | 선택 | 기본 매핑 | 게시판 ID별 분류명 |
| `BBS_CONFIG_LIST_URLS` | 선택 | 기본 URL | 게시판 ID별 목록 URL |
| `BBS_PAGE_SIZE` | 선택 | `20` | API 페이지당 항목 수 |
| `INCLUDE_NON_TOP` | 선택 | `1` | 비TOP 공지 포함 여부 |
| `NON_TOP_MAX_PAGES` | 선택 | `0` | 비TOP 탐색 추가 제한. `0`이면 전체 안전 상한인 100페이지만 적용 |
| `INCREMENTAL_CRAWL` | 선택 | `1` | 실행 상태를 이용한 증분 수집 |
| `NOTION_UPLOAD_FILES` | 선택 | `1` | 본문 이미지를 Notion 파일로 업로드할지 여부 |
| `FAILURE_REPEAT_SECONDS` | 선택 | `21600` | 예약 실행에서 같은 실패를 다시 실패로 표시할 간격. `0`이면 매번 표시 |
| `HTML_PATH` | 선택 | 없음 | 실제 사이트 대신 사용할 로컬 HTML |

불리언 옵션은 `1/0`, `true/false`, `yes/no`, `on/off`를 사용할 수 있다.

## GitHub Actions

저장소의 `Settings > Secrets and variables > Actions`에 다음 값을 등록한다.

Repository secrets:

- `NOTION_TOKEN`
- `NOTION_DB_ID`

대상 데이터베이스에 데이터 소스가 여러 개라면 다음 비밀값도 등록한다.

- `NOTION_DATA_SOURCE_ID`

`ci.yml`은 pull request와 `main` 브랜치 변경을 검증한다. `crawler.yml`은 `main` 브랜치의 예약 실행과 수동 실행을 제공하며 필요한 비밀값을 확인한 뒤 동기화를 시작한다. 수동 실행에서는 드라이런이나 스키마 마이그레이션을 선택할 수 있다. 스키마 마이그레이션은 `dry_run=false`와 함께 선택한다.

예약된 크롤러 실행 단계에서 처음 확인한 실패는 작업을 실패로 표시한다. 같은 원인으로 분류된 실패가 계속되면 `FAILURE_REPEAT_SECONDS` 동안 실행 기록과 경고는 남기되 작업의 반복 실패 표시는 생략하고, 간격이 지나면 다시 실패로 표시한다. 따라서 이 간격 안에 성공으로 표시된 예약 실행만으로 복구를 판단하지 않고 반복 실패 경고를 함께 확인한다. 수동 실행과 기존 작업의 수동 재실행은 실패를 항상 실패로 표시하며, 정상 동기화가 끝나면 이전 실패 상태를 지운다. 저장소 확인, 환경 준비, 의존성 설치, 캐시 검증·저장처럼 크롤러 실행 단계 밖에서 발생한 실패는 이 간격을 적용하지 않는다.

## 검증

주요 로컬 검증 명령은 다음과 같다.

```bash
python -m compileall -q main.py scripts .github/scripts
ruff check .
python -m mypy --strict main.py
MYPYPATH=scripts python -m mypy --strict scripts .github/scripts
REQUIRE_BROWSER_TESTS=1 python -m coverage run -m unittest discover -s tests -p "test_*.py" -v
python -m coverage report --fail-under=60
```

잠금 파일, 커버리지와 워크플로 검사를 포함한 전체 검증은 [`.github/workflows/ci.yml`](.github/workflows/ci.yml)을 기준으로 한다.
