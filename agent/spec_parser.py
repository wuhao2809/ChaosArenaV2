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
    """Parse a ChaosArena two-tier markdown spec into a structured form."""
    required_section = _slice_section(markdown, "Required Test Categories")

    required: list[RequiredCategory] = []
    headers = list(_R_HEADER_RE.finditer(required_section))
    for i, m in enumerate(headers):
        r_id = m.group(1)
        title = m.group(2).strip()
        body_start = m.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(required_section)
        body = required_section[body_start:body_end].strip()
        required.append(RequiredCategory(r_id=r_id, title=title, body=body))

    return ParsedSpec(
        required=required,
        open_exploration=_slice_section(markdown, "Open Exploration"),
        out_of_scope=_slice_section(markdown, "Out of Scope"),
    )
