"""Multi-agent orchestrator for large ChaosArena specs.

This is intentionally not distributed yet. It implements the first useful
architectural split:

  - LLM Orchestrator/Judge: groups Required Rs into batches and plans repairs
  - batch executor: evaluates one batch at a time
  - rule-based Judge: merges per-R verdicts into one final result

The goal is to validate whether smaller executor contexts improve completion
rate before adding true parallelism.
"""

import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.config import (
    BEDROCK_PRICING_VERSION,
    DEFAULT_MAX_TURNS,
    DEFAULT_R_ESTIMATED_TURNS,
    MAX_TOKENS,
    TEMPERATURE,
)
from batch_executor import estimate_cost, get_client, get_model_id, run_agent
from spec_parser import ParsedSpec, RequiredCategory, parse_spec


DEFAULT_BATCH_ESTIMATED_TURNS = 6
MAX_BATCH_ESTIMATED_TURNS = 8  # hard cap: coordinator batches exceeding this are split
COORDINATOR_MAX_TOKENS = min(4096, MAX_TOKENS)


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


def _batch_from_ids(index: int, r_ids: list[str], parsed_spec: ParsedSpec) -> RBatch:
    by_id = {r.r_id: r for r in parsed_spec.required}
    required = [by_id[r_id] for r_id in r_ids if r_id in by_id]
    return RBatch(index=index, required=required)


def _required_summaries(parsed_spec: ParsedSpec) -> list[dict[str, Any]]:
    return [
        {
            "r_id": r.r_id,
            "title": r.title,
            "estimated_turns": int(getattr(r, "estimated_turns", DEFAULT_R_ESTIMATED_TURNS)),
            "body": r.body.strip(),
        }
        for r in parsed_spec.required
    ]


def _extract_json_object(text: str) -> dict[str, Any]:
    """Parse the first JSON object from an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def _usage_from_response(usage: Any, phase: str) -> dict[str, Any]:
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    return {
        "phase": phase,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "cost_usd": round(estimate_cost(input_tokens, output_tokens, cache_creation, cache_read), 6),
    }


def _coordinator_json_call(
    phase: str,
    system_text: str,
    user_text: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Ask the LLM Coordinator for a JSON decision."""
    client = get_client()
    model = get_model_id()
    response = client.messages.create(
        model=model,
        system=system_text,
        messages=[{"role": "user", "content": user_text}],
        max_tokens=COORDINATOR_MAX_TOKENS,
        temperature=TEMPERATURE,
    )
    text = "\n".join(
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    )
    usage = _usage_from_response(response.usage, phase=phase)
    usage["model"] = model
    usage["raw_response"] = text
    try:
        return _extract_json_object(text), usage
    except Exception as exc:
        usage["parse_error"] = str(exc)
        return None, usage


def _coordinator_system_prompt() -> str:
    return (Path(__file__).resolve().parent / "prompts" / "orchestrator_system.txt").read_text()


def _validate_batch_plan(
    data: dict[str, Any] | None,
    parsed_spec: ParsedSpec,
) -> list[list[str]]:
    """Normalize a coordinator plan into valid, non-duplicated R-id groups.

    Also enforces MAX_BATCH_ESTIMATED_TURNS: any coordinator batch that exceeds
    the hard cap is re-split using the rule-based planner.
    """
    required = parsed_spec.required_ids
    required_set = set(required)
    seen: set[str] = set()
    normalized: list[list[str]] = []

    raw_batches = (data or {}).get("batches", [])
    if not isinstance(raw_batches, list):
        raw_batches = []
    for raw in raw_batches:
        raw_ids = raw.get("r_ids") if isinstance(raw, dict) else raw
        if not isinstance(raw_ids, list):
            continue
        ids: list[str] = []
        for value in raw_ids:
            if not isinstance(value, str):
                continue
            r_id = value.strip().upper()
            if r_id in required_set and r_id not in seen:
                ids.append(r_id)
                seen.add(r_id)
        if not ids:
            continue
        # Hard-cap enforcement: if coordinator packed too many turns, re-split.
        candidate = _batch_from_ids(1, ids, parsed_spec)
        if candidate.estimated_turns > MAX_BATCH_ESTIMATED_TURNS:
            oversized_spec = ParsedSpec(
                required=candidate.required,
                open_exploration="",
                out_of_scope="",
            )
            sub_batches = plan_r_batches(oversized_spec, target_estimated_turns=DEFAULT_BATCH_ESTIMATED_TURNS)
            normalized.extend(sb.ids for sb in sub_batches)
        else:
            normalized.append(ids)

    # Safety fallback: never drop Required Rs if the coordinator omitted them.
    missing = [r_id for r_id in required if r_id not in seen]
    if missing:
        fallback_spec = ParsedSpec(
            required=_batch_from_ids(1, missing, parsed_spec).required,
            open_exploration="",
            out_of_scope="",
        )
        fallback = plan_r_batches(fallback_spec)
        normalized.extend(batch.ids for batch in fallback)
    return normalized


