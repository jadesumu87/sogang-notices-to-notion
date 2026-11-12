"""Playwright Chromium을 내려받고 실제로 실행되는지 확인한다.

GitHub 러너에는 Chromium 실행에 필요한 시스템 라이브러리가 이미 있어서,
브라우저만 내려받아도 실행된다. Ubuntu 저장소는 실행을 확인하지 못했을 때만
쓴다. 저장소가 몇 시간씩 응답하지 않아도 실행 전체가 멈추지 않게 하기 위해서다.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Sequence

BROWSER = "chromium"
DOWNLOAD_RETRY_DELAYS_SECONDS = (15.0, 45.0)
SYSTEM_DEPENDENCY_RETRY_DELAYS_SECONDS = (60.0,)
LAUNCH_CHECK_TIMEOUT_SECONDS = 180
PROBE_TEXT = "서강 공지"
LAUNCH_CHECK_CODE = """
import sys
from playwright.sync_api import sync_playwright

expected = sys.argv[1]
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page()
        page.set_content("<main><p id='probe'></p></main>")
        page.evaluate(
            "text => { document.getElementById('probe').textContent = text; }",
            expected,
        )
        text = page.inner_text("#probe")
    finally:
        browser.close()
if text != expected:
    raise SystemExit("Chromium 렌더링 결과가 예상과 다릅니다")
"""

CommandRunner = Callable[[Sequence[str]], int]
Sleeper = Callable[[float], None]
LaunchCheck = Callable[[], bool]


def run_command(command: Sequence[str]) -> int:
    print("실행: " + " ".join(command), flush=True)
    return subprocess.run(list(command), check=False).returncode


def run_with_retries(
    command: Sequence[str],
    retry_delays: Sequence[float],
    runner: CommandRunner,
    sleep: Sleeper,
) -> bool:
    attempts = len(retry_delays) + 1
    for attempt in range(1, attempts + 1):
        if runner(command) == 0:
            return True
        if attempt < attempts:
            delay = retry_delays[attempt - 1]
            print(
                f"실패했습니다({attempt}/{attempts}). "
                f"{delay:.0f}초 뒤 다시 시도합니다.",
                flush=True,
            )
            sleep(delay)
    return False


def browser_launches() -> bool:
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", LAUNCH_CHECK_CODE, PROBE_TEXT],
            check=False,
            timeout=LAUNCH_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"Chromium 실행 확인이 {LAUNCH_CHECK_TIMEOUT_SECONDS}초 안에 "
            "끝나지 않았습니다.",
            flush=True,
        )
        return False
    return completed.returncode == 0


def report_failure(message: str, optional: bool) -> int:
    if optional:
        print(
            f"::warning::{message}. "
            "크롤러는 브라우저 없이 API와 HTTP 수집으로 계속합니다.",
            flush=True,
        )
        return 0
    print(f"::error::{message}.", flush=True)
    return 1


def install_browser(
    optional: bool,
    runner: CommandRunner = run_command,
    sleep: Sleeper = time.sleep,
    launch_check: LaunchCheck = browser_launches,
) -> int:
    playwright = [sys.executable, "-m", "playwright"]
    if not run_with_retries(
        [*playwright, "install", BROWSER],
        DOWNLOAD_RETRY_DELAYS_SECONDS,
        runner,
        sleep,
    ):
        return report_failure("Playwright Chromium을 내려받지 못했습니다", optional)
    if launch_check():
        print("Chromium 실행을 확인했습니다.", flush=True)
        return 0
    print(
        "Chromium이 실행되지 않아 시스템 의존성을 설치합니다.",
        flush=True,
    )
    if not run_with_retries(
        [*playwright, "install-deps", BROWSER],
        SYSTEM_DEPENDENCY_RETRY_DELAYS_SECONDS,
        runner,
        sleep,
    ):
        return report_failure(
            "Chromium 시스템 의존성을 설치하지 못했습니다",
            optional,
        )
    if launch_check():
        print(
            "시스템 의존성을 설치한 뒤 Chromium 실행을 확인했습니다.",
            flush=True,
        )
        return 0
    return report_failure(
        "시스템 의존성을 설치한 뒤에도 Chromium이 실행되지 않습니다",
        optional,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Playwright Chromium을 내려받고 실행되는지 확인합니다.",
    )
    parser.add_argument(
        "--optional",
        action="store_true",
        help="브라우저를 준비하지 못해도 경고만 남기고 성공으로 끝냅니다.",
    )
    args = parser.parse_args(argv)
    return install_browser(optional=bool(args.optional))


if __name__ == "__main__":
    raise SystemExit(main())
