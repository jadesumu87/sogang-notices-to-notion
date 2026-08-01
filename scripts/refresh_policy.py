import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from utils import normalize_title_key, parse_compact_datetime


RECENT_REFRESH_INTERVAL = timedelta(hours=1)
MONTH_REFRESH_INTERVAL = timedelta(days=1)
ARCHIVE_REFRESH_INTERVAL = timedelta(days=7)
RECENT_AGE_LIMIT = timedelta(days=8)
MONTH_AGE_LIMIT = timedelta(days=31)


def parse_utc_datetime(value: object) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(
            str(value or "").replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_notice_observation(
    notice_id: str,
    title: object,
    published_at: object,
    top: bool,
) -> dict[str, str]:
    normalized_published_at = parse_compact_datetime(
        str(published_at or "")
    )
    fingerprint_payload = {
        "notice_id": str(notice_id).strip(),
        "published_at": normalized_published_at
        or str(published_at or "").strip(),
        "title": normalize_title_key(str(title or "")),
        "top": bool(top),
    }
    observation = {
        "fingerprint": hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
    }
    if normalized_published_at:
        observation["published_at"] = normalized_published_at
    return observation


def refresh_interval_for_notice(
    published_at: object,
    now: datetime,
) -> timedelta:
    published = parse_utc_datetime(published_at)
    if published is None:
        return ARCHIVE_REFRESH_INTERVAL
    age = max(timedelta(0), now.astimezone(timezone.utc) - published)
    if age < RECENT_AGE_LIMIT:
        return RECENT_REFRESH_INTERVAL
    if age < MONTH_AGE_LIMIT:
        return MONTH_REFRESH_INTERVAL
    return ARCHIVE_REFRESH_INTERVAL


def initial_archive_refresh_offset_days(notice_id: str) -> int:
    return int(
        hashlib.sha256(str(notice_id).encode("utf-8")).hexdigest()[:8],
        16,
    ) % 7


def notice_refresh_due(
    notice_id: str,
    current: dict[str, Any],
    previous: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> bool:
    current_now = (now or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    )
    previous = previous or {}
    previous_fingerprint = str(previous.get("fingerprint") or "")
    current_fingerprint = str(current.get("fingerprint") or "")
    if (
        previous_fingerprint
        and current_fingerprint
        and previous_fingerprint != current_fingerprint
    ):
        return True
    interval = refresh_interval_for_notice(
        current.get("published_at") or previous.get("published_at"),
        current_now,
    )
    last_detail_at = parse_utc_datetime(previous.get("last_detail_at"))
    if last_detail_at is not None:
        return current_now - last_detail_at >= interval
    if interval < ARCHIVE_REFRESH_INTERVAL:
        return True
    refresh_offset = initial_archive_refresh_offset_days(notice_id)
    first_seen_at = parse_utc_datetime(previous.get("first_seen_at"))
    if first_seen_at is not None:
        return current_now >= first_seen_at + timedelta(
            days=refresh_offset
        )
    return refresh_offset == 0


def select_due_notice_ids(
    source_state: dict[str, Any],
    known_ids: set[str],
    now: Optional[datetime] = None,
) -> list[str]:
    raw_state = source_state.get("notice_refresh_state", {})
    if not isinstance(raw_state, dict):
        return []
    current_now = now or datetime.now(timezone.utc)
    due: list[str] = []
    for notice_id in sorted(known_ids, key=lambda value: (len(value), value)):
        observation = raw_state.get(notice_id)
        if not isinstance(observation, dict):
            continue
        if notice_refresh_due(
            notice_id,
            observation,
            observation,
            current_now,
        ):
            due.append(notice_id)
    return due
