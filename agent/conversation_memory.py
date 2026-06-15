"""Conversation memory management for ChaosArena runs.

This module keeps two views of the same run:

- full_messages: the complete transcript for debugging and offline analysis
- active_messages: the compact transcript sent back to the model

Completed Required categories are archived as generic R-level summaries so the
runner can trim raw probe history without hard-coding any service semantics.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


@dataclass
class RequiredCategorySpec:
    """Minimal spec metadata needed for memory summaries."""

    r_id: str
    title: str
    body: str


@dataclass
class _TurnSlice:
    """One assistant turn and its matching tool-result message."""

    turn: int
    assistant_message: dict[str, Any]
    tool_result_message: dict[str, Any] | None = None
    text_summary: str = ""
    tool_call_summaries: list[dict[str, Any]] | None = None


class Memory:
    """Maintain active and archived run memory for a single agent execution."""

    def __init__(
        self,
        initial_message: dict[str, Any],
        required_specs: list[RequiredCategorySpec],
        enable_r_context_trimming: bool = True,
    ) -> None:
        self._initial_message = deepcopy(initial_message)
        self._required_specs = {spec.r_id: spec for spec in required_specs}
        self._enable_r_context_trimming = enable_r_context_trimming

        self._full_messages: list[dict[str, Any]] = [deepcopy(initial_message)]
        self._active_messages: list[dict[str, Any]] = [deepcopy(initial_message)]
        self._live_turns: list[_TurnSlice] = []
        self._completed_r_archives: list[dict[str, Any]] = []
        self._pinned_facts: dict[str, dict[str, Any]] = {}
        self._digest_message: dict[str, Any] | None = None
        self._budget_context: dict[str, Any] | None = None

    def get_active_messages(self) -> list[dict[str, Any]]:
        return deepcopy(self._active_messages)

    def get_full_messages(self) -> list[dict[str, Any]]:
        return deepcopy(self._full_messages)

    def metrics(self) -> dict[str, int]:
        active_chars = sum(self._message_chars(msg) for msg in self._active_messages)
        full_chars = sum(self._message_chars(msg) for msg in self._full_messages)
        digest_chars = self._message_chars(self._digest_message) if self._digest_message else 0
        return {
            "active_message_count": len(self._active_messages),
            "full_message_count": len(self._full_messages),
            "completed_r_archive_count": len(self._completed_r_archives),
            "pinned_fact_count": len(self._pinned_facts),
            "active_prompt_chars": active_chars,
            "full_transcript_chars": full_chars,
            "digest_chars": digest_chars,
            "live_turn_count": len(self._live_turns),
            "amended_r_count": sum(1 for a in self._completed_r_archives if a.get("amended")),
        }

    def record_assistant_response(self, content_blocks: list[Any], turn: int) -> None:
        normalized = self._normalize_content_blocks(content_blocks)
        text_summary = " ".join(
            block.get("text", "").strip()
            for block in normalized
            if block.get("type") == "text" and block.get("text", "").strip()
        )
        assistant_message = {"role": "assistant", "content": normalized}
        self._full_messages.append(deepcopy(assistant_message))
        self._active_messages.append(deepcopy(assistant_message))
        self._live_turns.append(
            _TurnSlice(
                turn=turn,
                assistant_message=assistant_message,
                text_summary=text_summary,
                tool_call_summaries=[],
            )
        )

    def record_tool_results(
        self,
        tool_results: list[dict[str, Any]],
        turn: int,
        turn_tool_calls: list[dict[str, Any]],
    ) -> None:
        if not self._live_turns or self._live_turns[-1].turn != turn:
            raise ValueError(f"tool results for turn {turn} do not match active memory state")

        message = {"role": "user", "content": deepcopy(tool_results)}
        self._full_messages.append(deepcopy(message))
        self._active_messages.append(deepcopy(message))
        self._live_turns[-1].tool_result_message = message
        self._live_turns[-1].tool_call_summaries = self._summarize_tool_calls(turn_tool_calls)

    def complete_rs(self, completed_rs: list[tuple[str, dict[str, Any]]]) -> None:
        if not self._enable_r_context_trimming:
            return

        live_turns_snapshot = deepcopy(self._live_turns)
        for r_id, verdict in completed_rs:
            archive = self._build_r_archive(r_id, verdict, live_turns_snapshot)
            existing_index = self._find_archive_index(r_id)
            if existing_index is None:
                self._completed_r_archives.append(archive)
            else:
                previous = self._completed_r_archives[existing_index]
                versions = deepcopy(previous.get("versions", []))
                if not versions:
                    versions.append(self._archive_version(previous))
                versions.append(self._archive_version(archive))
                archive["amended"] = True
                archive["amendment_count"] = len(versions) - 1
                archive["versions"] = versions
                self._completed_r_archives[existing_index] = archive
        self._live_turns = []
        self._rebuild_active_messages()

    def remember_fact(
        self,
        key: str,
        value: Any,
        note: str = "",
        source_r_id: str | None = None,
        turn: int | None = None,
    ) -> dict[str, Any]:
        fact = {
            "key": key,
            "value": deepcopy(value),
            "note": note,
            "source_r_id": source_r_id,
            "turn": turn,
        }
        self._pinned_facts[key] = fact
        self._rebuild_active_messages()
        return deepcopy(fact)

    def update_budget_context(
        self,
        turn: int,
        max_turns: int,
        remaining_r_ids: list[str],
    ) -> None:
        """Refresh the compact runtime budget shown in active memory."""
        self._budget_context = {
            "turn": turn,
            "max_turns": max_turns,
            "remaining_r_ids": list(remaining_r_ids),
        }
        self._rebuild_active_messages()

    def export_state(self) -> dict[str, Any]:
        return {
            "trimming_enabled": self._enable_r_context_trimming,
            "metrics": self.metrics(),
            "completed_r_archives": deepcopy(self._completed_r_archives),
            "pinned_facts": deepcopy(list(self._pinned_facts.values())),
            "budget_context": deepcopy(self._budget_context),
            "active_digest": deepcopy(self._digest_message),
            "live_turns": [self._export_turn_slice(t) for t in self._live_turns],
            "full_message_count": len(self._full_messages),
            "active_message_count": len(self._active_messages),
        }

    def _rebuild_active_messages(self) -> None:
        rebuilt = [deepcopy(self._initial_message)]
        digest = self._build_digest_message()
        if digest is not None:
            rebuilt.append(digest)
        for turn_slice in self._live_turns:
            rebuilt.append(deepcopy(turn_slice.assistant_message))
            if turn_slice.tool_result_message is not None:
                rebuilt.append(deepcopy(turn_slice.tool_result_message))
        self._digest_message = deepcopy(digest)
        self._active_messages = rebuilt

    def _build_digest_message(self) -> dict[str, Any] | None:
        if not self._completed_r_archives and not self._pinned_facts and not self._budget_context:
            return None

        lines = []
        if self._budget_context:
            lines.extend(self._build_budget_digest_lines())

        if self._completed_r_archives or self._pinned_facts:
            if lines:
                lines.append("")
            lines.extend([
                "=== COMPLETED R MEMORY DIGEST ===",
                "Raw history for completed Required categories has been archived outside the active prompt.",
                "Do not retest a completed R unless later evidence contradicts it.",
                "If contradiction appears, resubmit submit_verdict_for_R with updated evidence.",
            ])
        if self._completed_r_archives:
            lines.extend(["", "Completed Required categories:"])
            for archive in self._completed_r_archives:
                amendment = f"; amended {archive['amendment_count']}x" if archive.get("amended") else ""
                lines.append(
                    f"- {archive['r_id']} ({archive['spec_title']}): {archive['verdict']} "
                    f"[{archive['confidence']}{amendment}] — {archive['summary']}"
                )

        if self._pinned_facts:
            lines.extend(["", "Pinned reusable facts:"])
            for fact in self._pinned_facts.values():
                source_prefix = f"{fact['source_r_id']}: " if fact.get("source_r_id") else ""
                note_suffix = f" — {fact['note']}" if fact.get("note") else ""
                lines.append(
                    f"- {source_prefix}{fact['key']} = {self._stringify_value(fact['value'])}{note_suffix}"
                )

        return {
            "role": "user",
            "content": [{"type": "text", "text": "\n".join(lines)}],
        }

    def _build_budget_digest_lines(self) -> list[str]:
        context = self._budget_context or {}
        turn = context.get("turn", "?")
        max_turns = context.get("max_turns", "?")
        remaining = context.get("remaining_r_ids", [])

        lines = [
            "=== TURN PROGRESS ===",
            f"Next turn: {turn}/{max_turns}.",
        ]
        if not remaining:
            lines.append("Remaining Required Rs: none. Submit overall verdict after any brief Open Exploration.")
        else:
            lines.append(f"Remaining Required Rs: {', '.join(remaining)}.")
        return lines

    def _build_r_archive(
        self,
        r_id: str,
        verdict: dict[str, Any],
        turn_slices: list[_TurnSlice],
    ) -> dict[str, Any]:
        spec = self._required_specs.get(r_id)
        tool_names: list[str] = []
        for turn_slice in turn_slices:
            for item in turn_slice.tool_call_summaries or []:
                tool_names.append(item["name"])

        unique_tool_names = list(dict.fromkeys(tool_names))
        summary_parts = []
        if verdict.get("evidence"):
            summary_parts.append(self._truncate_text(str(verdict["evidence"]).strip(), 220))
        if unique_tool_names:
            summary_parts.append(f"tools={', '.join(unique_tool_names)}")

        return {
            "r_id": r_id,
            "spec_title": spec.title if spec else "",
            "spec_body": spec.body if spec else "",
            "verdict": verdict.get("verdict", "UNKNOWN"),
            "confidence": verdict.get("confidence", "UNKNOWN"),
            "evidence": verdict.get("evidence", ""),
            "summary": " | ".join(part for part in summary_parts if part),
            "amended": bool(verdict.get("amended", False)),
            "amendment_count": int(verdict.get("amendment_count", 0) or 0),
            "amendment": deepcopy(verdict.get("amendment")),
            "versions": deepcopy(verdict.get("versions", [])),
            "turns": [self._export_turn_slice(t) for t in turn_slices],
        }

    def _find_archive_index(self, r_id: str) -> int | None:
        for i, archive in enumerate(self._completed_r_archives):
            if archive.get("r_id") == r_id:
                return i
        return None

    @staticmethod
    def _archive_version(archive: dict[str, Any]) -> dict[str, Any]:
        return {
            "verdict": archive.get("verdict", "UNKNOWN"),
            "confidence": archive.get("confidence", "UNKNOWN"),
            "evidence": archive.get("evidence", ""),
            "summary": archive.get("summary", ""),
            "amendment": deepcopy(archive.get("amendment")),
        }

    @staticmethod
    def _normalize_content_blocks(content_blocks: list[Any]) -> list[dict[str, Any]]:
        normalized = []
        for block in content_blocks:
            if hasattr(block, "model_dump"):
                normalized.append(block.model_dump())
            elif isinstance(block, dict):
                normalized.append(deepcopy(block))
            else:
                normalized.append({"raw": str(block)})
        return normalized

    @staticmethod
    def _summarize_tool_calls(turn_tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summaries = []
        for tool_call in turn_tool_calls:
            result_summary = ""
            result = tool_call.get("result")
            if isinstance(result, dict):
                pieces = []
                if "status" in result:
                    pieces.append(f"status={result['status']}")
                if "latency_ms" in result:
                    pieces.append(f"latency_ms={result['latency_ms']}")
                if "status_histogram" in result:
                    pieces.append(f"status_histogram={result['status_histogram']}")
                if "held" in result:
                    pieces.append(f"held={result['held']}")
                monitor = result.get("monitor")
                if isinstance(monitor, dict) and "violations_count" in monitor:
                    pieces.append(f"violations_count={monitor['violations_count']}")
                if "verdict" in result:
                    pieces.append(f"verdict={result['verdict']}")
                result_summary = ", ".join(str(p) for p in pieces)
            elif result is not None:
                result_summary = str(result)

            summaries.append(
                {
                    "name": tool_call.get("name", ""),
                    "args": deepcopy(tool_call.get("args")),
                    "result_summary": result_summary[:240],
                }
            )
        return summaries

    @staticmethod
    def _export_turn_slice(turn_slice: _TurnSlice) -> dict[str, Any]:
        return {
            "turn": turn_slice.turn,
            "assistant_message": deepcopy(turn_slice.assistant_message),
            "tool_result_message": deepcopy(turn_slice.tool_result_message),
            "text_summary": turn_slice.text_summary,
            "tool_call_summaries": deepcopy(turn_slice.tool_call_summaries),
        }

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        return text if len(text) <= limit else text[:limit] + "..."

    @staticmethod
    def _stringify_value(value: Any) -> str:
        if isinstance(value, str):
            return value
        return Memory._truncate_text(str(value), 120)

    @staticmethod
    def _message_chars(message: dict[str, Any] | None) -> int:
        if not message:
            return 0
        content = message.get("content", "")
        if isinstance(content, str):
            return len(content)
        total = 0
        for block in content:
            if isinstance(block, dict):
                for key in ("text", "content"):
                    value = block.get(key)
                    if isinstance(value, str):
                        total += len(value)
        return total
