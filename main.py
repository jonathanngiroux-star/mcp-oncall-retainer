#!/usr/bin/env python3
"""MCP server for oncall-retainer - Agent Incident On-Call"""

import json
import os
from datetime import datetime, timedelta
from mcp.server import Server
from mcp.types import Tool, TextContent

app = Server("mcp-oncall-retainer")

INCIDENTS = {
    "inc-001": {
        "id": "inc-001",
        "title": "Agent Trading Bot - API Timeout",
        "agent_id": "agent-001",
        "severity": "high",
        "status": "resolved",
        "created_at": "2024-12-20T14:00:00",
        "acknowledged_at": "2024-12-20T14:02:00",
        "resolved_at": "2024-12-20T14:45:00",
        "responder": "oncall-engineer-1",
        "root_cause": "Upstream API rate limit",
        "resolution": "Failover to backup endpoint",
        "response_time_minutes": 2,
        "resolution_time_minutes": 45
    },
    "inc-002": {
        "id": "inc-002",
        "title": "Monitoring Agent - Memory Leak",
        "agent_id": "agent-002",
        "severity": "critical",
        "status": "resolved",
        "created_at": "2024-12-18T09:00:00",
        "acknowledged_at": "2024-12-18T09:05:00",
        "resolved_at": "2024-12-18T11:00:00",
        "responder": "oncall-engineer-2",
        "root_cause": "Memory leak in data processor",
        "resolution": "Restart + hotfix deployed",
        "response_time_minutes": 5,
        "resolution_time_minutes": 120
    }
}

ONCALL_SCHEDULE = [
    {"engineer": "oncall-engineer-1", "start": "2024-12-23T00:00:00", "end": "2024-12-30T00:00:00", "timezone": "UTC"},
    {"engineer": "oncall-engineer-2", "start": "2024-12-30T00:00:00", "end": "2025-01-06T00:00:00", "timezone": "UTC"},
    {"engineer": "oncall-engineer-3", "start": "2025-01-06T00:00:00", "end": "2025-01-13T00:00:00", "timezone": "UTC"}
]

app = Server("mcp-oncall-retainer")

TOOLS = [
    Tool(
        name="get_oncall_status",
        description="Get current on-call status and schedule",
        inputSchema={
            "type": "object",
            "properties": {
                "weeks_ahead": {"type": "integer", "default": 2, "description": "Weeks of schedule to return"}
            }
        }
    ),
    Tool(
        name="create_incident",
        description="Create a new incident",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Incident title"},
                "agent_id": {"type": "string", "description": "Affected agent ID"},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"], "description": "Severity level"},
                "description": {"type": "string", "description": "Incident description"}
            },
            "required": ["title", "agent_id", "severity"]
        }
    ),
    Tool(
        name="get_incident_history",
        description="Get incident history for an agent",
        inputSchema={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent identifier (optional)"},
                "days": {"type": "integer", "default": 30, "description": "Days of history"},
                "severity": {"type": "string", "enum": ["low", "medium", "high", "critical", "all"], "default": "all"}
            }
        }
    ),
    Tool(
        name="get_response_metrics",
        description="Get on-call response metrics",
        inputSchema={
            "type": "object",
            "properties": {
                "period_days": {"type": "integer", "default": 30, "description": "Period in days"}
            }
        }
    )
]

@app.list_tools()
async def list_tools():
    return TOOLS

