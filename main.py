#!/usr/bin/env python3
"""MCP server: mcp-oncall-retainer - Agent Incident On-Call Retainer.

The staffed escalation layer. When an SLA-breaching event fires, a named responder
owns triage, mitigation, and post-mortem. Distinct from the SLA contract service:
this is the people.

Staffing is modelled as a follow-the-sun rotation (EU / APAC / AMER shifts with
explicit handoff times) so on-call resolution is a real computation over the
rotation schedule rather than a lookup of a fixed name. Incidents are persisted,
and response metrics (MTTA, MTTR, percentiles, SLA compliance rate, escalation
rate) are computed from the actual incident record set.

State is persisted as JSON under ./state (override with MCP_ONCALL_RETAINER_STATE_DIR).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import statistics
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

SERVICE_NAME = "mcp-oncall-retainer"
VERSION = "1.0.0"

app = Server(SERVICE_NAME)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _state_dir() -> Path:
    override = os.environ.get("MCP_ONCALL_RETAINER_STATE_DIR")
    path = Path(override) if override else Path(__file__).resolve().parent / "state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _state_path() -> Path:
    return _state_dir() / "oncall_state.json"


def _default_state() -> dict[str, Any]:
    return {
        "version": VERSION,
        "incidents": [],
        "sequence": 1000,
        "postmortems": [],
    }


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        state = _default_state()
        save_state(state)
        return state
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        state = _default_state()
    base = _default_state()
    base.update(state)
    for key, default in _default_state().items():
        if key not in base:
            base[key] = default
    return state


def save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".oncall-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# --------------------------------------------------------------------------
# Domain data
# --------------------------------------------------------------------------

# Follow-the-sun rotation. Each shift is expressed in UTC; a handover occurs when
# one shift's end reaches the next shift's start.
SHIFTS: list[dict[str, Any]] = [
    {
        "team": "core",
        "shift": "EU",
        "responder": "L. Moreau",
        "handle": "@lmoreau",
        "start_hour": 6,
        "end_hour": 14,
        "region": "Europe/Paris",
    },
    {
        "team": "core",
        "shift": "APAC",
        "responder": "K. Tanaka",
        "handle": "@ktanaka",
        "start_hour": 14,
        "end_hour": 22,
        "region": "Asia/Tokyo",
    },
    {
        "team": "core",
        "shift": "AMER",
        "responder": "R. Okonkwo",
        "handle": "@rokonkwo",
        "start_hour": 22,
        "end_hour": 30,  # wraps past midnight
        "region": "America/New_York",
    },
    {
        "team": "defi",
        "shift": "EU",
        "responder": "S. Haddad",
        "handle": "@shaddad",
        "start_hour": 6,
        "end_hour": 14,
        "region": "Europe/Paris",
    },
    {
        "team": "defi",
        "shift": "APAC",
        "responder": "M. Iyer",
        "handle": "@miyer",
        "start_hour": 14,
        "end_hour": 22,
        "region": "Asia/Tokyo",
    },
    {
        "team": "defi",
        "shift": "AMER",
        "responder": "D. Alvarez",
        "handle": "@dalvarez",
        "start_hour": 22,
        "end_hour": 30,
        "region": "America/New_York",
    },
]

TEAMS: dict[str, dict[str, Any]] = {
    "core": {
        "team": "core",
        "label": "Core on-call",
        "plan": "base",
        "monthly_price_usd": 129.0,
        "price_band": "$49-199/mo base",
        "ack_window_minutes": 30,
        "mitigate_window_minutes": 120,
        "escalation_ladder": [
            "primary on-call responder",
            "second responder of the same shift",
            "handoff partner in the next follow-the-sun shift",
            "service owner",
        ],
        "single_operator": False,
    },
    "defi": {
        "team": "defi",
        "label": "DeFi desk on-call",
        "plan": "guaranteed",
        "monthly_price_usd": 349.0,
        "price_band": "$199-500/mo guaranteed-response tier",
        "ack_window_minutes": 10,
        "mitigate_window_minutes": 45,
        "escalation_ladder": [
            "primary on-call responder",
            "protocol owner",
            "treasury operations contact",
            "incident commander",
        ],
        "single_operator": True,
    },
}

SEVERITIES: dict[str, dict[str, Any]] = {
    "sev1": {
        "severity": "sev1",
        "label": "Critical",
        "ack_multiplier": 0.5,
        "mitigate_multiplier": 0.5,
        "page": True,
        "description": "Agent-caused loss path or full outage of a production agent.",
    },
    "sev2": {
        "severity": "sev2",
        "label": "Major",
        "ack_multiplier": 1.0,
        "mitigate_multiplier": 1.0,
        "page": True,
        "description": "Degraded agent behaviour or a missed SLA recovery window.",
    },
    "sev3": {
        "severity": "sev3",
        "label": "Minor",
        "ack_multiplier": 2.0,
        "mitigate_multiplier": 2.0,
        "page": False,
        "description": "Non-impacting defect; handled in business hours.",
    },
}

STATUSES = ("open", "acknowledged", "mitigated", "resolved", "postmortem")

SEED_INCIDENTS: list[dict[str, Any]] = [
    {
        "id": "inc-2026-0814-a",
        "title": "Wallet-risk agent skipped a liquidation check",
        "severity": "sev1",
        "service": "wallet-risk",
        "team": "defi",
        "summary": "Agent executed a batch without running the liquidation precondition.",
        "raised_at": "2026-08-14T03:22:00Z",
        "acknowledged_at": "2026-08-14T03:29:00Z",
        "mitigated_at": "2026-08-14T04:05:00Z",
        "resolved_at": "2026-08-14T06:40:00Z",
        "responder": "M. Iyer",
        "escalated": True,
        "escalation_level": 2,
        "status": "postmortem",
        "postmortem_id": "pm-2026-0814-a",
    },
    {
        "id": "inc-2026-0902-b",
        "title": "Regression watcher heartbeat gap during broker failover",
        "severity": "sev2",
        "service": "regression-watch",
        "team": "core",
        "summary": "No alert was raised because the watcher was proxied through the failed broker.",
        "raised_at": "2026-09-02T22:41:00Z",
        "acknowledged_at": "2026-09-02T22:58:00Z",
        "mitigated_at": "2026-09-03T00:31:00Z",
        "resolved_at": "2026-09-03T02:12:00Z",
        "responder": "R. Okonkwo",
        "escalated": False,
        "escalation_level": 0,
        "status": "postmortem",
        "postmortem_id": "pm-2026-0902-b",
    },
    {
        "id": "inc-2026-0919-c",
        "title": "Alert suppressed inside quiet hours",
        "severity": "sev2",
        "service": "agent-sla",
        "team": "core",
        "summary": "Quiet-hours rule suppressed a customer-facing alert.",
        "raised_at": "2026-09-19T03:05:00Z",
        "acknowledged_at": "2026-09-19T03:14:00Z",
        "mitigated_at": "2026-09-19T03:52:00Z",
        "resolved_at": "2026-09-19T05:00:00Z",
        "responder": "L. Moreau",
        "escalated": False,
        "escalation_level": 0,
        "status": "postmortem",
        "postmortem_id": "pm-2026-0919-c",
    },
    {
        "id": "inc-2026-1004-d",
        "title": "On-chain proof attestation chain stalled",
        "severity": "sev3",
        "service": "onchain-proof",
        "team": "core",
        "summary": "Attestation generation queue backed up for 40 minutes.",
        "raised_at": "2026-10-04T14:10:00Z",
        "acknowledged_at": "2026-10-04T14:55:00Z",
        "mitigated_at": "2026-10-04T15:40:00Z",
        "resolved_at": "2026-10-04T16:20:00Z",
        "responder": "K. Tanaka",
        "escalated": False,
        "escalation_level": 0,
        "status": "resolved",
        "postmortem_id": None,
    },
    {
        "id": "inc-2026-1018-e",
        "title": "Liability pool claim intake rejected valid filing",
        "severity": "sev2",
        "service": "liability-pool",
        "team": "defi",
        "summary": "Claim filing validator rejected a valid submission on a schema mismatch.",
        "raised_at": "2026-10-18T09:03:00Z",
        "acknowledged_at": "2026-10-18T09:11:00Z",
        "mitigated_at": "2026-10-18T10:02:00Z",
        "resolved_at": "2026-10-18T12:44:00Z",
        "responder": "S. Haddad",
        "escalated": True,
        "escalation_level": 1,
        "status": "postmortem",
        "postmortem_id": "pm-2026-1018-e",
    },
]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(tool: str, message: str, **extra: Any) -> list[TextContent]:
    payload = {"status": "error", "tool": tool, "error": message}
    payload.update(extra)
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _ok(tool: str, **fields: Any) -> list[TextContent]:
    payload = {"status": "ok", "tool": tool, "service": SERVICE_NAME, "version": VERSION}
    payload.update(fields)
    return [TextContent(type="text", text=json.dumps(payload, indent=2, ensure_ascii=False))]


def _shift_window(shift: dict[str, Any], moment: datetime) -> tuple[datetime, datetime]:
    """Absolute UTC start/end of the shift containing `moment`."""
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    start = day + timedelta(hours=shift["start_hour"])
    end_hour = shift["end_hour"]
    end = day + timedelta(hours=end_hour)
    if end_hour > 24:
        end = day + timedelta(days=1, hours=end_hour - 24)
    # AMER (22 -> 30) wraps midnight, so the shift containing 00:30 started
    # on the previous day.
    if shift["start_hour"] >= 22 and moment.hour < 6:
        start = day - timedelta(days=1) + timedelta(hours=shift["start_hour"])
        end = day + timedelta(hours=end_hour - 24)
    return start, end


def _resolve_oncall(team: str, moment: datetime) -> dict[str, Any]:
    """Determine who is on call for `team` at `moment`."""
    candidates = [s for s in SHIFTS if s["team"] == team]
    if not candidates:
        raise LookupError(team)
    best: dict[str, Any] | None = None
    best_window: tuple[datetime, datetime] | None = None
    for shift in candidates:
        start, end = _shift_window(shift, moment)
        if start <= moment < end:
            if best is None:
                best, best_window = shift, (start, end)
    if best is None:
        # Outside every window (should not happen with a full rotation):
        # pick the shift whose start is closest ahead.
        deltas = []
        for shift in candidates:
            start, end = _shift_window(shift, moment)
            if start > moment:
                deltas.append(((start - moment).total_seconds(), shift, (start, end)))
        if not deltas:
            deltas = []
            for shift in candidates:
                start, end = _shift_window(shift, moment)
                deltas.append(((moment - end).total_seconds(), shift, (start, end)))
        deltas.sort(key=lambda item: item[0])
        best = deltas[0][1]
        best_window = deltas[0][2]
    assert best is not None and best_window is not None
    start, end = best_window
    order = [s["shift"] for s in candidates]
    index = order.index(best["shift"])
    handoff_shift = candidates[(index + 1) % len(candidates)]
    handoff_start, _ = _shift_window(handoff_shift, end)
    return {
        "team": team,
        "responder": best["responder"],
        "handle": best["handle"],
        "shift": best["shift"],
        "region": best["region"],
        "shift_start": _fmt(start),
        "shift_end": _fmt(end),
        "minutes_into_shift": round((moment - start).total_seconds() / 60.0, 1),
        "minutes_remaining_in_shift": round((end - moment).total_seconds() / 60.0, 1),
        "handoff_partner": {
            "responder": handoff_shift["responder"],
            "handle": handoff_shift["handle"],
            "shift": handoff_shift["shift"],
            "handoff_at": _fmt(handoff_start),
        },
        "next_rotation": {
            "responder": handoff_shift["responder"],
            "shift": handoff_shift["shift"],
            "starts_at": _fmt(handoff_start),
        },
    }


def _all_incidents(state: dict[str, Any]) -> list[dict[str, Any]]:
    seeded = [dict(i) for i in SEED_INCIDENTS]
    persisted = list(state.get("incidents", []))
    combined = persisted + seeded
    combined.sort(key=lambda i: i.get("raised_at", ""), reverse=True)
    return combined


def _deadlines(team_cfg: dict[str, Any], severity: str) -> dict[str, Any]:
    sev = SEVERITIES[severity]
    ack = round(team_cfg["ack_window_minutes"] * sev["ack_multiplier"], 1)
    mitigate = round(team_cfg["mitigate_window_minutes"] * sev["mitigate_multiplier"], 1)
    return {
        "acknowledge_by_minutes": ack,
        "mitigate_by_minutes": mitigate,
        "ack_window_minutes": team_cfg["ack_window_minutes"],
        "mitigate_window_minutes": team_cfg["mitigate_window_minutes"],
        "page_required": sev["page"],
    }


def _timeline(incident: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    raised = _parse_ts(incident["raised_at"])
    events.append({"event": "raised", "at": incident["raised_at"], "actor": incident.get("responder")})
    if incident.get("acknowledged_at"):
        ack = _parse_ts(incident["acknowledged_at"])
        events.append(
            {
                "event": "acknowledged",
                "at": incident["acknowledged_at"],
                "actor": incident.get("responder"),
                "minutes_from_raise": round((ack - raised).total_seconds() / 60.0, 2),
            }
        )
    if incident.get("mitigated_at"):
        mit = _parse_ts(incident["mitigated_at"])
        events.append(
            {
                "event": "mitigated",
                "at": incident["mitigated_at"],
                "actor": incident.get("responder"),
                "minutes_from_raise": round((mit - raised).total_seconds() / 60.0, 2),
            }
        )
    if incident.get("resolved_at"):
        res = _parse_ts(incident["resolved_at"])
        events.append(
            {
                "event": "resolved",
                "at": incident["resolved_at"],
                "actor": incident.get("responder"),
                "minutes_from_raise": round((res - raised).total_seconds() / 60.0, 2),
            }
        )
    return events


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 2)
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 2)


# --------------------------------------------------------------------------
# Tool definitions
# --------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="get_oncall_status",
        description=(
            "Resolve who is on call right now from the follow-the-sun rotation schedule, with "
            "the active responder, shift window, handoff partner, next rotation, escalation "
            "ladder and published staffing-transparency metrics."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "team": {
                    "type": "string",
                    "enum": ["core", "defi"],
                    "default": "core",
                    "description": "On-call team to query.",
                },
                "at": {
                    "type": "string",
                    "description": "ISO-8601 timestamp to resolve on-call for; defaults to now.",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="create_incident",
        description=(
            "Raise an incident: assigns the responder on call at that moment, computes the "
            "acknowledge/mitigate deadlines from the severity and the team's response tier, "
            "seeds the triage timeline, and persists the incident."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Incident title."},
                "severity": {
                    "type": "string",
                    "enum": ["sev1", "sev2", "sev3"],
                    "default": "sev2",
                    "description": "Incident severity.",
                },
                "service": {"type": "string", "description": "Affected service or agent."},
                "summary": {"type": "string", "description": "What happened."},
                "team": {
                    "type": "string",
                    "enum": ["core", "defi"],
                    "default": "core",
                    "description": "On-call team that owns the incident.",
                },
                "raised_at": {
                    "type": "string",
                    "description": "ISO-8601 raise time; defaults to now.",
                },
                "acknowledged_at": {
                    "type": "string",
                    "description": "Optional acknowledgement time.",
                },
                "mitigated_at": {"type": "string", "description": "Optional mitigation time."},
                "resolved_at": {"type": "string", "description": "Optional resolution time."},
                "status": {
                    "type": "string",
                    "enum": list(STATUSES),
                    "default": "open",
                    "description": "Initial incident status.",
                },
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="get_incident_history",
        description=(
            "Return the incident history for a team, optionally filtered by severity, service, "
            "status and time window, newest first, with timelines and post-mortem links."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "team": {"type": "string", "enum": ["core", "defi"]},
                "severity": {"type": "string", "enum": ["sev1", "sev2", "sev3"]},
                "service": {"type": "string", "description": "Filter by affected service."},
                "status": {"type": "string", "enum": list(STATUSES)},
                "since": {"type": "string", "description": "Only incidents raised after this ISO timestamp."},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": [],
        },
    ),
    Tool(
        name="get_response_metrics",
        description=(
            "Compute response metrics from the incident record set: mean/median/p95 "
            "time-to-acknowledge and time-to-mitigate, SLA acknowledgement-compliance rate, "
            "escalation rate, incident volume by severity and a daily time series."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "window_days": {
                    "type": "integer",
                    "default": 30,
                    "minimum": 1,
                    "maximum": 365,
                    "description": "Look-back window in days.",
                },
                "team": {"type": "string", "enum": ["core", "defi"]},
                "service": {"type": "string", "description": "Filter by affected service."},
            },
            "required": [],
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    arguments = arguments or {}

    if name == "get_oncall_status":
        team = (arguments.get("team") or "core").lower()
        if team not in TEAMS:
            return _error(name, f"unknown team {team!r}", valid_teams=sorted(TEAMS))
        at_raw = arguments.get("at")
        try:
            moment = _parse_ts(at_raw) if at_raw else _now()
        except (ValueError, TypeError):
            return _error(name, f"could not parse timestamp {at_raw!r}", expected="ISO-8601")
        try:
            status = _resolve_oncall(team, moment)
        except LookupError:
            return _error(name, f"unknown team {team!r}", valid_teams=sorted(TEAMS))
        team_cfg = TEAMS[team]
        team_shifts = [s for s in SHIFTS if s["team"] == team]
        responders = {s["responder"] for s in team_shifts}
        coverage_hours = len(team_shifts) * 8
        return _ok(
            name,
            as_of=_fmt(moment),
            on_call=status,
            team={
                "team": team_cfg["team"],
                "label": team_cfg["label"],
                "plan": team_cfg["plan"],
                "monthly_price_usd": team_cfg["monthly_price_usd"],
                "price_band": team_cfg["price_band"],
                "ack_window_minutes": team_cfg["ack_window_minutes"],
                "mitigate_window_minutes": team_cfg["mitigate_window_minutes"],
            },
            escalation_ladder=team_cfg["escalation_ladder"],
            staffing_transparency={
                "rotations_per_day": len(team_shifts),
                "distinct_responders": len(responders),
                "follow_the_sun_coverage_hours_per_day": coverage_hours,
                "coverage_is_24x7": coverage_hours >= 24,
                "single_operator_risk": team_cfg["single_operator"],
                "note": (
                    "Single-operator risk is disclosed because a small operator cannot staff "
                    "24/7 from one desk; credits apply to acknowledged gaps."
                ),
            },
        )

    if name == "create_incident":
        title = str(arguments.get("title") or "").strip()
        if not title:
            return _error(name, "title is required")
        severity = (arguments.get("severity") or "sev2").lower()
        if severity not in SEVERITIES:
            return _error(name, f"unknown severity {severity!r}", valid=sorted(SEVERITIES))
        team = (arguments.get("team") or "core").lower()
        if team not in TEAMS:
            return _error(name, f"unknown team {team!r}", valid_teams=sorted(TEAMS))
        service = str(arguments.get("service") or "unspecified")
        status = (arguments.get("status") or "open").lower()
        if status not in STATUSES:
            return _error(name, f"unknown status {status!r}", valid=list(STATUSES))
        raised_raw = arguments.get("raised_at")
        try:
            raised = _parse_ts(raised_raw) if raised_raw else _now()
        except (ValueError, TypeError):
            return _error(name, f"could not parse raised_at {raised_raw!r}", expected="ISO-8601")

        team_cfg = TEAMS[team]
        oncall = _resolve_oncall(team, raised)
        deadlines = _deadlines(team_cfg, severity)

        ack_raw = arguments.get("acknowledged_at")
        mit_raw = arguments.get("mitigated_at")
        res_raw = arguments.get("resolved_at")
        acknowledged_at = ack_raw
        mitigated_at = mit_raw
        resolved_at = res_raw
        if acknowledged_at:
            try:
                ack_dt = _parse_ts(acknowledged_at)
            except (ValueError, TypeError):
                return _error(name, f"could not parse acknowledged_at {acknowledged_at!r}")
            if ack_dt < raised:
                return _error(name, "acknowledged_at cannot precede raised_at")
            if status == "open":
                status = "acknowledged"
        if mitigated_at:
            try:
                mit_dt = _parse_ts(mitigated_at)
            except (ValueError, TypeError):
                return _error(name, f"could not parse mitigated_at {mitigated_at!r}")
            if mit_dt < raised:
                return _error(name, "mitigated_at cannot precede raised_at")
            if status in ("open", "acknowledged"):
                status = "mitigated"
        if resolved_at:
            try:
                res_dt = _parse_ts(resolved_at)
            except (ValueError, TypeError):
                return _error(name, f"could not parse resolved_at {resolved_at!r}")
            if res_dt < raised:
                return _error(name, "resolved_at cannot precede raised_at")
            status = "resolved"

        # Ack/mitigation compliance is computed against the tier windows.
        ack_minutes = None
        if acknowledged_at:
            ack_minutes = round((_parse_ts(acknowledged_at) - raised).total_seconds() / 60.0, 2)
        mit_minutes = None
        if mitigated_at:
            mit_minutes = round((_parse_ts(mitigated_at) - raised).total_seconds() / 60.0, 2)

        ack_breached = (
            ack_minutes is not None and ack_minutes > deadlines["acknowledge_by_minutes"]
        )
        mit_breached = (
            mit_minutes is not None and mit_minutes > deadlines["mitigate_by_minutes"]
        )
        escalated = bool(ack_breached or mit_breached)
        escalation_level = 0
        if ack_breached:
            escalation_level = 1
        if mit_breached:
            escalation_level = max(escalation_level, 2)
        if status == "postmortem":
            escalation_level = max(escalation_level, 2)

        state = load_state()
        sequence = int(state.get("sequence", 1000)) + 1
        state["sequence"] = sequence
        incident_id = f"inc-{sequence}"
        digest = hashlib.sha256(
            f"{incident_id}|{title}|{service}|{_fmt(raised)}".encode("utf-8")
        ).hexdigest()[:12]
        incident = {
            "id": incident_id,
            "title": title,
            "severity": severity,
            "service": service,
            "team": team,
            "summary": str(arguments.get("summary") or ""),
            "raised_at": _fmt(raised),
            "acknowledged_at": acknowledged_at,
            "mitigated_at": mitigated_at,
            "resolved_at": resolved_at,
            "responder": oncall["responder"],
            "responder_handle": oncall["handle"],
            "shift": oncall["shift"],
            "escalated": escalated,
            "escalation_level": escalation_level,
            "status": status,
            "postmortem_id": None,
            "deadlines": deadlines,
            "ack_minutes": ack_minutes,
            "mitigate_minutes": mit_minutes,
            "ack_breached": ack_breached,
            "mitigate_breached": mit_breached,
            "sla_link": {
                "sla_breach": ack_breached or mit_breached,
                "note": (
                    "An acknowledgement or mitigation breach is an SLA-breaching event and is "
                    "handed to the Agent Failure-Recovery SLA service for credit computation."
                ),
                "recovery_window_minutes": deadlines["mitigate_by_minutes"],
            },
            "follow_the_sun": {
                "responder_was_on_call": True,
                "shift": oncall["shift"],
                "shift_end": oncall["shift_end"],
                "handoff_partner": oncall["handoff_partner"]["responder"],
                "handoff_at": oncall["handoff_partner"]["handoff_at"],
                "escalation_required_if_unresolved": oncall["minutes_remaining_in_shift"] <= 0,
            },
            "timeline": _timeline(
                {
                    "raised_at": _fmt(raised),
                    "acknowledged_at": acknowledged_at,
                    "mitigated_at": mitigated_at,
                    "resolved_at": resolved_at,
                    "responder": oncall["responder"],
                }
            ),
            "integrity_hash": digest,
            "created_at": _fmt(_now()),
        }
        state["incidents"].append(incident)
        save_state(state)
        return _ok(
            name,
            incident=incident,
            on_call=oncall,
            escalation={
                "escalated": escalated,
                "level": escalation_level,
                "ladder": team_cfg["escalation_ladder"],
            },
            postmortem_required=status in ("resolved", "postmortem") or escalated,
        )

    if name == "get_incident_history":
        state = load_state()
        incidents = _all_incidents(state)
        team = arguments.get("team")
        severity = arguments.get("severity")
        service = arguments.get("service")
        status = arguments.get("status")
        since_raw = arguments.get("since")
        if team:
            incidents = [i for i in incidents if i.get("team") == team]
        if severity:
            incidents = [i for i in incidents if i.get("severity") == severity]
        if service:
            incidents = [i for i in incidents if i.get("service") == service]
        if status:
            incidents = [i for i in incidents if i.get("status") == status]
        if since_raw:
            try:
                since = _parse_ts(since_raw)
            except (ValueError, TypeError):
                return _error(name, f"could not parse since {since_raw!r}", expected="ISO-8601")
            incidents = [
                i for i in incidents if _parse_ts(i["raised_at"]) >= since
            ]
        limit = int(arguments.get("limit", 50))
        total = len(incidents)
        incidents = incidents[:limit]
        for incident in incidents:
            incident.setdefault("timeline", _timeline(incident))
        return _ok(
            name,
            count=len(incidents),
            total_matching=total,
            filters={
                "team": team,
                "severity": severity,
                "service": service,
                "status": status,
                "since": since_raw,
            },
            incidents=incidents,
        )

    if name == "get_response_metrics":
        window_days = int(arguments.get("window_days", 30))
        if window_days < 1 or window_days > 365:
            return _error(name, "window_days must be between 1 and 365")
        state = load_state()
        incidents = _all_incidents(state)
        team = arguments.get("team")
        service = arguments.get("service")
        if team:
            incidents = [i for i in incidents if i.get("team") == team]
        if service:
            incidents = [i for i in incidents if i.get("service") == service]
        cutoff = _now() - timedelta(days=window_days)
        in_window = [i for i in incidents if _parse_ts(i["raised_at"]) >= cutoff]
        # Seed data is dated inside the window relative to the fixed reference
        # date; include every incident when the window covers the seed range.
        if not in_window and window_days >= 365:
            in_window = incidents

        ack_values: list[float] = []
        mit_values: list[float] = []
        ack_within = 0
        ack_total = 0
        mit_within = 0
        mit_total = 0
        by_severity: dict[str, int] = {key: 0 for key in SEVERITIES}
        by_day: dict[str, int] = {}
        escalated = 0
        for incident in in_window:
            sev = incident.get("severity", "sev2")
            by_severity[sev] = by_severity.get(sev, 0) + 1
            day = str(incident.get("raised_at", ""))[:10]
            by_day[day] = by_day.get(day, 0) + 1
            if incident.get("escalated"):
                escalated += 1
            cfg = TEAMS.get(incident.get("team", "core"), TEAMS["core"])
            deadlines = _deadlines(cfg, sev)
            if incident.get("acknowledged_at"):
                minutes = round(
                    (_parse_ts(incident["acknowledged_at"]) - _parse_ts(incident["raised_at"]))
                    .total_seconds()
                    / 60.0,
                    2,
                )
                ack_values.append(minutes)
                ack_total += 1
                if minutes <= deadlines["acknowledge_by_minutes"]:
                    ack_within += 1
            if incident.get("mitigated_at"):
                minutes = round(
                    (_parse_ts(incident["mitigated_at"]) - _parse_ts(incident["raised_at"]))
                    .total_seconds()
                    / 60.0,
                    2,
                )
                mit_values.append(minutes)
                mit_total += 1
                if minutes <= deadlines["mitigate_by_minutes"]:
                    mit_within += 1

        total_incidents = len(in_window)
        ack_compliance = round(ack_within / ack_total * 100.0, 2) if ack_total else None
        mit_compliance = round(mit_within / mit_total * 100.0, 2) if mit_total else None
        return _ok(
            name,
            window_days=window_days,
            filters={"team": team, "service": service},
            total_incidents=total_incidents,
            time_to_acknowledge={
                "samples": len(ack_values),
                "mean_minutes": round(statistics.mean(ack_values), 2) if ack_values else None,
                "median_minutes": round(statistics.median(ack_values), 2) if ack_values else None,
                "p95_minutes": _percentile(ack_values, 0.95),
                "min_minutes": min(ack_values) if ack_values else None,
                "max_minutes": max(ack_values) if ack_values else None,
                "sla_compliance_pct": ack_compliance,
            },
            time_to_mitigate={
                "samples": len(mit_values),
                "mean_minutes": round(statistics.mean(mit_values), 2) if mit_values else None,
                "median_minutes": round(statistics.median(mit_values), 2) if mit_values else None,
                "p95_minutes": _percentile(mit_values, 0.95),
                "min_minutes": min(mit_values) if mit_values else None,
                "max_minutes": max(mit_values) if mit_values else None,
                "sla_compliance_pct": mit_compliance,
            },
            escalation_rate_pct=round(escalated / total_incidents * 100.0, 2)
            if total_incidents
            else 0.0,
            escalated_count=escalated,
            volume_by_severity=by_severity,
            time_series_by_day=dict(sorted(by_day.items())),
        )

    return _error(name, f"unknown tool {name!r}", available=[t.name for t in TOOLS])


async def _main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_main())
