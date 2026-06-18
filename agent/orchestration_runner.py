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
    PRICING_VERSION,
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

    MIN_R_ESTIMATED_TURNS = 3
    for r in parsed_spec.required:
        estimate = max(MIN_R_ESTIMATED_TURNS, int(getattr(r, "estimated_turns", DEFAULT_R_ESTIMATED_TURNS)))
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
            "estimated_turns": max(3, int(getattr(r, "estimated_turns", DEFAULT_R_ESTIMATED_TURNS))),
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


_EXPLORATION_SYSTEM = """\
You are ChaosArena in Open Exploration mode.

All Required acceptance criteria for this service have already been verified by \
specialized executor agents. You have NO Required Rs to cover — jump directly to \
Open Exploration.

Your goal: discover bugs, edge cases, and vulnerabilities that the spec did not enumerate.

Focus on:
- Authorization / ownership boundaries (can one user access another's data?)
- Input validation edges (empty strings, Unicode, very large/negative numbers, malformed JSON)
- Idempotency (does a repeated PUT/POST produce duplicate records or diverge?)
- Error-code semantics (500 where 4xx expected, missing 404s, wrong error bodies)
- Schema-shape stability (are response fields consistent across calls?)
- Concurrency edge cases not covered by Required Rs

Report every finding with `record_event`. Always supply the `title` field — a short \
headline (≤10 words) that lets a reader scan the report at a glance:
- OBSERVATION — notable but inconclusive (e.g. "Spec expected value differs from actual")
- WARNING — suspicious behavior that needs human review (e.g. "Negative side length returns 200")
- VIOLATION — definite breach not covered by any Required R (e.g. "Fisher NaN serialized as JSON string")

Call `submit_verdict` when you have found 3–5 substantive findings or your turn \
budget is nearly spent. Do not start new probes once fewer than 2 turns remain.
"""


def _exploration_executor_prompt(
    run_id: str,
    r_verdicts_summary: str,
    discovery_context: str,
    auth_context: str = "",
    playbook: str = "",
) -> str:
    """Build the system prompt for the dedicated exploration executor."""
    if playbook.strip():
        ctx_block = (
            f"\n\n# API PLAYBOOK (live-probed — follow exactly)\n\n{playbook.strip()}\n"
        )
    else:
        auth_block = (
            f"\n\n# AUTHENTICATION — LIVE-VERIFIED (follow exactly, do NOT deviate)\n\n"
            f"{auth_context.strip()}\n"
            if auth_context.strip() else ""
        )
        api_ctx = (
            f"\n\n# API CONTEXT (pre-run discovery)\n\n{discovery_context.strip()}\n"
            if discovery_context.strip() else ""
        )
        ctx_block = auth_block + api_ctx
    r_summary = (
        f"\n\n# ALREADY VERIFIED (do not re-test these)\n\n{r_verdicts_summary.strip()}\n"
        if r_verdicts_summary.strip() else ""
    )
    return _EXPLORATION_SYSTEM + ctx_block + r_summary + f"\n\n# RUN ID\n\n{run_id}\n"


