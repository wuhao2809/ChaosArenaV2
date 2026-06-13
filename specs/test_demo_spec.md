# v1-album-store — System Spec (drafted)

## Description

A REST API for a multi-album photo store. Clients can create or update albums via idempotent PUT, retrieve individual albums or list all albums, upload photos asynchronously (POST returns 202 immediately; a background worker processes the file and transitions status to 'completed'), and delete photos with the requirement that both the database record and the backing file are removed within 5 seconds. Each album maintains an independent, monotonically-increasing per-album sequence number (seq) that must be assigned synchronously in the POST handler, not by the background worker. There is no authentication layer; the owner field is plain data.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent seq assignment within the same album

- **Given**: Album A exists with zero photos
- **When**: 10 concurrent POST /albums/A/photos requests are issued within the same 50ms window
- **Then**: All 10 receive 202; the seq values across all responses are exactly the set {1,2,3,4,5,6,7,8,9,10} with no duplicates and no gaps; subsequent GET calls for each photo_id confirm the same unique seq values
- **Priority**: HIGH

### R2. Concurrent PUT same album_id (upsert race)

- **Given**: Album X does not exist
- **When**: 5 concurrent PUT /albums/X requests with identical bodies are issued simultaneously
- **Then**: All 5 return 200 or 201; GET /albums/X returns exactly one record with consistent field values; GET /albums does not contain duplicate entries for X; no 500 responses
- **Priority**: HIGH

### R3. Concurrent DELETE of the same photo

- **Given**: Photo P in album A is in 'completed' state with a known storage URL
- **When**: Two DELETE /albums/A/photos/P requests are issued in parallel
- **Then**: At least one returns 200 or 204; neither returns 5xx; the second may return 404 or 200/204 (idempotent); GET /albums/A/photos/P returns 404 after both complete; the backing file is deleted exactly once (no double-delete 500 from storage)
- **Priority**: HIGH

### R4. Background worker vs concurrent DELETE race

- **Given**: Photo P has just been accepted (status='processing') and the background worker has begun processing it
- **When**: DELETE /albums/A/photos/P is issued while the worker is actively writing the result
- **Then**: Either DELETE wins (photo record gone, worker's write is discarded or no-ops) or worker completes first (photo reaches 'completed', then DELETE removes it); in no case does a 500 occur, an orphan file remain in storage after DELETE, or the photo record reappear as 'completed' after a successful DELETE
- **Priority**: HIGH

<!-- Category: async_invariants — Async / temporal invariants -->

### R5. Photo processing completes within 30-second deadline

- **Given**: A valid photo has been uploaded and received a 202 with status='processing'
- **When**: GET /albums/:album_id/photos/:photo_id is polled every 2 seconds for up to 30 seconds
- **Then**: Status transitions to 'completed' (not 'failed', not stuck in 'processing') within 30 seconds; the response at 'completed' includes a non-null 'url' field and the correct 'seq' value
- **Priority**: HIGH

### R6. Completed photo URL is directly fetchable

- **Given**: Photo P has reached status='completed' and the response contains a 'url' field
- **When**: An HTTP GET is issued directly to that URL (no auth headers)
- **Then**: The URL returns HTTP 200 with a non-empty body; not 403, 404, 410, or 500; the URL must remain valid for at least the duration of the test run (pre-signed URL expiry must exceed the test window)
- **Priority**: HIGH

### R7. Backing file removed from storage within 5 seconds of DELETE

- **Given**: Photo P is in 'completed' state; its 'url' has been captured and confirmed to return 200
- **When**: DELETE /albums/A/photos/P is called and returns 200 or 204; then 5 seconds elapse
- **Then**: GET /albums/A/photos/P returns 404; an HTTP GET to the previously captured URL no longer returns 200 (expected 403, 404, or 410); both conditions must hold within the 5-second window
- **Priority**: HIGH

### R8. seq field present and correct during 'processing' state

- **Given**: Photo P has just been uploaded; the 202 response body contains a seq value S
- **When**: GET /albums/:album_id/photos/:photo_id is called immediately (before processing completes)
- **Then**: Response is 200 with status='processing'; the 'seq' field is present and equals S (not null, not missing, not 0); this must hold even if the background worker has not yet touched the record
- **Priority**: HIGH

<!-- Category: auth_boundaries — Authorization boundaries -->

*Category auth_boundaries marked N/A by drafter: The specification defines no authentication or authorization layer — there are no tokens, sessions, API keys, or roles; the 'owner' field is plain unvalidated data and is not used for access control.*

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R9. Photo upload to non-existent album

- **Given**: Album X has never been created
- **When**: POST /albums/X/photos with a valid multipart photo field
- **Then**: 404 Not Found; no photo record is created; no orphan entry appears in any subsequent GET; not 202 or 500
- **Priority**: HIGH

### R10. Malformed or adversarial album_id in path (path traversal, SQL injection, overlong string)

- **Given**: No album exists for the given id
- **When**: PUT or GET with album_id set to '../admin', "'; DROP TABLE albums;--", or a 10 000-character string
- **Then**: 400 Bad Request or 404 Not Found; not 500; no database error message leaked in the response body; no unintended file-system access
- **Priority**: HIGH

### R11. GET /albums returns all albums without implicit pagination truncation

- **Given**: 1 000 albums have been created across prior test scenarios and load tests
- **When**: GET /albums is called with no query parameters
- **Then**: Response contains all 1 000 album entries (each with at minimum an 'album_id' field); the list is not silently capped at 100, 200, or any other default page size; response is 200 not 500
- **Priority**: HIGH

### R12. Oversize photo upload

- **Given**: Album A exists
- **When**: POST /albums/A/photos with a 500 MB binary payload in the 'photo' field
- **Then**: Either 413 Payload Too Large or 202 Accepted (if the service is designed to handle large files per S15); must not return 500, must not silently hang the connection indefinitely; if 202, the photo must eventually reach 'completed' or 'failed' within the processing deadline
- **Priority**: HIGH

## Open Exploration

Beyond the required categories, a tester should probe: (1) whether the health endpoint degrades under concurrent load — since ChaosArena runs a health probe between every scenario, a slow or erroring /health under background stress will surface in the run report even if it does not directly cost points; (2) whether the background worker queue has a bounded depth — under S15 large-payload concurrent uploads, if the queue fills up, new uploads should receive 202 and eventually complete or fail rather than silently dropping work or returning 500; (3) whether pre-signed storage URLs have a TTL long enough to survive the full test run (S12 + S15 combined can span many minutes); (4) whether the per-album seq counter survives a service restart mid-test (if the counter is in-memory only, a crash resets it and subsequent uploads collide with existing seq values); and (5) whether GET /albums is implemented with a full table scan that becomes a latency bottleneck during S13/S14 mixed read/write load as the album count grows into the thousands — an unindexed or unbounded query here could cause the mixed-workload p95 to blow past the 5× reference threshold.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*