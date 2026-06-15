"""Spec drafter: natural-language description → structured markdown spec.

One Bedrock Claude call. Prompt design follows lit_scan_round6_prompt_eng.md:
- Anthropic XML tags (verified: Anthropic engineering blog Dec 2024)
- Multi-stage structured chain-of-thought (verified: Ryan et al. 2024
  SymPrompt arXiv 2402.00097; Yao 2023 ToT arXiv 2305.10601)
- Hard-coded 4-category taxonomy injection — race / async / auth / edge
  (justified: Tambon et al. 2024 arXiv 2403.08937 shows generic LLM-bug
  taxonomies omit concurrency)
- JSON-schema-shaped output, validated then rendered to markdown
  (verified: Willard & Louf 2023 Outlines arXiv 2307.09702)
- Self-review pass (verified: Anthropic "Building Effective Agents"
  evaluator-optimizer pattern Dec 2024)

Ground-truth verification of spec quality is the agent's downstream job;
this module only produces a candidate the TA reviews before running.
"""

import json
import os
import re
from pathlib import Path

from anthropic import Anthropic, AnthropicBedrock

from config.config import (
    DEFAULT_MAX_TURNS, DRAFTER_TEMPERATURE, DRAFTER_MAX_TOKENS,
    DEFAULT_BEDROCK_MODEL, DEFAULT_DIRECT_MODEL,
    BEDROCK_PRICING_VERSION, INPUT_COST_PER_MTOK, OUTPUT_COST_PER_MTOK,
    CACHE_CREATION_PER_MTOK, CACHE_READ_PER_MTOK,
    CATEGORY_TURN_COST, PRIORITY_ORDER,
    DEFAULT_R_ESTIMATED_TURNS,
)


def _get_client():
    backend = os.environ.get("ANTHROPIC_BACKEND", "bedrock").lower()
    if backend == "bedrock":
        return AnthropicBedrock(aws_region=os.environ.get("AWS_REGION", "us-west-2"))
    return Anthropic()


def _get_model_id() -> str:
    explicit = os.environ.get("MODEL_ID")
    if explicit:
        return explicit
    backend = os.environ.get("ANTHROPIC_BACKEND", "bedrock").lower()
    return DEFAULT_BEDROCK_MODEL if backend == "bedrock" else DEFAULT_DIRECT_MODEL


def _extract_json(text: str) -> dict:
    """Robustly extract the JSON object from drafter output.

    Handles:
    - Sonnet 4.6 extended thinking inside <thinking>...</thinking> tags
    - ```json fenced code blocks (takes the LAST valid one)
    - Raw JSON objects embedded in prose
    """
    # Strip extended thinking blocks first — Sonnet 4.6 outputs these before JSON.
    clean = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL).strip()

    # Try all fenced blocks, prefer the last valid JSON (model may emit thinking
    # with partial JSON before the real output).
    fences = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    for candidate in reversed(fences):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    # No valid fence — walk backwards to find the last complete balanced {} object.
    depth = 0
    end_pos = -1
    for i in range(len(clean) - 1, -1, -1):
        if clean[i] == '}':
            if end_pos == -1:
                end_pos = i
            depth += 1
        elif clean[i] == '{':
            depth -= 1
            if depth == 0 and end_pos != -1:
                try:
                    return json.loads(clean[i:end_pos + 1])
                except json.JSONDecodeError:
                    # Reset and keep searching further back.
                    end_pos = -1
                    depth = 0

    raise ValueError(
        f"No valid JSON object found in drafter output. "
        f"Preview: {clean[:300]!r}"
    )


