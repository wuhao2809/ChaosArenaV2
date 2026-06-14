"""Tool implementations for the ChaosArena MVP agent.

Thirteen tools are exposed to Claude:

  - http_call(method, path, body, headers): single stateless HTTP request
  - http_call_with_session(session_id, method, path, body, headers): single
      HTTP request reusing a named session. Cookies, Authorization headers,
      and TCP keep-alive are persisted across calls sharing the same
      session_id. Use for auth flows: POST /login on session "alice" sets a
      cookie; subsequent calls with session_id="alice" carry it.
  - parallel_n(n, method, path, body, headers): N concurrent identical requests,
      returns aggregate stats (status histogram, p50/p95/p99 latency), and
      optionally compact per-response bodies when collect_bodies=true
  - race_pair(action_a, action_b): two DIFFERENT actions released by a shared
      barrier; returns each action's result plus the timing skew between
      release points.
  - barrier_concurrent(actions): N (2-20) different actions all released by
      one barrier.
  - poll_until(request, expect_status, interval_s, timeout_s): repeat a single
      HTTP request every interval_s seconds until the response status matches
      expect_status, or until timeout_s expires. Use for async pipelines like
      "POST returns 202; poll status until completed".
  - assert_for_duration(request, expect_status, duration_s, interval_s):
      repeat a request every interval_s for duration_s seconds; EVERY check
      must satisfy expect_status. Returns first violation timestamp if held
      breaks. Use for invariants like "after DELETE, GET returns 404 for at
      least 15 seconds".
  - monitor_while(monitor_request, foreground_action, expect_status, ...):
      sample a monitor endpoint in the background while a foreground action
      executes, then optionally continue monitoring for a short post-action
      window. Use for transient invariant violations.
  - record_event(event_type, detail): write a forensic event to the audit log
  - set_R_context(r_ids, reason): set the persistent Required-R context that
      subsequent probe calls are charged to until changed.
  - remember_fact(key, value, note): pin a reusable fact into compact memory
  - submit_verdict_for_R(r_id, verdict, confidence, evidence): per-R structured
      verdict. Agent must call this for each Required category in cover_all
      mode before submit_verdict is accepted.
  - submit_verdict(verdict, reasoning): overall verdict; ends the run.

set_R_context, remember_fact, submit_verdict_for_R, and submit_verdict are
handled specially by the runner (see runner.py); they have schema entries here
but no dispatch functions — the runner intercepts them.
"""

import os
import time
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

import requests


# Module-level event log. The runner's run_agent() clears it at the start of each run.
EVENT_LOG: list[dict[str, Any]] = []

# Module-level session pool keyed by user-supplied session_id. The runner's
# run_agent() clears this at the start of each evaluation so sessions never
# bleed between runs. Each Session() instance owns its own cookie jar +
# connection pool. Operations on the pool itself are guarded by `_session_lock`
# in case parallel_n / barrier_concurrent ever touch sessions concurrently in
# the future.
SESSIONS: dict[str, requests.Session] = {}
_session_lock = threading.Lock()


