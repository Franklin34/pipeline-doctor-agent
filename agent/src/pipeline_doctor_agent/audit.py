"""Audit log of every tool call: agent, tool, arguments (scrubbed), result status."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from pipeline_doctor_agent.observability import metrics
from pipeline_doctor_agent.preprocess.scrub import scrub

log = logging.getLogger("pipeline_doctor.audit")
MAX_VALUE = 300


def redact(obj: Any) -> Any:
    """Recursively scrub secrets from strings and cap their length (for logs and audit)."""
    if isinstance(obj, str):
        text = scrub(obj).text
        return text if len(text) <= MAX_VALUE else text[:MAX_VALUE] + "...(clipped)"
    if isinstance(obj, dict):
        return {str(k): redact(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [redact(v) for v in obj]
    return obj


@dataclass(frozen=True)
class AuditRecord:
    run_id: str
    ts: float
    agent: str
    tool: str
    args: dict[str, Any]
    status: str  # ok | error | blocked
    detail: str = ""


@dataclass
class AuditLog:
    run_id: str
    records: list[AuditRecord] = field(default_factory=list)

    def record(
        self, agent: str, tool: str, args: dict[str, Any], status: str, detail: str = ""
    ) -> None:
        rec = AuditRecord(
            run_id=self.run_id,
            ts=time.time(),
            agent=agent,
            tool=tool,
            args=redact(args),
            status=status,
            detail=redact(detail),
        )
        self.records.append(rec)
        m = metrics.current()
        m.tool_call(agent, status)
        if status == "blocked":
            m.guardrail_block(agent)
        level = logging.WARNING if status == "blocked" else logging.INFO
        log.log(level, json.dumps(asdict(rec), default=str))

    def count(self, status: str) -> int:
        return sum(1 for r in self.records if r.status == status)
