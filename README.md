# sogang-notices-to-notion

서강대학교 공지(장학, 학사) 게시판을 크롤링해서 Notion 데이터베이스에 동기화하는 스크립트다.

## 주요 기능

- 공지 목록 수집과 상세 본문 파싱
- Notion 페이지 생성/업데이트, 본문 블록과 첨부파일 반영
- 첨부파일 정책(허용 도메인, 개수 제한) 적용
- GitHub Actions Workflow로 주기 실행

## 요구 사항

- Python 3.11 이상
- `playwright`, `Pillow`

## 설치

```bash
pip install -r requirements.txt
python -m playwright install --with-deps
```

## 설정

`main.py`는 `.env`를 자동으로 읽는다.

```ini
NOTION_TOKEN=your_notion_token
NOTION_DB_ID=your_database_id
```

## Notion 데이터베이스 속성

이 코드에서 사용하는 모든 속성은 없으면 자동으로 생성한다. 다만 `공지사항`(title) 속성은 데이터베이스의 기존 title 속성 이름을 `공지사항`으로 변경한다. 타입이 다르면 실행 시 오류가 난다. 값은 데이터가 있을 때만 채운다.

| 속성 이름 | 타입 |
| --- | --- |
| 공지사항 | title |
| TOP | checkbox |
| 분류 | select |
| 유형 | select |
| 작성자 | select |
| 작성일 | date |
| 조회수 | number |
| 첨부파일 | files |
| URL | url |
| 본문 해시 | rich_text |

## 환경 변수

필수 값

- `NOTION_TOKEN`: Notion 통합(integration) 토큰
- `NOTION_DB_ID`: Notion 데이터베이스 ID

자주 쓰는 옵션

- `NOTION_API_VERSION`: Notion API 버전, 기본값은 `2022-06-28`
- `BBS_CONFIG_FK`: 단일 게시판 설정 ID, 기본값은 `141`
- `BBS_CONFIG_FKS`: 게시판 설정 ID 목록, 기본값은 `141,2`
- `BBS_CONFIG_CLASSIFY`: 게시판 ID별 분류 매핑, 기본값은 `141:장학공지,2:학사공지`
- `BBS_CONFIG_LIST_URLS`: 게시판 ID별 목록 URL 매핑, 기본값은 `141:https://www.sogang.ac.kr/ko/scholarship-notice,2:https://www.sogang.ac.kr/ko/academic-support/notices`
- `BBS_PAGE_SIZE`: 페이지당 목록 수, 기본값은 `20`
- `INCLUDE_NON_TOP`: 일반 공지 포함 여부, 기본값은 `1`
- `NON_TOP_MAX_PAGES`: 일반 공지 최대 페이지, 기본값은 `3`이며 `0`이면 제한 없음
- `SYNC_MODE`: `overwrite` 또는 `preserve`, 기본값은 `overwrite`
- `NOTION_UPLOAD_FILES`: 이미지 파일 업로드 여부, 기본값은 `1`
- `NOTION_DEDUPE_ON_START`: 시작 시 URL 중복 정리, 기본값은 `1`
- `BROWSER`: `chromium`, `chrome`, `edge`, `firefox`, `webkit`, `safari` 중 하나, 기본값은 `chromium`
- `HEADLESS`: 헤드리스 실행 여부, 기본값은 `1`
- `HTML_PATH`: 로컬 HTML 파일 경로를 지정하면 네트워크 대신 해당 파일을 파싱
- `ATTACHMENT_ALLOWED_DOMAINS`: 첨부파일 허용 도메인, 기본값은 `sogang.ac.kr`
- `ATTACHMENT_MAX_COUNT`: 첨부파일 최대 개수, 기본값은 `15`
- `ATTACHMENT_SELFTEST`: 첨부파일 정책 셀프 테스트, 켜면 실행 후 종료
- `USER_AGENT`: 요청 헤더의 User-Agent

## 실행

```bash
python main.py
```

로컬 HTML 파일로 테스트하려면 아래처럼 실행한다.

```bash
HTML_PATH=sample.html python main.py
```

## GitHub Actions

`.github/workflows/crawler.yml`에서 매시 정각 크론 스케줄로 실행한다. 시크릿으로 `NOTION_TOKEN`, `NOTION_DB_ID`를 등록해야 한다.
