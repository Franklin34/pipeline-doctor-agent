"""JSON logs that carry the trace id, so a log line can be matched to its trace and back.

The standard formatter drops everything passed through ``extra=``; this one keeps it. Values are
cut to a fixed length so one oversized field cannot flood the log pipeline.
"""

from __future__ import annotations

import json
import logging

from pipeline_doctor_agent.observability.tracing import current_ids

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
MAX_FIELD = 500


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage()[:MAX_FIELD],
        }
        ids = current_ids()
        if ids:
            entry["trace_id"], entry["span_id"] = ids
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_"):
                entry[key] = (
                    value if isinstance(value, int | float | bool) else str(value)[:MAX_FIELD]
                )
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)[-2000:]
        return json.dumps(entry, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