def _plan_batches_with_coordinator(
    parsed_spec: ParsedSpec,
    max_turns: int,
) -> tuple[list[RBatch], list[dict[str, Any]]]:
    """Use a real LLM coordinator for the initial batch plan."""
    user_text = json.dumps(
        {
            "task": "Plan executor batches for this ChaosArena run.",
            "constraints": [
                "Every Required R must appear exactly once.",
                "Group related Rs when they can share setup/evidence.",
                "Keep each batch small enough for one focused executor.",
                f"Hard cap: total estimated_turns per batch must be ≤ {MAX_BATCH_ESTIMATED_TURNS}. Prefer ≤ {DEFAULT_BATCH_ESTIMATED_TURNS}.",
                "A single complex R (estimated_turns ≥ 5) belongs in its own batch.",
                "Batches run in parallel — do NOT assume one batch's output is available to another.",
            ],
            "global_max_turns": max_turns,
            "required": _required_summaries(parsed_spec),
            "output_schema": {
                "batches": [
                    {
                        "r_ids": ["R1", "R2"],
                        "rationale": "why these Rs should share one executor",
                    }
                ]
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    data, usage = _coordinator_json_call(
        phase="initial_batch_plan",
        system_text=_coordinator_system_prompt(),
        user_text=user_text,
    )
    groups = _validate_batch_plan(data, parsed_spec)
    batches = [_batch_from_ids(index=i, r_ids=ids, parsed_spec=parsed_spec) for i, ids in enumerate(groups, start=1)]
    if not batches:
        batches = plan_r_batches(parsed_spec)
        usage["fallback"] = "rule_based_plan_no_valid_coordinator_batches"
    usage["planned_batches"] = [batch.ids for batch in batches]
    return batches, [usage]


def _plan_repair_batch_with_coordinator(
    parsed_spec: ParsedSpec,
    missing_r_ids: list[str],
    remaining_turns: int,
    batch_results: list[dict[str, Any]],
    repair_index: int,
) -> tuple[RBatch | None, dict[str, Any]]:
    """Ask the coordinator which missing Rs should be retried next."""
    if not missing_r_ids or remaining_turns <= 0:
        return None, {
            "phase": "repair_plan",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cost_usd": 0.0,
            "skipped": True,
        }
    result_summary = [
        {
            "batch": item["batch_label"],
            "r_ids": item["r_ids"],
            "verdict": item["result"].get("verdict"),
            "covered": sorted((item["result"].get("r_verdicts") or {}).keys()),
            "missing": item["result"].get("r_missing", []),
            "turns": item["result"].get("turns"),
            "reasoning": str(item["result"].get("reasoning", ""))[:1200],
        }
        for item in batch_results
    ]
    missing_details = [
        item for item in _required_summaries(parsed_spec)
        if item["r_id"] in set(missing_r_ids)
    ]
    user_text = json.dumps(
        {
            "task": "Plan one repair executor batch for missing Required Rs.",
            "remaining_executor_turns": remaining_turns,
            "missing_r_ids": missing_r_ids,
            "missing_required_details": missing_details,
            "previous_batch_results": result_summary,
            "constraints": [
                "Return at most one batch.",
                "Only include missing R ids.",
                "Choose the highest value set that can realistically finish within remaining_executor_turns.",
                "If no repair is realistic, return an empty r_ids list.",
            ],
            "output_schema": {
                "r_ids": ["R5", "R6"],
                "rationale": "why this repair batch is feasible",
            },
        },
        ensure_ascii=False,
        indent=2,
    )
    data, usage = _coordinator_json_call(
        phase=f"repair_plan_{repair_index}",
        system_text=_coordinator_system_prompt(),
        user_text=user_text,
    )
    raw_ids = (data or {}).get("r_ids", [])
    valid_missing = set(missing_r_ids)
    ids: list[str] = []
    if isinstance(raw_ids, list):
        for value in raw_ids:
            if isinstance(value, str):
                r_id = value.strip().upper()
                if r_id in valid_missing and r_id not in ids:
                    ids.append(r_id)
    if not ids and remaining_turns >= 4:
        ids = [missing_r_ids[0]]
        usage["fallback"] = "single_missing_r"
    usage["planned_repair_r_ids"] = ids
    if not ids:
        return None, usage
    return _batch_from_ids(index=repair_index, r_ids=ids, parsed_spec=parsed_spec), usage


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
        "<!-- Orchestrated executor batch spec: only the Rs below are in scope for this executor. -->",
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
        "Disabled for orchestrated executor batches. After all Required Rs in this batch have per-R verdicts, call `submit_verdict` immediately.",
        "",
        "## Out of Scope",
        "",
        parsed_spec.out_of_scope.strip() or "Anything outside this batch's Required Rs.",
        "",
    ])
    return "\n".join(lines)


def _batch_max_turns(
    batch: RBatch,
    global_max_turns: int,
    remaining_turns: int | None = None,
    is_repair: bool = False,
    remaining_batches: int = 1,
) -> int:
    """Allocate a per-executor turn budget.

    Initial batches: 1.5× estimated + 2, floor 6.
    Repair batches:  2.0× estimated + 4, floor 10.
    Both are also capped at remaining_turns // remaining_batches (fairness).
    """
    estimated = max(1, batch.estimated_turns)
    if is_repair:
        per_batch_cap = max(10, math.ceil(estimated * 2.0) + 4)
    else:
        per_batch_cap = max(6, math.ceil(estimated * 1.5) + 2)
    if remaining_turns is not None:
        fairness_cap = max(6, remaining_turns // max(1, remaining_batches))
        return min(per_batch_cap, fairness_cap)
    return min(max(6, global_max_turns), per_batch_cap)


MIN_VIABLE_BATCH_TURNS = 4  # skip a batch rather than run it with no real budget

MAX_PARALLEL_WORKERS = 8  # cap concurrent executor threads


def _run_one_batch(
    batch: RBatch,
    run_id: str,
    system_prompt: str,
    spec: str,
    parsed_spec: ParsedSpec,
    target: str,
    discovery_context: str,
    max_turns: int,
    dump_messages_to: "Path | None",
) -> dict[str, Any]:
    """Run one executor batch in a worker thread."""
    batch_run_id = f"{run_id}__{batch.label}"
    # No fairness cap for parallel batches — they don't compete for a sequential pool.
    batch_max_turns = _batch_max_turns(batch, max_turns)
    batch_spec = _format_batch_spec(spec, parsed_spec, batch)
    batch_prompt = _executor_prompt(system_prompt, run_id, batch, discovery_context)
    batch_memory_path = (
        dump_messages_to.with_name(f"run_{batch_run_id}_messages{dump_messages_to.suffix}")
        if dump_messages_to is not None else None
    )
    print(f"\n{'='*72}\n[executor:{batch.label}] Rs={batch.ids} max_turns={batch_max_turns}\n{'='*72}")
    started_at = datetime.now(timezone.utc).isoformat()
    result = run_agent(
        system_prompt=batch_prompt,
        spec=batch_spec,
        target=target,
        dump_messages_to=batch_memory_path,
        max_turns=batch_max_turns,
        run_id_override=batch_run_id,
        auto_submit_on_r_coverage=True,
        write_trace=False,
        executor_label=batch.label,
    )
    return {
        "batch_label": batch.label,
        "r_ids": batch.ids,
        "max_turns": batch_max_turns,
        "started_at_utc": started_at,
        "result": result,
    }


def _executor_prompt(system_prompt: str, run_id: str, batch: RBatch, discovery_context: str = "") -> str:
    ids = ", ".join(batch.ids)
    api_ctx_block = (
        f"\n\n# API CONTEXT (pre-run discovery)\n\n{discovery_context.strip()}\n"
        if discovery_context.strip() else ""
    )
    return (
        system_prompt
        + api_ctx_block
        + "\n\n# Orchestrated Batch Executor Mode\n\n"
        + f"You are one executor assigned only this Required-R batch: {ids}.\n"
        + "Do not attempt Required Rs outside this batch. Do not perform Open Exploration.\n"
        + "Use unique resource ids prefixed with "
        + f"`{run_id}_{batch.label}` to avoid interfering with other executor batches.\n"
        + "After every R in this batch has a `submit_verdict_for_R`, call `submit_verdict` immediately.\n"
    )


_DISCOVERY_SYSTEM = """\
You are a black-box API analyst. Given a service spec and a health check result, output a
compact JSON object describing the API surface so that test executors can probe immediately
without guessing HTTP methods or wasting turns on wrong paths.

Return ONLY a JSON object — no markdown fences, no prose outside the JSON.

Schema:
{
  "endpoints": [
    {"method": "PUT", "path": "/albums/{album_id}", "purpose": "create or update album"},
    ...
  ],
  "collection_paths": ["/albums", "/photos"],
  "notes": "any important usage notes visible in the spec (e.g. auth flow order, idempotency)"
}

Rules:
- Infer endpoints from the acceptance criteria. Only include paths the spec explicitly tests.
- Use {param_name} for variable path segments, e.g. /albums/{album_id}.
- Mark every path whose GET returns a list of resources as a collection_path.
- Keep each purpose to one short phrase. notes may be empty string.
"""


def _run_discovery_probe(target: str, spec: str) -> tuple[str, dict[str, Any]]:
    """Ask the LLM to infer the API surface from the spec, confirm the service is up.

    Returns (context_string, usage_dict). The context string is injected into every
    batch executor prompt so executors don't cold-start with wrong HTTP methods or
    resort to collection endpoints that flood the context with historical test data.
    """
    from tools import dispatch_tool

    lines: list[str] = []

    health = dispatch_tool("http_call", {"method": "GET", "path": "/health"}, target)
    lines.append(f"Service health: GET /health → {health.get('status', '?')}")

    user_text = f"Health check result: {health}\n\nSpec:\n{spec}"
    result, usage = _coordinator_json_call("api_discovery", _DISCOVERY_SYSTEM, user_text)

    if result and isinstance(result.get("endpoints"), list) and result["endpoints"]:
        lines.append("")
        lines.append("Inferred API endpoint map (from spec):")
        for ep in result["endpoints"]:
            method = str(ep.get("method", "?")).upper()
            path = ep.get("path", "?")
            purpose = ep.get("purpose", "")
            lines.append(f"  {method:<8} {path}  — {purpose}" if purpose else f"  {method:<8} {path}")
        collection = result.get("collection_paths") or []
        if collection:
            lines.append("")
            lines.append(f"Collection endpoints (return lists — DO NOT call for discovery): {', '.join(collection)}")
        notes = (result.get("notes") or "").strip()
        if notes:
            lines.append("")
            lines.append(f"Notes: {notes}")
    else:
        lines.append("LLM could not infer API shape from spec. Executors: probe cautiously.")

    lines.append("")
    lines.append(
        "IMPORTANT: Never call collection endpoints for discovery — they return all historical "
        "test data and will flood your context. Use only single-resource paths with IDs you control."
    )
    return "\n".join(lines), usage


def _merge_usage(
    batch_results: list[dict[str, Any]],
    coordinator_usages: list[dict[str, Any]],
) -> dict[str, Any]:
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": 0.0,
        "pricing_version": BEDROCK_PRICING_VERSION,
        "coordinator": [],
        "per_batch": [],
    }
    for item in coordinator_usages:
        usage["input_tokens"] += int(item.get("input_tokens", 0))
        usage["output_tokens"] += int(item.get("output_tokens", 0))
        usage["cache_creation_input_tokens"] += int(item.get("cache_creation_input_tokens", 0))
        usage["cache_read_input_tokens"] += int(item.get("cache_read_input_tokens", 0))
        usage["cost_usd"] += float(item.get("cost_usd", 0.0))
        compact = dict(item)
        raw = compact.get("raw_response")
        if isinstance(raw, str) and len(raw) > 1500:
            compact["raw_response"] = raw[:1500] + f"... [truncated, {len(raw)} chars total]"
        usage["coordinator"].append(compact)
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
    coordinator_usages: list[dict[str, Any]],
    max_turns: int,
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
    if failed:
        verdict = "FAIL"
    elif missing:
        verdict = "TIMEOUT"
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
        "Multi-agent orchestration aggregate verdict.",
        "",
        "Batch results:",
        *batch_lines,
        "",
        f"Required coverage: {len(r_verdicts)}/{len(required_ids)}.",
        f"Missing Rs: {missing or 'none'}.",
        f"Failed Rs: {failed or 'none'}.",
    ])

    now = datetime.now(timezone.utc).isoformat()
    usage = _merge_usage(batch_results, coordinator_usages)
    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "tool_calls": total_tool_calls,
        "turns": total_turns,
        "eval_mode": "orchestrated_cover_all",
        "required_ids": required_ids,
        "r_verdicts": r_verdicts,
        "r_verdict_history": r_verdict_history,
        "r_amendments": r_amendments,
        "r_missing": missing,
        "r_turn_budgets": {},
        "exploratory_findings": exploratory_findings,
        "events": events,
        "usage": usage,
        "orchestration": {
            "coordinator": "llm_batch_planner_with_repair",
            "coordinator_type": "llm",
            "batch_target_estimated_turns": DEFAULT_BATCH_ESTIMATED_TURNS,
            "global_max_turns": max_turns,
            "executor_turns_used": total_turns,
            "executor_turns_remaining": max(0, max_turns - total_turns),
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
            "eval_mode": "orchestrated_cover_all",
            "max_turns": max_turns,
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
        "orchestration": aggregate.get("orchestration", {}),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"[orchestrator] aggregate trace written to {path}")


