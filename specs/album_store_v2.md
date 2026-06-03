# Album Store — System Spec (v2 testbed)

## Description

A simple photo-album service. Users create albums, upload photos to them
(asynchronously), retrieve photos by ID, and delete photos. This spec
describes the behavior the deployed service must satisfy for partner-quality
release.

This spec is a controlled testbed for the ChaosArena evaluator. It uses the
two-tier format (Required + Open Exploration). Each Required category
corresponds to an injectable failure mode in the stub; the agent should
catch all of them.

## Endpoints

| Method | Path | Body | Success response |
|---|---|---|---|
| `GET`    | `/health`                            | —                                 | `200` `{"status":"ok"}` |
| `PUT`    | `/albums/<album_id>`                 | `{"title": "...", "description": "...", "owner": "..."}` | `200` (existed) or `201` (new) with album object |
| `GET`    | `/albums/<album_id>`                 | —                                 | `200` with album object, or `404` |
| `GET`    | `/albums`                            | —                                 | `200` with JSON array of albums |
| `POST`   | `/albums/<album_id>/photos`          | multipart/form-data, photo bytes  | `202` `{"photo_id": "...", "status": "processing"}` |
| `GET`    | `/albums/<album_id>/photos/<pid>`    | —                                 | `200` with photo object (status: processing/completed/failed), or `404` |
| `DELETE` | `/albums/<album_id>/photos/<pid>`    | —                                 | `200` or `204` on success, `404` if absent |

## Required Test Categories

The agent MUST verify each Required category and submit a per-category
verdict (PASSED / FAILED / UNTESTABLE) with evidence.

### R1. Health and basic CRUD round-trip

- `GET /health` returns 200 with `{"status":"ok"}` within 100ms.
- `PUT /albums/<id>` followed by `GET /albums/<id>` returns the same fields
  (`title`, `description`, `owner`).
- `GET /albums` includes the album just created.

### R2. Concurrent write integrity (race condition)

When 20 concurrent `PUT /albums/<same_id>` requests are issued (each with
distinct `title`), every successful `PUT` must be reflected in storage. After
all return, the persisted record must match the body of one of the writers.
**No write may silently disappear.**

Stronger check: after 20 PUTs to 20 *distinct* `album_id`s issued
concurrently, `GET /albums` must list all 20.

### R3. Async photo upload completes

After `POST /albums/<id>/photos`, polling `GET /albums/<id>/photos/<pid>`
every 1 second should observe status transition from `processing` to
`completed` within 30 seconds. The completed record must include a `url`
field.

### R4. Delete is sync; no orphan after async pipeline

After `DELETE /albums/<id>/photos/<pid>` returns, subsequent `GET` for that
photo MUST return `404`. This must hold continuously for at least 15 seconds
after the delete — the async worker MUST NOT later resurrect the record by
writing a `completed` status. (This catches an async-worker race / orphan.)

### R5. Stale-read prohibition

After `DELETE /albums/<id>/photos/<pid>` returns 200/204, every subsequent
`GET` of that photo within the next 5 seconds must return `404`. No stale
record may be returned, even briefly.

### R6. Graceful client-error responses

- `PUT /albums/<id>` with empty/missing `title` returns `400` (NOT 500).
- `GET /albums/<id>` for a non-existent id returns `404`.
- `POST /albums/<id>/photos` with body > 1MB returns `413` (payload too
  large) — never `500`.

## Open Exploration

After every Required category has been verified, the agent may freely
probe the service for failures the spec did not enumerate. Categories of
interest (LLM-coauthored systems frequently  these):

- Authorization or ownership boundary leaks
- Inconsistent error-code semantics (e.g., 500 where 4xx expected)
- Resource exhaustion / unbounded responses
- Idempotency violations on PUT
- Pagination correctness if any endpoint returns lists
- Schema-shape stability of response bodies

Use `record_event(event_type="OBSERVATION" | "WARNING" | "VIOLATION", detail=...)`
to log anything noteworthy. These events will appear in the final report.

## Verdict Policy

- Per Required category: agmissent emits PASSED / FAILED / UNTESTABLE with
  cited evidence (status codes, latency, body excerpts).
- Overall verdict: FAIL if any Required category FAILED; otherwise PASS.

## Out of Scope

- Authentication and authorization (reserved for v3 spec).
- Data persistence (in-memory acceptable for this testbed).
- Pagination of `/albums`.
- Deletion of albums (only photo deletion is required).
