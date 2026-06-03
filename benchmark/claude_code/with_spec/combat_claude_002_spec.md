# v1-album-store — System Spec (drafted)

## Description

A REST API for an album store that supports idempotent album creation and retrieval, asynchronous photo uploads (background worker transitions status from 'processing' to 'completed' or 'failed'; a per-album monotonically-increasing seq number is assigned synchronously at upload time), photo status polling with a live URL when completed, and photo deletion that must remove both the metadata record and the backing file within 5 seconds. The service has no authentication layer; all callers are equivalent. ChaosArena accumulates state across all correctness and load-test scenarios and scores both correctness and p95 latency under concurrent load.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent per-album seq assignment

- **Given**: An album exists with N photos already uploaded
- **When**: 20 concurrent POST /albums/:album_id/photos requests are issued simultaneously to the same album
- **Then**: All 20 requests receive 202; the seq values in the 20 response bodies are distinct positive integers forming a contiguous range (N+1 through N+20); no two responses share the same seq value; no seq is 0 or negative
- **Priority**: HIGH

### R2. Concurrent album upsert idempotency

- **Given**: An album_id does not yet exist
- **When**: 20 concurrent PUT /albums/:album_id requests with identical bodies are issued simultaneously
- **Then**: All 20 return 200 or 201; GET /albums lists exactly one record for that album_id (no duplicate rows); GET /albums/:album_id returns the correct body without 500
- **Priority**: HIGH

### R3. Delete-during-processing worker resurrection

- **Given**: A photo has just been uploaded and its status is 'processing' (DELETE is issued before the background worker writes 'completed')
- **When**: DELETE /albums/:album_id/photos/:photo_id is issued immediately after the 202 response, before the worker finishes
- **Then**: DELETE returns 200 or 204; GET /albums/:album_id/photos/:photo_id returns 404 immediately and continues to return 404 for at least 10 seconds; the background worker must not write 'completed' back after the delete, resurrecting the record
- **Priority**: HIGH

### R4. Concurrent deletes of the same photo

- **Given**: A photo exists in 'completed' state with a known backing file URL
- **When**: Two DELETE /albums/:album_id/photos/:photo_id requests are issued in parallel
- **Then**: Exactly one returns 200 or 204; the other returns 404; the backing file is removed exactly once (no double-delete storage error surfaced as a 5xx on either response)
- **Priority**: HIGH

<!-- Category: async_invariants — Async / temporal invariants -->

### R5. Photo processing completes within 30-second deadline

- **Given**: POST /albums/:album_id/photos returns 202 with status 'processing'
- **When**: GET /albums/:album_id/photos/:photo_id is polled every 500ms for up to 30 seconds
- **Then**: Status transitions to 'completed' or 'failed' within 30 seconds; status never regresses from 'completed' back to 'processing'; the seq field is present and unchanged at every poll
- **Priority**: HIGH

### R6. Backing URL is live at the instant status flips to completed

- **Given**: GET /albums/:album_id/photos/:photo_id returns status 'completed' with a non-empty url field
- **When**: The url is fetched immediately (within 1 second of receiving the completed response)
- **Then**: The url returns HTTP 200; the file is durably stored before the worker writes 'completed' — the status must not be set to 'completed' before the file is fully committed to storage
- **Priority**: HIGH

### R7. Metadata and file both gone within 5 seconds of DELETE

- **Given**: A photo is in 'completed' state; its url has been captured and verified to return 200
- **When**: DELETE /albums/:album_id/photos/:photo_id returns 200 or 204, then 5 seconds elapse
- **Then**: GET /albums/:album_id/photos/:photo_id returns 404; fetching the previously-captured url no longer returns 200 (must return 403, 404, 410, or equivalent); both conditions must hold within the 5-second window
- **Priority**: HIGH

### R8. seq field present synchronously in the 202 response body

- **Given**: An album exists
- **When**: POST /albums/:album_id/photos returns 202
- **Then**: The 202 response body contains a seq field that is a positive integer (not null, not 0, not absent); seq must be assigned in the POST handler, not deferred to the background worker — verified by inspecting the 202 body directly without a subsequent GET
- **Priority**: MEDIUM

### R9. GET /albums reflects all albums immediately after concurrent creates

- **Given**: 50 concurrent PUT /albums requests each with a distinct album_id all return 200 or 201
- **When**: GET /albums is called immediately after all 50 PUTs complete
- **Then**: The response includes all 50 album_ids; no album is missing due to a write-behind cache, read replica lag, or uncommitted transaction; each item contains at minimum the album_id field
- **Priority**: MEDIUM

