"""Lightweight markdown spec parser for ChaosArena two-tier specs.

Extracts:
- Required test categories (R1, R2, ... under `## Required Test Categories`)
- Open Exploration text (free-form, under `## Open Exploration`)
- Out of Scope text (under `## Out of Scope`)

Spec format convention (see specs/album_store_v2.md for reference):

    ## Required Test Categories

    ### R1. <title>
    <body...>

    ### R2. <title>
    <body...>

    ## Open Exploration
    <free-form prose>

    ## Out of Scope
    <free-form prose>

We use only stdlib `re` — no mistune / markdown AST library. Rationale:
the spec format is intentionally simple, regex is transparent and
trivially reproducible for paper artifacts.
"""

import re
from dataclasses import dataclass


@dataclass
class RequiredCategory:
    r_id: str          # e.g. "R1"
    title: str         # title after the period
    body: str          # body text until next ### or ##
    estimated_turns: int = 2  # approximate budget from spec metadata, if present


@dataclass
class ParsedSpec:
    required: list[RequiredCategory]
    open_exploration: str
    out_of_scope: str

    @property
    def required_ids(self) -> list[str]:
        return [r.r_id for r in self.required]


# Match `### R<digits>. <title>` at start of a line.
_R_HEADER_RE = re.compile(r"^###\s+(R\d+)\.\s+(.+?)\s*$", re.MULTILINE)

# Match `## <Section Name>` at start of a line.
_SECTION_RE = re.compile(r"^##\s+([^#].*?)\s*$", re.MULTILINE)
_ESTIMATED_TURNS_RE = re.compile(
    r"^\s*-\s+\*\*Estimated turns\*\*:\s*(\d+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _parse_estimated_turns(body: str, default: int = 2) -> int:
    """Extract `- **Estimated turns**: N` from an R body, clamped defensively."""
    m = _ESTIMATED_TURNS_RE.search(body)
    if not m:
        return default
    try:
        return max(1, min(4, int(m.group(1))))
    except ValueError:
        return default


def _slice_section(text: str, section_name: str) -> str:
    """Return the body of `## <section_name>` until the next `## ...` or EOF."""
    sections = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(sections):
        if m.group(1).strip().lower() == section_name.lower():
            start = m.end()
            end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
            return text[start:end].strip()
    return ""


def parse_spec(markdown: str) -> ParsedSpec:
    """Parse a ChaosArena two-tier markdown spec into a structured form.

    Fault-tolerant: if the spec uses a non-standard format and no Rs are
    found, returns an empty required list rather than raising. The runner
    handles this gracefully — the agent still tracks Rs dynamically through
    submit_verdict_for_R calls; cover_all enforcement is skipped.
    """
    try:
        required_section = _slice_section(markdown, "Required Test Categories")

        required: list[RequiredCategory] = []
        headers = list(_R_HEADER_RE.finditer(required_section))
        for i, m in enumerate(headers):
            r_id = m.group(1)
            title = m.group(2).strip()
            body_start = m.end()
            body_end = headers[i + 1].start() if i + 1 < len(headers) else len(required_section)
            body = required_section[body_start:body_end].strip()
            required.append(
                RequiredCategory(
                    r_id=r_id,
                    title=title,
                    body=body,
                    estimated_turns=_parse_estimated_turns(body),
                )
            )

        if not required:
            import sys
            print(
                "[spec_parser] WARNING: no ### Rn. headers found in spec. "
                "The agent will self-manage R coverage; cover_all pre-enforcement disabled.",
                file=sys.stderr,
            )

        return ParsedSpec(
            required=required,
            open_exploration=_slice_section(markdown, "Open Exploration"),
            out_of_scope=_slice_section(markdown, "Out of Scope"),
        )
    except Exception as exc:
        import sys
        print(f"[spec_parser] WARNING: parse error ({exc}). Proceeding with empty R list.", file=sys.stderr)
        return ParsedSpec(required=[], open_exploration="", out_of_scope="")