def _format_exploration_spec(original_spec: str) -> str:
    """Strip Required Test Categories from the spec so the exploration agent has no Rs to cover."""
    marker = "## Required Test Categories"
    idx = original_spec.find(marker)
    if idx == -1:
        return original_spec
    prefix = original_spec[:idx].rstrip()
    oe_marker = "## Open Exploration"
    oe_idx = original_spec.find(oe_marker, idx)
    if oe_idx != -1:
        return prefix + "\n\n" + original_spec[oe_idx:]
    return prefix


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
    AUTH_OVERHEAD = 2
    estimated = max(1, batch.estimated_turns)
    if is_repair:
        per_batch_cap = max(10, math.ceil(estimated * 2.0) + 4 + AUTH_OVERHEAD)
    else:
        per_batch_cap = max(6, math.ceil(estimated * 1.5) + 2 + AUTH_OVERHEAD)
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
    auth_context: str = "",
    playbook: str = "",
) -> dict[str, Any]:
    """Run one executor batch in a worker thread."""
    batch_run_id = f"{run_id}__{batch.label}"
    # No fairness cap for parallel batches — they don't compete for a sequential pool.
    batch_max_turns = _batch_max_turns(batch, max_turns)
    batch_spec = _format_batch_spec(spec, parsed_spec, batch)
    shared_prefix, batch_suffix = _executor_prompt(
        system_prompt, run_id, batch, discovery_context, auth_context, playbook
    )
    print(f"\n{'='*72}\n[executor:{batch.label}] Rs={batch.ids} max_turns={batch_max_turns}\n{'='*72}")
    started_at = datetime.now(timezone.utc).isoformat()
    result = run_agent(
        system_prompt=batch_suffix,
        shared_prefix=shared_prefix,
        spec=batch_spec,
        target=target,
        dump_messages_to=None,
        max_turns=batch_max_turns,
        run_id_override=batch_run_id,
        auto_submit_on_r_coverage=True,
        write_trace=False,
        executor_label=batch.label,
        auth_context=auth_context,
    )
    return {
        "batch_label": batch.label,
        "r_ids": batch.ids,
        "max_turns": batch_max_turns,
        "started_at_utc": started_at,
        "result": result,
    }


def _executor_prompt(
    system_prompt: str,
    run_id: str,
    batch: RBatch,
    discovery_context: str = "",
    auth_context: str = "",
    playbook: str = "",
) -> tuple[str, str]:
    """Return (shared_prefix, batch_suffix).

    shared_prefix is identical for every batch in a run — safe to cache once and
    share across all parallel executors.
    batch_suffix is the small batch-specific section (~200 tokens, no cache needed).
    """
    ids = ", ".join(batch.ids)
    # Playbook (from probe agent) supersedes both auth and discovery context.
    # Fall back to individual blocks when no playbook is available.
    if playbook.strip():
        shared_prefix = (
            system_prompt
            + f"\n\n# API PLAYBOOK (live-probed — follow exactly)\n\n{playbook.strip()}\n"
        )
    else:
        auth_block = (
            f"\n\n# AUTHENTICATION — LIVE-VERIFIED (follow exactly, do NOT deviate)\n\n"
            f"{auth_context.strip()}\n"
            if auth_context.strip() else ""
        )
        api_ctx_block = (
            f"\n\n# API CONTEXT (pre-run discovery)\n\n{discovery_context.strip()}\n"
            if discovery_context.strip() else ""
        )
        shared_prefix = system_prompt + auth_block + api_ctx_block

    batch_suffix = (
        "\n\n# Orchestrated Batch Executor Mode\n\n"
        + f"You are one executor assigned only this Required-R batch: {ids}.\n"
        + "Do not attempt Required Rs outside this batch. Do not perform Open Exploration.\n"
        + "Use unique resource ids prefixed with "
        + f"`{run_id}_{batch.label}` to avoid interfering with other executor batches.\n"
        + "After every R in this batch has a `submit_verdict_for_R`, call `submit_verdict` immediately.\n"
    )
    return shared_prefix, batch_suffix


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


_PROBE_TOOL_NAMES = frozenset({"http_call", "http_call_with_session", "remember_fact", "record_event"})

_SUBMIT_PLAYBOOK_SCHEMA: dict[str, Any] = {
    "name": "submit_playbook",
    "description": (
        "Submit the final API playbook. Call this when you have finished probing all "
        "key endpoints. This ends the probe session immediately."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "Complete API playbook in markdown. Must include: "
                    "## Authentication, ## Endpoint Map, ## Response Shapes, "
                    "## Known Constraints, ## Notes."
                ),
            }
        },
        "required": ["content"],
    },
}