def _get_or_create_session(session_id: str) -> requests.Session:
    """Return the Session for `session_id`, creating it if needed."""
    with _session_lock:
        sess = SESSIONS.get(session_id)
        if sess is None:
            sess = requests.Session()
            SESSIONS[session_id] = sess
        return sess


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "http_call",
        "description": (
            "Make a single HTTP request. By default targets the service under "
            "test via `path`. To fetch an external URL (e.g. a pre-signed S3 "
            "URL returned in a response body), pass the full URL in the `url` "
            "field instead of `path`. Returns status code, response body, and "
            "latency in milliseconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                    "description": "HTTP method.",
                },
                "path": {
                    "type": ["string", "null"],
                    "description": (
                        "URL path relative to the target base URL, starting with /. "
                        "Example: '/health' or '/albums/abc123'. "
                        "Omit when using `url` for an external request."
                    ),
                },
                "url": {
                    "type": ["string", "null"],
                    "description": (
                        "Full absolute URL for requests outside the target service. "
                        "Use this to verify external resources like pre-signed S3 URLs "
                        "returned in response bodies. When set, `path` is ignored."
                    ),
                },
                "body": {
                    "type": ["object", "null"],
                    "description": (
                        "Optional JSON body for POST/PUT/PATCH requests. "
                        "Omit or set to null for GET/DELETE."
                    ),
                },
                "headers": {
                    "type": ["object", "null"],
                    "description": "Optional extra HTTP headers as key-value pairs.",
                },
                "multipart": {
                    "type": ["object", "null"],
                    "description": (
                        "If set, sends as multipart/form-data instead of JSON. "
                        "Use for file upload endpoints (e.g. POST /albums/:id/photos). "
                        "The tool generates synthetic file bytes — no real file needed."
                    ),
                    "properties": {
                        "field": {
                            "type": "string",
                            "description": "Form field name for the file, e.g. 'file' or 'photo'.",
                        },
                        "filename": {
                            "type": "string",
                            "description": "Filename to send, e.g. 'photo.jpg'.",
                        },
                        "content_type": {
                            "type": "string",
                            "description": (
                                "MIME type of the file. "
                                "Use 'image/jpeg' for a normal photo, "
                                "'text/plain' or 'application/octet-stream' for non-image tests."
                            ),
                        },
                        "size_bytes": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Number of random bytes to generate as file content. "
                                "Use 0 for empty-file test, "
                                "1048577 for oversize test (1 MB + 1 byte), "
                                "1024 for a typical small upload."
                            ),
                        },
                    },
                    "required": ["field"],
                },
            },
            "required": ["method"],
        },
    },
    {
        "name": "http_call_with_session",
        "description": (
            "Make a single HTTP request reusing a NAMED session. Cookies set "
            "by the server (e.g. via Set-Cookie on a /login response) are "
            "automatically persisted and replayed on subsequent calls with "
            "the same session_id. The Authorization header you set on one "
            "call IS NOT auto-replayed — pass it again if needed; only "
            "cookies are sticky. TCP keep-alive is also reused per session. "
            "Use this for auth flows and any scenario where state must "
            "persist across requests from the SAME logical user.\n\n"
            "Common patterns:\n"
            "  • POST /auth/login on session 'alice' (server returns "
            "Set-Cookie); then GET /me on session 'alice' (cookie carried).\n"
            "  • Two sessions 'alice' and 'bob' both log in; GET /users/alice "
            "as bob should return 4xx (cross-user authorization boundary).\n"
            "Returns status code, response body, and latency in milliseconds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": (
                        "Logical session identifier you choose. Reusing the "
                        "same id reuses the same cookie jar + connection. "
                        "Examples: 'alice', 'bob', 'admin'."
                    ),
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                    "description": "HTTP method.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "URL path relative to the target base URL, starting with /. "
                        "Example: '/auth/login' or '/me'."
                    ),
                },
                "body": {
                    "type": ["object", "null"],
                    "description": (
                        "Optional JSON body for POST/PUT/PATCH. "
                        "Omit or set to null for GET/DELETE."
                    ),
                },
                "headers": {
                    "type": ["object", "null"],
                    "description": (
                        "Optional extra HTTP headers as key-value pairs. "
                        "Cookies set by the server are auto-managed; "
                        "Authorization headers are NOT auto-replayed."
                    ),
                },
                "multipart": {
                    "type": ["object", "null"],
                    "description": "Same as http_call multipart — sends multipart/form-data instead of JSON.",
                    "properties": {
                        "field": {"type": "string"},
                        "filename": {"type": "string"},
                        "content_type": {"type": "string"},
                        "size_bytes": {"type": "integer", "minimum": 0},
                    },
                    "required": ["field"],
                },
            },
            "required": ["session_id", "method", "path"],
        },
    },
    {
        "name": "parallel_n",
        "description": (
            "Fire N identical HTTP requests CONCURRENTLY against the target service. "
            "Returns aggregate statistics: per-status-code histogram, count of non-2xx "
            "responses, latency percentiles (p50/p95/p99), and elapsed wall time. "
            "Set collect_bodies=true when the response payload itself is the evidence "
            "(for example concurrent photo uploads that must return unique photo_id/seq). "
            "Use this to test concurrency / race / load behavior — any 5xx under "
            "concurrent load typically indicates a thread-safety bug in the target."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "n": {
                    "type": "integer",
                    "minimum": 2,
                    "maximum": 100,
                    "description": "Number of concurrent requests (2-100).",
                },
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                },
                "path": {
                    "type": "string",
                    "description": "URL path starting with /. Same path for all N requests.",
                },
                "body": {
                    "type": ["object", "null"],
                    "description": "Optional JSON body, same for all N requests.",
                },
                "headers": {
                    "type": ["object", "null"],
                    "description": "Optional extra headers, same for all N requests.",
                },
                "multipart": {
                    "type": ["object", "null"],
                    "description": "Same as http_call multipart — sends multipart/form-data for all N concurrent requests.",
                    "properties": {
                        "field": {"type": "string"},
                        "filename": {"type": "string"},
                        "content_type": {"type": "string"},
                        "size_bytes": {"type": "integer", "minimum": 0},
                    },
                    "required": ["field"],
                },
                "collect_bodies": {
                    "type": "boolean",
                    "description": (
                        "If true, include compact per-response bodies/statuses in the result. "
                        "Use only when response fields such as ids or sequence numbers are required evidence."
                    ),
                },
                "max_bodies": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum number of response bodies to return when collect_bodies=true. Default 20.",
                },
            },
            "required": ["n", "method", "path"],
        },
    },
    {
        "name": "race_pair",
        "description": (
            "Issue TWO different HTTP requests CONCURRENTLY against the target, "
            "released by a shared thread barrier. Use to test races between "
            "heterogeneous operations — e.g., concurrent DELETE vs UPDATE on the "
            "same resource, or two different writes against the same album_id. "
            "Returns each action's status/body/latency plus the timing skew "
            "between barrier releases (in microseconds). Differs from parallel_n "
            "in that the two actions can be different (different method/path/body)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action_a": {
                    "type": "object",
                    "description": "First action.",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                        "path": {"type": "string"},
                        "body": {"type": ["object", "null"]},
                        "headers": {"type": ["object", "null"]},
                        "multipart": {
                            "type": ["object", "null"],
                            "description": "Same as http_call multipart — sends multipart/form-data for this action.",
                            "properties": {
                                "field": {"type": "string"},
                                "filename": {"type": "string"},
                                "content_type": {"type": "string"},
                                "size_bytes": {"type": "integer", "minimum": 0},
                            },
                            "required": ["field"],
                        },
                    },
                    "required": ["method", "path"],
                },
                "action_b": {
                    "type": "object",
                    "description": "Second action.",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                        "path": {"type": "string"},
                        "body": {"type": ["object", "null"]},
                        "headers": {"type": ["object", "null"]},
                        "multipart": {
                            "type": ["object", "null"],
                            "description": "Same as http_call multipart — sends multipart/form-data for this action.",
                            "properties": {
                                "field": {"type": "string"},
                                "filename": {"type": "string"},
                                "content_type": {"type": "string"},
                                "size_bytes": {"type": "integer", "minimum": 0},
                            },
                            "required": ["field"],
                        },
                    },
                    "required": ["method", "path"],
                },
            },
            "required": ["action_a", "action_b"],
        },
    },
    {
        "name": "barrier_concurrent",
        "description": (
            "Issue N (2-20) DIFFERENT HTTP requests CONCURRENTLY, all released by "
            "one shared thread barrier. Generalization of race_pair. Each action "
            "may have a different method, path, body, headers. Returns each "
            "action's result plus barrier-release skew statistics. Use for "
            "multi-way races where 2 actions are not enough — e.g., 5 distinct "
            "writes against the same resource simultaneously."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 20,
                    "items": {
                        "type": "object",
                        "properties": {
                            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                            "path": {"type": "string"},
                            "body": {"type": ["object", "null"]},
                            "headers": {"type": ["object", "null"]},
                            "multipart": {
                                "type": ["object", "null"],
                                "description": "Same as http_call multipart — sends multipart/form-data for this action.",
                                "properties": {
                                    "field": {"type": "string"},
                                    "filename": {"type": "string"},
                                    "content_type": {"type": "string"},
                                    "size_bytes": {"type": "integer", "minimum": 0},
                                },
                                "required": ["field"],
                            },
                        },
                        "required": ["method", "path"],
                    },
                    "description": "List of 2-20 distinct actions to fire concurrently.",
                },
            },
            "required": ["actions"],
        },
    },
    {
        "name": "poll_until",
        "description": (
            "Poll a single HTTP endpoint every `interval_s` seconds until the "
            "response status matches one of `expect_status`, OR until "
            "`timeout_s` elapses. Use for async / eventual-consistency "
            "scenarios — e.g., POST a photo (returns 202 processing), then "
            "poll_until expect_status=[200] interval_s=1 timeout_s=30 to wait "
            "for completion. Returns whether the condition was met, elapsed "
            "time, attempt count, and the final response. ONE tool call "
            "internally executes many HTTP requests — does not consume "
            "additional turns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "object",
                    "description": "The HTTP request to repeat.",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                        "path": {"type": "string"},
                        "body": {"type": ["object", "null"]},
                        "headers": {"type": ["object", "null"]},
                    },
                    "required": ["method", "path"],
                },
                "expect_status": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "description": "Status code(s) that satisfy the condition. Polling stops when any matches.",
                },
                "match_body_substring": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional. If provided, the response body (stringified) must also contain this substring "
                        "for the condition to be considered met."
                    ),
                },
                "interval_s": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 10,
                    "description": "Seconds between polls. Default 1.0.",
                },
                "timeout_s": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Max seconds to keep polling. Default 30.",
                },
            },
            "required": ["request", "expect_status"],
        },
    },
    {
        "name": "assert_for_duration",
        "description": (
            "Poll a single HTTP endpoint every `interval_s` seconds for "
            "`duration_s` seconds. EVERY check must satisfy `expect_status` — "
            "the first violation is reported. Use for invariants that must "
            "hold continuously — e.g., 'after DELETE, GET must return 404 for "
            "at least 15 seconds'. Returns whether the invariant held, total "
            "checks performed, and (if violated) the violation timestamp + "
            "first-bad response. ONE tool call internally executes many HTTP "
            "requests — does not consume additional turns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "object",
                    "description": "The HTTP request to repeat.",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                        "path": {"type": "string"},
                        "body": {"type": ["object", "null"]},
                        "headers": {"type": ["object", "null"]},
                    },
                    "required": ["method", "path"],
                },
                "expect_status": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "description": "Status code(s) that satisfy the invariant on every check.",
                },
                "duration_s": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Total seconds to keep checking.",
                },
                "interval_s": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 10,
                    "description": "Seconds between checks. Default 1.0.",
                },
            },
            "required": ["request", "expect_status", "duration_s"],
        },
    },
    {
        "name": "monitor_while",
        "description": (
            "Start a background monitor loop, execute one foreground HTTP action, "
            "then optionally keep monitoring briefly after the action completes. "
            "Use this for transient invariants that may be violated only during "
            "another operation, e.g. 'resource must stay 404 while a background "
            "worker races with DELETE'. Returns foreground_result, monitor check "
            "counts, first violations, and a compact timeline. This is stronger "
            "than sequential probing because the monitor runs concurrently with "
            "the foreground action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "monitor_request": {
                    "type": "object",
                    "description": "HTTP request to sample repeatedly in the background.",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                        "path": {"type": "string"},
                        "body": {"type": ["object", "null"]},
                        "headers": {"type": ["object", "null"]},
                    },
                    "required": ["method", "path"],
                },
                "foreground_action": {
                    "type": "object",
                    "description": "Single HTTP action to execute while the monitor is running.",
                    "properties": {
                        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
                        "path": {"type": "string"},
                        "body": {"type": ["object", "null"]},
                        "headers": {"type": ["object", "null"]},
                    },
                    "required": ["method", "path"],
                },
                "expect_status": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "description": "Monitor response status code(s) that are allowed on every check.",
                },
                "match_body_substring": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional. If provided, the monitor body must contain this substring on every check."
                    ),
                },
                "interval_s": {
                    "type": "number",
                    "minimum": 0.05,
                    "maximum": 5,
                    "description": "Seconds between monitor checks. Default 0.25.",
                },
                "post_action_s": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 30,
                    "description": "Seconds to keep monitoring after foreground_action returns. Default 2.",
                },
                "timeout_s": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Hard timeout for the full monitor_while call. Default 30.",
                },
            },
            "required": ["monitor_request", "foreground_action", "expect_status"],
        },
    },
    {
        "name": "record_event",
        "description": (
            "Record a forensic event in the run's audit log. Use this to note "
            "observations, warnings, or violations as you go — they become part "
            "of the final report's evidence trail. Does not affect tool behavior."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "enum": ["OBSERVATION", "WARNING", "VIOLATION", "NOTE"],
                    "description": "Category of event.",
                },
                "detail": {
                    "type": "string",
                    "description": "Free-form description (be specific about what was observed).",
                },
            },
            "required": ["event_type", "detail"],
        },
    },
    {
        "name": "set_R_context",
        "description": (
            "Set the persistent Required-R context for subsequent probe tools. "
            "Use this before probe calls after the first discovery turn. Budget "
            "for each probe turn is charged fractionally across the active r_ids "
            "until this context is changed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "r_ids": {
                    "type": "array",
                    "items": {"type": "string", "pattern": r"^R\d+$"},
                    "minItems": 1,
                    "maxItems": 6,
                    "description": "Required R ids the next probe calls support, e.g. ['R5'] or ['R5','R8'].",
                },
                "reason": {
                    "type": "string",
                    "description": "One concise sentence explaining what this R context is testing.",
                },
            },
            "required": ["r_ids", "reason"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Pin a small reusable fact into compact run memory. Use this when "
            "you create an ID, URL, session name, or other value that future "
            "Rs may need after earlier raw tool history is trimmed away. Keep "
            "facts short and generic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Short stable fact name, e.g. 'album_a_id', 'alice_session', 'photo_url'.",
                },
                "value": {
                    "type": ["string", "number", "integer", "boolean", "object", "array", "null"],
                    "description": "The value to preserve. Prefer short strings or small JSON values.",
                },
                "note": {
                    "type": ["string", "null"],
                    "description": "Optional short note about why this fact matters later.",
                },
                "source_r_id": {
                    "type": ["string", "null"],
                    "pattern": r"^R\d+$",
                    "description": "Optional R id associated with this fact, if known.",
                },
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "submit_verdict_for_R",
        "description": (
            "Record a per-R-category verdict. Call this once per Required test "
            "category (R1, R2, ...) as soon as you have decisive evidence for "
            "that category — do not wait until the end. If later evidence "
            "contradicts an earlier verdict, call this again for the same R; "
            "the runner records an amendment history and the latest verdict "
            "is used in the final report. The runner tracks which Rs have "
            "been recorded and returns the remaining set in the tool result "
            "so you can manage your turn budget."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "r_id": {
                    "type": "string",
                    "pattern": r"^R\d+$",
                    "description": "R category id, e.g. 'R1', 'R2'.",
                },
                "verdict": {
                    "type": "string",
                    "enum": ["PASSED", "FAILED", "UNTESTABLE"],
                    "description": "Outcome for this R category.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["HIGH", "MEDIUM", "LOW"],
                    "description": "How confident you are in this verdict.",
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Concise factual statement of what was observed: "
                        "status codes, response excerpts, latencies, "
                        "concurrency results. Be specific."
                    ),
                },
            },
            "required": ["r_id", "verdict", "confidence", "evidence"],
        },
    },
    {
        "name": "submit_verdict",
        "description": (
            "Submit the OVERALL verdict and end the run. In fail_fast mode, "
            "may be called at any time once you have enough evidence (e.g., "
            "any critical R FAILED). In cover_all mode, all Required Rs must "
            "have a per-R verdict recorded before this call is accepted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["PASS", "FAIL"],
                    "description": "Overall verdict.",
                },
                "reasoning": {
                    "type": "string",
                    "description": (
                        "Structured per-criterion analysis. For each acceptance "
                        "criterion in the spec, state whether it PASSED, FAILED, or "
                        "was UNTESTABLE with the available tools, and the specific "
                        "evidence observed (status codes, response bodies, latencies)."
                    ),
                },
            },
            "required": ["verdict", "reasoning"],
        },
    },
]