def _validate_spec_json(payload: dict) -> dict:
    """Light validation. Raises if a required key is missing or empty."""
    required_top = {
        "service_name",
        "description",
        "race_conditions",
        "async_invariants",
        "auth_boundaries",
        "edge_cases",
        "open_exploration_hint",
    }
    missing = required_top - set(payload.keys())
    if missing:
        raise ValueError(f"Drafter output missing required keys: {sorted(missing)}")

    for cat in ("race_conditions", "async_invariants", "auth_boundaries", "edge_cases"):
        v = payload[cat]
        if isinstance(v, dict):
            if "n_a_justification" not in v:
                raise ValueError(f"Category {cat} is a dict but lacks n_a_justification")
        elif isinstance(v, list):
            if not v:
                raise ValueError(f"Category {cat} is an empty list (forbidden)")
            for i, tc in enumerate(v):
                for key in ("name", "given", "when", "then", "priority"):
                    if key not in tc:
                        raise ValueError(f"{cat}[{i}] missing field '{key}'")
                if tc["priority"] not in ("HIGH", "MEDIUM", "LOW"):
                    raise ValueError(
                        f"{cat}[{i}] priority must be HIGH/MEDIUM/LOW, "
                        f"got {tc['priority']!r}"
                    )
                tc["estimated_turns"] = _coerce_estimated_turns(
                    tc.get("estimated_turns"),
                    CATEGORY_TURN_COST.get(cat, DEFAULT_R_ESTIMATED_TURNS),
                )
        else:
            raise ValueError(f"Category {cat} must be list or n_a dict, got {type(v).__name__}")

    return payload


def _coerce_estimated_turns(value: object, default: int = DEFAULT_R_ESTIMATED_TURNS) -> int:
    try:
        return int(value) if value is not None else int(default)
    except (TypeError, ValueError):
        return int(default)


def _render_markdown(spec: dict) -> str:
    """Render the validated JSON spec to a two-tier markdown spec consumable
    by spec_parser.py downstream.

    Each TestCase becomes a `### Rn.` block under `## Required Test Categories`.
    R-numbering is sequential across all four categories.
    n_a_justification categories are skipped with a note.
    """
    lines: list[str] = []
    lines.append(f"# {spec['service_name']} — System Spec (drafted)")
    lines.append("")
    lines.append("## Description")
    lines.append("")
    lines.append(spec["description"])
    lines.append("")
    lines.append(
        "*This spec was drafted by ChaosArena's `spec_drafter` from a "
        "natural-language description. A TA should review and edit "
        "before running an evaluation.*"
    )
    lines.append("")
    lines.append("## Required Test Categories")
    lines.append("")

    r_counter = 1
    category_titles = {
        "race_conditions": "Race-condition tests (concurrent operations on shared state)",
        "async_invariants": "Async / temporal invariants",
        "auth_boundaries": "Authorization boundaries",
        "edge_cases": "Edge cases (input validation, oversize, error semantics)",
    }

    for cat in ("race_conditions", "async_invariants", "auth_boundaries", "edge_cases"):
        v = spec[cat]
        lines.append(f"<!-- Category: {cat} — {category_titles[cat]} -->")
        lines.append("")
        if isinstance(v, dict):
            lines.append(
                f"*Category {cat} marked N/A by drafter: {v['n_a_justification']}*"
            )
            lines.append("")
        else:
            for tc in v:
                lines.append(f"### R{r_counter}. {tc['name']}")
                lines.append("")
                lines.append(f"- **Given**: {tc['given']}")
                lines.append(f"- **When**: {tc['when']}")
                lines.append(f"- **Then**: {tc['then']}")
                lines.append(f"- **Priority**: {tc.get('priority', 'MEDIUM')}")
                lines.append(
                    f"- **Estimated turns**: "
                    f"{_coerce_estimated_turns(tc.get('estimated_turns'), CATEGORY_TURN_COST[cat])}"
                )
                lines.append("")
                r_counter += 1

    lines.append("## Open Exploration")
    lines.append("")
    lines.append(spec["open_exploration_hint"])
    lines.append("")
    lines.append("## Out of Scope")
    lines.append("")
    lines.append(
        "(The drafter does not infer Out-of-Scope items. The TA should add "
        "any explicit exclusions during review.)"
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "*Drafter notes for the TA reviewer: The drafter is required by "
        "construction to produce sections for race / async / auth / edge. "
        "Categories marked N/A include the drafter's stated justification. "
        "Verify the justification before accepting; chaos-engineering value "
        "is highest in categories the drafter chose to populate.*"
    )
    return "\n".join(lines)


def _estimate_run_turns(spec: dict) -> int:
    """Rough total turn estimate for a cover_all run on this spec."""
    total = 0
    for cat, cost in CATEGORY_TURN_COST.items():
        v = spec.get(cat, {})
        if isinstance(v, list):
            total += sum(_coerce_estimated_turns(tc.get("estimated_turns"), cost) for tc in v)
    return total


