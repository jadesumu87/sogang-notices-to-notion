from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class SourceStatus(str, Enum):
    SUCCESS = "success"
    CONFIRMED_EMPTY = "confirmed_empty"
    VALID_EMPTY = "confirmed_empty"
    PARTIAL = "partial"
    DEGRADED = "partial"
    FAILED = "failed"


class FailureCategory(str, Enum):
    NONE = "none"
    UPSTREAM = "upstream"
    SOURCE_UPSTREAM = "source_upstream"
    CONTRACT_DRIFT = "contract_drift"
    SOURCE_CONTRACT = "source_contract"
    SOURCE_PARTIAL = "source_partial"
    NETWORK = "network"
    NOTION = "notion"
    DESTINATION_RATE_LIMIT = "destination_rate_limit"
    DESTINATION_AUTH = "destination_auth"
    DESTINATION_CONTRACT = "destination_contract"
    RUNNER = "runner"
    CI_INFRASTRUCTURE = "ci_infrastructure"
    CODE = "code"
    STALE = "stale"
    SECURITY_POLICY = "security_policy"
    INTERNAL = "internal"


class DestinationConsistencyError(RuntimeError):
    pass


class LocalConfigurationError(RuntimeError):
    failure_origin = "local_config"

    def __init__(self, message: str, failure_kind: str) -> None:
        super().__init__(message)
        self.failure_kind = failure_kind


class ItemCompleteness(str, Enum):
    COMPLETE = "complete"
    METADATA_ONLY = "metadata_only"
    PARTIAL = "partial"


class MutationKind(str, Enum):
    CREATE = "create"
    UPDATE_PROPERTIES = "update_properties"
    REPLACE_BODY = "replace_body"
    DISABLE_TOP = "disable_top"
    CONFLICT = "conflict"
    NOOP = "noop"


@dataclass(frozen=True)
class Notice:
    source_id: str
    notice_id: str
    title: str
    url: str
    date: str = ""
    classification: str = ""
    top: bool = False
    completeness: ItemCompleteness = ItemCompleteness.COMPLETE
    raw: dict[str, Any] = field(default_factory=dict, compare=False)

    @classmethod
    def from_item(
        cls,
        source_id: str,
        notice_id: str,
        item: dict[str, Any],
        completeness: ItemCompleteness = ItemCompleteness.COMPLETE,
    ) -> "Notice":
        return cls(
            source_id=str(source_id).strip(),
            notice_id=str(notice_id).strip(),
            title=str(item.get("title") or "").strip(),
            url=str(item.get("url") or "").strip(),
            date=str(item.get("date") or "").strip(),
            classification=str(item.get("classification") or "").strip(),
            top=bool(item.get("top")),
            completeness=completeness,
            raw=dict(item),
        )


@dataclass(frozen=True)
class MutationAction:
    kind: MutationKind
    source_id: str
    notice_id: str
    page_id: str = ""
    operation_id: str = ""
    reason: str = ""


@dataclass
class MutationPlan:
    run_id: str
    actions: list[MutationAction] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    quarantined_source_ids: list[str] = field(default_factory=list)

    @property
    def write_count(self) -> int:
        return sum(
            action.kind
            not in {MutationKind.NOOP, MutationKind.CONFLICT}
            for action in self.actions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "write_count": self.write_count,
            "actions": [
                {
                    "kind": action.kind.value,
                    "source_id": action.source_id,
                    "notice_id": action.notice_id,
                    "page_id": action.page_id,
                    "operation_id": action.operation_id,
                    "reason": action.reason,
                }
                for action in self.actions
            ],
            "conflicts": list(self.conflicts),
            "quarantined_source_ids": list(
                self.quarantined_source_ids
            ),
        }


@dataclass
class RunRecord:
    run_id: str
    scheduled_at: str
    started_at: str
    run_attempt: str = "1"
    execution_id: str = ""
    commit_sha: str = ""
    finished_at: str = ""
    full_reconcile: bool = False
    dry_run: bool = False
    status: str = "running"
    failure_category: FailureCategory = FailureCategory.NONE
    source_results: list[dict[str, Any]] = field(default_factory=list)
    planned_writes: int = 0
    applied_writes: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["failure_category"] = self.failure_category.value
        return payload


@dataclass
class SyncCounters:
    created: int = 0
    property_updates: int = 0
    body_updates: int = 0
    top_disabled: int = 0
    unchanged: int = 0
    writes: int = 0
    http_reads: int = 0
    upload_attempts: int = 0
    external_download_requests: int = 0
    external_download_stopped_reason: str = ""
    external_download_status_code: Optional[int] = None
    external_download_retry_after: Optional[str] = None
    external_download_retry_after_seconds: Optional[float] = None
    external_download_elapsed_seconds: float = 0.0
    pending_seen: int = 0
    pending_recovered: int = 0
    quarantined_source_ids: list[str] = field(default_factory=list)
    unresolved_pending_page_ids: list[str] = field(
        default_factory=list
    )
    unresolved_pending_notices: dict[str, list[str]] = field(
        default_factory=dict
    )
    recovered_pending_notices: dict[str, list[str]] = field(
        default_factory=dict
    )
    top_absence_observations: dict[str, list[str]] = field(
        default_factory=dict
    )
    top_present_ids: dict[str, list[str]] = field(default_factory=dict)
    shrink_candidate_observations: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )
    shrink_candidate_clears: list[str] = field(default_factory=list)
    observation_run_id: str = ""
    observation_logical_run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SourceSpec:
    config_fk: str
    classification: str
    list_url: str
    required: bool = True


