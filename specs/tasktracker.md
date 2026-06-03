# TaskTracker — System Spec (MVP Demo Target)

## Description

A minimal task-tracking REST API. Users create tasks, list them, fetch
them by ID. This spec describes what the deployed service must satisfy
for a partner-quality release.

## Endpoints

| Method | Path | Body | Success response |
|---|---|---|---|
| `GET` | `/health` | — | `200` with `{"status":"ok"}` |
| `POST` | `/tasks` | `{"title": "<string>"}` | `201` with `{"task_id":"<string>","title":"<string>"}` |
| `GET` | `/tasks/<task_id>` | — | `200` with the task object, or `404` if not found |
| `GET` | `/tasks` | — | `200` with a JSON array of task objects |

## Acceptance criteria

The deployed service must satisfy ALL of the following:

1. **Health reachable** — `GET /health` returns 200 with body containing
   `"status":"ok"` within 100 ms.

2. **Read-your-writes** — after `POST /tasks` returns 201 with a
   `task_id`, an immediate `GET /tasks/<task_id>` must return 200 with
   the same `title`.

3. **No 5xx under concurrent load** — when 20 `POST /tasks` requests
   are issued concurrently, **all 20 must succeed** (status 2xx). Any
   5xx response under this load is a critical failure indicative of
   broken concurrency handling (race conditions, lost locks,
   non-thread-safe state).

4. **List shows what was written** — `GET /tasks` must return a JSON
   array that includes every previously created task_id.

5. **Error response codes correct** — `POST /tasks` with empty or
   missing `title` returns 400. `GET /tasks/<id>` for a non-existent
   ID returns 404. Never 500 for these client errors.

## Verdict policy

- All 5 criteria PASS → overall verdict **PASS**.
- Any criterion FAILED → overall verdict **FAIL** with reasoning that
  identifies which criterion failed and the observed evidence.

## Out of scope

- Authentication / authorization
- Data persistence (in-memory is acceptable for this MVP target)
- Update / delete operations
- Pagination
