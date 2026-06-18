# v1-album-store — System Spec (drafted)

## Description

A REST API for an album store that supports idempotent album creation and retrieval, asynchronous photo uploads with per-album monotonically-increasing sequence numbers assigned synchronously in the POST handler, photo processing status polling, and photo deletion that must remove both the metadata record and the backing file within five seconds. No authentication layer is defined; all callers are treated equivalently. The service must sustain correctness under concurrent load across all endpoints.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent per-album seq assignment collision

- **Given**: An album exists with no photos
- **When**: 10 POST /albums/:album_id/photos requests are issued in parallel to the same album
- **Then**: All 10 return 202; the seq values in the 10 response bodies are distinct positive integers forming exactly the set {1,2,...,10} with no duplicates and no value of 0 or null
- **Priority**: HIGH
- **Estimated turns**: 4

### R2. Concurrent PUT idempotency — no duplicate album records

- **Given**: An album_id that does not yet exist
- **When**: 10 concurrent PUT /albums/:album_id requests with identical bodies are issued
- **Then**: All return 200 or 201; GET /albums/:album_id returns exactly one record; GET /albums lists that album_id exactly once (not 10 times)
- **Priority**: HIGH
- **Estimated turns**: 3

### R3. Concurrent album creates — list completeness under write contention

- **Given**: 50 distinct album_ids that do not yet exist
- **When**: 50 concurrent PUT /albums/:album_id requests are issued, one per album_id, all in parallel
- **Then**: All return 200 or 201; a subsequent GET /albums returns a list containing all 50 album_ids — no lost writes under concurrency
- **Priority**: HIGH
- **Estimated turns**: 3

### R4. Delete-during-processing worker resurrection

- **Given**: An album exists; a photo upload returns 202 with status=processing
- **When**: DELETE /albums/:album_id/photos/:photo_id is issued immediately (before the background worker completes), then 15 seconds elapse
- **Then**: DELETE returns 200 or 204; GET /albums/:album_id/photos/:photo_id returns 404 both immediately after DELETE and after 15 seconds — the background worker must not resurrect the record by writing completed status or url after the deletion
- **Priority**: HIGH
- **Estimated turns**: 4

<!-- Category: async_invariants — Async / temporal invariants -->

### R5. seq assigned synchronously in 202 — not deferred to background worker

- **Given**: An album exists
- **When**: POST /albums/:album_id/photos returns 202
- **Then**: The 202 response body contains a seq field that is a positive integer; seq must not be null, 0, or absent from the immediate 202 response regardless of whether the background worker has started
- **Priority**: HIGH
- **Estimated turns**: 2

### R6. seq value stable and consistent across 202 and GET at all lifecycle stages

- **Given**: A photo upload returns 202 with seq=N
- **When**: GET /albums/:album_id/photos/:photo_id is called while status=processing and again after status=completed
- **Then**: Both GET responses return seq=N matching the value in the 202 response; seq must not change, disappear, or be reassigned between lifecycle stages
- **Priority**: HIGH
- **Estimated turns**: 2

### R7. Photo processing completes within 30-second deadline

- **Given**: A photo upload returns 202 with status=processing
- **When**: GET /albums/:album_id/photos/:photo_id is polled every 2 seconds for up to 30 seconds
- **Then**: Status transitions to completed or failed within 30 seconds; the photo never remains permanently stuck in processing
- **Priority**: HIGH
- **Estimated turns**: 2

### R8. Completed status and url written atomically — no intermediate state

- **Given**: A photo is transitioning from processing to completed
- **When**: GET /albums/:album_id/photos/:photo_id is polled rapidly (every 200ms) through the transition; the first response that shows status=completed is captured
- **Then**: That first completed response contains a non-empty url field; it must never be the case that status=completed is observed while url is absent or null in the same response
- **Priority**: HIGH
- **Estimated turns**: 3

### R9. Completed photo url is fetchable and returns 200

- **Given**: A photo has status=completed with a url field
- **When**: The url value is fetched directly via HTTP GET
- **Then**: The HTTP GET to the url returns 200; the url must be a real, accessible resource — not a placeholder, localhost address, or presigned URL that has already expired
- **Priority**: HIGH
- **Estimated turns**: 2

### R10. File removed from storage within 5 seconds of DELETE

- **Given**: A photo has status=completed with a known url; the url currently returns 200
- **When**: DELETE /albums/:album_id/photos/:photo_id returns 200 or 204, then 5 seconds elapse
- **Then**: GET /albums/:album_id/photos/:photo_id returns 404; a direct HTTP GET to the previously captured url no longer returns 200 (must return 403, 404, or 410) — both the metadata record and the backing file must be gone
- **Priority**: HIGH
- **Estimated turns**: 2

