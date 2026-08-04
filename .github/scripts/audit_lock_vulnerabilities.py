from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from verify_lock import parse_requirements

MAX_RESPONSE_BYTES = 2_000_000
FETCH_RETRY_DELAYS_SECONDS = (1.0, 2.0, 4.0)
RETRYABLE_HTTP_STATUS_CODES = {408, 425, 429, 500, 502, 503, 504}


def is_retryable_fetch_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in RETRYABLE_HTTP_STATUS_CODES
    return isinstance(exc, (TimeoutError, urllib.error.URLError))


def fetch_release(name: str, version: str) -> dict[str, Any]:
    encoded_name = urllib.parse.quote(name, safe="")
    encoded_version = urllib.parse.quote(version, safe="")
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{encoded_name}/{encoded_version}/json",
        headers={
            "Accept": "application/json",
            "User-Agent": "sogang-notices-lock-audit",
        },
    )
    for attempt in range(len(FETCH_RETRY_DELAYS_SECONDS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            break
        except (TimeoutError, urllib.error.URLError) as exc:
            if (
                not is_retryable_fetch_error(exc)
                or attempt == len(FETCH_RETRY_DELAYS_SECONDS)
            ):
                raise
            time.sleep(FETCH_RETRY_DELAYS_SECONDS[attempt])
    if len(body) > MAX_RESPONSE_BYTES:
        raise RuntimeError("PyPI 취약점 응답이 허용 크기를 넘었습니다.")
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise RuntimeError("PyPI 취약점 응답 형식이 올바르지 않습니다.")
    return payload


def locked_releases(paths: list[Path]) -> list[tuple[str, str]]:
    releases: set[tuple[str, str]] = set()
    for path in paths:
        packages = parse_requirements(
            path.read_text(encoding="utf-8"),
            require_hashes=True,
        )
        releases.update(packages.items())
    return sorted(releases)


def vulnerability_ids(payload: dict[str, Any]) -> list[str]:
    vulnerabilities = payload.get("vulnerabilities")
    if not isinstance(vulnerabilities, list):
        raise RuntimeError("PyPI 취약점 목록 형식이 올바르지 않습니다.")
    identifiers: list[str] = []
    for vulnerability in vulnerabilities:
        if not isinstance(vulnerability, dict):
            raise RuntimeError("PyPI 취약점 항목 형식이 올바르지 않습니다.")
        withdrawn = vulnerability.get("withdrawn")
        if withdrawn is not None:
            if not isinstance(withdrawn, str) or not withdrawn.strip():
                raise RuntimeError("PyPI 취약점 철회 시각 형식이 올바르지 않습니다.")
            continue
        identifier = str(vulnerability.get("id") or "").strip()
        if not identifier:
            raise RuntimeError("PyPI 취약점 식별자가 없습니다.")
        identifiers.append(identifier)
    return sorted(set(identifiers))


def audit_releases(
    releases: list[tuple[str, str]],
    fetcher: Callable[[str, str], dict[str, Any]] = fetch_release,
) -> list[str]:
    findings: list[str] = []
    for name, version in releases:
        payload = fetcher(name, version)
        info = payload.get("info")
        if not isinstance(info, dict):
            raise RuntimeError(f"{name}=={version}: PyPI 패키지 정보가 없습니다.")
        returned_version = str(info.get("version") or "")
        if returned_version != version:
            raise RuntimeError(
                f"{name}=={version}: PyPI 응답 버전이 일치하지 않습니다."
            )
        for identifier in vulnerability_ids(payload):
            findings.append(f"{name}=={version}: {identifier}")
    return findings


def main(argv: list[str]) -> int:
    if not argv:
        print(
            "사용법: audit_lock_vulnerabilities.py LOCK [LOCK ...]",
            file=sys.stderr,
        )
        return 2
    try:
        findings = audit_releases(locked_releases([Path(value) for value in argv]))
    except (
        OSError,
        RuntimeError,
        ValueError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ) as exc:
        print(
            f"잠금 파일 취약점 조회 실패: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    if findings:
        for finding in findings:
            print(finding, file=sys.stderr)
        return 1
    print("잠금된 의존성에서 알려진 취약점이 발견되지 않았습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
