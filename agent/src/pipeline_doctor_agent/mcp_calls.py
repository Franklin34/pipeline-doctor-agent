"""A policy-checked, audited MCP session shared by every code-driven writer (tickets, pull requests)."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from mcp.client import Client

from pipeline_doctor_agent.audit import AuditLog
from pipeline_doctor_agent.policy.tools import ToolPolicy


class ToolCallError(RuntimeError):
    """A tool call was refused by policy or failed on the server."""


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    return [exc]


@contextlib.contextmanager
def flatten_errors() -> Iterator[None]:
    """Unwrap an exception group that holds exactly one real error.

    An MCP client runs inside an anyio task group, which wraps whatever the body raises in an
    ExceptionGroup. Callers (and the queue's error log) care about the actual error, such as
    "refused: protected path", not "unhandled errors in a TaskGroup". Use it OUTSIDE the client context.
    """
    try:
        yield
    except BaseExceptionGroup as group:
        leaves = _leaves(group)
        if len(leaves) == 1:
            raise leaves[0] from None
        raise


class PolicySession:
    """Wraps a Client: every call passes the tool policy for ``agent`` and is written to the audit log."""

    def __init__(self, client: Client, policy: ToolPolicy, audit: AuditLog, agent: str) -> None:
        self.client, self.policy, self.audit, self.agent = client, policy, audit, agent

    async def call(self, tool: str, **args: Any) -> dict[str, Any]:
        reason = self.policy.check(self.agent, tool, args)
        if reason:
            self.audit.record(self.agent, tool, args, "blocked", reason)
            raise ToolCallError(f"refused by policy: {reason}")
        res = await self.client.call_tool(tool, args)
        if res.is_error:
            detail = "".join(getattr(c, "text", "") for c in res.content)[:300]
            self.audit.record(self.agent, tool, args, "error", detail)
            raise ToolCallError(f"{tool} failed: {detail}")
        self.audit.record(self.agent, tool, args, "ok")
        return dict(res.structured_content or {})