def _build_multipart(multipart: dict) -> tuple[dict, int]:
    """Build a requests `files` dict from a multipart spec.

    Returns (files_dict, size_bytes) where files_dict is passed to
    requests.request(files=...). The tool generates synthetic random bytes —
    no real file is required. This is sufficient for testing upload endpoints
    that accept any binary payload.
    """
    field = multipart.get("field", "file")
    filename = multipart.get("filename", "upload.bin")
    content_type = multipart.get("content_type", "application/octet-stream")
    size_bytes = int(multipart.get("size_bytes", 1024))
    data = os.urandom(size_bytes) if size_bytes > 0 else b""
    return {field: (filename, data, content_type)}, size_bytes


def http_call(
    method: str,
    path: str | None,
    target: str,
    body: dict | None = None,
    headers: dict | None = None,
    multipart: dict | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """Execute a single HTTP request. Uses `url` if provided, else builds from target + path."""
    if url:
        final_url = url
    else:
        p = path or "/"
        if not p.startswith("/"):
            p = "/" + p
        final_url = target.rstrip("/") + p

    # Multipart uploads need more time (especially oversize tests).
    timeout = 30 if multipart else 10

    start = time.perf_counter()
    try:
        if multipart:
            files, size_bytes = _build_multipart(multipart)
            response = requests.request(
                method=method.upper(),
                url=final_url,
                files=files,
                headers=headers or {},
                timeout=timeout,
            )
        else:
            response = requests.request(
                method=method.upper(),
                url=final_url,
                json=body,
                headers=headers or {},
                timeout=timeout,
            )
        latency_ms = int((time.perf_counter() - start) * 1000)

        try:
            body_data: Any = response.json()
        except ValueError:
            body_data = response.text

        return {
            "status": response.status_code,
            "body": body_data,
            "latency_ms": latency_ms,
        }
    except requests.exceptions.RequestException as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": latency_ms,
        }


