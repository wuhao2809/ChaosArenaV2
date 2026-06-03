"""ChaosArena — unified entry point.

All output files are named from a single --run-id:

  <run-id>_spec.md     → specs/          (only when --nl-input drafts a spec)
  <run-id>_trace.json  → trace/          (always written)
  <run-id>_memory.json → memory/         (always written)

Usage examples:

  # Draft spec from NL, run evaluation, all outputs named "race_001":
  python main.py --nl-input ../nl_specs/album_store.txt \\
                 --target http://localhost:8080 \\
                 --run-id race_001

  # Run from an existing spec, named run:
  python main.py --spec ../specs/album_store_v2.md \\
                 --target http://localhost:8080 \\
                 --run-id default_baseline_001

  # Draft only (no evaluation):
  python main.py --nl-input ../nl_specs/album_store.txt \\
                 --draft-only --run-id album_store_spec_v1

  # Omit --run-id to use a unix timestamp as the name.
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from config.config import DEFAULT_MAX_TURNS
from runner import run_agent
from spec_drafter import draft_spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_run_id(run_id_arg: str | None) -> str:
    """Return the run-id to use: user-supplied or a UTC timestamp."""
    if run_id_arg:
        return run_id_arg
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _output_paths(run_id: str, script_dir: Path) -> dict[str, Path]:
    """Derive all output paths from a single run-id."""
    return {
        "spec":   script_dir.parent / "specs"  / f"{run_id}_spec.md",
        "trace":  script_dir        / "trace"  / f"{run_id}_trace.json",
        "memory": script_dir        / "memory" / f"{run_id}_memory.json",
    }


def _print_verdict(result: dict, drafter_usage: dict | None, paths: dict[str, Path]) -> None:
    """Print the final verdict box."""
    print()
    print("=" * 72)
    print(f"VERDICT: {result['verdict']}")
    print("=" * 72)
    print(result["reasoning"])
    print()
    print(
        f"[stats] turns={result['turns']}  "
        f"tool_calls={result['tool_calls']}  "
        f"eval_mode={result.get('eval_mode', '?')}"
    )

    rs = result.get("r_verdicts") or {}
    if rs:
        print(f"[per-R] {len(rs)} of {len(result.get('required_ids', []))} Rs covered:")
        for r_id in result.get("required_ids", []):
            r = rs.get(r_id)
            if r:
                print(f"   {r_id}: {r['verdict']:<11} (conf={r['confidence']})")
            else:
                print(f"   {r_id}: (no verdict)")

    findings = result.get("exploratory_findings") or []
    if findings:
        print(f"[exploration] {len(findings)} finding(s) outside Required:")
        for f in findings[:10]:
            detail = f.get("detail", "")
            if len(detail) > 140:
                detail = detail[:140] + "…"
            print(f"   [{f.get('event_type', '?')}] {detail}")
        if len(findings) > 10:
            print(f"   ... ({len(findings) - 10} more)")

    agent_usage = result.get("usage")
    drafter_cost = drafter_usage["cost_usd"] if drafter_usage else 0.0
    if agent_usage:
        agent_cost = agent_usage["cost_usd"]
        total_cost = round(agent_cost + drafter_cost, 6)
        print(
            f"[usage] agent:   tokens_in={agent_usage['input_tokens']:,}  "
            f"tokens_out={agent_usage['output_tokens']:,}  "
            f"cost=${agent_cost:.4f}"
        )
        if drafter_usage:
            print(
                f"[usage] drafter: tokens_in={drafter_usage['input_tokens']:,}  "
                f"tokens_out={drafter_usage['output_tokens']:,}  "
                f"cost=${drafter_cost:.4f}"
            )
        print(
            f"[usage] total:   ${total_cost:.4f}  "
            f"(pricing {agent_usage['pricing_version']})"
        )

    repro = result.get("repro")
    if repro:
        print(
            f"[repro] git={repro['git_commit']}  "
            f"spec_sha256={repro['spec_sha256'][:12]}…  "
            f"temp={repro['temperature']}"
        )

    print()
    print(f"[outputs] trace  → {paths['trace']}")
    print(f"[outputs] memory → {paths['memory']}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="ChaosArena — LLM-agent HTTP service evaluator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "All outputs are named from --run-id:\n"
            "  specs/<run-id>_spec.md     (when --nl-input is used)\n"
            "  trace/<run-id>_trace.json  (always)\n"
            "  memory/<run-id>_memory.json (always)\n"
        ),
    )

    spec_group = parser.add_mutually_exclusive_group(required=True)
    spec_group.add_argument(
        "--spec",
        help="Path to an existing spec markdown file.",
    )
    spec_group.add_argument(
        "--nl-input",
        help="Path to a natural-language service description. "
             "Drafts a spec first, then optionally runs the evaluation.",
    )

    parser.add_argument(
        "--run-id",
        default=None,
        help=(
            "Name for this run. Controls all output filenames: "
            "<run-id>_spec.md / <run-id>_trace.json / <run-id>_memory.json. "
            "Example: 'race_bugmode_001'. Defaults to a UTC timestamp."
        ),
    )
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="Draft the spec and exit without running the agent. Only valid with --nl-input.",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="Base URL of the service to evaluate (e.g. http://localhost:8080). "
             "Required unless --draft-only.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help=f"Maximum agent turns (default: {DEFAULT_MAX_TURNS}). "
             "Confirmed interactively before the run starts.",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="Path to system prompt file (default: system_prompt.txt next to main.py).",
    )
    parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Skip all interactive prompts (spec trim, run confirmation, max-turns). "
             "Useful for CI / scripting.",
    )

    args = parser.parse_args()

    # Validate flag combinations.
    if args.draft_only and not args.nl_input:
        parser.error("--draft-only requires --nl-input")
    if not args.draft_only and not args.target:
        parser.error("--target is required unless --draft-only")

    script_dir = Path(__file__).resolve().parent
    run_id = _resolve_run_id(args.run_id)
    paths = _output_paths(run_id, script_dir)
    drafter_usage: dict | None = None

    print(f"[chaosarena] run-id: {run_id}")

    # ------------------------------------------------------------------
    # Step 1 — obtain the spec (draft or load)
    # ------------------------------------------------------------------
    if args.nl_input:
        nl_path = Path(args.nl_input)
        if not nl_path.exists():
            print(f"[error] NL input not found: {nl_path}", file=sys.stderr)
            return 2
        nl = nl_path.read_text()

        print(f"[chaosarena] Drafting spec from {nl_path.name}…")
        md, _, drafter_usage = draft_spec(nl, interactive=not args.no_interactive)

        paths["spec"].parent.mkdir(parents=True, exist_ok=True)
        paths["spec"].write_text(md)
        print(f"[chaosarena] Spec  → {paths['spec']}")
        print(
            f"[chaosarena] Drafter cost: ${drafter_usage['cost_usd']:.4f}  "
            f"(in={drafter_usage['input_tokens']:,} out={drafter_usage['output_tokens']:,})"
        )

        if args.draft_only:
            return 0

        if sys.stdin.isatty() and not args.no_interactive:
            try:
                answer = input("\n[chaosarena] Run evaluation now? [y/N] ").strip().lower()
            except EOFError:
                answer = "n"
            if answer not in ("y", "yes"):
                print("[chaosarena] Exiting without running evaluation.")
                return 0

        spec = md

    else:
        spec_path = Path(args.spec)
        if not spec_path.exists():
            print(f"[error] spec not found: {spec_path}", file=sys.stderr)
            return 2
        spec = spec_path.read_text()

    # ------------------------------------------------------------------
    # Step 2 — load system prompt
    # ------------------------------------------------------------------
    prompt_path = (
        Path(args.system_prompt) if args.system_prompt
        else script_dir / "system_prompt.txt"
    )
    if not prompt_path.exists():
        print(f"[error] system prompt not found: {prompt_path}", file=sys.stderr)
        return 2
    system_prompt = prompt_path.read_text()

    # ------------------------------------------------------------------
    # Step 3 — confirm max_turns, then run
    # ------------------------------------------------------------------
    if sys.stdin.isatty() and not args.no_interactive:
        try:
            raw = input(
                f"[chaosarena] Max turns: {args.max_turns} "
                f"(default {DEFAULT_MAX_TURNS}). Press Enter to keep, or type a new number: "
            ).strip()
            if raw:
                args.max_turns = int(raw)
                print(f"[chaosarena] Max turns set to {args.max_turns}.")
        except (ValueError, EOFError):
            pass

    for p in (paths["trace"], paths["memory"]):
        p.parent.mkdir(parents=True, exist_ok=True)

    result = run_agent(
        system_prompt=system_prompt,
        spec=spec,
        target=args.target.rstrip("/"),
        dump_messages_to=paths["memory"],
        max_turns=args.max_turns,
        run_id_override=run_id,
    )

    _print_verdict(result, drafter_usage, paths)

    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
