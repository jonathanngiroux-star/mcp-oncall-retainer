# mcp-oncall-retainer

MCP server for Agent Incident On-Call - on-call status, incident management, response metrics.

## Tools

### get_oncall_status
Get current on-call status and upcoming schedule.

**Parameters:**
- `weeks_ahead` (integer, optional): Weeks of schedule to return (default: 2)

### create_incident
Create a new incident.

**Parameters:**
- `title` (string, required): Incident title
- `agent_id` (string, required): Affected agent ID
- `severity` (string, required): "low", "medium", "high", or "critical"
- `description` (string, optional): Incident description

### get_incident_history
Get incident history for an agent.

**Parameters:**
- `agent_id` (string, optional): Agent identifier
- `days` (integer, optional): Days of history (default: 30)
- `severity` (string, optional): "low", "medium", "high", "critical", or "all" (default: "all")

### get_response_metrics
Get on-call response metrics (response time, resolution time, SLA compliance).

**Parameters:**
- `period_days` (integer, optional): Period in days (default: 30)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

## Connecting to Claude Desktop

```json
{
  "mcpServers": {
    "oncall-retainer": {
      "command": "python",
      "args": ["/path/to/mcp-oncall-retainer/main.py"],
      "cwd": "/path/to/mcp-oncall-retainer"
    }
  }
}
```

## License

MIT