def http_call_with_session(
    session_id: str,
    method: str,
    path: str,
    target: str,
    body: dict | None = None,
    headers: dict | None = None,
    multipart: dict | None = None,
) -> dict[str, Any]:
    """Execute one HTTP request through a named persistent session.

    Cookies set by the server are automatically persisted in the session's
    cookie jar and re-sent on subsequent calls with the same session_id.
    """
    if not path.startswith("/"):
        path = "/" + path
    url = target.rstrip("/") + path

    sess = _get_or_create_session(session_id)
    timeout = 30 if multipart else 10

    start = time.perf_counter()
    try:
        if multipart:
            files, _ = _build_multipart(multipart)
            response = sess.request(
                method=method.upper(),
                url=url,
                files=files,
                headers=headers or {},
                timeout=timeout,
            )
        else:
            response = sess.request(
                method=method.upper(),
                url=url,
                json=body,
                headers=headers or {},
                timeout=timeout,
            )
        latency_ms = int((time.perf_counter() - start) * 1000)

        try:
            body_data: Any = response.json()
        except ValueError:
            body_data = response.text

        # Surface cookies on this response so the agent can see auth was
        # established. This list is only those set on THIS response, not the
        # full jar. Useful for verifying "did /login actually set a cookie".
        cookies_set = [
            {"name": c.name, "domain": c.domain or "", "path": c.path or "/"}
            for c in response.cookies
        ]

        return {
            "status": response.status_code,
            "body": body_data,
            "latency_ms": latency_ms,
            "session_id": session_id,
            "cookies_set_by_response": cookies_set,
        }
    except requests.exceptions.RequestException as exc:
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "status": None,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": latency_ms,
            "session_id": session_id,
        }


