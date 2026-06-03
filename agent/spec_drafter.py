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


DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
DEFAULT_DIRECT_MODEL = "claude-sonnet-4-5"

DRAFTER_TEMPERATURE = 0.0
DRAFTER_MAX_TOKENS = 8192


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
    """Strip any prose / fences and parse the JSON object the LLM emitted."""
    # Strip ```json fences if present
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))
    # Otherwise find the first balanced JSON object
    obj_match = re.search(r"\{.*\}", text, re.DOTALL)
    if not obj_match:
        raise ValueError("No JSON object found in drafter output.")
    return json.loads(obj_match.group(0))


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
                for key in ("name", "given", "when", "then"):
                    if key not in tc:
                        raise ValueError(f"{cat}[{i}] missing field '{key}'")
        else:
            raise ValueError(f"Category {cat} must be list or n_a dict, got {type(v).__name__}")

    return payload


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


def draft_spec(
    nl_description: str,
    system_prompt_path: Path | None = None,
) -> tuple[str, dict]:
    """Run one Bedrock call to draft a spec from natural language.

    Returns (markdown_text, raw_json_dict). Raises on validation failure.
    """
    here = Path(__file__).resolve().parent
    sp_path = system_prompt_path or (here / "system_prompt_drafter.txt")
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

    response = client.messages.create(
        model=model,
        system=system_prompt,
        messages=[{"role": "user", "content": user_msg}],
        max_tokens=DRAFTER_MAX_TOKENS,
        temperature=DRAFTER_TEMPERATURE,
    )

    raw_text = "".join(b.text for b in response.content if b.type == "text")
    spec_json = _extract_json(raw_text)
    spec_json = _validate_spec_json(spec_json)
    markdown = _render_markdown(spec_json)
    return markdown, spec_json


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
    args = parser.parse_args()

    if args.input == "-":
        nl = sys.stdin.read()
    else:
        nl = Path(args.input).read_text()

    md, raw = draft_spec(nl)

    if args.output:
        Path(args.output).write_text(md)
        print(f"[drafter] markdown spec written to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(md)

    if args.also_write_json:
        Path(args.also_write_json).write_text(json.dumps(raw, indent=2, ensure_ascii=False))
        print(f"[drafter] raw JSON written to {args.also_write_json}", file=sys.stderr)