@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "get_oncall_status":
        weeks_ahead = arguments.get("weeks_ahead", 2)
        
        current = None
        upcoming = []
        now = datetime.now()
        
        for shift in ONCALL_SCHEDULE:
            start = datetime.fromisoformat(shift["start"])
            end = datetime.fromisoformat(shift["end"])
            if start <= now <= end:
                current = shift
            elif start > now:
                upcoming.append(shift)
        
        result = {
            "status": "ok",
            "tool": "get_oncall_status",
            "current_oncall": current,
            "upcoming_shifts": upcoming[:weeks_ahead],
            "checked_at": datetime.now().isoformat()
        }
        return [TextContent(type="text", text=json.dumps(result))]
    
    elif name == "create_incident":
        incident_id = f"inc-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        incident = {
            "id": incident_id,
            "title": arguments.get("title"),
            "agent_id": arguments.get("agent_id"),
            "severity": arguments.get("severity"),
            "description": arguments.get("description", ""),
            "status": "open",
            "created_at": datetime.now().isoformat(),
            "acknowledged_at": None,
            "resolved_at": None,
            "responder": None,
            "root_cause": None,
            "resolution": None
        }
        INCIDENTS[incident_id] = incident
        
        result = {
            "status": "ok",
            "tool": "create_incident",
            "incident": incident,
            "message": f"Incident {incident_id} created. On-call engineer notified."
        }
        return [TextContent(type="text", text=json.dumps(result))]
    
    elif name == "get_incident_history":
        agent_id = arguments.get("agent_id")
        days = arguments.get("days", 30)
        severity = arguments.get("severity", "all")
        
        cutoff = datetime.now() - timedelta(days=days)
        incidents = list(INCIDENTS.values())
        
        if agent_id:
            incidents = [i for i in incidents if i["agent_id"] == agent_id]
        if severity != "all":
            incidents = [i for i in incidents if i["severity"] == severity]
        
        incidents = [i for i in incidents if datetime.fromisoformat(i["created_at"]) >= cutoff]
        incidents.sort(key=lambda x: x["created_at"], reverse=True)
        
        result = {
            "status": "ok",
            "tool": "get_incident_history",
            "count": len(incidents),
            "incidents": incidents
        }
        return [TextContent(type="text", text=json.dumps(result))]
    
    elif name == "get_response_metrics":
        period_days = arguments.get("period_days", 30)
        
        cutoff = datetime.now() - timedelta(days=period_days)
        incidents = [i for i in INCIDENTS.values() if datetime.fromisoformat(i["created_at"]) >= cutoff]
        
        if not incidents:
            return [TextContent(type="text", text=json.dumps({"status": "ok", "tool": "get_response_metrics", "message": "No incidents in period"}))]
        
        total = len(incidents)
        acked = [i for i in incidents if i["acknowledged_at"]]
        resolved = [i for i in incidents if i["resolved_at"]]
        
        avg_response = sum((datetime.fromisoformat(i["acknowledged_at"]) - datetime.fromisoformat(i["created_at"])).total_seconds() / 60 for i in acked) / len(acked) if acked else 0
        avg_resolution = sum((datetime.fromisoformat(i["resolved_at"]) - datetime.fromisoformat(i["created_at"])).total_seconds() / 60 for i in resolved) / len(resolved) if resolved else 0
        
        by_severity = {}
        for s in ["low", "medium", "high", "critical"]:
            sev_incidents = [i for i in incidents if i["severity"] == s]
            by_severity[s] = len(sev_incidents)
        
        result = {
            "status": "ok",
            "tool": "get_response_metrics",
            "period_days": period_days,
            "total_incidents": total,
            "acknowledged": len(acked),
            "resolved": len(resolved),
            "avg_response_time_minutes": round(avg_response, 1),
            "avg_resolution_time_minutes": round(avg_resolution, 1),
            "by_severity": by_severity,
            "sla_compliance": {
                "response_under_5min": len([i for i in acked if (datetime.fromisoformat(i["acknowledged_at"]) - datetime.fromisoformat(i["created_at"])).total_seconds() / 60 <= 5]) / len(acked) * 100 if acked else 100,
                "resolution_under_60min": len([i for i in resolved if (datetime.fromisoformat(i["resolved_at"]) - datetime.fromisoformat(i["created_at"])).total_seconds() / 60 <= 60]) / len(resolved) * 100 if resolved else 100
            }
        }
        return [TextContent(type="text", text=json.dumps(result))]
    
    return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

if __name__ == "__main__":
    import asyncio
    from mcp.server.stdio import stdio_server
    
    async def serve():
        reader, writer = await stdio_server()
        await app.run(reader, writer)
    
    asyncio.run(serve())