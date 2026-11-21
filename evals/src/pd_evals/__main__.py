"""``python -m pd_evals``: run the golden dataset.

Default is the scripted model: deterministic, free, runs in CI, and proves everything around the
model (tools, policy, screening, gate, scrubbing). ``--live`` uses the real model: that is what
measures diagnosis quality, it costs money, and it needs credentials, so it is never the default.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from pipeline_doctor_agent.settings import Settings

from pd_evals.runner import build_jenkins_mcp, run_scenario
from pd_evals.scenario import load_scenarios
from pd_evals.scoring import SOFT, render_markdown, score

ROOT = Path(__file__).resolve().parents[3]
BASELINE = ROOT / "evals" / "baseline.json"


def hermetic(s: Settings) -> Settings:
    """An evaluation runs on fixtures only. Whatever the environment says (real GitHub or Jira, a
    token, automatic fixes), the eval must never reach a real service or write anything."""
    return replace(
        s,
        mcp_mode="mock",
        github_mode="mock",
        jira_mode="mock",
        github_token=None,
        jira_api_token=None,
        fix_enabled=False,
        queue_backend="redis",
    )


async def amain(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(ROOT / "fixtures", set(args.scenario) or None)
    base = hermetic(Settings.from_env() if args.live else Settings())
    settings = replace(
        base,
        fixtures_dir=str(ROOT / "fixtures"),
        jenkins_mcp_command=os.environ.get("JENKINS_MCP_COMMAND") or build_jenkins_mcp(ROOT),
        run_timeout_s=120,
    )
    pairs = [(sc, await run_scenario(sc, settings, live=args.live)) for sc in scenarios]
    baseline = None
    if BASELINE.exists() and not args.live:  # the baseline describes the scripted model only
        baseline = json.loads(BASELINE.read_text())
    report = score(pairs, baseline)
    mode = "live" if args.live else "scripted"
    text = render_markdown(pairs, report, mode)
    print(text)
    if args.report:
        Path(args.report).write_text(text)
    if args.write_baseline:
        BASELINE.write_text(json.dumps({k: report.metrics[k] for k in SOFT}, indent=2) + "\n")
        print(f"baseline written to {BASELINE}")
    return 0 if report.ok else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="pd_evals")
    p.add_argument("--live", action="store_true", help="use the real model (costs money)")
    p.add_argument("--scenario", action="append", default=[], help="run only this scenario id")
    p.add_argument("--report", help="also write the markdown report to this file")
    p.add_argument("--write-baseline", action="store_true", help="store current soft metrics")
    sys.exit(asyncio.run(amain(p.parse_args())))


if __name__ == "__main__":
    main()