def _percentile(sorted_values: list[float], p: float) -> float:
    """Return the p-th percentile (0-100) of a pre-sorted list."""
    if not sorted_values:
        return 0
    idx = max(0, min(len(sorted_values) - 1, int(len(sorted_values) * p / 100)))
    return sorted_values[idx]


def _compact_response_body(body: Any, max_text_chars: int = 1000) -> Any:
    """Return a bounded response body suitable for concurrent result summaries."""
    if isinstance(body, str):
        if len(body) > max_text_chars:
            return body[:max_text_chars] + f"... [truncated, {len(body)} chars total]"
        return body
    if isinstance(body, list):
        compact = [_compact_response_body(item, max_text_chars=max_text_chars) for item in body[:20]]
        if len(body) > 20:
            compact.append({"_truncated_items": len(body) - 20})
        return compact
    if isinstance(body, dict):
        compact: dict[str, Any] = {}
        for key, value in list(body.items())[:30]:
            compact[str(key)] = _compact_response_body(value, max_text_chars=max_text_chars)
        if len(body) > 30:
            compact["_truncated_keys"] = len(body) - 30
        return compact
    return body


def parallel_n(
    n: int,
    method: str,
    path: str,
    target: str,
    body: dict | None = None,
    headers: dict | None = None,
    multipart: dict | None = None,
    collect_bodies: bool = False,
    max_bodies: int = 20,
) -> dict[str, Any]:
    """Fire N identical requests concurrently against the target."""
    statuses: list[int] = []
    latencies: list[int] = []
    network_errors: list[str] = []
    response_samples: list[dict[str, Any]] = []

    overall_start = time.perf_counter()
    # Cap thread count at min(n, 50) — 50 is enough concurrency for our scale,
    # avoids exhausting fd / connection limits on slow targets.
    max_workers = min(n, 50)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(http_call, method, path, target, body, headers, multipart): idx
            for idx in range(n)
        }
        for f in as_completed(futures):
            idx = futures[f]
            result = f.result()
            if result.get("status") is None:
                network_errors.append(result.get("error", "unknown error"))
                if collect_bodies and len(response_samples) < max_bodies:
                    response_samples.append(
                        {
                            "request_index": idx,
                            "status": None,
                            "error": result.get("error"),
                            "latency_ms": result.get("latency_ms"),
                        }
                    )
            else:
                statuses.append(result["status"])
                latencies.append(result["latency_ms"])
                if collect_bodies and len(response_samples) < max_bodies:
                    response_samples.append(
                        {
                            "request_index": idx,
                            "status": result["status"],
                            "body": _compact_response_body(result.get("body")),
                            "latency_ms": result["latency_ms"],
                        }
                    )
    elapsed_ms = int((time.perf_counter() - overall_start) * 1000)

    latencies.sort()
    by_status = {str(code): count for code, count in Counter(statuses).items()}
    non_2xx_count = sum(1 for s in statuses if not (200 <= s < 300))

    output = {
        "n_requested": n,
        "completed": len(statuses),
        "by_status": by_status,
        "non_2xx_count": non_2xx_count,
        "p50_ms": _percentile(latencies, 50),
        "p95_ms": _percentile(latencies, 95),
        "p99_ms": _percentile(latencies, 99),
        "elapsed_ms": elapsed_ms,
        "network_errors": network_errors[:3],
    }
    if collect_bodies:
        response_samples.sort(key=lambda item: item.get("request_index", 0))
        output["responses"] = response_samples
        output["responses_truncated"] = max(0, n - len(response_samples))
    return output