def _run_api_probe_agent(target: str, spec: str, max_turns: int = 10) -> tuple[str, dict[str, Any]]:
    """Run a short LLM tool-use loop to probe the API surface and produce a playbook.

    Returns (playbook_markdown, usage_dict).
    The playbook is injected into every batch executor's shared cached system prompt.
    Falls back to ("", {}) on any failure.
    """
    from tools import dispatch_tool, SESSIONS, EVENT_LOG, TOOL_SCHEMAS

    EVENT_LOG.clear()
    for sess in list(SESSIONS.values()):
        try:
            sess.close()
        except Exception:
            pass
    SESSIONS.clear()

    # Pre-probe auth so the agent doesn't waste turns on basic auth discovery.
    pre_auth = _probe_auth(target)
    auth_hint = (
        f"\n\n=== PRE-VERIFIED AUTH ===\n{pre_auth}\n"
        f"Skip authentication discovery — auth is already confirmed above. "
        f"Focus your turns on endpoint shapes, required body fields, and edge constraints."
        if pre_auth.strip() else ""
    )

    client = get_client()
    model = get_model_id()

    here = Path(__file__).resolve().parent
    probe_system = (here / "prompts" / "api_probe_system.txt").read_text()

    probe_tools = [t for t in TOOL_SCHEMAS if t.get("name") in _PROBE_TOOL_NAMES]
    probe_tools = probe_tools + [_SUBMIT_PLAYBOOK_SCHEMA]

    initial_text = (
        f"=== SERVICE SPEC ===\n{spec}\n\n"
        f"=== TARGET ===\n{target}"
        f"{auth_hint}\n\nBegin probing."
    )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "text", "text": initial_text}]}
    ]
    cached_probe_system = [
        {"type": "text", "text": probe_system, "cache_control": {"type": "ephemeral"}}
    ]

    total_input = total_output = 0
    total_cache_creation = total_cache_read = 0
    total_cost = 0.0
    playbook = ""
    turns_used = 0

    for turn in range(1, max_turns + 1):
        turns_used = turn
        resp = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=cached_probe_system,
            tools=probe_tools,
            messages=messages,
        )

        u = resp.usage
        ci = getattr(u, "cache_creation_input_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        total_input += u.input_tokens
        total_output += u.output_tokens
        total_cache_creation += ci
        total_cache_read += cr
        total_cost += estimate_cost(u.input_tokens, u.output_tokens, ci, cr)

        content_blocks = [
            (b.model_dump() if hasattr(b, "model_dump") else b) for b in resp.content
        ]
        messages.append({"role": "assistant", "content": content_blocks})

        tool_results: list[dict[str, Any]] = []
        done = False

        for block in resp.content:
            if not (hasattr(block, "type") and block.type == "tool_use"):
                continue
            name = block.name
            args = block.input or {}
            if name == "submit_playbook":
                playbook = args.get("content", "")
                done = True
                result: Any = {"submitted": True}
            elif name in _PROBE_TOOL_NAMES:
                result = dispatch_tool(name, args, target)
            else:
                result = {"error": f"Unknown probe tool: {name}"}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if done or resp.stop_reason == "end_turn":
            break

        # Inject runtime reminder 2 turns before budget runs out
        turns_remaining = max_turns - turn
        if turns_remaining == 2:
            messages.append({
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": (
                        "[runtime reminder] You have 2 turns left. "
                        "You MUST call submit_playbook NOW with everything you have discovered so far. "
                        "Do not make any more http_call probes — compile and submit the playbook immediately."
                    ),
                }],
            })

    print(
        f"[probe] done in {turns_used} turns  cost=${total_cost:.4f}  "
        f"playbook={len(playbook)} chars"
    )
    usage_dict: dict[str, Any] = {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_creation_input_tokens": total_cache_creation,
        "cache_read_input_tokens": total_cache_read,
        "cost_usd": round(total_cost, 6),
        "turns": turns_used,
        "model": model,
    }
    return playbook, usage_dict


