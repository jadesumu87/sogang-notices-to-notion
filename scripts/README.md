# Scripts

이 디렉토리는 실제 수집·파싱·동기화 로직이 모여 있는 내부 실행 모듈 모음이다. 루트 `main.py`는 기존 실행 습관을 유지하기 위한 얇은 진입점이고, 실제 공지 수집부터 Notion 반영까지의 흐름은 이 디렉토리 안에서 완성된다.

## 개요

- `scripts/main.py`가 전체 실행을 총괄한다.
- 공지 목록과 상세 정보는 `crawler.py`가 API를 우선 사용하고, 필요할 때 HTML/HTTP 보완 조회와 Playwright 기반 수집으로 보완한다.
- HTML 본문과 첨부파일 파싱은 `bbs_parser.py`가 담당한다.
- 기존 Notion 페이지 탐색, 속성 구성, 본문 동기화는 `sync.py`가 맡는다.
- Notion API 호출, 파일 업로드, 데이터베이스 속성 보정은 `notion_client.py`에 모여 있다.
- 설정값, 공통 정규화 규칙, 유틸리티 함수는 `settings.py`, `common.py`, `utils.py`, `log.py`에 나뉘어 있다.

## 디렉토리 구조

```text
scripts/
├─ README.md
├─ bbs_parser.py
├─ common.py
├─ crawler.py
├─ log.py
├─ main.py
├─ notion_client.py
├─ settings.py
├─ sync.py
└─ utils.py
```

- `scripts/`는 패키지형 구조가 아니라, 루트 실행점에서 바로 import하는 평면 모듈 구조를 유지한다.
- 실행 경로와 import 이름이 단순한 대신, 모듈 책임을 파일별로 비교적 분명하게 나눠 두는 쪽을 택했다.

## 실행 흐름

### 1. 환경 준비

`settings.py`가 `.env`를 읽고, 기본 게시판·Notion 속성명·첨부 정책·동기화 모드 같은 실행 설정을 정리한다. `log.py`는 환경 정보와 운영 로그 형식을 맞춘다.

### 2. 공지 수집

`scripts/main.py`는 입력 모드에 따라 로컬 HTML 또는 실제 사이트 수집을 고른다. 사이트 수집 경로에서는 `crawler.py`가 게시판 목록과 상세 정보를 모으며, 기본적으로 API를 우선 사용한다.

API 결과만으로 본문·작성일·첨부파일을 충분히 확인하기 어려우면 `crawler.py`가 HTML/HTTP 보완 조회나 Playwright 기반 재시도를 수행한다. 이 단계에서 `common.py`의 상세 URL 추출 규칙과 `bbs_parser.py`의 본문/첨부 추출 규칙을 함께 사용한다.

### 3. 본문·첨부 정규화

`bbs_parser.py`는 HTML을 Notion 블록으로 바꾸고, 첨부파일과 작성일 같은 상세 메타데이터를 추출한다. `utils.py`와 `common.py`는 제목 보정, URL 정규화, 본문 해시 비교용 블록 정규화, 첨부 후보 판정 같은 공통 처리를 맡는다.

이 단계에서 첨부가 "확정적으로 없음"인지, "이번 실행에서 확인 실패"인지 구분해 이후 `files=[]`가 잘못 나가지 않도록 상태를 보존한다.

### 4. Notion 동기화

`scripts/main.py`는 수집한 항목을 기준으로 기존 페이지를 찾고, `sync.py`와 `notion_client.py`를 이용해 속성·본문·첨부를 반영한다.

- `sync.py`는 기존 페이지 탐색, 중복 정리, 본문 컨테이너 유지, 속성 payload 구성을 담당한다.
- `notion_client.py`는 데이터베이스 속성 보정, 페이지 생성·수정, 파일 업로드, 재시도 로직을 담당한다.

이 흐름에서 `첨부 상태`, `본문 해시`, `본문 미디어 상태` 같은 내부 관리 속성도 함께 유지한다.

## 파일별 역할

### `main.py`

실행의 시작점이다. 환경을 준비하고, 입력 경로를 결정하고, 수집 결과를 Notion 데이터베이스와 동기화한다.

### `crawler.py`

목록 조회, 상세 API 호출, HTML/HTTP fallback, Playwright 재시도, 첨부 정책 selftest를 담당한다. 현재 수집 파이프라인에서 가장 많은 운영 예외 처리가 들어 있는 파일이다.

### `bbs_parser.py`

사이트 HTML에서 표 행, 작성일, 첨부파일, 본문 블록을 추출한다. Tiptap 계열 본문을 Notion 블록으로 바꾸는 파서도 여기에 있다.

### `sync.py`

기존 페이지 탐색, URL 중복 정리, 페이지 속성 구성, 본문 컨테이너 갱신, 재사용 가능한 업로드 상태 보강 로직을 모아 둔다.

### `notion_client.py`

Notion API 요청 래퍼, 오류 메시지 정리, 재시도/backoff, 데이터베이스 속성 생성·보정, 외부 파일 다운로드와 Notion 파일 업로드를 담당한다.

### `settings.py`

환경 변수 해석, 기본 게시판 설정, URL/속성 이름 상수, 첨부파일/본문 판정용 정규식과 정책 값을 제공한다.

### `common.py`

상세 URL 추출, 리스트 행 해석, 제목 보정, 본문 블록 정규화처럼 여러 모듈이 같은 기준으로 써야 하는 공통 규칙을 모아 둔다.

### `utils.py`

날짜 파싱, URL 정규화, 본문 해시 계산, 첨부 후보 판정, Notion 블록 조립, 파일명/콘텐츠 타입 보조 처리 등 범용 유틸리티를 제공한다.

### `log.py`

로깅 초기화와 환경 정보 출력만 담당하는 얇은 모듈이다. 로그 이름과 형식을 고정해 운영 로그 검색을 쉽게 만든다.

## 수정할 때 먼저 볼 파일

- 수집 경로를 바꿀 때: `crawler.py`, `bbs_parser.py`, `common.py`
- Notion 속성이나 동기화 규칙을 바꿀 때: `sync.py`, `notion_client.py`, `settings.py`
- 본문 해시나 첨부 판정 기준을 바꿀 때: `utils.py`, `common.py`, `crawler.py`
- 환경 변수나 기본 설정을 바꿀 때: `settings.py`, 루트 `README.md`, `.env.example`

특히 첨부 처리와 본문 미디어 재사용은 서로 연결돼 있으므로, 한쪽만 보고 수정하지 않는 편이 안전하다.

## 점검 경로

현재 별도의 자동 테스트 스위트는 없고, 아래 점검 경로를 기본 검증으로 사용한다.

```bash
python -m py_compile main.py scripts/*.py
ATTACHMENT_SELFTEST=1 python main.py
```

GitHub Actions 워크플로도 본 실행 전에 같은 수준의 최소 검증을 먼저 수행한다.