<!-- Category: auth_boundaries — Authorization boundaries -->

### R11. Cross-album photo read — photo_id scoped to its owning album

- **Given**: Photo P was uploaded to album A and GET /albums/A/photos/P returns 200
- **When**: GET /albums/B/photos/P is requested where B is a different, valid album_id
- **Then**: Response is 404; photo metadata for album A is not returned under album B's path — the service must enforce album-scoped photo ownership even without formal authentication
- **Priority**: MEDIUM
- **Estimated turns**: 2

### R12. Cross-album photo delete — cannot delete photo via wrong album path

- **Given**: Photo P belongs to album A and is in status=completed
- **When**: DELETE /albums/B/photos/P is issued where B is a different, valid album_id
- **Then**: Response is 404; photo P is unaffected and GET /albums/A/photos/P still returns 200 with status=completed
- **Priority**: MEDIUM
- **Estimated turns**: 2

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R13. Missing or empty photo field in multipart upload

- **Given**: An album exists
- **When**: POST /albums/:album_id/photos is sent as multipart/form-data with (a) no photo field at all, or (b) a photo field with 0 bytes
- **Then**: Response is 400 Bad Request; not 500 Internal Server Error; no photo record or seq counter increment occurs
- **Priority**: HIGH
- **Estimated turns**: 1

### R14. POST photo to non-existent album

- **Given**: An album_id that has never been created
- **When**: POST /albums/:album_id/photos is sent with a valid photo file
- **Then**: Response is 404 Not Found; not 202, 201, or 500; no orphan photo record is created and no seq counter is initialized for the non-existent album
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R15. album_id path parameter and body field mismatch on PUT

- **Given**: No prior state required
- **When**: PUT /albums/ID_A is called with a request body containing album_id: ID_B where ID_A ≠ ID_B
- **Then**: Response is 400 Bad Request, or the path parameter ID_A is treated as authoritative and the body album_id is ignored; the service must not silently create or update a record under ID_B while the path specifies ID_A
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R16. Injection and path-traversal characters in album_id

- **Given**: No prior state required
- **When**: GET /albums/:album_id is called with each of: (a) "' OR '1'='1", (b) "../../../etc/passwd", (c) "<script>alert(1)</script>", (d) a 2048-character alphanumeric string
- **Then**: Each returns 400 or 404; none returns 500; no stack trace, internal file path, or database error message is present in any response body
- **Priority**: HIGH
- **Estimated turns**: 1

### R17. Oversize photo upload

- **Given**: An album exists
- **When**: POST /albums/:album_id/photos is sent with a photo file of approximately 100 MB
- **Then**: Response is 202 Accepted (if large files are supported) or 413 Payload Too Large; must not be 500; if 202, the photo must eventually reach status=completed or status=failed within the processing deadline
- **Priority**: HIGH
- **Estimated turns**: 2

### R18. Missing or null required fields on PUT /albums

- **Given**: No prior state required
- **When**: PUT /albums/:album_id is called with (a) title field absent, (b) owner field set to null, or (c) an entirely empty JSON body {}
- **Then**: Each variant returns 400 Bad Request; not 500; no partial album record is persisted under the album_id
- **Priority**: MEDIUM
- **Estimated turns**: 1

### R19. Oversize title or description on PUT /albums

- **Given**: No prior state required
- **When**: PUT /albums/:album_id is called with title or description set to a 100,000-character string
- **Then**: Response is 400 or 413; not 500; no silent truncation that stores a different value than what was submitted
- **Priority**: LOW
- **Estimated turns**: 1

## Open Exploration

Probe whether GET /albums has an implicit result-size cap (e.g. a hardcoded LIMIT 100 or default pagination) that would silently drop albums after heavy load-test accumulation — the spec requires every album ever created to appear. Investigate whether the per-album seq counter is stored durably or held in memory, and whether a service restart resets it to 1 causing seq collisions with existing photos. Check whether the background photo worker has retry logic that could write a second completed record for a photo that already reached completed, and whether that second write changes the url. Verify that the health endpoint returns exactly the string value "ok" for the status field and does not return a boolean true or an uppercase "OK" that would fail the strict body check in S6. Finally, probe whether a DELETE on an already-deleted photo_id returns 404 (idempotent delete) or 500, and whether rapid repeated DELETEs on the same photo_id increment the seq counter or corrupt album state.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*