def _probe_auth(target: str) -> str:
    """Probe the live service to discover its authentication mechanism.

    Tries common auth patterns, verifies the session actually works on a
    protected endpoint, and detects required query parameters (e.g. ?name=).
    Returns a description string. Never raises — returns "" on complete failure.
    """
    from tools import dispatch_tool, SESSIONS
    import uuid

    suffix = uuid.uuid4().hex[:8]
    username = f"probe_{suffix}"
    email = f"{username}@chaos.test"
    password = "Probe@1234!"
    name = f"Probe {suffix}"

    # Try 1: session-cookie via POST /register
    for reg_path in ("/register", "/signup", "/customer", "/api/register", "/users"):
        try:
            # Try minimal body first, then add optional fields that some services require
            body_variants = [
                {"name": name, "username": username, "email": email, "password": password},
                {"name": name, "email": email, "password": password},
                {
                    "name": name, "username": username, "email": email, "password": password,
                    "phone": "12345678901", "address": "123 Test Street",
                },
                {
                    "name": name, "email": email, "password": password,
                    "phone": "12345678901", "address": "123 Test Street",
                },
            ]
            r, body = None, {}
            for body_candidate in body_variants:
                r = dispatch_tool("http_call", {"method": "POST", "path": reg_path, "json_body": body_candidate}, target)
                if r.get("status", 0) in (200, 201):
                    body = body_candidate
                    break
            status = r.get("status", 0) if r else 0
            if status in (200, 201):
                sess_id = f"auth_probe_{suffix}"
                reg_body2 = {**body, "name": name + "2", "username": username + "2",
                             "email": f"{username}2@chaos.test"}
                r2 = dispatch_tool("http_call_with_session", {
                    "method": "POST", "path": reg_path,
                    "json_body": reg_body2,
                    "session_id": sess_id,
                }, target)
                if sess_id in SESSIONS and SESSIONS[sess_id].cookies:
                    cookie_names = list(SESSIONS[sess_id].cookies.keys())
                    name2 = name + "2"

                    # Verify the session actually works on a protected endpoint.
                    # Also detect whether a ?name= query param is required.
                    protected_paths = ["/customer/cart", "/customer/orders",
                                       "/customer", "/api/profile", "/profile"]
                    name_param_required = False
                    for ppath in protected_paths:
                        try:
                            rp = dispatch_tool("http_call_with_session", {
                                "method": "GET", "path": ppath,
                                "session_id": sess_id,
                            }, target)
                            if rp.get("status") not in (401, 403, 404, 500):
                                break  # works without ?name=
                            if rp.get("status") in (401, 403):
                                # Try with ?name= query param
                                rp2 = dispatch_tool("http_call_with_session", {
                                    "method": "GET", "path": f"{ppath}?name={name2}",
                                    "session_id": sess_id,
                                }, target)
                                if rp2.get("status") not in (401, 403):
                                    name_param_required = True
                                    break
                        except Exception:
                            continue

                    name_note = (
                        f" CRITICAL: You MUST also append ?name=<registered_name> to every "
                        f"protected endpoint call (e.g. GET /customer/cart?name={{name}}, "
                        f"PUT /customer/cart?name={{name}}). "
                        f"The service returns 401 if ?name= is missing even with a valid session."
                        if name_param_required else ""
                    )
                    body_fields = ", ".join(f'"{k}": ...' for k in body.keys())
                    return (
                        f"LIVE-VERIFIED: POST {reg_path} with JSON body "
                        f"{{{body_fields}}} "
                        f"returns HTTP {r2.get('status', '?')} and sets session cookies "
                        f"({', '.join(cookie_names)}). "
                        f"Use http_call_with_session with a unique session_id per user — cookies are sticky. "
                        f"There is NO separate /login endpoint (do not try Basic Auth — it returns 401). "
                        f"To create two users, register twice with different credentials and different session_ids."
                        f"{name_note}"
                    )
                elif status in (200, 201):
                    body_text = str(r.get("body", ""))
                    if any(k in body_text.lower() for k in ("token", "access_token", "jwt")):
                        return (
                            f"LIVE-VERIFIED: POST {reg_path} with JSON credentials returns "
                            f"HTTP {status} with a bearer token in the response body. "
                            f"Extract the token and send as 'Authorization: Bearer <token>' on all protected requests."
                        )
        except Exception:
            continue

    # Try 2: bearer token via POST /login or /auth/login
    for login_path in ("/login", "/auth/login", "/api/login", "/authenticate", "/api/authenticate"):
        try:
            body = {"username": username, "password": password}
            r = dispatch_tool("http_call", {"method": "POST", "path": login_path, "json_body": body}, target)
            if r.get("status") in (200, 201):
                body_text = str(r.get("body", ""))
                if any(k in body_text.lower() for k in ("token", "access_token", "jwt")):
                    return (
                        f"LIVE-VERIFIED: POST {login_path} with {{\"username\": ..., \"password\": ...}} "
                        f"returns HTTP {r.get('status')} with a bearer token in the response body. "
                        f"Use 'Authorization: Bearer <token>' on all subsequent protected requests."
                    )
        except Exception:
            continue

    return ""


