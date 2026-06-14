"""Pseudo multi-agent runner for large ChaosArena specs.

This is intentionally not distributed yet. It implements the first useful
architectural split:

  - rule-based Coordinator: groups Required Rs into small batches
  - existing run_agent Executor: evaluates one batch at a time
  - rule-based Judge: merges per-R verdicts into one final result

The goal is to validate whether smaller executor contexts improve completion
rate before adding true parallelism or an LLM coordinator.
"""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.config import (
    BEDROCK_PRICING_VERSION,
    DEFAULT_MAX_TURNS,
    DEFAULT_R_ESTIMATED_TURNS,
)
from runner import run_agent
from spec_parser import ParsedSpec, RequiredCategory, parse_spec


DEFAULT_BATCH_ESTIMATED_TURNS = 8


@dataclass(frozen=True)
class RBatch:
    index: int
    required: list[RequiredCategory]

    @property
    def ids(self) -> list[str]:
        return [r.r_id for r in self.required]

    @property
    def estimated_turns(self) -> int:
        return sum(int(getattr(r, "estimated_turns", DEFAULT_R_ESTIMATED_TURNS)) for r in self.required)

    @property
    def label(self) -> str:
        ids = self.ids
        if not ids:
            return f"batch{self.index:02d}"
        return f"batch{self.index:02d}_{ids[0]}-{ids[-1]}"


def plan_r_batches(
    parsed_spec: ParsedSpec,
    target_estimated_turns: int = DEFAULT_BATCH_ESTIMATED_TURNS,
) -> list[RBatch]:
    """Group Required Rs into consecutive batches with bounded estimated size.

    Consecutive grouping is deliberately simple and service-agnostic. It keeps
    related requirements near each other because drafter output usually orders
    Rs by category and dependency.
    """
    batches: list[RBatch] = []
    current: list[RequiredCategory] = []
    current_estimate = 0

    for r in parsed_spec.required:
        estimate = int(getattr(r, "estimated_turns", DEFAULT_R_ESTIMATED_TURNS))
        would_exceed = current and current_estimate + estimate > target_estimated_turns
        if would_exceed:
            batches.append(RBatch(index=len(batches) + 1, required=current))
            current = []
            current_estimate = 0
        current.append(r)
        current_estimate += estimate

    if current:
        batches.append(RBatch(index=len(batches) + 1, required=current))

    return batches


def _section_prefix(original_spec: str) -> str:
    marker = "## Required Test Categories"
    idx = original_spec.find(marker)
    if idx == -1:
        return "# ChaosArena Batch Spec\n\n"
    return original_spec[:idx].rstrip() + "\n\n"


def _format_batch_spec(original_spec: str, parsed_spec: ParsedSpec, batch: RBatch) -> str:
    """Build a smaller markdown spec containing only one R batch."""
    lines = [
        _section_prefix(original_spec),
        "## Required Test Categories",
        "",
        "<!-- Pseudo multi-agent batch spec: only the Rs below are in scope for this executor. -->",
        "",
    ]
    for r in batch.required:
        lines.extend([
            f"### {r.r_id}. {r.title}",
            "",
            r.body.strip(),
            "",
        ])

    lines.extend([
        "## Open Exploration",
        "",
        "Disabled for pseudo multi-agent executor batches. After all Required Rs in this batch have per-R verdicts, call `submit_verdict` immediately.",
        "",
        "## Out of Scope",
        "",
        parsed_spec.out_of_scope.strip() or "Anything outside this batch's Required Rs.",
        "",
    ])
    return "\n".join(lines)


def _batch_max_turns(batch: RBatch, global_max_turns: int) -> int:
    """Allocate a conservative per-executor turn budget."""
    estimated = max(1, batch.estimated_turns)
    budget = max(6, math.ceil(estimated * 1.6) + 3)
    return min(max(6, global_max_turns), budget)


def _executor_prompt(system_prompt: str, run_id: str, batch: RBatch) -> str:
    ids = ", ".join(batch.ids)
    return (
        system_prompt
        + "\n\n# Pseudo Multi-Agent Executor Mode\n\n"
        + f"You are one executor assigned only this Required-R batch: {ids}.\n"
        + "Do not attempt Required Rs outside this batch. Do not perform Open Exploration.\n"
        + "Use unique resource ids prefixed with "
        + f"`{run_id}_{batch.label}` to avoid interfering with other executor batches.\n"
        + "After every R in this batch has a `submit_verdict_for_R`, call `submit_verdict` immediately.\n"
    )