@dataclass
class SiteFetchResult:
    ok: bool
    status_code: Optional[int] = None
    body: Optional[bytes] = None
    content_type: str = ""
    final_url: str = ""
    category: FailureCategory = FailureCategory.NONE
    error: str = ""
    attempts: int = 1
    retry_after_seconds: Optional[float] = None


@dataclass
class ListPageResult:
    ok: bool
    entries: list[dict[str, Any]] = field(default_factory=list)
    valid_empty: bool = False
    category: FailureCategory = FailureCategory.NONE
    error: str = ""
    status_code: Optional[int] = None
    requested_page: int = 0
    effective_page: Optional[int] = None
    page_size: Optional[int] = None
    total_count: Optional[int] = None
    has_more: Optional[bool] = None
    terminal_verified: bool = False
    retry_after_seconds: Optional[float] = None


@dataclass
class FallbackPageResult:
    ok: bool
    requested_page: int
    source_config_fk: str
    entries: list[dict[str, Any]] = field(default_factory=list)
    effective_page: Optional[int] = None
    final_url: str = ""
    contract_verified: bool = False
    explicit_empty: bool = False
    raw_entry_count: int = 0
    category: FailureCategory = FailureCategory.NONE
    error: str = ""
    retry_after_seconds: Optional[float] = None


@dataclass
class FallbackDetailResult:
    ok: bool
    notice_id: str
    url: str
    title: str = ""
    date: str = ""
    body_blocks: list[dict[str, Any]] = field(default_factory=list)
    body_status: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    attachments_status: str = ""
    category: FailureCategory = FailureCategory.NONE
    error: str = ""
    retry_after_seconds: Optional[float] = None


@dataclass
class SourceCrawlResult:
    source: SourceSpec
    status: SourceStatus
    items: list[dict[str, Any]] = field(default_factory=list)
    method: str = ""
    pages_scanned: int = 0
    observed_count: int = 0
    observed_ids: list[str] = field(default_factory=list)
    refreshed_known_ids: list[str] = field(default_factory=list)
    refresh_window_end_id: str = ""
    backfill_resume_page: int = 1
    backfill_anchor_ids: list[str] = field(default_factory=list)
    top_urls: list[str] = field(default_factory=list)
    top_dates: dict[str, list[str]] = field(default_factory=dict)
    category: FailureCategory = FailureCategory.NONE
    error: str = ""
    detail_failures: int = 0
    rejected_count: int = 0
    checkpoint_found: bool = True
    terminal_reached: bool = False
    list_contract_valid: bool = True
    fallback_from_error: str = ""
    termination_reason: str = ""
    full_snapshot: bool = False
    reconcile_complete: bool = False
    coverage_complete: bool = False
    reconcile_requested: Optional[bool] = None
    top_snapshot_verified: bool = False
    retry_after_seconds: Optional[float] = None
    observed_at: str = field(default_factory=utc_now_iso)

    @property
    def write_safe(self) -> bool:
        if self.status not in {
            SourceStatus.SUCCESS,
            SourceStatus.VALID_EMPTY,
        }:
            return False
        if (
            self.error
            or self.detail_failures
            or self.rejected_count
            or not self.list_contract_valid
            or not self.terminal_reached
            or self.termination_reason
            not in {
                "natural_end",
                "non_top_boundary",
                "backfill_window",
                "incremental_checkpoint",
            }
        ):
            return False
        if any(
            str(item.get("completeness") or "")
            != ItemCompleteness.COMPLETE.value
            for item in self.items
        ):
            return False
        if self.status == SourceStatus.VALID_EMPTY:
            return not self.items and self.observed_count == 0
        return True

    def to_dict(self, include_items: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["category"] = self.category.value
        if not include_items:
            payload["items"] = [
                {
                    "title": str(item.get("title") or "")[:300],
                    "url": str(item.get("url") or "")[:2000],
                    "date": str(item.get("date") or ""),
                    "top": bool(item.get("top")),
                }
                for item in self.items
            ]
        return payload


@dataclass
class ValidationIssue:
    code: str
    message: str
    source_config_fk: str = ""
    fatal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CrawlReport:
    sources: list[SourceCrawlResult]
    issues: list[ValidationIssue] = field(default_factory=list)
    observed_at: str = field(default_factory=utc_now_iso)

    @property
    def items(self) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for result in self.sources:
            merged.extend(result.items)
        return merged

    @property
    def write_safe(self) -> bool:
        required_safe = all(
            result.write_safe for result in self.sources if result.source.required
        )
        return required_safe and not any(issue.fatal for issue in self.issues)

    @property
    def failure_category(self) -> FailureCategory:
        for result in self.sources:
            if result.category != FailureCategory.NONE:
                return result.category
        if any(issue.fatal for issue in self.issues):
            return FailureCategory.SOURCE_CONTRACT
        return FailureCategory.NONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "write_safe": self.write_safe,
            "failure_category": self.failure_category.value,
            "item_count": len(self.items),
            "sources": [result.to_dict() for result in self.sources],
            "issues": [issue.to_dict() for issue in self.issues],
        }


class SourceAdapter(Protocol):
    def crawl(
        self,
        source: SourceSpec,
        known_ids: Optional[set[str]] = None,
        incremental: bool = False,
        source_state: Optional[dict[str, Any]] = None,
    ) -> SourceCrawlResult:
        ...
