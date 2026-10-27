# mcp-oncall-retainer

MCP server for the **Agent Incident On-Call Retainer** - the staffed escalation layer. When an
SLA-breaching event fires, a named responder owns triage, mitigation, and post-mortem. Distinct
from the SLA contract service: this is the people.

Pricing in the V3 catalogue: $49-199/mo base; $199-500/mo guaranteed-response tiers; desks
indicated willingness above $400 when bundled with SLA terms. The "can a small operator truly
staff 24/7?" objection is answered with published staffing transparency, follow-the-sun coverage
and credits.

## Tools

### `get_oncall_status`
Resolve who is on call at a given moment from the follow-the-sun rotation schedule.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `team` | string enum `core`/`defi` | `core` | On-call team to query |
| `at` | string | now | ISO-8601 timestamp to resolve on-call for |

Returns the active responder, shift window, minutes into/remaining in shift, the handoff partner
and its handoff time, the next rotation, the team's escalation ladder, and staffing-transparency
metrics (rotations per day, distinct responders, follow-the-sun coverage hours, whether coverage
is 24x7, single-operator risk).

### `create_incident`
Raise an incident and persist it.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `title` | string | **required** | Incident title |
| `severity` | string enum `sev1`/`sev2`/`sev3` | `sev2` | Severity |
| `service` | string | `unspecified` | Affected service or agent |
| `summary` | string | - | What happened |
| `team` | string enum `core`/`defi` | `core` | Owning on-call team |
| `raised_at` | string | now | ISO-8601 raise time |
| `acknowledged_at` | string | - | Optional acknowledgement time |
| `mitigated_at` | string | - | Optional mitigation time |
| `resolved_at` | string | - | Optional resolution time |
| `status` | string enum `open`/`acknowledged`/`mitigated`/`resolved`/`postmortem` | `open` | Initial status |

The responder is resolved from the rotation at the raise time. Acknowledge and mitigate deadlines
are derived from the severity multipliers and the team's response tier; the response tier's
mitigation window doubles as the SLA recovery window, and any breach is linked to the Agent
Failure-Recovery SLA service for credit computation. Timestamps that precede `raised_at` are
rejected with `status: "error"`.

### `get_incident_history`
Incident history, newest first.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `team` | string enum | - | Filter by team |
| `severity` | string enum | - | Filter by severity |
| `service` | string | - | Filter by affected service |
| `status` | string enum | - | Filter by status |
| `since` | string | - | Only incidents raised at or after this ISO timestamp |
| `limit` | integer | 50 | Maximum entries (1-500) |

Each incident carries its triage timeline (raised / acknowledged / mitigated / resolved with
minutes-from-raise) and its post-mortem link.

### `get_response_metrics`
Response metrics computed from the incident record set.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `window_days` | integer | 30 | Look-back window in days (1-365) |
| `team` | string enum | - | Filter by team |
| `service` | string | - | Filter by service |

Returns mean/median/p95/min/max time-to-acknowledge and time-to-mitigate, acknowledgement and
mitigation SLA compliance percentages, escalation rate, incident volume by severity, and a daily
time series. All figures are computed from the actual incident records with Python statistics.

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

State is persisted as JSON in `./state/oncall_state.json`. Override the location with
`MCP_ONCALL_RETAINER_STATE_DIR`.

## Connecting to an MCP client

```json
{
  "mcpServers": {
    "oncall-retainer": {
      "command": "python3",
      "args": ["/tmp/v3-services/mcp-oncall-retainer/main.py"]
    }
  }
}
```

## License

MIT
