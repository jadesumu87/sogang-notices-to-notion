import hashlib
import json
import math
import os
import re
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from log import LOGGER, redact_sensitive_urls
from models import (
    CrawlReport,
    DestinationConsistencyError,
    FailureCategory,
    LocalConfigurationError,
    RunRecord,
    SyncCounters,
    utc_now_iso,
)


STATE_SCHEMA_VERSION = 2
PUBLIC_CACHE_SOURCE_FIELDS = frozenset(
    {
        "backfill_active",
        "backfill_anchor_ids",
        "backfill_resume_page",
        "backfill_started_at",
        "classification",
        "detail_refresh_cursor_id",
        "empty_confirmation_pending",
        "empty_last_logical_run_id",
        "empty_last_observed_at",
        "empty_last_run_id",
        "empty_observation_count",
        "fallback_circuit_open_until",
        "fallback_consecutive_failures",
        "last_attempt_at",
        "last_coverage_reconcile_at",
        "last_fallback_failure_at",
        "last_full_reconcile_at",
        "last_item_count",
        "last_reconcile_attempt_at",
        "last_success_at",
        "last_top_observed_ids",
        "method",
        "observed_ids",
        "pending_notice_ids",
        "source_circuit_open_until",
        "status",
        "top_absence_counts",
        "top_absence_last_logical_run_id",
        "top_absence_last_observed_at",
        "top_absence_last_run_id",
    }
)
PUBLIC_CACHE_RUN_FIELDS = frozenset(
    {
        "execution_id",
        "run_attempt",
        "run_id",
    }
)
PUBLIC_CACHE_SHRINK_FIELDS = frozenset(
    {
        "candidate_id",
        "last_observed_at",
        "last_observed_logical_run_id",
        "last_observed_run_id",
        "observations",
        "reasons",
    }
)
PUBLIC_CACHE_DESTINATION_HOLD_FIELDS = frozenset(
    {
        "candidate_id",
        "last_observed_at",
        "last_observed_logical_run_id",
        "last_observed_run_id",
        "observations",
        "reason",
    }
)
PUBLIC_CACHE_TOP_LEVEL_FIELDS = frozenset(
    {
        "consecutive_failures",
        "last_attempt_at",
        "last_coverage_reconcile_at",
        "last_full_reconcile_at",
        "last_partial_success_at",
        "last_success_at",
    }
)
ACTIVE_INCIDENT_FIELDS = frozenset(
    {
        "count",
        "failure_signaled_at",
        "fingerprint",
        "first_seen_at",
        "occurrence_id",
    }
)
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
INCIDENT_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
INCIDENT_DYNAMIC_TOKEN_PATTERN = re.compile(
    r"\b(?:[0-9a-f]{8,}|\d+)\b",
    re.IGNORECASE,
)
INCIDENT_HTTP_STATUS_PATTERN = re.compile(
    r"(?i)(?:(?<![a-z0-9])http[_ :=-]*([1-5]\d{2})\b|"
    r"(?<![a-z0-9])status(?:_code)?[_ :=-]*([1-5]\d{2})\b)"
)


class RunStateIntegrityError(RuntimeError):
    pass


def get_max_observed_ids() -> int:
    raw = os.environ.get("RUN_STATE_MAX_OBSERVED_IDS", "50000").strip()
    try:
        value = int(raw)
    except ValueError:
        return 50000
    return min(50000, max(100, value))


def get_fallback_failure_threshold() -> int:
    raw = os.environ.get("FALLBACK_CIRCUIT_FAILURE_THRESHOLD", "3").strip()
    try:
        value = int(raw)
    except ValueError:
        return 3
    return min(20, max(1, value))


def get_fallback_cooldown_seconds() -> int:
    raw = os.environ.get("FALLBACK_CIRCUIT_COOLDOWN_SECONDS", "10800").strip()
    try:
        value = int(raw)
    except ValueError:
        return 10800
    return min(86400, max(60, value))


def get_source_block_cooldown_seconds() -> int:
    raw = os.environ.get("SOURCE_BLOCK_COOLDOWN_SECONDS", "21600").strip()
    try:
        value = int(raw)
    except ValueError:
        return 21600
    return min(604800, max(300, value))


def get_failure_repeat_seconds() -> int:
    raw = os.environ.get("FAILURE_REPEAT_SECONDS", "21600").strip()
    try:
        value = int(raw)
    except ValueError:
        return 21600
    return min(604800, max(0, value))


def safe_nonnegative_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed_value = float(str(value))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed_value):
        return default
    return max(0, math.ceil(parsed_value))