def _execute_action_at_barrier(
    barrier: threading.Barrier,
    action: dict,
    target: str,
) -> dict[str, Any]:
    """Wait at the barrier, then execute the action and record release time.

    Returns the http_call result enriched with `release_ts_ns` (the moment
    the barrier released this thread, in nanoseconds since an arbitrary
    epoch — only useful for relative comparison).
    """
    barrier.wait()
    release_ts_ns = time.perf_counter_ns()
    result = http_call(
        method=action["method"],
        path=action["path"],
        target=target,
        body=action.get("body"),
        headers=action.get("headers"),
        multipart=action.get("multipart"),
    )
    result["release_ts_ns"] = release_ts_ns
    return result


def race_pair(
    action_a: dict,
    action_b: dict,
    target: str,
) -> dict[str, Any]:
    """Fire two different actions concurrently, released by a shared barrier."""
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_a = executor.submit(_execute_action_at_barrier, barrier, action_a, target)
        future_b = executor.submit(_execute_action_at_barrier, barrier, action_b, target)
        result_a = future_a.result()
        result_b = future_b.result()

    skew_us = abs(result_a["release_ts_ns"] - result_b["release_ts_ns"]) // 1000
    # Strip the internal release timestamp from the agent-visible payload.
    a_view = {k: v for k, v in result_a.items() if k != "release_ts_ns"}
    b_view = {k: v for k, v in result_b.items() if k != "release_ts_ns"}

    return {
        "action_a": {
            "method": action_a["method"],
            "path": action_a["path"],
            "result": a_view,
        },
        "action_b": {
            "method": action_b["method"],
            "path": action_b["path"],
            "result": b_view,
        },
        "release_skew_us": skew_us,
    }