def _merge_usage(batch_results: list[dict[str, Any]]) -> dict[str, Any]:
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "pricing_version": BEDROCK_PRICING_VERSION,
        "per_batch": [],
    }
    for item in batch_results:
        batch_usage = item.get("result", {}).get("usage") or {}
        usage["input_tokens"] += int(batch_usage.get("input_tokens", 0))
        usage["output_tokens"] += int(batch_usage.get("output_tokens", 0))
        usage["cache_creation_input_tokens"] += int(batch_usage.get("cache_creation_input_tokens", 0))
        usage["cache_read_input_tokens"] += int(batch_usage.get("cache_read_input_tokens", 0))
        usage["cost_usd"] += float(batch_usage.get("cost_usd", 0.0))
        usage["per_batch"].append({
            "batch": item.get("batch_label"),
            "r_ids": item.get("r_ids", []),
            "usage": batch_usage,
        })
    usage["cost_usd"] = round(float(usage["cost_usd"]), 6)
    return usage


def _judge_aggregate(
    original_spec: str,
    target: str,
    run_id: str,
    parsed_spec: ParsedSpec,
    batch_results: list[dict[str, Any]],
) -> dict[str, Any]:
    required_ids = parsed_spec.required_ids
    r_verdicts: dict[str, dict[str, Any]] = {}
    r_verdict_history: dict[str, list[dict[str, Any]]] = {}
    r_amendments: dict[str, Any] = {}
    exploratory_findings: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    total_turns = 0
    total_tool_calls = 0

    for item in batch_results:
        result = item["result"]
        total_turns += int(result.get("turns", 0))
        total_tool_calls += int(result.get("tool_calls", 0))
        r_verdicts.update(result.get("r_verdicts") or {})
        r_verdict_history.update(result.get("r_verdict_history") or {})
        r_amendments.update(result.get("r_amendments") or {})
        exploratory_findings.extend(result.get("exploratory_findings") or [])
        events.extend(result.get("events") or [])

    missing = [r_id for r_id in required_ids if r_id not in r_verdicts]
    failed = [
        r_id for r_id, verdict in r_verdicts.items()
        if str(verdict.get("verdict", "")).upper() == "FAILED"
    ]
    batch_timeouts = [
        item["batch_label"] for item in batch_results
        if item["result"].get("verdict") in {"TIMEOUT", "INCONCLUSIVE"}
    ]

    if failed:
        verdict = "FAIL"
    elif missing or batch_timeouts:
        verdict = "TIMEOUT" if batch_timeouts else "INCONCLUSIVE"
    else:
        verdict = "PASS"

    batch_lines = []
    for item in batch_results:
        result = item["result"]
        covered = len(result.get("r_verdicts") or {})
        expected = len(item.get("r_ids") or [])
        batch_lines.append(
            f"- {item['batch_label']} ({', '.join(item['r_ids'])}): "
            f"{result.get('verdict', 'UNKNOWN')}, covered {covered}/{expected}, "
            f"turns={result.get('turns', '?')}, tools={result.get('tool_calls', '?')}"
        )

    reasoning = "\n".join([
        "Pseudo multi-agent aggregate verdict.",
        "",
        "Batch results:",
        *batch_lines,
        "",
        f"Required coverage: {len(r_verdicts)}/{len(required_ids)}.",
        f"Missing Rs: {missing or 'none'}.",
        f"Failed Rs: {failed or 'none'}.",
    ])

    now = datetime.now(timezone.utc).isoformat()
    usage = _merge_usage(batch_results)
    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "tool_calls": total_tool_calls,
        "turns": total_turns,
        "eval_mode": "pseudo_multi_agent_cover_all",
        "required_ids": required_ids,
        "r_verdicts": r_verdicts,
        "r_verdict_history": r_verdict_history,
        "r_amendments": r_amendments,
        "r_missing": missing,
        "r_turn_budgets": {},
        "exploratory_findings": exploratory_findings,
        "events": events,
        "usage": usage,
        "pseudo_multi_agent": {
            "coordinator": "rule_based_consecutive_estimated_turn_batches",
            "batch_target_estimated_turns": DEFAULT_BATCH_ESTIMATED_TURNS,
            "batches": [
                {
                    "label": item["batch_label"],
                    "r_ids": item["r_ids"],
                    "max_turns": item["max_turns"],
                    "verdict": item["result"].get("verdict"),
                    "turns": item["result"].get("turns"),
                    "tool_calls": item["result"].get("tool_calls"),
                    "missing": item["result"].get("r_missing", []),
                }
                for item in batch_results
            ],
        },
        "repro": {
            "started_at_utc": batch_results[0]["started_at_utc"] if batch_results else now,
            "finished_at_utc": now,
            "model": batch_results[0]["result"].get("repro", {}).get("model", "?") if batch_results else "?",
            "target_url": target,
            "eval_mode": "pseudo_multi_agent_cover_all",
            "max_turns": DEFAULT_MAX_TURNS,
            "temperature": batch_results[0]["result"].get("repro", {}).get("temperature", "?") if batch_results else "?",
            "git_commit": batch_results[0]["result"].get("repro", {}).get("git_commit", "?") if batch_results else "?",
            "spec_sha256": hashlib.sha256(original_spec.encode("utf-8")).hexdigest(),
            "system_prompt_sha256": batch_results[0]["result"].get("repro", {}).get("system_prompt_sha256", "?") if batch_results else "?",
        },
        "batch_results": batch_results,
    }


