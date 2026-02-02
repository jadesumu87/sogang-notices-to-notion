# Sogang Notices to Notion

Syncs Sogang University scholarship and academic notices into a Notion database.

## Overview
- Purpose: Keep scholarship and academic notices centralized and searchable in Notion.
- Audience: Personal use by the maintainer; adaptable for students or admins who want a Notion sync.
- Scope: Crawl notice boards, parse detail pages and attachments, and create or update Notion pages. Out of scope: UI, notifications, and multi-tenant management.

## Demo
- Run output (actual):
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

## Tech Stack
- Language: Python 3.11+
- Framework: None (CLI script)
- Infra/Tools: Playwright, Notion API, GitHub Actions

## Project Structure
```text
.
├─ .github/
│  └─ workflows/
│     └─ crawler.yml
├─ main.py
├─ requirements.txt
└─ README.md
```

## Quick Start

### Requirements

* Python 3.11+
* Playwright browsers (`python -m playwright install --with-deps`)
* Optional: virtual environment

### Optional: Virtual Environment

```bash
python -m venv .venv
source .venv/bin/activate
```

### Install

```bash
pip install -r requirements.txt
python -m playwright install --with-deps
```

### Configure

Create a `.env` file with the required variables.

```bash
cat > .env <<'EOF'
NOTION_TOKEN=your_notion_token
NOTION_DB_ID=your_database_id
EOF
```

### Run

```bash
python main.py
```

## Usage

* Example:

```bash
HTML_PATH=sample.html python main.py
```

## Configuration

* Environment variables: `.env`

### Required

```bash
NOTION_TOKEN=your_notion_token
NOTION_DB_ID=your_database_id
```

Notes:
- The script will rename the existing title property to `공지사항` if needed.
- If a non-title property already uses the name `공지사항`, or if no title property exists, the run will fail.

### Common optional

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

### Advanced

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

## Testing

```bash
# no automated tests yet
```

## Deployment

* How to deploy: GitHub Actions
* Notes: Configure repository secrets `NOTION_TOKEN` and `NOTION_DB_ID`. The workflow in `.github/workflows/crawler.yml` runs every hour.

## Roadmap

* [ ] v1: Reliable scholarship and academic notice sync with attachments
* [ ] v2: Add filtering rules, richer parsing, and optional notifications

## Contributing

* Issues/PR rules: Issues and PRs are welcome, but maintainership is selective and not guaranteed.
* Commit message: `type(scope): subject` (Conventional Commits)

## License

* MIT

## Security

* Vulnerability reporting: Please use GitHub Security Advisories (Private Vulnerability Reporting).
