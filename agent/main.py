"""ChaosArena MVP — agent CLI entry point.

Usage:
    python main.py --spec ../specs/tasktracker.md --target http://localhost:8080
"""

import argparse
import sys
from pathlib import Path

from runner import run_agent


def main() -> int:
    parser = argparse.ArgumentParser(description="ChaosArena MVP agent")
    parser.add_argument(
        "--spec",
        required=True,
        help="Path to the spec markdown file",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Base URL of the deployed service to evaluate (e.g. http://localhost:8080)",
    )
    parser.add_argument(
        "--system-prompt",
        default=None,
        help="Path to system prompt file (default: system_prompt.txt next to main.py)",
    )
    parser.add_argument(
        "--dump-messages",
        default=None,
        help="Optional path to write the full conversation history as JSON after the run",
    )
    parser.add_argument(
        "--eval-mode",
        choices=["fail_fast", "cover_all"],
        default="fail_fast",
        help=(
            "Evaluation mode. fail_fast (default): agent may submit overall "
            "verdict any time. cover_all: agent must emit submit_verdict_for_R "
            "for every Required R before submit_verdict is accepted."
        ),
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    prompt_path = Path(args.system_prompt) if args.system_prompt else script_dir / "system_prompt.txt"
    spec_path = Path(args.spec)

    if not prompt_path.exists():
        print(f"[error] system prompt not found: {prompt_path}", file=sys.stderr)
        return 2
    if not spec_path.exists():
        print(f"[error] spec not found: {spec_path}", file=sys.stderr)
        return 2

    system_prompt = prompt_path.read_text()
    spec = spec_path.read_text()

    result = run_agent(
        system_prompt=system_prompt,
        spec=spec,
        target=args.target.rstrip("/"),
        dump_messages_to=Path(args.dump_messages) if args.dump_messages else None,
        eval_mode=args.eval_mode,
    )

    # Print final verdict in a visible box.
    print()
    print("=" * 72)
    print(f"VERDICT: {result['verdict']}")
    print("=" * 72)
    print(result["reasoning"])
    print()
    print(f"[stats] turns={result['turns']}  tool_calls={result['tool_calls']}  eval_mode={result.get('eval_mode','?')}")
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

    usage = result.get("usage")
    if usage:
        print(
            f"[usage] tokens_in={usage['input_tokens']:,}  "
            f"tokens_out={usage['output_tokens']:,}  "
            f"cost=${usage['cost_usd']:.4f}  "
            f"(pricing {usage['pricing_version']})"
        )

    repro = result.get("repro")
    if repro:
        print(
            f"[repro] git={repro['git_commit']}  "
            f"spec_sha256={repro['spec_sha256'][:12]}…  "
            f"temp={repro['temperature']}"
        )

    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