def run_orchestrated_evaluation(
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
        print("[orchestrator] no Required Rs parsed; falling back to single-agent executor")
        return run_agent(
            system_prompt=system_prompt,
            spec=spec,
            target=target,
            dump_messages_to=dump_messages_to,
            max_turns=max_turns,
            run_id_override=run_id,
        )

    batches, coordinator_usages = _plan_batches_with_coordinator(parsed_spec, max_turns=max_turns)
    print(f"[orchestrator] LLM orchestrator planned {len(batches)} executor batch(es):")
    for batch in batches:
        print(f"[orchestrator]   {batch.label}: {batch.ids} estimated={batch.estimated_turns}")

    print()
    print("[orchestrator] running pre-run LLM API discovery...")
    try:
        discovery_context, discovery_usage = _run_discovery_probe(target, spec)
        coordinator_usages.append({**discovery_usage, "phase": "api_discovery"})
        print("[orchestrator] discovery results:")
        for line in discovery_context.splitlines():
            if line.strip():
                print(f"[orchestrator]   {line}")
    except Exception as exc:
        print(f"[orchestrator] discovery failed ({exc}); executors will self-discover")
        discovery_context = ""

    batch_results: list[dict[str, Any]] = []
    cum_cost_usd = 0.0

    # Run all initial batches in parallel — they share no sequential turn pool.
    workers = min(len(batches), MAX_PARALLEL_WORKERS)
    print(f"[orchestrator] running {len(batches)} initial batch(es) in parallel (workers={workers})")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_one_batch,
                batch, run_id, system_prompt, spec, parsed_spec,
                target, discovery_context, max_turns, dump_messages_to,
            ): batch
            for batch in batches
        }
        for future in as_completed(futures):
            item = future.result()
            batch_results.append(item)
            cum_cost_usd += float(item["result"].get("usage", {}).get("cost_usd", 0.0))
            print(f"[orchestrator] {item['batch_label']} done  cum_cost=${cum_cost_usd:.4f}")

    # Sort by label so trace output and repair loop see a consistent order.
    batch_results.sort(key=lambda x: x["batch_label"])

    # Repair budget: parallel phase "costs" as many turns as the slowest batch.
    executor_turns_used = max(
        (int(item["result"].get("turns", 0)) for item in batch_results), default=0
    )

    MAX_REPAIR_ATTEMPTS_PER_R = 2  # give up on an R after this many repair attempts with no verdict
    repair_index = len(batch_results) + 1
    repair_attempts: dict[str, int] = {}  # r_id → number of repair batches it was assigned to
    while True:
        covered = {
            r_id
            for item in batch_results
            for r_id in (item.get("result", {}).get("r_verdicts") or {}).keys()
        }
        # Exclude Rs that have been attempted too many times without producing a verdict
        abandoned = {r for r, cnt in repair_attempts.items() if cnt >= MAX_REPAIR_ATTEMPTS_PER_R and r not in covered}
        if abandoned:
            print(f"[orchestrator] giving up on {sorted(abandoned)} — attempted {MAX_REPAIR_ATTEMPTS_PER_R}x in repair without verdict")
        missing = [r_id for r_id in parsed_spec.required_ids if r_id not in covered and r_id not in abandoned]
        remaining_turns = max_turns - executor_turns_used
        if not missing or remaining_turns < MIN_VIABLE_BATCH_TURNS:
            break
        repair_batch, repair_usage = _plan_repair_batch_with_coordinator(
            parsed_spec=parsed_spec,
            missing_r_ids=missing,
            remaining_turns=remaining_turns,
            batch_results=batch_results,
            repair_index=repair_index,
        )
        coordinator_usages.append(repair_usage)
        if repair_batch is None or not repair_batch.ids:
            print(f"[orchestrator] coordinator did not plan a feasible repair batch; missing={missing}")
            break

        for r_id in repair_batch.ids:
            repair_attempts[r_id] = repair_attempts.get(r_id, 0) + 1

        repair_label = f"repair{repair_index:02d}_{repair_batch.ids[0]}-{repair_batch.ids[-1]}"
        batch_run_id = f"{run_id}__{repair_label}"
        batch_max_turns = _batch_max_turns(repair_batch, max_turns, remaining_turns, is_repair=True)
        batch_spec = _format_batch_spec(spec, parsed_spec, repair_batch)
        batch_prompt = _executor_prompt(system_prompt, run_id, repair_batch, discovery_context)
        if dump_messages_to is not None:
            batch_memory_path = dump_messages_to.with_name(
                f"run_{batch_run_id}_messages{dump_messages_to.suffix}"
            )
        else:
            batch_memory_path = None

        print()
        print("=" * 72)
        print(
            f"[orchestrator] repair executor {repair_label}: "
            f"Rs={repair_batch.ids} max_turns={batch_max_turns} remaining_global={remaining_turns}"
        )
        print("=" * 72)

        started_at = datetime.now(timezone.utc).isoformat()
        result = run_agent(
            system_prompt=batch_prompt,
            spec=batch_spec,
            target=target,
            dump_messages_to=batch_memory_path,
            max_turns=batch_max_turns,
            run_id_override=batch_run_id,
            auto_submit_on_r_coverage=True,
            write_trace=False,
            executor_label=repair_label,
        )
        executor_turns_used += int(result.get("turns", 0))
        batch_results.append({
            "batch_label": repair_label,
            "r_ids": repair_batch.ids,
            "max_turns": batch_max_turns,
            "started_at_utc": started_at,
            "result": result,
        })
        cum_cost_usd += float(result.get("usage", {}).get("cost_usd", 0.0))
        print(f"[orchestrator] {repair_label} done  cum_cost=${cum_cost_usd:.4f}  turns_used={executor_turns_used}/{max_turns}")
        repair_index += 1

    aggregate = _judge_aggregate(
        original_spec=spec,
        target=target,
        run_id=run_id,
        parsed_spec=parsed_spec,
        batch_results=batch_results,
        coordinator_usages=coordinator_usages,
        max_turns=max_turns,
    )
    if dump_messages_to is not None:
        dump_messages_to.parent.mkdir(parents=True, exist_ok=True)
        dump_messages_to.write_text(
            json.dumps(aggregate, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"[orchestrator] aggregate messages written to {dump_messages_to}")
    _write_aggregate_trace(run_id, aggregate)
    return aggregate
