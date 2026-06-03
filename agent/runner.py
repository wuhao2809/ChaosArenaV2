"""Agent runner: Bedrock-backed Claude tool-use loop.

Reads a system prompt + spec + target URL, runs Claude in a tool-use loop
until either submit_verdict is called or MAX_TURNS is exceeded.
"""

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import Anthropic, AnthropicBedrock

from tools import EVENT_LOG, SESSIONS, TOOL_SCHEMAS, dispatch_tool
from spec_parser import parse_spec, ParsedSpec

MAX_TURNS = 25
MAX_TOKENS = 8192

# Reproducibility: lock sampling. Bedrock for Claude rejects passing
# both temperature and top_p simultaneously, so we send temperature only
# and record (but do not send) top_p/top_k for paper provenance.
TEMPERATURE = 0.0
TOP_P_RECORDED = 1.0
TOP_K_RECORDED = 1

# Default Bedrock model ID. Override via MODEL_ID env var if this isn't available
# in your region or you want a different model.
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
DEFAULT_DIRECT_MODEL = "claude-sonnet-4-5"

# AWS Bedrock pricing for Claude Sonnet 4.5 (us-west-2), 2026-Q1.
# Source: https://aws.amazon.com/bedrock/pricing/
# Update this version tag whenever pricing changes; record it in run metadata
# for reproducibility.
BEDROCK_PRICING_VERSION = "2026-Q1"
INPUT_COST_PER_MTOK = 3.00         # $/1M tokens for input
OUTPUT_COST_PER_MTOK = 15.00       # $/1M tokens for output
CACHE_CREATION_PER_MTOK = 3.75     # $/1M tokens (cache write, ephemeral)
CACHE_READ_PER_MTOK = 0.30         # $/1M tokens (cache hit)


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    cache_creation_tokens: int = 0,
    cache_read_tokens: int = 0,
) -> float:
    """Estimate USD cost for a Bedrock Sonnet 4.5 call from token counts."""
    return (
        input_tokens * INPUT_COST_PER_MTOK / 1_000_000
        + output_tokens * OUTPUT_COST_PER_MTOK / 1_000_000
        + cache_creation_tokens * CACHE_CREATION_PER_MTOK / 1_000_000
        + cache_read_tokens * CACHE_READ_PER_MTOK / 1_000_000
    )


def get_client():
    """Construct an Anthropic client, defaulting to Bedrock."""
    backend = os.environ.get("ANTHROPIC_BACKEND", "bedrock").lower()
    if backend == "bedrock":
        return AnthropicBedrock(
            aws_region=os.environ.get("AWS_REGION", "us-west-2"),
        )
    return Anthropic()


def get_model_id() -> str:
    explicit = os.environ.get("MODEL_ID")
    if explicit:
        return explicit
    backend = os.environ.get("ANTHROPIC_BACKEND", "bedrock").lower()
    return DEFAULT_BEDROCK_MODEL if backend == "bedrock" else DEFAULT_DIRECT_MODEL


def _print_block_text(content_blocks: list) -> None:
    """Print any text reasoning emitted by the assistant."""
    for block in content_blocks:
        if block.type == "text" and block.text.strip():
            print(f"  Claude: {block.text.strip()}")


def _truncate(s: str, n: int = 200) -> str:
    return s if len(s) <= n else s[:n] + f"... [truncated, {len(s)} total]"


def _git_commit() -> str:
    """Current git HEAD short hash, or 'unavailable'."""
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
    eval_mode: str,
    target: str,
    model: str,
) -> dict[str, Any]:
    """Collect everything needed to reproduce this run."""
    return {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "temperature": TEMPERATURE,
        "top_p_recorded": TOP_P_RECORDED,
        "top_k_recorded": TOP_K_RECORDED,
        "note": "Bedrock rejects both temperature+top_p; only temperature is sent. top_p/top_k are recorded for provenance.",
        "max_turns": MAX_TURNS,
        "max_tokens_per_turn": MAX_TOKENS,
        "eval_mode": eval_mode,
        "target_url": target,
        "spec_sha256": hashlib.sha256(spec_text.encode("utf-8")).hexdigest(),
        "system_prompt_sha256": hashlib.sha256(system_prompt_text.encode("utf-8")).hexdigest(),
        "git_commit": _git_commit(),
    }