def _interactive_trim(spec: dict) -> dict:
    """Show Rs sorted by priority + estimated turn cost; let the user pick a cutoff.

    Returns a (possibly filtered) copy of spec. Categories that lose all their
    test cases are replaced with an n_a_justification dict so the markdown
    renderer and downstream spec_parser still see a valid structure.
    Skipped entirely when stdin is not a TTY (piped / CI mode).
    """
    import sys

    if not sys.stdin.isatty():
        return spec

    # Collect every TestCase across all four categories with its origin index.
    rows: list[tuple[str, int, dict, int]] = []  # (category, orig_idx, tc, est_turns)
    for cat in ("race_conditions", "async_invariants", "auth_boundaries", "edge_cases"):
        v = spec.get(cat, {})
        if isinstance(v, list):
            for i, tc in enumerate(v):
                cost = _coerce_estimated_turns(tc.get("estimated_turns"), CATEGORY_TURN_COST[cat])
                rows.append((cat, i, tc, cost))

    if not rows:
        return spec

    # Sort: HIGH → MEDIUM → LOW, then stable by original order within priority.
    rows.sort(key=lambda r: PRIORITY_ORDER.get(r[2].get("priority", "LOW"), 2))

    # Print the table.
    print("\n[spec_drafter] Generated test cases (sorted by priority):\n")
    cumulative = 0
    for seq, (cat, _, tc, cost) in enumerate(rows, start=1):
        priority = tc.get("priority", "?")
        cumulative += cost
        print(
            f"  R{seq:2d} [{priority:<6}] {tc['name'][:48]:<48}"
            f"  ~{cost} turn{'s' if cost > 1 else ' '}   cumulative: {cumulative}"
        )

    total_turns = sum(r[3] for r in rows)
    high_rs    = [(cat, idx) for cat, idx, tc, _ in rows if tc.get("priority") == "HIGH"]
    med_rs     = [(cat, idx) for cat, idx, tc, _ in rows if tc.get("priority") in ("HIGH", "MEDIUM")]
    cost_by_key = {(cat, idx): cost for cat, idx, _, cost in rows}
    high_turns = sum(cost_by_key[(cat, idx)] for cat, idx in high_rs)
    med_turns = sum(cost_by_key[(cat, idx)] for cat, idx in med_rs)

    # Suggest a cutoff based on the turn budget.
    if total_turns <= DEFAULT_MAX_TURNS * 0.8:
        suggestion = "A"
    elif med_turns <= DEFAULT_MAX_TURNS * 0.8:
        suggestion = "M"
    else:
        suggestion = "H"

    print(f"\n  Total: {len(rows)} Rs, ~{total_turns} turns  (MAX_TURNS={DEFAULT_MAX_TURNS})\n")
    print(f"  [H] HIGH only          {len(high_rs):2d} Rs, ~{high_turns} turns")
    print(f"  [M] MEDIUM and above   {len(med_rs):2d} Rs, ~{med_turns} turns")
    print(f"  [A] All                {len(rows):2d} Rs, ~{total_turns} turns")
    print(f"  [C] Custom (space-separated R numbers, e.g. 1 2 4)\n")
    print(f"  Suggested: [{suggestion}]")

    keep_keys: set[tuple[str, int]] = set()
    while True:
        try:
            choice = input("  Keep [H/M/A/C]? ").strip().upper()
        except EOFError:
            choice = suggestion

        if choice == "H":
            keep_keys = set(high_rs)
            break
        elif choice == "M":
            keep_keys = set(med_rs)
            break
        elif choice == "A":
            keep_keys = {(cat, idx) for cat, idx, _, _ in rows}
            break
        elif choice == "C":
            try:
                raw = input("  R numbers to keep: ").strip()
                chosen_seqs = {int(x) for x in raw.split()}
                keep_keys = {
                    (cat, idx)
                    for seq, (cat, idx, _, _) in enumerate(rows, start=1)
                    if seq in chosen_seqs
                }
                break
            except (ValueError, EOFError):
                print("  Invalid — try again.")
        else:
            print("  Please enter H, M, A, or C.")

    # Build the filtered spec, preserving all non-list keys unchanged.
    filtered = dict(spec)
    for cat in ("race_conditions", "async_invariants", "auth_boundaries", "edge_cases"):
        v = spec[cat]
        if not isinstance(v, list):
            continue  # already N/A — leave untouched
        kept = [tc for i, tc in enumerate(v) if (cat, i) in keep_keys]
        if kept:
            filtered[cat] = kept
        else:
            filtered[cat] = {
                "n_a_justification": "All cases in this category were removed during interactive trim."
            }

    kept_total = sum(
        len(filtered[c]) for c in ("race_conditions", "async_invariants", "auth_boundaries", "edge_cases")
        if isinstance(filtered[c], list)
    )
    kept_turns = _estimate_run_turns(filtered)
    print(f"\n  [spec_drafter] Keeping {kept_total} Rs, ~{kept_turns} estimated turns.\n")
    return filtered