def _run_discovery_probe(target: str, spec: str) -> tuple[str, str, dict[str, Any]]:
    """Ask the LLM to infer the API surface from the spec, confirm the service is up.

    Returns (api_context_string, auth_info_string, usage_dict).
    auth_info_string is returned separately so callers can inject it as a
    distinct hardcoded block — not mixed with LLM-inferred content.
    """
    from tools import dispatch_tool

    lines: list[str] = []

    health = dispatch_tool("http_call", {"method": "GET", "path": "/health"}, target)
    lines.append(f"Service health: GET /health → {health.get('status', '?')}")

    # Probe auth mechanism — returned separately, NOT appended to lines
    auth_info = _probe_auth(target)
    if auth_info:
        print(f"[orchestrator] auth probe: {auth_info[:120]}...")
    else:
        print("[orchestrator] auth probe: no mechanism detected — executors will self-discover")

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
    return "\n".join(lines), auth_info, usage


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
        "pricing_version": PRICING_VERSION,
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
    global_tool_counts: dict[str, int] = {}

    for item in batch_results:
        result = item["result"]
        total_turns += int(result.get("turns", 0))
        total_tool_calls += int(result.get("tool_calls", 0))
        r_verdicts.update(result.get("r_verdicts") or {})
        r_verdict_history.update(result.get("r_verdict_history") or {})
        r_amendments.update(result.get("r_amendments") or {})
        exploratory_findings.extend(result.get("exploratory_findings") or [])
        events.extend(result.get("events") or [])
        for tool, cnt in (result.get("tool_counts") or {}).items():
            global_tool_counts[tool] = global_tool_counts.get(tool, 0) + cnt

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
        "r_titles": {r.r_id: r.title for r in parsed_spec.required},
        "r_verdicts": r_verdicts,
        "r_verdict_history": r_verdict_history,
        "r_amendments": r_amendments,
        "r_missing": missing,
        "r_turn_budgets": {},
        "exploratory_findings": exploratory_findings,
        "events": events,
        "usage": usage,
        "tool_counts": dict(sorted(global_tool_counts.items(), key=lambda x: -x[1])),
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
                    "tool_counts": item["result"].get("tool_counts") or {},
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
        "tool_counts": aggregate.get("tool_counts", {}),
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
    print("[orchestrator] running pre-run API probe agent...")
    playbook = ""
    discovery_context = ""
    auth_context = ""
    try:
        probe_max_turns = max(5, int(max_turns * 0.30))
        playbook, probe_usage = _run_api_probe_agent(target, spec, max_turns=probe_max_turns)
        coordinator_usages.append({**probe_usage, "phase": "api_probe"})
        # Use playbook as auth_context so it also survives Memory trimming (in initial_message)
        auth_context = playbook
        if not playbook:
            raise ValueError("probe agent returned empty playbook")
    except Exception as exc:
        print(f"[orchestrator] probe agent failed ({exc}); falling back to legacy discovery")
        try:
            discovery_context, auth_context, discovery_usage = _run_discovery_probe(target, spec)
            coordinator_usages.append({**discovery_usage, "phase": "api_discovery"})
            print("[orchestrator] fallback discovery results:")
            for line in discovery_context.splitlines():
                if line.strip():
                    print(f"[orchestrator]   {line}")
        except Exception as exc2:
            print(f"[orchestrator] fallback discovery also failed ({exc2}); executors will self-discover")
            discovery_context = ""
            auth_context = ""

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
                target, discovery_context, max_turns, auth_context, playbook,
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

    # Sum semantics: max_turns is the hard ceiling across all agent turns combined.
    executor_turns_used = sum(
        int(item["result"].get("turns", 0)) for item in batch_results
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
        shared_prefix, batch_suffix = _executor_prompt(
            system_prompt, run_id, repair_batch, discovery_context, auth_context, playbook
        )
        print()
        print("=" * 72)
        print(
            f"[orchestrator] repair executor {repair_label}: "
            f"Rs={repair_batch.ids} max_turns={batch_max_turns} remaining_global={remaining_turns}"
        )
        print("=" * 72)

        started_at = datetime.now(timezone.utc).isoformat()
        result = run_agent(
            system_prompt=batch_suffix,
            shared_prefix=shared_prefix,
            spec=batch_spec,
            target=target,
            dump_messages_to=None,
            max_turns=batch_max_turns,
            run_id_override=batch_run_id,
            auto_submit_on_r_coverage=True,
            write_trace=False,
            executor_label=repair_label,
            auth_context=auth_context,
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

    # --- Dedicated exploration phase ---
    # Only runs when ALL Required Rs are covered and budget remains.
    covered_final = {
        r_id
        for item in batch_results
        for r_id in (item.get("result", {}).get("r_verdicts") or {}).keys()
    }
    all_rs_covered = all(r in covered_final for r in parsed_spec.required_ids)
    remaining_for_exploration = max_turns - executor_turns_used

    if all_rs_covered and remaining_for_exploration >= MIN_VIABLE_BATCH_TURNS:
        # Build a compact summary of what was already verified for the exploration agent.
        all_r_verdicts: dict[str, Any] = {}
        for item in batch_results:
            all_r_verdicts.update(item.get("result", {}).get("r_verdicts") or {})
        r_verdicts_summary = "\n".join(
            f"- {r_id}: {v.get('verdict', '?')} — {v.get('evidence', '')[:120]}"
            for r_id, v in sorted(all_r_verdicts.items())
        )

        exploration_run_id = f"{run_id}__exploration"
        exploration_prompt = _exploration_executor_prompt(
            run_id=run_id,
            r_verdicts_summary=r_verdicts_summary,
            discovery_context=discovery_context,
            auth_context=auth_context,
            playbook=playbook,
        )
        exploration_spec = _format_exploration_spec(spec)

        print()
        print("=" * 72)
        print(f"[orchestrator] exploration agent: remaining_turns={remaining_for_exploration}")
        print("=" * 72)

        started_at = datetime.now(timezone.utc).isoformat()
        exploration_result = run_agent(
            system_prompt=exploration_prompt,
            spec=exploration_spec,
            target=target,
            dump_messages_to=None,
            max_turns=remaining_for_exploration,
            run_id_override=exploration_run_id,
            auto_submit_on_r_coverage=False,
            write_trace=False,
            executor_label="exploration",
            auth_context=auth_context,
        )
        executor_turns_used += int(exploration_result.get("turns", 0))
        batch_results.append({
            "batch_label": "exploration",
            "r_ids": [],
            "max_turns": remaining_for_exploration,
            "started_at_utc": started_at,
            "result": exploration_result,
        })
        cum_cost_usd += float(exploration_result.get("usage", {}).get("cost_usd", 0.0))
        print(f"[orchestrator] exploration done  cum_cost=${cum_cost_usd:.4f}  turns_used={executor_turns_used}/{max_turns}")
    elif parsed_spec.required_ids and not all_rs_covered:
        missing_exp = [r for r in parsed_spec.required_ids if r not in covered_final]
        print(f"[orchestrator] skipping exploration — {len(missing_exp)} Rs still uncovered: {missing_exp}")
    else:
        print(f"[orchestrator] skipping exploration — only {remaining_for_exploration} turn(s) remaining")

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
