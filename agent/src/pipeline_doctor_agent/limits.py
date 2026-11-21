"""Run limits: LLM calls, tool calls, tokens. (Wall-clock is enforced by the investigator.)"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn


class BudgetExceeded(RuntimeError):
    """A run limit was hit; the run is abandoned and reported, never silently continued."""

    def __init__(self, limit: str, detail: str) -> None:
        super().__init__(f"{limit}: {detail}")
        self.limit = limit


@dataclass
class RunBudget:
    max_llm_calls: int
    max_tool_calls: int
    token_budget: int
    llm_calls: int = 0
    tool_calls: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    # Remembered so the cause survives however the agent framework wraps (or loses) the exception,
    # for example when two parallel branches fail at the same moment.
    tripped: BudgetExceeded | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.output_tokens

    @property
    def steps(self) -> int:
        return self.llm_calls + self.tool_calls

    def on_llm_call(self) -> None:
        self.llm_calls += 1
        if self.llm_calls > self.max_llm_calls:
            self._trip("max_llm_calls", f"more than {self.max_llm_calls} model calls")

    def on_tool_call(self) -> None:
        self.tool_calls += 1
        if self.tool_calls > self.max_tool_calls:
            self._trip("max_tool_calls", f"more than {self.max_tool_calls} tool calls")

    def on_usage(self, prompt: int, output: int) -> None:
        self.prompt_tokens += prompt
        self.output_tokens += output
        if self.total_tokens > self.token_budget:
            self._trip(
                "token_budget", f"{self.total_tokens} tokens used, budget {self.token_budget}"
            )

    def _trip(self, limit: str, detail: str) -> NoReturn:
        self.tripped = self.tripped or BudgetExceeded(limit, detail)
        raise self.tripped
