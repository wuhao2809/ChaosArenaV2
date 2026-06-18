"""Batch executor: LLM tool-use loop.

Reads a system prompt + spec + target URL, runs the LLM in a tool-use loop
until either submit_verdict is called or max_turns is exceeded.

Evaluation mode is always cover_all: the executor must emit submit_verdict_for_R
for every Required category before submit_verdict is accepted. Turns beyond
the Required coverage budget are available for Open Exploration.
"""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import Anthropic, AnthropicBedrock

from config.config import (
    DEFAULT_MAX_TURNS, MAX_TOKENS, TEMPERATURE, TOP_P_RECORDED, TOP_K_RECORDED,
    DEFAULT_BEDROCK_MODEL, DEFAULT_DIRECT_MODEL,
    PRICING_VERSION, INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK,
    CACHE_CREATION_PER_MTOK, CACHE_READ_PER_MTOK,
)
from conversation_memory import Memory, RequiredCategorySpec
from tools import EVENT_LOG, SESSIONS, TOOL_SCHEMAS, dispatch_tool
from spec_parser import parse_spec, ParsedSpec


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate USD cost from token counts."""
    return (
        input_tokens * INPUT_COST_PER_MTOK / 1_000_000
        + output_tokens * OUTPUT_COST_PER_MTOK / 1_000_000
        + cache_creation_tokens * CACHE_CREATION_PER_MTOK / 1_000_000
        + cache_read_tokens * CACHE_READ_PER_MTOK / 1_000_000
    )


def get_client():
    """Construct the LLM client based on LLM_BACKEND env var."""
    backend = os.environ.get("ANTHROPIC_BACKEND", "bedrock").lower()
    if backend == "bedrock":
        return AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-west-2"))
    return Anthropic()


def get_model_id() -> str:
    explicit = os.environ.get("MODEL_ID")
    if explicit:
        return explicit
    backend = os.environ.get("ANTHROPIC_BACKEND", "bedrock").lower()
    return DEFAULT_BEDROCK_MODEL if backend == "bedrock" else DEFAULT_DIRECT_MODEL


def _print_block_text(content_blocks: list) -> None:
    for block in content_blocks:
        if block.type == "text" and block.text.strip():
            print(f"  LLM: {block.text.strip()}")


def _truncate(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + f"... [truncated, {len(s)} total]"


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2,
            cwd=Path(__file__).resolve().parent,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return "unavailable"


def _build_repro_metadata(
    spec_text: str,
    system_prompt_text: str,
    target: str,
    model: str,
    max_turns: int,
) -> dict[str, Any]:
    """Collect everything needed to reproduce this run."""
    return {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "temperature": TEMPERATURE,
        "top_p_recorded": TOP_P_RECORDED,
        "top_k_recorded": TOP_K_RECORDED,
        "note": "Only temperature is sent to the LLM; top_p/top_k are recorded for provenance.",
        "eval_mode": "cover_all",
        "max_turns": max_turns,
        "max_tokens_per_turn": MAX_TOKENS,
        "target_url": target,
        "spec_sha256": hashlib.sha256(spec_text.encode("utf-8")).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(system_prompt_text.encode("utf-8")).hexdigest(),
        "git_commit": _git_commit(),
    }


def _dump_messages(messages: list[dict], path: Path, meta: dict, events: list[dict], memory_state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta, "events": events, "memory_state": memory_state, "messages": messages}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[executor] conversation dumped to {path}")


def _write_trace(
    run_id: str,
    repro_meta: dict,
    turns_trace: list[dict],
    verdict: dict,
    total_tool_calls: int,
    total_cost: float,
) -> None:
    """Write a structured per-turn trace to trace/<run_id>.json.

    The trace is always written (unlike dump_messages which is opt-in).
    It is the primary data source for paper §5 experiments.
    """
    trace_dir = Path(__file__).resolve().parent / "trace"
    trace_dir.mkdir(exist_ok=True)
    path = trace_dir / f"run_{run_id}.json"

    payload = {
        "run_id": run_id,
        "started_at_utc": repro_meta.get("started_at_utc"),
        "finished_at_utc": repro_meta.get("finished_at_utc"),
        "model": repro_meta.get("model"),
        "target": repro_meta.get("target_url"),
        "spec_sha256": repro_meta.get("spec_sha256"),
        "system_prompt_sha256": repro_meta.get("system_prompt_sha256"),
        "git_commit": repro_meta.get("git_commit"),
        "max_turns": repro_meta.get("max_turns"),
        "eval_mode": "cover_all",
        "verdict": verdict.get("verdict"),
        "total_turns": verdict.get("turns"),
        "total_tool_calls": total_tool_calls,
        "total_cost_usd": round(total_cost, 6),
        "r_verdicts": verdict.get("r_verdicts", {}),
        "r_verdict_history": verdict.get("r_verdict_history", {}),
        "r_amendments": verdict.get("r_amendments", {}),
        "r_missing": verdict.get("r_missing", []),
        "turns": turns_trace,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    print(f"[executor] trace written to {path}")


def run_agent(
    system_prompt: str,
    spec: str,
    target: str,
    dump_messages_to: Path | None = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    run_id_override: str | None = None,
    auto_submit_on_r_coverage: bool = False,
    write_trace: bool = True,
    executor_label: str = "",
    auth_context: str = "",
    shared_prefix: str = "",
) -> dict[str, Any]:
    """Drive the agent through a cover_all tool-use loop. Returns the verdict dict.

    The agent must emit submit_verdict_for_R for every Required category before
    submit_verdict is accepted. Turns remaining after full R coverage are
    available for Open Exploration.

    When auto_submit_on_r_coverage=True the runtime auto-generates submit_verdict
    the moment all Required Rs are covered, skipping the extra LLM turn. Used by
    the orchestrator for batch executors that have Open Exploration disabled.

    If dump_messages_to is provided, the full conversation history is written
    to that JSON path after the run completes.
    """
    # Clear module-level state for this run.
    EVENT_LOG.clear()
    for _sess in list(SESSIONS.values()):
        try:
            _sess.close()
        except Exception:
            pass
    SESSIONS.clear()

    parsed_spec: ParsedSpec = parse_spec(spec)
    required_ids = parsed_spec.required_ids
    required_specs = [
        RequiredCategorySpec(r_id=r.r_id, title=r.title, body=r.body)
        for r in parsed_spec.required
    ]
    client = get_client()
    model = get_model_id()

    coverage_line = (
        f"Required test categories (you MUST cover each before submitting verdict): "
        f"{', '.join(required_ids) if required_ids else '(none parsed)'}"
    )
    mode_line = (
        "Eval mode: cover_all. "
        "You MUST emit submit_verdict_for_R for every Required R before "
        "submit_verdict will be accepted. "
        f"After all Rs are covered, use any remaining turns for Open Exploration. "
        f"Hard stop: call submit_verdict by turn {max_turns}."
    )

    # Cache breakpoint 3: initial user message (spec + target + run config).
    # Auth context is prepended here — it lives in _initial_message which is
    # NEVER dropped by Memory trimming, so auth info survives context resets.
    auth_block = (
        f"=== AUTHENTICATION (LIVE-VERIFIED — follow exactly, do NOT deviate) ===\n"
        f"{auth_context.strip()}\n\n"
        if auth_context.strip() else ""
    )
    initial_user_text = (
        auth_block
        + f"=== SPEC ===\n{spec}\n\n"
        + f"=== TARGET ===\n{target}\n\n"
        + f"=== RUN CONFIG ===\n{coverage_line}\n{mode_line}\n\n"
        + f"Begin the evaluation."
    )
    initial_message = {"role": "user", "content": [
        {"type": "text", "text": initial_user_text, "cache_control": {"type": "ephemeral"}}
    ]}
    memory = Memory(
        initial_message=initial_message,
        required_specs=required_specs,
        enable_r_context_trimming=True,
    )

    repro_meta = _build_repro_metadata(
        spec_text=spec,
        system_prompt_text=shared_prefix + system_prompt,
        target=target,
        model=model,
        max_turns=max_turns,
    )

    print(f"[executor] model      = {model}")
    print(f"[executor] target     = {target}")
    print(f"[executor] max_turns  = {max_turns}  temp={TEMPERATURE} (top_p/top_k recorded only)")
    print(f"[executor] eval_mode  = cover_all")
    print(f"[executor] required Rs = {required_ids}")
    if len(required_ids) > 12:
        print(
            f"[executor] WARNING: {len(required_ids)} Required Rs — "
            f"agent must verdict all before submit_verdict is accepted. "
            f"Risk of timeout at max_turns={max_turns}. Consider trimming spec to <= 12 Rs."
        )
    print(f"[executor] git_commit = {repro_meta['git_commit']}  spec_sha256 = {repro_meta['spec_sha256'][:12]}...\n")

    run_id = run_id_override if run_id_override else str(int(datetime.now(timezone.utc).timestamp()))

    # Build cached versions of the two static inputs.
    # Cache breakpoint 1: system prompt.
    # When shared_prefix is provided (orchestrated mode), split into two blocks:
    #   Block 1 (shared_prefix): identical across all parallel batch executors →
    #     cached once, read by every subsequent turn of every batch.
    #   Block 2 (system_prompt): batch-specific Rs section (~200 tokens, no cache).
    # Without shared_prefix (single-agent mode), one block with cache_control.
    if shared_prefix.strip():
        cached_system = [
            {"type": "text", "text": shared_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": system_prompt},
        ]
    else:
        cached_system = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    # Cache breakpoint 2: tool schemas (same for every turn).
    # The cache boundary sits at the last tool — everything up to and including
    # it is cached after the first creation.
    cached_tools = list(TOOL_SCHEMAS)
    if cached_tools:
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}

    verdict: dict[str, Any] | None = None
    tool_call_count = 0
    turn = 0
    r_verdicts: dict[str, dict[str, str]] = {}
    r_verdict_history: dict[str, list[dict[str, Any]]] = {}
    tool_counts: dict[str, int] = {}
    per_turn_usage: list[dict[str, int]] = []
    all_turns_trace: list[dict] = []
    total_input = 0
    total_output = 0
    total_cache_creation = 0
    total_cache_read = 0
    for turn in range(1, max_turns + 1):
        print()
        label_prefix = f"[{executor_label}] " if executor_label else ""
        print(f"--- {label_prefix}Turn {turn}/{max_turns} ---")

        remaining_before_turn = [r for r in required_ids if r not in r_verdicts]
        memory.update_budget_context(
            turn=turn,
            max_turns=max_turns,
            remaining_r_ids=remaining_before_turn,
        )

        response = client.messages.create(
            model=model,
            system=cached_system,
            messages=memory.get_active_messages(),
            tools=cached_tools,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )

        usage = response.usage
        turn_input = usage.input_tokens
        turn_output = usage.output_tokens
        turn_cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        turn_cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        total_input += turn_input
        total_output += turn_output
        total_cache_creation += turn_cache_creation
        total_cache_read += turn_cache_read

        cum_cost = estimate_cost(total_input, total_output, total_cache_creation, total_cache_read)
        cache_label = f" cache_hit={turn_cache_read:,}" if turn_cache_read else ""
        per_turn_usage.append({
            "turn": turn,
            "input_tokens": turn_input,
            "output_tokens": turn_output,
            "cache_creation_input_tokens": turn_cache_creation,
            "cache_read_input_tokens": turn_cache_read,
        })
        print(
            f"  [tokens] in={turn_input:,} out={turn_output:,}"
            f"{cache_label} "
            f"cum_in={total_input:,} cum_out={total_output:,} cum_cost=${cum_cost:.4f}"
        )

        memory.record_assistant_response(response.content, turn=turn)
        _print_block_text(response.content)

        # Collect the LLM's text reasoning for this turn.
        turn_text = " ".join(
            block.text.strip()
            for block in response.content
            if hasattr(block, "type") and block.type == "text" and block.text.strip()
        )
        turn_tool_calls: list[dict[str, Any]] = []

        tool_results: list[dict] = []
        completed_r_ids: list[str] = []
        verdict_seen = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_call_count += 1
            tool_counts[block.name] = tool_counts.get(block.name, 0) + 1
            args_str = json.dumps(block.input, ensure_ascii=False)
            print(f"  → {block.name}({_truncate(args_str, 300)})")

            trace_call: dict[str, Any] = {"name": block.name, "args": block.input, "result": None}

            if block.name == "submit_verdict_for_R":
                r_id = block.input.get("r_id", "?")
                prior_verdict = r_verdicts.get(r_id)
                is_amendment = prior_verdict is not None
                new_r_verdict: dict[str, Any] = {
                    "verdict": block.input.get("verdict", "UNKNOWN"),
                    "confidence": block.input.get("confidence", "UNKNOWN"),
                    "evidence": block.input.get("evidence", ""),
                    "turn": turn,
                    "amended": is_amendment,
                }
                history = r_verdict_history.setdefault(r_id, [])
                if is_amendment:
                    new_r_verdict["amendment"] = {
                        "previous_verdict": prior_verdict.get("verdict", "UNKNOWN"),
                        "previous_confidence": prior_verdict.get("confidence", "UNKNOWN"),
                        "previous_evidence": prior_verdict.get("evidence", ""),
                        "amendment_turn": turn,
                        "reason": block.input.get("evidence", ""),
                    }
                history.append(dict(new_r_verdict))
                new_r_verdict["amendment_count"] = max(0, len(history) - 1)
                new_r_verdict["versions"] = [dict(v) for v in history]
                r_verdicts[r_id] = new_r_verdict
                remaining = [r for r in required_ids if r not in r_verdicts]
                verb = "Amended" if is_amendment else "Recorded"
                if remaining:
                    ack = (
                        f"{verb} {r_id} verdict: {r_verdicts[r_id]['verdict']} "
                        f"(confidence={r_verdicts[r_id]['confidence']}). "
                        f"Remaining Required: {remaining}."
                    )
                else:
                    turns_left = max_turns - turn
                    ack = (
                        f"{verb} {r_id} verdict: {r_verdicts[r_id]['verdict']} "
                        f"(confidence={r_verdicts[r_id]['confidence']}). "
                        f"All Required Rs covered. "
                        f"You have {turns_left} turn(s) left for Open Exploration — "
                        f"probe for issues the spec did not enumerate, log with record_event. "
                        f"Call submit_verdict when done."
                    )
                if is_amendment:
                    ack += (
                        f" This replaces prior {r_id} verdict "
                        f"{prior_verdict.get('verdict', 'UNKNOWN')} "
                        f"(confidence={prior_verdict.get('confidence', 'UNKNOWN')})."
                    )
                print(f"  ← {ack}")
                trace_call["result"] = ack
                trace_call["amended"] = is_amendment
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": ack,
                })
                completed_r_ids.append(r_id)

            elif block.name == "remember_fact":
                fact = memory.remember_fact(
                    key=block.input.get("key", ""),
                    value=block.input.get("value"),
                    note=block.input.get("note", "") or "",
                    source_r_id=block.input.get("source_r_id"),
                    turn=turn,
                )
                ack = f"Remembered fact {fact['key']}."
                print(f"  ← {ack}")
                trace_call["result"] = fact
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": ack,
                })

            elif block.name == "submit_verdict":
                missing = [r for r in required_ids if r not in r_verdicts]
                if missing:
                    refusal = (
                        f"REJECTED: all Required Rs must have a verdict before submit_verdict "
                        f"is accepted. Still missing: {missing}. "
                        f"Continue testing and emit submit_verdict_for_R for each missing R."
                    )
                    print(f"  ← {refusal}")
                    trace_call["result"] = refusal
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": refusal,
                    })
                else:
                    verdict = {
                        "verdict": block.input.get("verdict", "UNKNOWN"),
                        "reasoning": block.input.get("reasoning", ""),
                        "tool_calls": tool_call_count,
                        "turns": turn,
                    }
                    verdict_seen = True
                    trace_call["result"] = "Verdict accepted. Run complete."
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Verdict accepted. Run complete.",
                    })

            else:
                result = dispatch_tool(block.name, block.input, target)
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                print(f"  ← {_truncate(result_str, 300)}")
                trace_call["result"] = result
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

            turn_tool_calls.append(trace_call)

        turn_trace = {
            "turn": turn,
            "tokens": {
                "input": turn_input,
                "output": turn_output,
                "cache_creation": turn_cache_creation,
                "cache_read": turn_cache_read,
            },
            "cumulative_cost_usd": round(cum_cost, 6),
            "text": turn_text,
            "tool_calls": turn_tool_calls,
            "memory": memory.metrics(),
        }

        runtime_reminders: list[str] = []
        missing_now = [r for r in required_ids if r not in r_verdicts]
        if missing_now and turn >= int(max_turns * 0.85):
            runtime_reminders.append(
                f"[runtime deadline] turn {turn}/{max_turns}; missing Required Rs: {missing_now}. "
                "Stop opening broad new investigations. Submit verdicts for Rs with enough evidence; "
                "mark only genuinely blocked Rs UNTESTABLE."
            )

        if verdict_seen:
            if runtime_reminders and tool_results:
                tool_results[-1]["content"] = (
                    str(tool_results[-1].get("content", ""))
                    + "\n\n"
                    + "\n".join(runtime_reminders)
                )
            memory.record_tool_results(tool_results, turn=turn, turn_tool_calls=turn_tool_calls)
            if completed_r_ids:
                memory.complete_rs([(r_id, r_verdicts[r_id]) for r_id in completed_r_ids])
            turn_trace["memory"] = memory.metrics()
            all_turns_trace.append(turn_trace)
            break

        if tool_results:
            all_covered = required_ids and not [r for r in required_ids if r not in r_verdicts]
            # When all Rs are covered and we're at 85%+ of the turn budget,
            # inject a hard stop reminder so the agent doesn't drift indefinitely.
            if all_covered and turn >= int(max_turns * 0.85):
                runtime_reminders.append(
                    f"[runtime reminder] turn {turn}/{max_turns}; all Required Rs covered. "
                    "STOP exploration and call submit_verdict on the next turn."
                )
            if runtime_reminders:
                tool_results[-1]["content"] = (
                    str(tool_results[-1].get("content", ""))
                    + "\n\n"
                    + "\n".join(runtime_reminders)
                )
            memory.record_tool_results(tool_results, turn=turn, turn_tool_calls=turn_tool_calls)
            if completed_r_ids:
                memory.complete_rs([(r_id, r_verdicts[r_id]) for r_id in completed_r_ids])
            turn_trace["memory"] = memory.metrics()
            all_turns_trace.append(turn_trace)

            if auto_submit_on_r_coverage and all_covered:
                failed_rs = [r for r, v in r_verdicts.items() if v.get("verdict", "").upper() == "FAILED"]
                verdict = {
                    "verdict": "FAIL" if failed_rs else "PASS",
                    "reasoning": (
                        f"Auto-submitted by runtime: all {len(required_ids)} Required Rs covered on turn {turn}."
                    ),
                    "tool_calls": tool_call_count,
                    "turns": turn,
                }
                print(f"[executor] all Rs covered — auto-submitting verdict={verdict['verdict']} (saved 1 turn)")
                break
        else:
            all_turns_trace.append(turn_trace)
            print("[executor] agent ended turn without tool calls; treating as inconclusive")
            verdict = {
                "verdict": "INCONCLUSIVE",
                "reasoning": "Agent ended turn without calling submit_verdict.",
                "tool_calls": tool_call_count,
                "turns": turn,
            }
            break

    if verdict is None:
        verdict = {
            "verdict": "TIMEOUT",
            "reasoning": f"Agent did not submit verdict within {max_turns} turns.",
            "tool_calls": tool_call_count,
            "turns": max_turns,
        }

    verdict["eval_mode"] = "cover_all"
    verdict["required_ids"] = list(required_ids)
    verdict["r_titles"] = {r.r_id: r.title for r in parsed_spec.required}
    verdict["r_verdicts"] = dict(r_verdicts)
    verdict["r_verdict_history"] = {r_id: list(history) for r_id, history in r_verdict_history.items()}
    verdict["r_amendments"] = {
        r_id: {
            "amendment_count": max(0, len(history) - 1),
            "latest": history[-1] if history else {},
            "history": history,
        }
        for r_id, history in r_verdict_history.items()
        if len(history) > 1
    }
    verdict["r_missing"] = [r for r in required_ids if r not in r_verdicts]
    verdict["tool_counts"] = dict(sorted(tool_counts.items(), key=lambda x: -x[1]))

    repro_meta["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    verdict["repro"] = repro_meta

    verdict["exploratory_findings"] = [
        e for e in EVENT_LOG
        if e.get("event_type") in ("VIOLATION", "OBSERVATION", "WARNING")
    ]

    final_cost = estimate_cost(total_input, total_output, total_cache_creation, total_cache_read)
    verdict["usage"] = {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_creation_input_tokens": total_cache_creation,
        "cache_read_input_tokens": total_cache_read,
        "cost_usd": round(final_cost, 6),
        "pricing_version": PRICING_VERSION,
        "per_turn": per_turn_usage,
    }
    verdict["events"] = list(EVENT_LOG)

    if dump_messages_to is not None:
        _dump_messages(
            memory.get_full_messages(),
            dump_messages_to,
            meta={
                "model": model,
                "target": target,
                "eval_mode": "cover_all",
                "verdict": verdict["verdict"],
                "turns": verdict["turns"],
                "tool_calls": verdict["tool_calls"],
                "events_count": len(EVENT_LOG),
                "usage": verdict["usage"],
                "required_ids": list(required_ids),
                "r_verdicts": dict(r_verdicts),
                "r_amendments": verdict["r_amendments"],
                "r_missing": verdict["r_missing"],
                "exploratory_findings_count": len(verdict["exploratory_findings"]),
                "repro": repro_meta,
            },
            events=list(EVENT_LOG),
            memory_state=memory.export_state(),
        )

    if write_trace:
        _write_trace(
            run_id=run_id,
            repro_meta=repro_meta,
            turns_trace=all_turns_trace,
            verdict=verdict,
            total_tool_calls=tool_call_count,
            total_cost=final_cost,
        )

    return verdict