def draft_spec(
    nl_description: str,
    system_prompt_path: Path | None = None,
    interactive: bool = True,
) -> tuple[str, dict, dict]:
    """Run one Bedrock call to draft a spec from natural language.

    Returns (markdown_text, raw_json_dict, drafter_usage_dict).
    Raises on validation failure.
    """
    here = Path(__file__).resolve().parent
    sp_path = system_prompt_path or (here / "prompts" / "spec_drafter_system.txt")
    if not sp_path.exists():
        raise FileNotFoundError(f"Drafter system prompt not found: {sp_path}")
    system_prompt = sp_path.read_text()

    client = _get_client()
    model = _get_model_id()

    user_msg = (
        f"<input_service_description>\n{nl_description.strip()}\n"
        f"</input_service_description>\n\n"
        f"Now produce the JSON spec following the protocol in your system "
        f"prompt. Think first inside <thinking> tags, then emit JSON only."
    )

    print(f"[drafter] model = {model}")
    print(f"[drafter] sending to model… (may take 30–60s)", flush=True)

    response = client.messages.create(
        model=model,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=DRAFTER_MAX_TOKENS,
        temperature=DRAFTER_TEMPERATURE,
    )

    print(
        f"[drafter] response received — "
        f"in={response.usage.input_tokens:,} out={response.usage.output_tokens:,} tokens. "
        f"Extracting JSON…",
        flush=True,
    )
    raw_text = "".join(b.text for b in response.content if b.type == "text")
    spec_json = _extract_json(raw_text)

    print("[drafter] Validating spec…", flush=True)
    spec_json = _validate_spec_json(spec_json)

    n_cases = sum(
        len(spec_json[c]) for c in ("race_conditions", "async_invariants", "auth_boundaries", "edge_cases")
        if isinstance(spec_json.get(c), list)
    )
    print(f"[drafter] {n_cases} test cases generated. Estimating turn cost…", flush=True)

    if interactive:
        spec_json = _interactive_trim(spec_json)
    markdown = _render_markdown(spec_json)

    u = response.usage
    cache_create = getattr(u, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(u, "cache_read_input_tokens", 0) or 0
    drafter_usage = {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
        "cost_usd": round(
            u.input_tokens * INPUT_COST_PER_MTOK / 1_000_000
            + u.output_tokens * OUTPUT_COST_PER_MTOK / 1_000_000
            + cache_create * CACHE_CREATION_PER_MTOK / 1_000_000
            + cache_read * CACHE_READ_PER_MTOK / 1_000_000,
            6,
        ),
        "pricing_version": BEDROCK_PRICING_VERSION,
    }
    return markdown, spec_json, drafter_usage


if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(description="ChaosArena spec drafter (NL → spec.md)")
    parser.add_argument(
        "--input",
        required=True,
        help="Path to natural-language description, or '-' for stdin",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Where to write the markdown spec (default: stdout)",
    )
    parser.add_argument(
        "--also-write-json",
        default=None,
        help="Optional path to write the raw validated JSON for inspection",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Skip the interactive trim gate (useful for scripting / CI)",
    )
    args = parser.parse_args()

    if args.input == "-":
        nl = sys.stdin.read()
    else:
        nl = Path(args.input).read_text()

    md, raw, usage = draft_spec(nl, interactive=not args.no_interactive)

    if args.output:
        Path(args.output).write_text(md)
        print(f"[drafter] markdown spec written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(md)

    if args.also_write_json:
        Path(args.also_write_json).write_text(json.dumps(raw, indent=2, ensure_ascii=False))
        print(f"[drafter] raw JSON written to {args.also_write_json}", file=sys.stderr)

    print(
        f"[drafter] tokens_in={usage['input_tokens']:,}  "
        f"tokens_out={usage['output_tokens']:,}  "
        f"cost=${usage['cost_usd']:.4f}  ({usage['pricing_version']})",
        file=sys.stderr,
    )