<!-- Category: auth_boundaries — Authorization boundaries -->

### R10. Cross-album photo read — resource scoping

- **Given**: Photo P was uploaded to album A and has reached 'completed' state; album B is a separate, existing album
- **When**: GET /albums/B/photos/P is requested (photo_id belongs to album A, not B)
- **Then**: Response is 404; photo metadata and url are not returned; photo P remains fully accessible at GET /albums/A/photos/P with its original data intact
- **Priority**: HIGH

### R11. Cross-album photo delete — resource scoping

- **Given**: Photo P was uploaded to album A; album B is a separate, existing album
- **When**: DELETE /albums/B/photos/P is requested
- **Then**: Response is 404; photo P is not deleted; GET /albums/A/photos/P still returns 200 with the original seq, status, and url
- **Priority**: HIGH

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R12. Photo upload with missing or empty photo field

- **Given**: Album exists
- **When**: POST /albums/:album_id/photos with multipart/form-data that omits the 'photo' field entirely, or sends it as a zero-byte value
- **Then**: 400 Bad Request; not 202, not 500; no photo record is created; no seq number is consumed
- **Priority**: HIGH

### R13. Photo upload to non-existent album

- **Given**: The album_id in the URL does not correspond to any created album
- **When**: POST /albums/:album_id/photos with a valid image file
- **Then**: 404 Not Found; not 202 (no orphan photo record created with a dangling album reference); not 500
- **Priority**: HIGH

### R14. Oversize photo upload

- **Given**: Album exists
- **When**: POST /albums/:album_id/photos with a 50MB+ binary payload
- **Then**: Either 202 Accepted (service handles large files and eventually reaches 'completed' or 'failed' within the normal deadline) or 413 Payload Too Large; must not return 500; must not hang the server indefinitely or block other requests
- **Priority**: HIGH

### R15. Wrong Content-Type for photo upload

- **Given**: Album exists
- **When**: POST /albums/:album_id/photos with Content-Type: application/json and a JSON body instead of multipart/form-data
- **Then**: 400 Bad Request or 415 Unsupported Media Type; not 202, not 500
- **Priority**: MEDIUM

### R16. PUT with missing or null required fields (title absent, owner absent, or empty body)

- **Given**: A valid album_id is used in the URL
- **When**: PUT /albums/:album_id with a body missing the 'title' field, or with 'owner' set to null, or with an empty JSON object {}
- **Then**: 400 Bad Request; not 200 or 201 with a partial record stored; not 500; no record created or overwritten
- **Priority**: MEDIUM

### R17. album_id mismatch between URL path and request body

- **Given**: album_id in the URL path is 'X'; album_id in the JSON body is 'Y' (a different value)
- **When**: PUT /albums/X with body {"album_id": "Y", "title": "T", "owner": "o@e.com"}
- **Then**: Either 400 Bad Request, or the URL path value 'X' is used authoritatively and the body album_id is ignored; must not silently create or update a record under 'Y' while the caller addressed 'X'
- **Priority**: MEDIUM

### R18. Second DELETE on already-deleted photo

- **Given**: A photo was successfully deleted (first DELETE returned 200 or 204)
- **When**: DELETE /albums/:album_id/photos/:photo_id is called a second time
- **Then**: 404 Not Found; not 500; not 200 or 204 again
- **Priority**: MEDIUM

## Open Exploration

Several behaviors are underspecified and worth probing beyond the required categories: (1) Seq-after-delete reuse — if photo with seq=3 is deleted and a new photo is uploaded, does it get seq=4 (correct, monotonically increasing) or seq=3 (reuse, which the spec's 'unique' guarantee may forbid)? (2) Failed-worker recovery — if the background worker crashes mid-processing, does the photo transition to 'failed', and is the seq slot permanently consumed? Does a retry create a duplicate seq? (3) Health-check degradation under load — the spec hints that health probes run between every scenario pair; probe whether thread-pool or connection-pool exhaustion during S11–S15 causes health checks to time out, which would appear in the run report and signal architectural bottlenecks. (4) Pre-signed URL TTL — if the implementation uses short-lived pre-signed S3 URLs, the url in a 'completed' photo may expire before ChaosArena fetches it in a later scenario; verify the URL remains valid for at least the duration of the full test run. (5) Probe whether GET /albums/:album_id returns 404 for a non-existent album (the spec defines 404 for GET /albums/:album_id but only in the context of the album existing; a naive implementation might return 200 with an empty object).

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*