def _write_aggregate_trace(run_id: str, aggregate: dict[str, Any]) -> None:
    trace_dir = Path(__file__).resolve().parent / "trace"
    trace_dir.mkdir(exist_ok=True)
    path = trace_dir / f"run_{run_id}.json"
    payload = {
        "run_id": run_id,
        "eval_mode": aggregate.get("eval_mode"),
        "verdict": aggregate.get("verdict"),
        "reasoning": aggregate.get("reasoning"),
        "required_ids": aggregate.get("required_ids", []),
        "r_verdicts": aggregate.get("r_verdicts", {}),
        "r_missing": aggregate.get("r_missing", []),
        "usage": aggregate.get("usage", {}),
        "pseudo_multi_agent": aggregate.get("pseudo_multi_agent", {}),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"[pseudo] aggregate trace written to {path}")


def run_pseudo_multi_agent(
    system_prompt: str,
    spec: str,
    target: str,
    dump_messages_to: Path | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    run_id_override: str | None = None,
) -> dict[str, Any]:
    """Run one executor per R batch and aggregate their per-R verdicts."""
    run_id = run_id_override or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    parsed_spec = parse_spec(spec)
    if not parsed_spec.required:
        print("[pseudo] no Required Rs parsed; falling back to single-agent runner")
        return run_agent(
            system_prompt=system_prompt,
            spec=spec,
            target=target,
            dump_messages_to=dump_messages_to,
            max_turns=max_turns,
            run_id_override=run_id,
        )

    batches = plan_r_batches(parsed_spec)
    print(f"[pseudo] planned {len(batches)} executor batch(es):")
    for batch in batches:
        print(f"[pseudo]   {batch.label}: {batch.ids} estimated={batch.estimated_turns}")

    batch_results: list[dict[str, Any]] = []
    for batch in batches:
        batch_run_id = f"{run_id}__{batch.label}"
        batch_max_turns = _batch_max_turns(batch, max_turns)
        batch_spec = _format_batch_spec(spec, parsed_spec, batch)
        batch_prompt = _executor_prompt(system_prompt, run_id, batch)
        if dump_messages_to is not None:
            batch_memory_path = dump_messages_to.with_name(
                f"run_{batch_run_id}_messages{dump_messages_to.suffix}"
            )
        else:
            batch_memory_path = None

        print()
        print("=" * 72)
        print(f"[pseudo] executor {batch.label}: Rs={batch.ids} max_turns={batch_max_turns}")
        print("=" * 72)

        started_at = datetime.now(timezone.utc).isoformat()
        result = run_agent(
            system_prompt=batch_prompt,
            spec=batch_spec,
            target=target,
            dump_messages_to=batch_memory_path,
            max_turns=batch_max_turns,
            run_id_override=batch_run_id,
        )
        batch_results.append({
            "batch_label": batch.label,
            "r_ids": batch.ids,
            "max_turns": batch_max_turns,
            "started_at_utc": started_at,
            "result": result,
        })

    aggregate = _judge_aggregate(
        original_spec=spec,
        target=target,
        run_id=run_id,
        parsed_spec=parsed_spec,
        batch_results=batch_results,
    )
    if dump_messages_to is not None:
        dump_messages_to.parent.mkdir(parents=True, exist_ok=True)
        dump_messages_to.write_text(
            json.dumps(aggregate, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"[pseudo] aggregate messages written to {dump_messages_to}")
    _write_aggregate_trace(run_id, aggregate)
    return aggregate