def state_checksum(payload: dict[str, Any]) -> str:
    unsigned = {
        key: value
        for key, value in payload.items()
        if key != "state_checksum"
    }
    encoded = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_iso_datetime(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def default_run_state() -> dict[str, Any]:
    state: dict[str, Any] = {
        "schema_version": STATE_SCHEMA_VERSION,
        "last_attempt_at": None,
        "last_success_at": None,
        "last_full_reconcile_at": None,
        "last_coverage_reconcile_at": None,
        "last_partial_success_at": None,
        "consecutive_failures": 0,
        "sources": {},
        "last_incident": {},
        "active_incidents": {},
        "runs": [],
        "shrink_candidates": {},
        "destination_holds": {},
    }
    state["state_checksum"] = state_checksum(state)
    return state


def project_mapping(
    value: object,
    allowed_fields: frozenset[str],
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: deepcopy(field_value)
        for key, field_value in value.items()
        if key in allowed_fields
    }


def normalize_active_incident(
    fingerprint: str,
    value: object,
) -> dict[str, Any]:
    if not fingerprint or not isinstance(value, dict):
        raise RunStateIntegrityError("활성 장애 형식이 올바르지 않습니다")
    stored_fingerprint = str(value.get("fingerprint") or "")
    if stored_fingerprint != fingerprint:
        raise RunStateIntegrityError("활성 장애 형식이 올바르지 않습니다")
    first_seen_at = str(value.get("first_seen_at") or "")
    if not first_seen_at or parse_iso_datetime(first_seen_at) is None:
        raise RunStateIntegrityError("활성 장애 최초 시각이 올바르지 않습니다")
    raw_count = value.get("count", 1)
    if isinstance(raw_count, bool) or not isinstance(raw_count, int):
        raise RunStateIntegrityError("활성 장애 횟수가 올바르지 않습니다")
    count = max(1, raw_count)
    occurrence_id = str(value.get("occurrence_id") or "")
    if not occurrence_id:
        occurrence_id = hashlib.sha256(
            f"{fingerprint}\0{first_seen_at}".encode("utf-8")
        ).hexdigest()[:32]
    if len(occurrence_id) > 128:
        raise RunStateIntegrityError("활성 장애 식별자가 너무 깁니다")
    failure_signaled_at = str(
        value.get("failure_signaled_at")
        or ""
    )
    if (
        failure_signaled_at
        and parse_iso_datetime(failure_signaled_at) is None
    ):
        raise RunStateIntegrityError("활성 장애 표시 시각이 올바르지 않습니다")
    normalized = {
        "fingerprint": fingerprint,
        "first_seen_at": first_seen_at,
        "count": count,
        "occurrence_id": occurrence_id,
    }
    if failure_signaled_at:
        normalized["failure_signaled_at"] = failure_signaled_at
    return normalized


def build_public_cache_state(state: dict[str, Any]) -> dict[str, Any]:
    validated = validate_run_state_payload(state)
    projected = default_run_state()
    for key in PUBLIC_CACHE_TOP_LEVEL_FIELDS:
        projected[key] = deepcopy(validated.get(key))
    projected["sources"] = {
        source_id: project_mapping(
            source_state,
            PUBLIC_CACHE_SOURCE_FIELDS,
        )
        for source_id, source_state in validated["sources"].items()
    }
    projected["runs"] = [
        project_mapping(run, PUBLIC_CACHE_RUN_FIELDS)
        for run in validated["runs"][-2:]
        if isinstance(run, dict)
    ]
    projected["shrink_candidates"] = {
        key: project_mapping(
            candidate,
            PUBLIC_CACHE_SHRINK_FIELDS,
        )
        for key, candidate in validated["shrink_candidates"].items()
        if isinstance(candidate, dict)
    }
    projected["destination_holds"] = {
        key: project_mapping(
            hold,
            PUBLIC_CACHE_DESTINATION_HOLD_FIELDS,
        )
        for key, hold in validated["destination_holds"].items()
        if isinstance(hold, dict)
    }
    projected["active_incidents"] = {
        fingerprint: project_mapping(
            incident,
            ACTIVE_INCIDENT_FIELDS,
        )
        for fingerprint, incident in validated["active_incidents"].items()
    }
    last_fingerprint = str(
        validated.get("last_incident", {}).get("fingerprint") or ""
    )
    projected["last_incident"] = deepcopy(
        projected["active_incidents"].get(last_fingerprint, {})
    )
    projected["state_checksum"] = state_checksum(projected)
    return validate_run_state_payload(projected)


def migrate_v1_run_state(payload: dict[str, Any]) -> dict[str, Any]:
    migrated = default_run_state()
    migrated["consecutive_failures"] = safe_nonnegative_int(
        payload.get("consecutive_failures")
    )
    raw_sources = payload.get("sources")
    if isinstance(raw_sources, dict) and len(raw_sources) <= 100:
        migrated["sources"] = {
            source_id: project_mapping(
                source_state,
                PUBLIC_CACHE_SOURCE_FIELDS
                - {
                    "last_coverage_reconcile_at",
                    "last_full_reconcile_at",
                },
            )
            for source_id, source_state in raw_sources.items()
            if isinstance(source_id, str)
            and source_id
            and isinstance(source_state, dict)
        }
    migrated["state_checksum"] = state_checksum(migrated)
    return validate_run_state_payload(migrated)


def is_run_state_required() -> bool:
    return os.environ.get("RUN_STATE_REQUIRED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def validate_run_state_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RunStateIntegrityError("실행 상태 최상위 값은 객체여야 합니다")
    if payload.get("schema_version") != STATE_SCHEMA_VERSION:
        raise RunStateIntegrityError(
            "실행 상태 스키마 버전이 현재 코드와 일치하지 않습니다"
        )
    checksum = str(payload.get("state_checksum") or "")
    if not checksum or checksum != state_checksum(payload):
        raise RunStateIntegrityError("실행 상태 체크섬이 일치하지 않습니다")
    state = default_run_state()
    state.update(
        {
            key: deepcopy(payload[key])
            for key in state
            if key in payload
        }
    )
    if not isinstance(state.get("sources"), dict):
        raise RunStateIntegrityError("실행 상태 sources가 객체가 아닙니다")
    if not isinstance(state.get("last_incident"), dict):
        raise RunStateIntegrityError("실행 상태 last_incident가 객체가 아닙니다")
    if not isinstance(state.get("active_incidents"), dict):
        raise RunStateIntegrityError("실행 상태 active_incidents가 객체가 아닙니다")
    if not isinstance(state.get("runs"), list):
        raise RunStateIntegrityError("실행 상태 runs가 배열이 아닙니다")
    if not isinstance(state.get("shrink_candidates"), dict):
        raise RunStateIntegrityError(
            "실행 상태 shrink_candidates가 객체가 아닙니다"
        )
    if not isinstance(state.get("destination_holds"), dict):
        raise RunStateIntegrityError(
            "실행 상태 destination_holds가 객체가 아닙니다"
        )
    if (
        len(state["runs"]) > 100
        or len(state["shrink_candidates"]) > 5000
        or len(state["destination_holds"]) > 5000
    ):
        raise RunStateIntegrityError("실행 상태 보존 한도를 초과했습니다")
    if len(state["sources"]) > 100:
        raise RunStateIntegrityError("실행 상태 출처 보존 한도를 초과했습니다")
    if len(state["active_incidents"]) > 100:
        raise RunStateIntegrityError("활성 장애 보존 한도를 초과했습니다")
    for hold_key, hold in state["destination_holds"].items():
        if (
            not isinstance(hold_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", hold_key) is None
            or not isinstance(hold, dict)
            or str(hold.get("reason") or "")
            not in {
                "destructive_change_confirmation",
                "pending_refresh",
            }
            or not isinstance(hold.get("observations"), int)
            or isinstance(hold.get("observations"), bool)
            or not 1 <= hold["observations"] <= 100
            or (
                str(hold.get("reason") or "")
                == "destructive_change_confirmation"
                and re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(hold.get("candidate_id") or ""),
                )
                is None
            )
            or (
                str(hold.get("reason") or "") == "pending_refresh"
                and bool(str(hold.get("candidate_id") or ""))
            )
            or not str(hold.get("last_observed_run_id") or "")
            or len(str(hold.get("last_observed_run_id") or "")) > 160
            or not str(hold.get("last_observed_logical_run_id") or "")
            or len(str(hold.get("last_observed_logical_run_id") or "")) > 128
            or parse_iso_datetime(
                str(hold.get("last_observed_at") or "")
            )
            is None
        ):
            raise RunStateIntegrityError(
                "실행 상태 목적지 안전 보류 형식이 올바르지 않습니다"
            )
    normalized_incidents: dict[str, dict[str, Any]] = {}
    for fingerprint, incident in state["active_incidents"].items():
        if not isinstance(fingerprint, str):
            raise RunStateIntegrityError("활성 장애 형식이 올바르지 않습니다")
        normalized_incidents[fingerprint] = normalize_active_incident(
            fingerprint,
            incident,
        )
    state["active_incidents"] = normalized_incidents
    legacy_incident = state.get("last_incident", {})
    legacy_fingerprint = str(legacy_incident.get("fingerprint") or "")
    if legacy_fingerprint and legacy_fingerprint not in state["active_incidents"]:
        state["active_incidents"][legacy_fingerprint] = normalize_active_incident(
            legacy_fingerprint,
            legacy_incident,
        )
    state["last_incident"] = (
        deepcopy(state["active_incidents"].get(legacy_fingerprint, {}))
        if legacy_fingerprint
        else {}
    )
    for source_id, source_state in state["sources"].items():
        if not isinstance(source_id, str) or not isinstance(source_state, dict):
            raise RunStateIntegrityError("실행 상태 출처 형식이 올바르지 않습니다")
        observed_ids = source_state.get("observed_ids", [])
        if not isinstance(observed_ids, list) or len(observed_ids) > 50000:
            raise RunStateIntegrityError(
                "실행 상태 관측 ID 형식 또는 보존 한도가 올바르지 않습니다"
            )
        resume_page = source_state.get("backfill_resume_page", 1)
        if (
            isinstance(resume_page, bool)
            or not isinstance(resume_page, int)
            or not 1 <= resume_page <= 1000000
        ):
            raise RunStateIntegrityError(
                "실행 상태 백필 재개 페이지가 올바르지 않습니다"
            )
        anchor_ids = source_state.get("backfill_anchor_ids", [])
        if (
            not isinstance(anchor_ids, list)
            or len(anchor_ids) > 100
            or any(not isinstance(value, str) for value in anchor_ids)
        ):
            raise RunStateIntegrityError(
                "실행 상태 백필 기준 ID가 올바르지 않습니다"
            )
        pending_notice_ids = source_state.get("pending_notice_ids", [])
        if (
            not isinstance(pending_notice_ids, list)
            or len(pending_notice_ids) > 1000
            or len(set(pending_notice_ids)) != len(pending_notice_ids)
            or any(
                not isinstance(value, str) or not value.strip()
                for value in pending_notice_ids
            )
        ):
            raise RunStateIntegrityError(
                "실행 상태 대기 공지 ID 형식 또는 보존 한도가 올바르지 않습니다"
            )
        for flag_name in (
            "backfill_active",
            "empty_confirmation_pending",
        ):
            if (
                flag_name in source_state
                and not isinstance(source_state[flag_name], bool)
            ):
                raise RunStateIntegrityError(
                    "실행 상태 출처 플래그가 올바르지 않습니다"
                )
        for timestamp_name in (
            "last_coverage_reconcile_at",
            "last_full_reconcile_at",
            "last_reconcile_attempt_at",
        ):
            timestamp = source_state.get(timestamp_name)
            if (
                timestamp is not None
                and (
                    not isinstance(timestamp, str)
                    or parse_iso_datetime(timestamp) is None
                )
            ):
                raise RunStateIntegrityError(
                    "실행 상태 출처 조정 시각이 올바르지 않습니다"
                )
    state["state_checksum"] = state_checksum(state)
    return state


def load_run_state(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if is_run_state_required():
            raise RunStateIntegrityError(
                f"필수 실행 상태 파일이 없습니다: {path}"
            ) from exc
        return default_run_state()
    except json.JSONDecodeError as exc:
        if not is_run_state_required():
            LOGGER.warning(
                "실행 상태 파일을 해석할 수 없어 전체 조정 상태로 시작합니다"
            )
            return default_run_state()
        raise RunStateIntegrityError(
            f"실행 상태 파일을 해석할 수 없습니다: {path}"
        ) from exc
    except OSError as exc:
        raise RunStateIntegrityError(
            f"실행 상태 파일을 읽을 수 없습니다: {path}"
        ) from exc
    if isinstance(payload, dict) and payload.get("schema_version") == 1:
        LOGGER.warning(
            "이전 실행 상태를 안전한 전체 조정 상태로 변환합니다"
        )
        return migrate_v1_run_state(payload)
    try:
        return validate_run_state_payload(payload)
    except RunStateIntegrityError:
        if is_run_state_required():
            raise
        LOGGER.warning(
            "실행 상태 무결성을 확인할 수 없어 전체 조정 상태로 시작합니다"
        )
        return default_run_state()


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def write_run_state_atomic(path: Path, state: dict[str, Any]) -> None:
    sealed = dict(state)
    sealed["schema_version"] = STATE_SCHEMA_VERSION
    sealed["state_checksum"] = state_checksum(sealed)
    write_json_atomic(path, sealed)
    state.clear()
    state.update(sealed)


def sanitize_incident_text(value: object, limit: int = 500) -> str:
    cleaned = redact_sensitive_urls(
        CONTROL_CHARACTERS.sub("", str(value or ""))
    )
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit]


def stable_incident_component(value: object, limit: int) -> str:
    text = sanitize_incident_text(value, limit).casefold()
    text = INCIDENT_URL_PATTERN.sub("<url>", text)
    return INCIDENT_DYNAMIC_TOKEN_PATTERN.sub("<value>", text)


def stable_report_error(value: object, limit: int = 160) -> str:
    text = sanitize_incident_text(value, limit).casefold()

    def preserve_status(match: re.Match[str]) -> str:
        status = match.group(1) or match.group(2) or ""
        encoded = "".join(
            chr(ord("a") + int(digit))
            for digit in status
        )
        return f"<http-status-{encoded}>"

    text = INCIDENT_HTTP_STATUS_PATTERN.sub(preserve_status, text)
    text = INCIDENT_URL_PATTERN.sub("<url>", text)
    return INCIDENT_DYNAMIC_TOKEN_PATTERN.sub("<value>", text)


def stable_exception_signature(exc: BaseException) -> dict[str, str]:
    return {
        "type": (
            f"{type(exc).__module__}.{type(exc).__qualname__}"
        ),
        "origin": str(getattr(exc, "failure_origin", "") or ""),
        "kind": str(getattr(exc, "failure_kind", "") or ""),
        "status_code": str(getattr(exc, "status_code", "") or ""),
        "code": str(getattr(exc, "notion_code", "") or ""),
        "reason": stable_incident_component(
            getattr(exc, "reason", ""),
            120,
        ),
        "message": stable_incident_component(exc, 240),
    }


def report_fingerprint(
    category: FailureCategory,
    title: str,
    report: Optional[CrawlReport] = None,
    exception: Optional[BaseException] = None,
) -> str:
    source_state = []
    issue_state = []
    if report is not None:
        source_state = [
            {
                "config_fk": result.source.config_fk,
                "status": result.status.value,
                "category": result.category.value,
                "error": stable_report_error(result.error, 160),
            }
            for result in report.sources
        ]
        issue_state = sorted(
            [
                {
                    "code": issue.code,
                    "source_config_fk": issue.source_config_fk or "",
                    "message": stable_report_error(issue.message, 240),
                }
                for issue in report.issues
                if issue.fatal
            ],
            key=lambda value: (
                value["source_config_fk"],
                value["code"],
                value["message"],
            ),
        )
    raw = json.dumps(
        {
            "category": category.value,
            "title": sanitize_incident_text(title, 160),
            "sources": source_state,
            "issues": issue_state,
            "exception": (
                stable_exception_signature(exception)
                if exception is not None
                else None
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def classify_exception(exc: BaseException) -> FailureCategory:
    if isinstance(exc, DestinationConsistencyError):
        return FailureCategory.DESTINATION_CONTRACT
    if isinstance(exc, LocalConfigurationError):
        if exc.failure_kind == "destination_auth":
            return FailureCategory.DESTINATION_AUTH
        if exc.failure_kind == "destination_contract":
            return FailureCategory.DESTINATION_CONTRACT
        return FailureCategory.INTERNAL
    origin = str(getattr(exc, "failure_origin", "") or "")
    failure_kind = str(getattr(exc, "failure_kind", "") or "")
    status_code = getattr(exc, "status_code", None)
    notion_code = str(getattr(exc, "notion_code", "") or "")
    message = str(exc).lower()
    if origin == "source":
        if failure_kind == "access_block":
            return FailureCategory.SECURITY_POLICY
        return FailureCategory.SOURCE_UPSTREAM
    if origin == "notion":
        if failure_kind == "contract":
            return FailureCategory.DESTINATION_CONTRACT
        if status_code == 429:
            return FailureCategory.DESTINATION_RATE_LIMIT
        if status_code in {401, 403}:
            return FailureCategory.DESTINATION_AUTH
        if (
            isinstance(status_code, int)
            and 400 <= status_code < 500
        ) or notion_code in {
            "validation_error",
            "object_not_found",
        }:
            return FailureCategory.DESTINATION_CONTRACT
        return FailureCategory.NOTION
    if "notion" in message and (
        status_code == 400
        or notion_code in {"validation_error", "object_not_found"}
    ):
        return FailureCategory.DESTINATION_CONTRACT
    if "notion" in message:
        return FailureCategory.NOTION
    if "security" in message or "blocked" in message or "차단" in message:
        return FailureCategory.SECURITY_POLICY
    if "timeout" in message or "urlopen" in message or "network" in message:
        return FailureCategory.NETWORK
    return FailureCategory.INTERNAL


def source_reconcile_due(
    state: dict[str, Any],
    config_fk: str,
    interval_hours: int,
) -> bool:
    sources = state.get("sources", {})
    if not isinstance(sources, dict) or config_fk not in sources:
        return True
    source_state = sources.get(config_fk)
    if not isinstance(source_state, dict):
        return True
    if source_state.get("empty_confirmation_pending"):
        return True
    if interval_hours <= 0:
        return True
    last_attempt = parse_iso_datetime(
        str(source_state.get("last_reconcile_attempt_at") or "")
    )
    if last_attempt is None and source_state.get("backfill_active"):
        last_attempt = parse_iso_datetime(
            str(
                source_state.get("last_success_at")
                or source_state.get("last_attempt_at")
                or source_state.get("backfill_started_at")
                or ""
            )
        )
    references = [
        value
        for value in (
            last_attempt,
            parse_iso_datetime(
                str(
                    source_state.get("last_coverage_reconcile_at")
                    or state.get("last_coverage_reconcile_at")
                    or state.get("last_full_reconcile_at")
                    or ""
                )
            ),
        )
        if value is not None
    ]
    last = max(references) if references else None
    if last is None:
        return True
    age_seconds = (datetime.now(timezone.utc) - last).total_seconds()
    return age_seconds >= interval_hours * 3600


def should_full_reconcile(
    state: dict[str, Any],
    interval_hours: int,
    config_fks: Optional[list[str]] = None,
) -> bool:
    source_ids = list(config_fks or state.get("sources", {}).keys())
    if not source_ids:
        return True
    return any(
        source_reconcile_due(state, config_fk, interval_hours)
        for config_fk in source_ids
    )


def known_ids_for_source(state: dict[str, Any], config_fk: str) -> set[str]:
    source = state.get("sources", {}).get(config_fk, {})
    if not isinstance(source, dict):
        return set()
    raw_ids = source.get("observed_ids", [])
    pending_ids = source.get("pending_notice_ids", [])
    if not isinstance(raw_ids, list) or not isinstance(pending_ids, list):
        return set()
    return {
        str(value)
        for value in [*raw_ids, *pending_ids]
        if str(value).strip()
    }


def run_execution_id(run: object) -> str:
    if not isinstance(run, dict):
        return ""
    explicit = str(run.get("execution_id") or "").strip()
    if explicit:
        return explicit
    run_id = str(run.get("run_id") or "").strip()
    run_attempt = str(run.get("run_attempt") or "").strip()
    if run_id and run_attempt:
        return f"{run_id}:{run_attempt}"
    return run_id


def latest_run_identities(
    state: dict[str, Any],
) -> tuple[str, str]:
    runs = state.get("runs", [])
    if not isinstance(runs, list) or not runs:
        return "", ""
    latest = runs[-1]
    if not isinstance(latest, dict):
        return "", ""
    return (
        run_execution_id(latest),
        str(latest.get("run_id") or "").strip(),
    )


def observation_follows_distinct_logical_run(
    observation: dict[str, Any],
    latest_execution_id: str,
    latest_logical_run_id: str,
    observation_execution_key: str,
    observation_logical_key: str,
    current_logical_run_id: str,
) -> bool:
    if (
        not latest_execution_id
        or str(observation.get(observation_execution_key) or "")
        != latest_execution_id
    ):
        return False
    if not current_logical_run_id:
        return True
    previous_logical_run_id = str(
        observation.get(observation_logical_key) or ""
    )
    return bool(
        previous_logical_run_id
        and previous_logical_run_id == latest_logical_run_id
        and previous_logical_run_id != current_logical_run_id
    )


def update_state_from_report(
    state: dict[str, Any],
    report: CrawlReport,
    full_reconcile: bool,
    applied_source_ids: Optional[set[str]] = None,
    counters: Optional[SyncCounters] = None,
    safety_deferred: bool = False,
) -> dict[str, Any]:
    now = utc_now_iso()
    state["last_attempt_at"] = now
    unsafe_sources = {
        issue.source_config_fk
        for issue in report.issues
        if issue.fatal and issue.source_config_fk
    }
    global_blocked = any(
        issue.fatal
        and issue.code in {"duplicate_source", "cross_source_url_collision"}
        for issue in report.issues
    )
    if applied_source_ids is None:
        applied_source_ids = {
            result.source.config_fk
            for result in report.sources
            if result.write_safe
            and result.source.config_fk not in unsafe_sources
            and not global_blocked
        }
    safe_count = 0
    max_observed_ids = get_max_observed_ids()
    latest_run_id, latest_logical_run_id = latest_run_identities(state)
    current_logical_run_id = (
        counters.observation_logical_run_id
        if counters is not None
        else ""
    )
    for result in report.sources:
        reconcile_requested = (
            full_reconcile
            if result.reconcile_requested is None
            else result.reconcile_requested
        )
        source_state = state["sources"].setdefault(result.source.config_fk, {})
        previous_last_attempt_at = source_state.get("last_attempt_at")
        previous_last_success_at = source_state.get("last_success_at")
        source_state["last_attempt_at"] = now
        source_state["status"] = result.status.value
        source_state["method"] = result.method
        if reconcile_requested:
            source_state["last_reconcile_attempt_at"] = now
        elif (
            source_state.get("backfill_active")
            and not source_state.get("last_reconcile_attempt_at")
        ):
            legacy_reference = parse_iso_datetime(
                str(
                    previous_last_success_at
                    or previous_last_attempt_at
                    or source_state.get("backfill_started_at")
                    or ""
                )
            )
            source_state["last_reconcile_attempt_at"] = (
                legacy_reference.isoformat()
                if legacy_reference is not None
                else now
            )
        if result.write_safe:
            source_state["fallback_consecutive_failures"] = 0
            source_state.pop("fallback_circuit_open_until", None)
            source_state.pop("last_fallback_failure_at", None)
            source_state.pop("source_circuit_open_until", None)
            source_state.pop("source_circuit_reason", None)
        if (
            result.write_safe
            and result.source.config_fk not in unsafe_sources
            and result.source.config_fk in applied_source_ids
        ):
            previous_ids = source_state.get("observed_ids", [])
            confirmed_empty = result.status.value == "confirmed_empty"
            consecutive_empty = bool(
                confirmed_empty
                and observation_follows_distinct_logical_run(
                    source_state,
                    latest_run_id,
                    latest_logical_run_id,
                    "empty_last_run_id",
                    "empty_last_logical_run_id",
                    current_logical_run_id,
                )
                and safe_nonnegative_int(
                    source_state.get("empty_observation_count")
                )
                >= 1
            )
            merged_ids = (
                list(result.observed_ids)
                if (
                    reconcile_requested
                    and result.full_snapshot
                    and (
                        not confirmed_empty
                        or consecutive_empty
                    )
                )
                else list(
                    dict.fromkeys(
                        [
                            *result.observed_ids,
                            *(
                                previous_ids
                                if isinstance(previous_ids, list)
                                else []
                            ),
                        ]
                    )
                )
            )
            if len(merged_ids) > max_observed_ids:
                raise RunStateIntegrityError(
                    "관측 ID가 실행 상태 보존 한도를 초과했습니다"
                )
            source_state.update(
                {
                    "classification": result.source.classification,
                    "last_success_at": now,
                    "observed_ids": merged_ids,
                    "error": "",
                }
            )
            if confirmed_empty:
                source_state["empty_observation_count"] = (
                    safe_nonnegative_int(
                        source_state.get("empty_observation_count")
                    )
                    + 1
                    if (
                        observation_follows_distinct_logical_run(
                            source_state,
                            latest_run_id,
                            latest_logical_run_id,
                            "empty_last_run_id",
                            "empty_last_logical_run_id",
                            current_logical_run_id,
                        )
                    )
                    else 1
                )
                source_state["empty_last_run_id"] = (
                    counters.observation_run_id
                    if counters is not None
                    else ""
                )
                source_state["empty_last_logical_run_id"] = (
                    current_logical_run_id
                )
                source_state["empty_last_observed_at"] = now
                if consecutive_empty:
                    source_state.pop(
                        "empty_confirmation_pending",
                        None,
                    )
                else:
                    source_state["empty_confirmation_pending"] = True
            else:
                source_state.pop("empty_observation_count", None)
                source_state.pop("empty_last_run_id", None)
                source_state.pop("empty_last_logical_run_id", None)
                source_state.pop("empty_last_observed_at", None)
                source_state.pop("empty_confirmation_pending", None)
            if reconcile_requested and result.coverage_complete:
                if not confirmed_empty or consecutive_empty:
                    source_state["last_item_count"] = (
                        result.observed_count
                    )
                    source_state["last_coverage_reconcile_at"] = now
                    if result.reconcile_complete:
                        source_state["last_full_reconcile_at"] = now
                source_state["backfill_active"] = False
                source_state.pop("backfill_started_at", None)
                source_state.pop("backfill_resume_page", None)
                source_state.pop("backfill_anchor_ids", None)
            elif result.termination_reason == "backfill_window":
                source_state["backfill_active"] = True
                source_state.setdefault("backfill_started_at", now)
                source_state["backfill_resume_page"] = (
                    result.backfill_resume_page
                )
                source_state["backfill_anchor_ids"] = (
                    result.backfill_anchor_ids[:100]
                )
            if reconcile_requested and result.refreshed_known_ids:
                source_state["detail_refresh_cursor_id"] = (
                    result.refreshed_known_ids[-1]
                )
            elif (
                reconcile_requested
                and result.coverage_complete
                and result.refresh_window_end_id
            ):
                source_state["detail_refresh_cursor_id"] = (
                    result.refresh_window_end_id
                )
            safe_count += 1
        else:
            source_state["error"] = sanitize_incident_text(result.error)
            if result.termination_reason == "resume_error":
                source_state.pop("backfill_resume_page", None)
                source_state.pop("backfill_anchor_ids", None)
            if (
                not result.write_safe
                and result.method in {
                    "fallback_http",
                    "fallback_playwright",
                }
            ):
                failures = (
                    safe_nonnegative_int(
                        source_state.get("fallback_consecutive_failures")
                    )
                    + 1
                )
                source_state["fallback_consecutive_failures"] = failures
                source_state["last_fallback_failure_at"] = now
                if failures >= get_fallback_failure_threshold():
                    source_state["fallback_circuit_open_until"] = (
                        datetime.now(timezone.utc)
                        + timedelta(seconds=get_fallback_cooldown_seconds())
                    ).replace(microsecond=0).isoformat()
            if (
                not result.write_safe
                and (
                    result.method == "api_source_circuit_open"
                    or result.category == FailureCategory.SECURITY_POLICY
                    and result.method not in {
                        "source_circuit_open",
                        "host_circuit_open",
                    }
                )
            ):
                retry_after_seconds = min(
                    604800,
                    max(
                        0,
                        safe_nonnegative_int(
                            result.retry_after_seconds
                        ),
                    ),
                )
                cooldown_seconds = max(
                    get_source_block_cooldown_seconds(),
                    retry_after_seconds,
                )
                source_state["source_circuit_open_until"] = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=cooldown_seconds)
                ).replace(microsecond=0).isoformat()
                source_state["source_circuit_reason"] = sanitize_incident_text(
                    result.error,
                    160,
                )
    if counters is not None:
        configured_source_ids = {
            result.source.config_fk for result in report.sources
        }
        pending_source_ids = (
            set(counters.unresolved_pending_notices)
            | set(counters.recovered_pending_notices)
        )
        unknown_pending_sources = (
            pending_source_ids - configured_source_ids
        )
        if unknown_pending_sources:
            raise DestinationConsistencyError(
                "설정에 없는 출처의 대기 복구 상태는 저장할 수 없습니다"
            )
        for source_id in sorted(pending_source_ids):
            source_state = state["sources"].setdefault(source_id, {})
            previous_pending_ids = source_state.get(
                "pending_notice_ids",
                [],
            )
            if not isinstance(previous_pending_ids, list):
                previous_pending_ids = []
            recovered_ids = set(
                counters.recovered_pending_notices.get(source_id, [])
            )
            unresolved_ids = set(
                counters.unresolved_pending_notices.get(source_id, [])
            )
            pending_ids = sorted(
                (
                    {
                        str(value)
                        for value in previous_pending_ids
                        if str(value).strip()
                    }
                    - recovered_ids
                )
                | unresolved_ids
            )
            if len(pending_ids) > 1000:
                raise RunStateIntegrityError(
                    "대기 공지 ID가 실행 상태 보존 한도를 초과했습니다"
                )
            if pending_ids:
                source_state["pending_notice_ids"] = pending_ids
            else:
                source_state.pop("pending_notice_ids", None)
        for source_id, missing_ids in (
            counters.top_absence_observations.items()
        ):
            source_state = state["sources"].setdefault(source_id, {})
            previous_counts = source_state.get(
                "top_absence_counts",
                {},
            )
            if not isinstance(previous_counts, dict):
                previous_counts = {}
            consecutive = bool(
                observation_follows_distinct_logical_run(
                    source_state,
                    latest_run_id,
                    latest_logical_run_id,
                    "top_absence_last_run_id",
                    "top_absence_last_logical_run_id",
                    current_logical_run_id,
                )
            )
            source_state["top_absence_counts"] = {
                notice_id: min(
                    100,
                    (
                        safe_nonnegative_int(
                            previous_counts.get(notice_id)
                        )
                        if consecutive
                        else 0
                    )
                    + 1,
                )
                for notice_id in missing_ids[:1000]
            }
            source_state["last_top_observed_ids"] = (
                counters.top_present_ids.get(source_id, [])[:1000]
            )
            source_state["top_absence_last_run_id"] = (
                counters.observation_run_id
            )
            source_state["top_absence_last_logical_run_id"] = (
                current_logical_run_id
            )
            source_state["top_absence_last_observed_at"] = now
        candidates = state.setdefault("shrink_candidates", {})
        if not isinstance(candidates, dict):
            candidates = {}
            state["shrink_candidates"] = candidates
        for key in counters.shrink_candidate_clears:
            candidates.pop(key, None)
        for key, observation in (
            counters.shrink_candidate_observations.items()
        ):
            previous = candidates.get(key, {})
            count = (
                safe_nonnegative_int(previous.get("observations"))
                + 1
                if (
                    isinstance(previous, dict)
                    and previous.get("candidate_id")
                    == observation.get("candidate_id")
                    and observation_follows_distinct_logical_run(
                        previous,
                        latest_run_id,
                        latest_logical_run_id,
                        "last_observed_run_id",
                        "last_observed_logical_run_id",
                        current_logical_run_id,
                    )
                )
                else 1
            )
            candidates[key] = {
                "candidate_id": str(
                    observation.get("candidate_id") or ""
                ),
                "reasons": list(observation.get("reasons") or [])[:10],
                "observations": min(100, count),
                "last_observed_at": now,
                "last_observed_run_id": counters.observation_run_id,
                "last_observed_logical_run_id": (
                    current_logical_run_id
                ),
            }
        if len(candidates) > 5000:
            ordered = sorted(
                candidates.items(),
                key=lambda pair: str(
                    pair[1].get("last_observed_at") or ""
                ),
                reverse=True,
            )
            state["shrink_candidates"] = dict(ordered[:5000])
        if counters.destination_hold_observations and (
            not counters.observation_run_id
            or not current_logical_run_id
        ):
            raise DestinationConsistencyError(
                "목적지 안전 보류의 실행 식별자가 누락되었습니다"
            )
        destination_holds: dict[str, dict[str, Any]] = {}
        previous_holds = state.get("destination_holds", {})
        if not isinstance(previous_holds, dict):
            raise DestinationConsistencyError(
                "목적지 안전 보류 상태를 신뢰할 수 없습니다"
            )
        for key, observation in (
            counters.destination_hold_observations.items()
        ):
            previous = previous_holds.get(key, {})
            reason = str(observation.get("reason") or "")
            candidate_id = str(
                observation.get("candidate_id") or ""
            )
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[0-9a-f]{64}", key) is None
                or reason
                not in {
                    "destructive_change_confirmation",
                    "pending_refresh",
                }
                or (
                    reason == "destructive_change_confirmation"
                    and re.fullmatch(r"[0-9a-f]{64}", candidate_id)
                    is None
                )
                or (
                    reason == "pending_refresh"
                    and bool(candidate_id)
                )
            ):
                raise DestinationConsistencyError(
                    "목적지 안전 보류 관측값을 신뢰할 수 없습니다"
                )
            same_condition = bool(
                isinstance(previous, dict)
                and str(previous.get("reason") or "") == reason
                and (
                    reason != "destructive_change_confirmation"
                    or (
                        bool(candidate_id)
                        and str(previous.get("candidate_id") or "")
                        == candidate_id
                    )
                )
            )
            consecutive = bool(
                same_condition
                and isinstance(previous, dict)
                and observation_follows_distinct_logical_run(
                    previous,
                    latest_run_id,
                    latest_logical_run_id,
                    "last_observed_run_id",
                    "last_observed_logical_run_id",
                    current_logical_run_id,
                )
            )
            destination_holds[key] = {
                "candidate_id": candidate_id,
                "reason": reason,
                "observations": min(
                    100,
                    (
                        safe_nonnegative_int(
                            previous.get("observations")
                        )
                        if consecutive
                        else 0
                    )
                    + 1,
                ),
                "last_observed_at": now,
                "last_observed_run_id": counters.observation_run_id,
                "last_observed_logical_run_id": current_logical_run_id,
            }
        if len(destination_holds) > 5000:
            raise DestinationConsistencyError(
                "목적지 안전 보류 상태가 보존 한도를 초과했습니다"
            )
        state["destination_holds"] = destination_holds
    source_ids = [
        result.source.config_fk
        for result in report.sources
    ]
    coverage_watermarks = [
        parse_iso_datetime(
            str(
                state["sources"]
                .get(source_id, {})
                .get("last_coverage_reconcile_at")
                or ""
            )
        )
        for source_id in source_ids
    ]
    if source_ids and all(coverage_watermarks):
        state["last_coverage_reconcile_at"] = min(
            watermark
            for watermark in coverage_watermarks
            if watermark is not None
        ).isoformat()
    full_watermarks = [
        parse_iso_datetime(
            str(
                state["sources"]
                .get(source_id, {})
                .get("last_full_reconcile_at")
                or ""
            )
        )
        for source_id in source_ids
    ]
    if source_ids and all(full_watermarks):
        state["last_full_reconcile_at"] = min(
            watermark
            for watermark in full_watermarks
            if watermark is not None
        ).isoformat()
    if safety_deferred:
        if safe_count:
            state["last_partial_success_at"] = now
    elif report.write_safe:
        state["last_success_at"] = now
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
        if safe_count:
            state["last_partial_success_at"] = now
    return state


def create_run_record(full_reconcile: bool, dry_run: bool) -> RunRecord:
    run_id = (
        os.environ.get("GITHUB_RUN_ID")
        or os.environ.get("CRAWLER_RUN_ID")
        or uuid.uuid4().hex
    )
    run_attempt = (
        os.environ.get("GITHUB_RUN_ATTEMPT")
        or os.environ.get("CRAWLER_RUN_ATTEMPT")
        or "1"
    )
    started_at = utc_now_iso()
    return RunRecord(
        run_id=str(run_id),
        scheduled_at=os.environ.get("CRAWLER_SCHEDULED_AT", started_at),
        started_at=started_at,
        run_attempt=str(run_attempt),
        execution_id=f"{run_id}:{run_attempt}",
        commit_sha=os.environ.get("GITHUB_SHA", ""),
        full_reconcile=full_reconcile,
        dry_run=dry_run,
    )


def append_run_record(
    state: dict[str, Any],
    record: RunRecord,
    counters: Optional[SyncCounters] = None,
) -> None:
    if counters is not None:
        record.applied_writes = counters.writes
        record.metrics = {
            key: value
            for key, value in counters.to_dict().items()
            if key
            not in {
                "top_absence_observations",
                "top_present_ids",
                "shrink_candidate_observations",
                "shrink_candidate_clears",
                "destination_hold_observations",
                "observation_run_id",
                "observation_logical_run_id",
                "unresolved_pending_notices",
                "recovered_pending_notices",
            }
        }
    runs = state.setdefault("runs", [])
    record_payload = record.to_dict()
    execution_id = run_execution_id(record_payload)
    matching_indexes = [
        index
        for index, existing in enumerate(runs)
        if run_execution_id(existing) == execution_id
    ]
    if matching_indexes:
        first_index = matching_indexes[0]
        runs[first_index] = record_payload
        runs = [
            existing
            for index, existing in enumerate(runs)
            if index == first_index or index not in matching_indexes
        ]
    else:
        runs.append(record_payload)
    state["runs"] = runs[-100:]


def mark_exception_failure(
    state: dict[str, Any],
    exc: BaseException,
) -> dict[str, Any]:
    state["last_attempt_at"] = utc_now_iso()
    state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
    return state


def build_incident(
    state: dict[str, Any],
    category: FailureCategory,
    title: str,
    summary: str,
    report: Optional[CrawlReport] = None,
    *,
    exception: Optional[BaseException] = None,
) -> dict[str, Any]:
    now = utc_now_iso()
    fingerprint = report_fingerprint(
        category,
        title,
        report,
        exception,
    )
    active_incidents = state.setdefault("active_incidents", {})
    if not isinstance(active_incidents, dict):
        raise RunStateIntegrityError("실행 상태 active_incidents가 객체가 아닙니다")
    previous = active_incidents.get(fingerprint, {})
    if not isinstance(previous, dict):
        previous = {}
    previous_at = parse_iso_datetime(
        str(previous.get("failure_signaled_at") or "")
    )
    elapsed = (
        (datetime.now(timezone.utc) - previous_at).total_seconds()
        if previous_at
        else None
    )
    same_occurrence = previous.get("fingerprint") == fingerprint
    should_signal_failure = (
        not same_occurrence
        or elapsed is None
        or elapsed < 0
        or elapsed >= get_failure_repeat_seconds()
    )
    count = int(previous.get("count") or 0) + 1 if same_occurrence else 1
    occurrence_id = (
        str(previous.get("occurrence_id") or "")
        if same_occurrence
        else uuid.uuid4().hex
    )
    if not occurrence_id:
        occurrence_id = uuid.uuid4().hex
    incident = {
        "fingerprint": fingerprint,
        "category": category.value,
        "title": sanitize_incident_text(title, 160),
        "summary": sanitize_incident_text(summary, 1000),
        "occurred_at": now,
        "first_seen_at": (
            previous.get("first_seen_at", now)
            if same_occurrence
            else now
        ),
        "count": count,
        "occurrence_id": occurrence_id,
        "should_signal_failure": should_signal_failure,
        "run_url": os.environ.get("GITHUB_SERVER_URL", "")
        + (
            f"/{os.environ.get('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID')}"
            if os.environ.get("GITHUB_REPOSITORY") and os.environ.get("GITHUB_RUN_ID")
            else ""
        ),
    }
    if report is not None:
        incident["sources"] = [
            {
                "config_fk": result.source.config_fk,
                "classification": result.source.classification,
                "status": result.status.value,
                "category": result.category.value,
            }
            for result in report.sources
        ]
    active_incident = {
        "fingerprint": fingerprint,
        "first_seen_at": incident["first_seen_at"],
        "count": count,
        "occurrence_id": occurrence_id,
    }
    if same_occurrence and previous.get("failure_signaled_at"):
        active_incident["failure_signaled_at"] = previous[
            "failure_signaled_at"
        ]
    active_incidents[fingerprint] = active_incident
    if len(active_incidents) > 100:
        raise RunStateIntegrityError("활성 장애 보존 한도를 초과했습니다")
    state["last_incident"] = active_incident
    return incident


def mark_failure_signaled(
    state: dict[str, Any],
    incident: dict[str, Any],
) -> bool:
    active_incidents = state.setdefault("active_incidents", {})
    if not isinstance(active_incidents, dict):
        return False
    fingerprint = str(incident.get("fingerprint") or "")
    occurrence_id = str(incident.get("occurrence_id") or "")
    active = active_incidents.get(fingerprint, {})
    if (
        not fingerprint
        or not isinstance(active, dict)
        or active.get("fingerprint") != fingerprint
        or not occurrence_id
        or active.get("occurrence_id") != occurrence_id
    ):
        return False
    active["failure_signaled_at"] = utc_now_iso()
    active_incidents[fingerprint] = active
    state["last_incident"] = deepcopy(active)
    incident["should_signal_failure"] = True
    return True


def clear_active_incidents(state: dict[str, Any]) -> int:
    active_incidents = state.setdefault("active_incidents", {})
    if not isinstance(active_incidents, dict):
        raise RunStateIntegrityError("실행 상태 active_incidents가 객체가 아닙니다")
    count = len(active_incidents)
    state["active_incidents"] = {}
    state["last_incident"] = {}
    return count
