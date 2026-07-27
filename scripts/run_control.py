import os
import signal
import time
from types import FrameType
from typing import Optional

_stop_requested = False
_deadline_monotonic: Optional[float] = None
MAX_INTERRUPTIBLE_SLEEP_SECONDS = 60.0
SLEEP_POLL_SECONDS = 0.25


def _handle_stop_signal(_signum: int, _frame: Optional[FrameType]) -> None:
    global _stop_requested
    _stop_requested = True


def install_run_control() -> None:
    global _deadline_monotonic, _stop_requested
    _stop_requested = False
    raw = os.environ.get("INTERNAL_DEADLINE_SECONDS", "1200").strip()
    try:
        effective_seconds = float(max(60, int(raw)))
    except ValueError:
        effective_seconds = 1200.0
    _deadline_monotonic = time.monotonic() + effective_seconds
    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)


def check_run_control() -> None:
    if _stop_requested:
        raise RuntimeError("안전 종료 신호가 요청되었습니다")
    if (
        _deadline_monotonic is not None
        and time.monotonic() >= _deadline_monotonic
    ):
        raise RuntimeError("내부 실행 마감시간에 도달했습니다")


def remaining_run_seconds() -> Optional[float]:
    if _deadline_monotonic is None:
        return None
    return max(0.0, _deadline_monotonic - time.monotonic())


def get_destination_state_reserve_seconds() -> float:
    raw = os.environ.get(
        "DESTINATION_STATE_RESERVE_SECONDS",
        "300",
    ).strip()
    try:
        value = float(raw)
    except ValueError:
        return 300.0
    return min(1200.0, max(60.0, value))


def require_destination_state_reserve() -> None:
    check_run_control()
    remaining = remaining_run_seconds()
    reserve = get_destination_state_reserve_seconds()
    if remaining is not None and remaining < reserve:
        raise RuntimeError(
            "목적지 동기화와 상태 커밋을 위한 실행 시간이 부족합니다"
        )


def bounded_sleep_seconds(
    seconds: float,
    maximum_seconds: float = MAX_INTERRUPTIBLE_SLEEP_SECONDS,
) -> float:
    check_run_control()
    delay = min(max(0.0, seconds), max(0.0, maximum_seconds))
    remaining = remaining_run_seconds()
    if remaining is not None:
        delay = min(delay, remaining)
    return delay


def sleep_with_run_control(
    seconds: float,
    maximum_seconds: float = MAX_INTERRUPTIBLE_SLEEP_SECONDS,
) -> None:
    delay = bounded_sleep_seconds(seconds, maximum_seconds)
    end_at = time.monotonic() + delay
    while True:
        check_run_control()
        remaining = end_at - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(SLEEP_POLL_SECONDS, remaining))
    check_run_control()
