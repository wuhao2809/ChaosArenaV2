# v1-album-store — System Spec (drafted)

## Description

A REST API for an album store that supports creating and retrieving albums (PUT/GET /albums/:album_id), listing all albums (GET /albums), uploading photos asynchronously with a per-album monotonically-increasing sequence number assigned synchronously in the POST handler (POST /albums/:album_id/photos returning 202 immediately), polling photo processing status (GET /albums/:album_id/photos/:photo_id), and deleting photos with both the database record and the backing file removed within 5 seconds (DELETE /albums/:album_id/photos/:photo_id). No authentication layer is described; the service is scored on correctness scenarios and p95 latency under concurrent load.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent per-album seq assignment produces duplicates

- **Given**: Album A exists with 0 photos; implementation uses a non-atomic read-modify-write for the seq counter (e.g. SELECT MAX(seq)+1)
- **When**: 20 concurrent POST /albums/A/photos requests are issued simultaneously with valid photo payloads
- **Then**: All 20 return 202; the 20 seq values across all responses are distinct integers forming exactly the set {1..20}; no two responses share a seq value; no 5xx responses
- **Priority**: HIGH

### R2. Concurrent PUT same album_id creates duplicate records

- **Given**: Album_id X does not exist
- **When**: 20 concurrent PUT /albums/X requests with identical body are issued simultaneously
- **Then**: All return 200 or 201; GET /albums lists album_id X exactly once (no duplicate rows); GET /albums/X returns the stored fields without error; no 5xx responses
- **Priority**: HIGH

### R3. Concurrent conflicting PUTs to same album_id produce corrupted or lost data

- **Given**: Album_id X exists with title='Title-A'
- **When**: Two concurrent PUT /albums/X requests are issued — one with title='Title-B', one with title='Title-C'
- **Then**: Both return 200; GET /albums/X returns either 'Title-B' or 'Title-C' (last-write-wins is acceptable); the title field is never empty, null, or a mix of both strings; no 5xx
- **Priority**: HIGH

### R4. Concurrent DELETE of same photo causes 500 on second delete

- **Given**: Photo P (status=completed) exists in album A
- **When**: Two DELETE /albums/A/photos/P requests are issued in parallel
- **Then**: Exactly one returns 200 or 204; the other returns 404; no 500 (the second delete must not crash on a missing file or missing DB row); GET /albums/A/photos/P returns 404
- **Priority**: HIGH

### R5. Background worker resurrects deleted photo record

- **Given**: Photo P has just been uploaded and is in status='processing'
- **When**: DELETE /albums/A/photos/P is issued and returns 200 or 204 before the worker finishes; 15 additional seconds elapse
- **Then**: GET /albums/A/photos/P returns 404 immediately after DELETE and continues to return 404 after 15s; the background worker must not re-insert the record or write a file to storage after the delete completes
- **Priority**: HIGH

<!-- Category: async_invariants — Async / temporal invariants -->

### R6. Photo processing never completes — status stuck at 'processing'

- **Given**: A valid image file is uploaded via POST /albums/:album_id/photos, which returns 202
- **When**: GET /albums/:album_id/photos/:photo_id is polled every 500ms for up to 30 seconds
- **Then**: Status transitions from 'processing' to 'completed' (or 'failed') within 30s; the endpoint never returns 'processing' indefinitely; if 'completed', the url field is present
- **Priority**: HIGH

### R7. Completed photo URL is not actually fetchable

- **Given**: A photo has reached status='completed' and the response contains a url field
- **When**: An HTTP GET is issued directly to that url
- **Then**: The URL returns HTTP 200 with a non-empty body; not 403, 404, 500, or a redirect to an expired signed URL
- **Priority**: HIGH

### R8. Backing file not removed within 5 seconds after DELETE

- **Given**: Photo P is completed; its url has been captured and verified to return 200
- **When**: DELETE /albums/A/photos/P returns 200 or 204; 5 seconds elapse; the captured url is fetched again
- **Then**: The url no longer returns 200 (returns 403, 404, or equivalent); GET /albums/A/photos/P returns 404; both conditions must hold within the 5-second window
- **Priority**: HIGH

### R9. Orphan file written to storage after DELETE-during-processing

- **Given**: Photo P is in status='processing'; DELETE /albums/A/photos/P has returned 200 or 204
- **When**: 15 seconds elapse (enough time for the background worker to have finished processing)
- **Then**: GET /albums/A/photos/P continues to return 404; no URL for photo P becomes publicly accessible (the worker must detect the deleted record and discard the upload result rather than writing the file)
- **Priority**: HIGH

<!-- Category: auth_boundaries — Authorization boundaries -->

*Category auth_boundaries marked N/A by drafter: The specification describes no authentication or authorization layer — there are no tokens, sessions, roles, or ownership enforcement; the 'owner' field is stored metadata only, and all callers are treated as equivalent anonymous clients.*

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R10. POST photo to non-existent album

- **Given**: Album_id 'nonexistent-album-xyz' has never been created
- **When**: POST /albums/nonexistent-album-xyz/photos with a valid multipart photo payload
- **Then**: 404 Not Found with a JSON error body; not 202 Accepted, not 500; no photo record is created
- **Priority**: HIGH

### R11. Extremely large photo upload causes OOM or 500 instead of 413

- **Given**: Album A exists; the server has no explicit multipart size limit configured
- **When**: POST /albums/A/photos with a photo file of 500 MB
- **Then**: Either 413 Payload Too Large or 202 Accepted (if the service legitimately supports large files per S15); never a 500 due to out-of-memory, never an indefinite hang; response arrives within a reasonable timeout
- **Priority**: HIGH

## Open Exploration

Beyond the required categories, probe whether the health endpoint returns the exact JSON body {"status": "ok"} with lowercase string value under sustained load (the spec hints the engine runs a health check between every scenario, so a slow or degraded health response under concurrent load may silently affect scoring). Investigate whether the seq counter is persisted to durable storage rather than held in process memory — a service restart between test scenarios must not reset per-album seq counters to 1, which would cause duplicate seq values visible in GET /albums/:album_id/photos/:photo_id responses. Check whether the Content-Type: application/json header is present on all responses including 4xx and 5xx error paths, since the spec requires it universally. Probe whether album_id values containing URL-special characters (slashes, percent signs, Unicode) cause routing errors or 500s. Finally, verify that the background worker sets status to 'failed' with an appropriate error indicator rather than leaving photos in 'processing' forever when object-storage writes fail — a worker that silently swallows errors will cause ChaosArena's polling loop to time out on every upload under adverse conditions.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*