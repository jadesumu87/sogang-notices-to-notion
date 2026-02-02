# Sogang Notices to Notion

서강대학교 장학/학사 공지를 노션 데이터베이스로 동기화하여 새 공지 등록 시 자동 알림을 받도록 합니다.

## 개요
- 목적: 기존 공지 시스템의 알림 부재 문제 해결
- 대상: 개인용
- 범위: 공지 수집, 상세/첨부 파싱, Notion 페이지 생성·업데이트
    - 제외: UI, 멀티테넌트 운영
    - 알림은 노션 데이터베이스 자동화에 의존

## 데모
- 실행 로그(실제):
```text
2026-02-02 21:20:13 [INFO] 환경: Python=3.13.6, Playwright=설치됨
2026-02-02 21:20:13 [INFO] 환경: BROWSER=chromium, HEADLESS=1, BBS_CONFIG_FKS=141,2, SYNC_MODE=overwrite
2026-02-02 21:20:13 [INFO] 환경: BBS_CONFIG_CLASSIFY=141:장학공지, 2:학사공지
2026-02-02 21:20:13 [INFO] 환경: NOTION_VERSION=2022-06-28, NOTION_UPLOAD_FILES=1
2026-02-02 21:20:13 [INFO] 비TOP 포함 모드: 최대 페이지=3
2026-02-02 21:20:13 [INFO] 수집 설정: bbsConfigFk=141, 분류=장학공지
2026-02-02 21:20:13 [INFO] 페이지 로드 시작(API): 1
2026-02-02 21:20:13 [INFO] 페이지 1 항목 수(API): 20
... (continued)
```

## 기술 스택
- 언어: Python 3.11+
- 프레임워크: 없음(CLI 스크립트)
- 인프라/도구: Playwright, Notion API, GitHub Actions

## 프로젝트 구조
```text
.
├─ .github/
│  └─ workflows/
│     └─ crawler.yml
├─ main.py
├─ requirements.txt
├─ README.md
└─ README.ko.md
```

## 빠른 시작

### 요구 사항

* Python 3.11+
* Playwright 브라우저(`python -m playwright install --with-deps`)
* 선택: 가상환경

### 선택: 가상환경

```bash
python -m venv .venv
source .venv/bin/activate
```

### 설치

```bash
pip install -r requirements.txt
python -m playwright install --with-deps
```

### 설정

필수 변수를 담은 `.env` 파일을 생성합니다.

```bash
cat > .env <<'EOF'
NOTION_TOKEN=your_notion_token
NOTION_DB_ID=your_database_id
EOF
```

### 실행

```bash
python main.py
```

## 사용법

* 예시:

```bash
HTML_PATH=sample.html python main.py
```

## 설정

* 환경 변수: `.env`

### 필수

```bash
NOTION_TOKEN=your_notion_token
NOTION_DB_ID=your_database_id
```

참고:
- 기존 title 속성의 이름이 `공지사항`으로 변경됩니다.
- `공지사항`이라는 이름의 비-title 속성이 있거나 title 속성이 없으면 실행이 실패합니다.

### 자주 쓰는 옵션

```bash
BBS_CONFIG_FKS=141,2
BBS_CONFIG_CLASSIFY=141:Scholarship,2:Academic
BBS_CONFIG_LIST_URLS=141:https://www.sogang.ac.kr/ko/scholarship-notice,2:https://www.sogang.ac.kr/ko/academic-support/notices
BBS_PAGE_SIZE=20
INCLUDE_NON_TOP=1
NON_TOP_MAX_PAGES=3
SYNC_MODE=overwrite
NOTION_UPLOAD_FILES=1
BROWSER=chromium
HEADLESS=1
```

### 고급

```bash
NOTION_API_VERSION=2022-06-28
BBS_CONFIG_FK=141
NOTION_DEDUPE_ON_START=1
HTML_PATH=
ATTACHMENT_ALLOWED_DOMAINS=sogang.ac.kr
ATTACHMENT_MAX_COUNT=15
ATTACHMENT_SELFTEST=
USER_AGENT=Mozilla/5.0 (compatible; ScholarshipCrawler/1.0)
```

## 테스트

```bash
# 자동화된 테스트 없음
```

## 배포

* 배포 방식: GitHub Actions
* 참고:
    - Secrets에 `NOTION_TOKEN`, `NOTION_DB_ID`를 등록해야 합니다.
    - `.github/workflows/crawler.yml`이 매시 실행됩니다.

## 기여

* 이슈/PR 규칙: 이슈와 PR은 환영하지만 유지관리는 선택적으로 진행되며 반영이 보장되지 않습니다.
* 커밋 메시지: `type(scope): subject` (Conventional Commits)

## 라이선스

* MIT

## 보안

* 취약점 제보: GitHub Security Advisories의 비공개 제보(Private Vulnerability Reporting)를 사용합니다.
