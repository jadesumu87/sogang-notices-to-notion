# Contributing

버그 보고와 pull request를 환영한다. 제안의 검토와 반영 일정은 변경 범위와 유지보수 상황에 따라 달라질 수 있다.

## 이슈

- 재현 가능한 버그는 실행 환경, 재현 단계, 기대 동작과 실제 동작을 함께 적는다.
- 로그에는 토큰, 데이터베이스 ID, 개인정보 또는 전체 외부 응답 본문을 포함하지 않는다.
- 보안 취약점은 공개 이슈에 올리지 않고 `SECURITY.md`의 비공개 제보 방법을 사용한다.

## Pull request

- 변경은 검토 가능한 단위로 나누고 이유와 영향 범위를 설명한다.
- Notion을 변경하는 코드는 로컬 드라이런과 쓰기 권한 검사를 유지한다.
- 본문 동기화 변경은 최상위 단일 `quote`, 새 본문 확인 후 교체와 수동 블록 보존 규칙을 유지한다.
- 코드 변경에는 관련 테스트를 추가하거나 현재 테스트로 충분한 이유를 적는다.
- 실제 토큰, `.env` 또는 `.runtime/` 산출물을 커밋하지 않는다.

## 로컬 검증

Python `3.13.14`와 잠금 파일로 의존성을 설치한 뒤 다음 명령을 실행한다.

```bash
python -m compileall -q main.py scripts .github/scripts
ruff check .
python -m mypy --strict main.py
MYPYPATH=scripts python -m mypy --strict scripts .github/scripts
REQUIRE_BROWSER_TESTS=1 python -m coverage run -m unittest discover -s tests -p "test_*.py" -v
python -m coverage report --fail-under=60
```

잠금 파일, 커버리지와 워크플로 검사를 포함한 전체 검증은 `README.md`와 `.github/workflows/ci.yml`을 기준으로 한다.

## 커밋 메시지

`type: 한국어 요약` 형식을 권장한다.

```text
fix: 드라이런 권한 검사 수정
```