def barrier_concurrent(
    actions: list[dict],
    target: str,
) -> dict[str, Any]:
    """Fire N (2-20) different actions concurrently, all released by one barrier."""
    n = len(actions)
    if n < 2 or n > 20:
        return {"error": f"actions list must have 2-20 entries, got {n}"}

    barrier = threading.Barrier(n)
    results: list[dict] = [None] * n  # type: ignore[list-item]

    def run_one(i: int) -> None:
        results[i] = _execute_action_at_barrier(barrier, actions[i], target)

    with ThreadPoolExecutor(max_workers=n) as executor:
        for i in range(n):
            executor.submit(run_one, i)
        # Wait for all threads to finish by exiting the with block.

    release_times = [r["release_ts_ns"] for r in results]
    skew_us = (max(release_times) - min(release_times)) // 1000

    statuses = [r.get("status") for r in results if r.get("status") is not None]
    by_status = {str(code): count for code, count in Counter(statuses).items()}

    enriched = []
    for i, r in enumerate(results):
        view = {k: v for k, v in r.items() if k != "release_ts_ns"}
        enriched.append({
            "method": actions[i]["method"],
            "path": actions[i]["path"],
            "result": view,
        })

    return {
        "n_actions": n,
        "by_status": by_status,
        "max_release_skew_us": skew_us,
        "results": enriched,
    }


def _do_request(request: dict, target: str) -> dict[str, Any]:
    """Execute a single request from a {method, path, body?, headers?} dict."""
    return http_call(
        method=request["method"],
        path=request["path"],
        target=target,
        body=request.get("body"),
        headers=request.get("headers"),
    )


