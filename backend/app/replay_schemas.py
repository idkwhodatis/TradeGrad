"""Exact-leaf replay projections; nullable evidence is never fabricated."""

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import JsonValue

from backend.app.data_schemas import PreviewBar
from backend.app.trace_schemas import TraceContext, TraceView
from backend.app.workbench_schemas import WorkbenchSourceAssociation
from packages.research_trace.contracts import OrderLink, Sha256


class ReplayMarketContext(TraceView):
    dataset_version_id: UUID
    dataset_sha256: Sha256
    universe_version_id: UUID | None
    universe_sha256: Sha256 | None
    universe_state: str | None
    universe_as_of: datetime | None
    historical_membership_status: Literal[
        "user_fixed_assumption",
        "current_snapshot_only",
        "historical_intervals_declared",
        "point_in_time_evidence_declared",
        "not_collected",
    ]
    membership_status: Literal["available", "not_collected"]
    membership_reason: str
    timezone: str
    window_start: date
    window_end: date
    result_trust: str
    ledger_origin: str
    warnings: list[str]


class ReplayInstrument(TraceView):
    instrument_id: str
    listing_id: UUID
    symbol: str
    configured_evaluation_member: bool | None
    configured_session_member: bool | None


class ReplayInstrumentPage(TraceView):
    context: TraceContext
    market: ReplayMarketContext
    source_association: WorkbenchSourceAssociation
    session_date: date
    items: list[ReplayInstrument]
    total: int
    offset: int
    limit: int


class ReplayMarketPage(TraceView):
    context: TraceContext
    market: ReplayMarketContext
    decision_id: str
    session_date: date | None
    instrument_id: str
    instrument: ReplayInstrument | None
    status: Literal[
        "available",
        "no_rows",
        "outside_configured_universe",
        "not_collected",
        "unsupported",
    ]
    missing_reason: str | None
    information_cutoff: datetime | None
    decision_time: datetime | None
    bars: list[PreviewBar] | None
    total: int | None
    offset: int
    limit: int
    start: date
    end: date


class ExecutionEvidenceRow(TraceView):
    stage_id: str
    logical_order_id: str
    instrument_id: str
    record_kind: Literal["logical_order", "child_order", "fill"]
    record_id: str
    record_index: int | None
    status: Literal["available", "not_collected", "no_matching_record"]
    association_basis: Literal[
        "logical_order_id", "child_order_id", "fill_id", "unpaired_native_execution"
    ]
    record: dict[str, JsonValue] | None
    missing_reason: str | None
    link: OrderLink


class ReplayExecutionPage(TraceView):
    context: TraceContext
    decision_id: str
    instrument_id: str | None
    stage_id: str | None
    logical_order_id: str | None
    status: Literal["available", "no_linked_records", "not_collected"]
    missing_reason: str | None
    items: list[ExecutionEvidenceRow] | None
    total: int | None
    offset: int
    limit: int
    warnings: list[str]