def _serialize_messages(messages: list[dict]) -> list[dict]:
    """Convert the messages list into a JSON-serializable form.

    The assistant messages contain SDK objects (TextBlock, ToolUseBlock);
    Pydantic's model_dump() converts them to plain dicts.
    """
    out = []
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        serialized_blocks = []
        for block in content:
            if hasattr(block, "model_dump"):
                serialized_blocks.append(block.model_dump())
            elif isinstance(block, dict):
                serialized_blocks.append(block)
            else:
                serialized_blocks.append({"raw": str(block)})
        out.append({"role": role, "content": serialized_blocks})
    return out


def _dump_messages(messages: list[dict], path: Path, meta: dict, events: list[dict]) -> None:
    """Write the full conversation history + metadata + events to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": meta,
        "events": events,
        "messages": _serialize_messages(messages),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"[runner] conversation dumped to {path}")


def run_agent(
    system_prompt: str,
    spec: str,
    target: str,
    dump_messages_to: Path | None = None,
    eval_mode: str = "fail_fast",
) -> dict[str, Any]:
    """Drive the agent through a tool-use loop. Returns the verdict dict.

    Parameters
    ----------
    eval_mode : {"fail_fast", "cover_all"}
        - fail_fast (default): agent may submit overall verdict any time.
        - cover_all: agent must submit a per-R verdict for every Required
          category in the spec before submit_verdict is accepted.

    If dump_messages_to is provided, writes the full conversation history
    (after the run finishes, success or failure) to that JSON path.
    """
    if eval_mode not in {"fail_fast", "cover_all"}:
        raise ValueError(f"eval_mode must be fail_fast or cover_all, got {eval_mode!r}")

    # Clear module-level state for this run.
    EVENT_LOG.clear()
    # Close any open Session() objects so we don't leak connections, then
    # reset the dict — every run starts with no logged-in users.
    for _sess in SESSIONS.values():
        try:
            _sess.close()
        except Exception:
            pass
    SESSIONS.clear()

    parsed_spec: ParsedSpec = parse_spec(spec)
    required_ids = parsed_spec.required_ids   # e.g. ["R1", "R2", ...]

    client = get_client()
    model = get_model_id()

    coverage_line = (
        f"Required test categories (you must address each): "
        f"{', '.join(required_ids) if required_ids else '(none parsed)'}"
    )
    mode_line = f"Eval mode: {eval_mode}."
    if eval_mode == "cover_all":
        mode_line += (
            " You MUST emit submit_verdict_for_R for every Required R "
            "before submit_verdict will be accepted."
        )
    else:
        mode_line += (
            " You MAY submit_verdict at any time once decisive evidence "
            "(e.g., a critical R FAILED) is in hand. Calling "
            "submit_verdict_for_R per R is encouraged but not required."
        )

    initial_user = (
        f"=== SPEC ===\n{spec}\n\n"
        f"=== TARGET ===\n{target}\n\n"
        f"=== RUN CONFIG ===\n{coverage_line}\n{mode_line}\n\n"
        f"Begin the evaluation."
    )
    messages: list[dict] = [{"role": "user", "content": initial_user}]

    repro_meta = _build_repro_metadata(
        spec_text=spec,
        system_prompt_text=system_prompt,
        eval_mode=eval_mode,
        target=target,
        model=model,
    )

    print(f"[runner] model = {model}")
    print(f"[runner] target = {target}")
    print(f"[runner] max_turns = {MAX_TURNS}  temp={TEMPERATURE} (top_p/top_k recorded only)")
    print(f"[runner] eval_mode = {eval_mode}")
    print(f"[runner] required Rs = {required_ids}")
    if eval_mode == "cover_all" and len(required_ids) > 12:
        print(
            f"[runner] WARNING: {len(required_ids)} Required Rs in cover_all mode — "
            f"agent must verdict all before submit_verdict is accepted. "
            f"Risk of timeout at MAX_TURNS={MAX_TURNS}. Consider trimming spec to <= 12 Rs."
        )
    print(f"[runner] git_commit = {repro_meta['git_commit']}  spec_sha256 = {repro_meta['spec_sha256'][:12]}...\n")

    verdict: dict[str, Any] | None = None
    tool_call_count = 0
    turn = 0

    # Per-R verdict tracking. r_verdicts[r_id] -> {verdict, confidence, evidence}.
    r_verdicts: dict[str, dict[str, str]] = {}

    # Per-turn and cumulative token usage. We track each turn so the dump
    # JSON has a per-turn breakdown for paper figures / cost forecasting.
    per_turn_usage: list[dict[str, int]] = []
    total_input = 0
    total_output = 0
    total_cache_creation = 0
    total_cache_read = 0

    for turn in range(1, MAX_TURNS + 1):
        print()
        print(f"--- Turn {turn} ---")

        response = client.messages.create(
            model=model,
            system=system_prompt,
            messages=messages,
            tools=TOOL_SCHEMAS,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
        )

        # Accumulate token usage for this turn.
        usage = response.usage
        turn_input = usage.input_tokens
        turn_output = usage.output_tokens
        turn_cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        turn_cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        total_input += turn_input
        total_output += turn_output
        total_cache_creation += turn_cache_creation
        total_cache_read += turn_cache_read

        cum_cost = estimate_cost(
            total_input, total_output, total_cache_creation, total_cache_read
        )

        per_turn_usage.append({
            "turn": turn,
            "input_tokens": turn_input,
            "output_tokens": turn_output,
            "cache_creation_input_tokens": turn_cache_creation,
            "cache_read_input_tokens": turn_cache_read,
        })

        print(
            f"  [tokens] in={turn_input:,} out={turn_output:,} "
            f"cum_in={total_input:,} cum_out={total_output:,} cum_cost=${cum_cost:.4f}"
        )

        # Record assistant's response in conversation history.
        messages.append({"role": "assistant", "content": response.content})

        _print_block_text(response.content)

        tool_results: list[dict] = []
        verdict_seen = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            tool_call_count += 1
            args_str = json.dumps(block.input, ensure_ascii=False)
            print(f"  → {block.name}({_truncate(args_str, 300)})")

            if block.name == "submit_verdict_for_R":
                # Record per-R verdict; do NOT end the run.
                r_id = block.input.get("r_id", "?")
                r_verdicts[r_id] = {
                    "verdict": block.input.get("verdict", "UNKNOWN"),
                    "confidence": block.input.get("confidence", "UNKNOWN"),
                    "evidence": block.input.get("evidence", ""),
                }
                remaining = [r for r in required_ids if r not in r_verdicts]
                if remaining:
                    ack = (
                        f"Recorded {r_id} verdict: {r_verdicts[r_id]['verdict']} "
                        f"(confidence={r_verdicts[r_id]['confidence']}). "
                        f"Remaining Required: {remaining}. "
                        f"Eval mode: {eval_mode}."
                    )
                elif eval_mode == "cover_all":
                    ack = (
                        f"Recorded {r_id} verdict: {r_verdicts[r_id]['verdict']} "
                        f"(confidence={r_verdicts[r_id]['confidence']}). "
                        f"All Required Rs covered. "
                        f"NEXT STEP (cover_all): call submit_verdict NOW to end the run. "
                        f"Do NOT continue probing — exploration is for fail_fast mode."
                    )
                else:
                    ack = (
                        f"Recorded {r_id} verdict: {r_verdicts[r_id]['verdict']} "
                        f"(confidence={r_verdicts[r_id]['confidence']}). "
                        f"All Required Rs covered. "
                        f"You may now call submit_verdict, or use the remaining "
                        f"turn budget for brief Open Exploration. "
                        f"Hard limit: STOP exploration and call submit_verdict by turn {MAX_TURNS - 5} "
                        f"or after 5 substantive findings — whichever comes first."
                    )
                print(f"  ← {ack}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": ack,
                })

            elif block.name == "submit_verdict":
                # Overall verdict. Validate cover_all preconditions.
                missing = [r for r in required_ids if r not in r_verdicts]
                if eval_mode == "cover_all" and missing:
                    refusal = (
                        f"REJECTED: cover_all mode requires per-R verdict for every "
                        f"Required category before submit_verdict. Still missing: "
                        f"{missing}. Continue testing and emit submit_verdict_for_R "
                        f"for each missing R, then call submit_verdict again."
                    )
                    print(f"  ← {refusal}")
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
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Verdict accepted. Run complete.",
                    })

            else:
                result = dispatch_tool(block.name, block.input, target)
                result_str = json.dumps(result, ensure_ascii=False, default=str)
                print(f"  ← {_truncate(result_str, 300)}")
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

        if verdict_seen:
            # We still append the tool_result so the conversation is well-formed,
            # then break out.
            messages.append({"role": "user", "content": tool_results})
            break

        if tool_results:
            # If we're past 70% of budget AND all Rs are covered AND in
            # fail_fast mode, inject a stop-exploration reminder into the
            # last tool_result's content (Anthropic API requires
            # tool_results to follow tool_use immediately, so we piggyback).
            should_remind = (
                eval_mode == "fail_fast"
                and required_ids
                and not [r for r in required_ids if r not in r_verdicts]
                and turn >= int(MAX_TURNS * 0.7)
            )
            if should_remind and tool_results:
                last = tool_results[-1]
                last["content"] = (
                    str(last.get("content", ""))
                    + f"\n\n[runtime reminder] turn {turn}/{MAX_TURNS}; all "
                    f"Required Rs covered. STOP exploration and call "
                    f"submit_verdict on the next turn. Do not start new "
                    f"exploratory probes."
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            # No tool calls and no verdict — agent stopped reasoning. Treat as inconclusive.
            print("[runner] agent ended turn without tool calls; treating as inconclusive")
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
            "reasoning": f"Agent did not submit verdict within {MAX_TURNS} turns.",
            "tool_calls": tool_call_count,
            "turns": MAX_TURNS,
        }

    # Attach per-R verdicts and eval mode to the overall verdict.
    verdict["eval_mode"] = eval_mode
    verdict["required_ids"] = list(required_ids)
    verdict["r_verdicts"] = dict(r_verdicts)
    verdict["r_missing"] = [r for r in required_ids if r not in r_verdicts]

    # Stamp run completion + reproducibility info.
    repro_meta["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    verdict["repro"] = repro_meta

    # Exploratory findings = events recorded by the agent that are NOT
    # tied to a Required R. Useful for paper §5 to separate "in-spec" vs
    # "discovered in Open Exploration" violations.
    verdict["exploratory_findings"] = [
        e for e in EVENT_LOG
        if e.get("event_type") in ("VIOLATION", "OBSERVATION", "WARNING")
    ]

    # Attach token / cost stats to the verdict for caller visibility.
    final_cost = estimate_cost(
        total_input, total_output, total_cache_creation, total_cache_read
    )
    verdict["usage"] = {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cache_creation_input_tokens": total_cache_creation,
        "cache_read_input_tokens": total_cache_read,
        "cost_usd": round(final_cost, 6),
        "pricing_version": BEDROCK_PRICING_VERSION,
        "per_turn": per_turn_usage,
    }

    # Attach recorded events to the verdict for caller visibility.
    verdict["events"] = list(EVENT_LOG)

    if dump_messages_to is not None:
        _dump_messages(
            messages,
            dump_messages_to,
            meta={
                "model": model,
                "target": target,
                "eval_mode": eval_mode,
                "verdict": verdict["verdict"],
                "turns": verdict["turns"],
                "tool_calls": verdict["tool_calls"],
                "events_count": len(EVENT_LOG),
                "usage": verdict["usage"],
                "required_ids": list(required_ids),
                "r_verdicts": dict(r_verdicts),
                "r_missing": verdict["r_missing"],
                "exploratory_findings_count": len(verdict["exploratory_findings"]),
                "repro": repro_meta,
            },
            events=list(EVENT_LOG),
        )

    return verdict