def poll_until(
    request: dict,
    expect_status: list[int],
    target: str,
    match_body_substring: str | None = None,
    interval_s: float = 1.0,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Repeat a request until status (and optional substring) match, or timeout."""
    start = time.perf_counter()
    deadline = start + timeout_s
    expect_set = set(expect_status)
    attempts = 0
    last_result: dict[str, Any] = {}

    while True:
        attempts += 1
        last_result = _do_request(request, target)
        status = last_result.get("status")

        body_ok = True
        if match_body_substring is not None:
            body_str = str(last_result.get("body", ""))
            body_ok = match_body_substring in body_str

        if status in expect_set and body_ok:
            return {
                "matched": True,
                "elapsed_ms": int((time.perf_counter() - start) * 1000),
                "attempts": attempts,
                "last_status": status,
                "last_body": last_result.get("body"),
            }

        if time.perf_counter() >= deadline:
            return {
                "matched": False,
                "timeout": True,
                "elapsed_ms": int((time.perf_counter() - start) * 1000),
                "attempts": attempts,
                "last_status": status,
                "last_body": last_result.get("body"),
            }

        time.sleep(interval_s)


def assert_for_duration(
    request: dict,
    expect_status: list[int],
    duration_s: float,
    target: str,
    interval_s: float = 1.0,
) -> dict[str, Any]:
    """Repeat a request for duration_s; every check must match expect_status.

    Reports the first violation if any check fails. Returns a structured
    result with `held: bool`, total check count, elapsed time, and (when
    violated) the violating attempt's status + body.
    """
    start = time.perf_counter()
    deadline = start + duration_s
    expect_set = set(expect_status)
    checks = 0
    violation: dict[str, Any] | None = None

    while time.perf_counter() < deadline:
        checks += 1
        result = _do_request(request, target)
        status = result.get("status")
        if status not in expect_set and violation is None:
            violation = {
                "at_check": checks,
                "at_elapsed_ms": int((time.perf_counter() - start) * 1000),
                "observed_status": status,
                "observed_body": result.get("body"),
            }
            # Continue checking — caller may want to know whether violations
            # are sustained or transient.
        # Sleep but not past the deadline.
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        time.sleep(min(interval_s, remaining))

    return {
        "held": violation is None,
        "total_checks": checks,
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
        "violation": violation,
    }


def _monitor_check_ok(
    result: dict[str, Any],
    expect_set: set[int],
    match_body_substring: str | None,
) -> tuple[bool, str | None]:
    status = result.get("status")
    if status not in expect_set:
        return False, f"status {status} not in expected {sorted(expect_set)}"
    if match_body_substring is not None:
        body_str = str(result.get("body", ""))
        if match_body_substring not in body_str:
            return False, f"body missing substring {match_body_substring!r}"
    return True, None


def monitor_while(
    monitor_request: dict,
    foreground_action: dict,
    expect_status: list[int],
    target: str,
    match_body_substring: str | None = None,
    interval_s: float = 0.25,
    post_action_s: float = 2.0,
    timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Monitor one request concurrently while a foreground action executes."""
    interval_s = max(0.05, min(float(interval_s), 5.0))
    post_action_s = max(0.0, min(float(post_action_s), 30.0))
    timeout_s = max(1.0, min(float(timeout_s), 60.0))

    start = time.perf_counter()
    deadline = start + timeout_s
    expect_set = set(expect_status)
    stop_event = threading.Event()
    samples: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    total_checks = 0

    def monitor_loop() -> None:
        nonlocal total_checks
        check = 0
        while not stop_event.is_set() and time.perf_counter() < deadline:
            check += 1
            total_checks = check
            result = _do_request(monitor_request, target)
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            ok, reason = _monitor_check_ok(result, expect_set, match_body_substring)
            sample = {
                "check": check,
                "elapsed_ms": elapsed_ms,
                "status": result.get("status"),
                "ok": ok,
            }
            if len(samples) < 20:
                samples.append(sample)
            if not ok and len(violations) < 10:
                violations.append({
                    **sample,
                    "reason": reason,
                    "body": result.get("body"),
                })

            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            stop_event.wait(min(interval_s, remaining))

    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()

    # Give the monitor a chance to take an initial sample before perturbing the system.
    time.sleep(min(interval_s, 0.1))
    foreground_result = _do_request(foreground_action, target)
    foreground_elapsed_ms = int((time.perf_counter() - start) * 1000)

    post_deadline = min(deadline, time.perf_counter() + post_action_s)
    while time.perf_counter() < post_deadline:
        time.sleep(min(0.05, post_deadline - time.perf_counter()))

    stop_event.set()
    thread.join(timeout=1.0)

    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return {
        "held": not violations,
        "elapsed_ms": elapsed_ms,
        "foreground_elapsed_ms": foreground_elapsed_ms,
        "foreground_result": foreground_result,
        "monitor": {
            "request": monitor_request,
            "expect_status": sorted(expect_set),
            "match_body_substring": match_body_substring,
            "interval_s": interval_s,
            "post_action_s": post_action_s,
            "checks": total_checks,
            "violations_count": len(violations),
            "first_violation": violations[0] if violations else None,
            "timeline": samples,
            "violations": violations,
        },
        "timed_out": time.perf_counter() >= deadline,
    }


def record_event(event_type: str, detail: str) -> dict[str, Any]:
    """Append a forensic event to the run's audit log."""
    event = {
        "event_type": event_type,
        "detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    EVENT_LOG.append(event)
    return {"recorded": True, "event_id": len(EVENT_LOG)}


def dispatch_tool(name: str, input_args: dict, target: str) -> dict[str, Any]:
    """Route a tool call to its implementation. Memory/verdict tools are handled by runner."""
    if name == "http_call":
        return http_call(
            method=input_args["method"],
            path=input_args.get("path"),
            target=target,
            body=input_args.get("body"),
            headers=input_args.get("headers"),
            multipart=input_args.get("multipart"),
            url=input_args.get("url"),
        )
    if name == "http_call_with_session":
        return http_call_with_session(
            session_id=input_args["session_id"],
            method=input_args["method"],
            path=input_args["path"],
            target=target,
            body=input_args.get("body"),
            headers=input_args.get("headers"),
            multipart=input_args.get("multipart"),
        )
    if name == "parallel_n":
        return parallel_n(
            n=input_args["n"],
            method=input_args["method"],
            path=input_args["path"],
            target=target,
            body=input_args.get("body"),
            headers=input_args.get("headers"),
            multipart=input_args.get("multipart"),
            collect_bodies=bool(input_args.get("collect_bodies", False)),
            max_bodies=int(input_args.get("max_bodies", 20)),
        )
    if name == "race_pair":
        return race_pair(
            action_a=input_args["action_a"],
            action_b=input_args["action_b"],
            target=target,
        )
    if name == "barrier_concurrent":
        return barrier_concurrent(
            actions=input_args["actions"],
            target=target,
        )
    if name == "poll_until":
        return poll_until(
            request=input_args["request"],
            expect_status=input_args["expect_status"],
            target=target,
            match_body_substring=input_args.get("match_body_substring"),
            interval_s=input_args.get("interval_s", 1.0),
            timeout_s=input_args.get("timeout_s", 30.0),
        )
    if name == "assert_for_duration":
        return assert_for_duration(
            request=input_args["request"],
            expect_status=input_args["expect_status"],
            duration_s=input_args["duration_s"],
            target=target,
            interval_s=input_args.get("interval_s", 1.0),
        )
    if name == "monitor_while":
        return monitor_while(
            monitor_request=input_args["monitor_request"],
            foreground_action=input_args["foreground_action"],
            expect_status=input_args["expect_status"],
            target=target,
            match_body_substring=input_args.get("match_body_substring"),
            interval_s=input_args.get("interval_s", 0.25),
            post_action_s=input_args.get("post_action_s", 2.0),
            timeout_s=input_args.get("timeout_s", 30.0),
        )
    if name == "record_event":
        return record_event(
            event_type=input_args["event_type"],
            detail=input_args["detail"],
        )
    raise ValueError(f"Unknown tool: {name}")
