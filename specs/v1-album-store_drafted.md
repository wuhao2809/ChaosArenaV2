# v1-album-store — System Spec (drafted)

## Description

A REST API for managing photo albums with asynchronous photo processing. Users can create albums, upload photos that are processed in the background, and delete photos. Each album maintains a per-album monotonically increasing sequence number for photos. The service includes health checks and supports concurrent operations under load testing scenarios.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent photo upload seq counter race

- **Given**: An album exists with 0 photos
- **When**: Two clients POST /albums/:album_id/photos within 10ms
- **Then**: Both return 202 with unique seq values (1 and 2); no duplicate seq; no lost photo; final photo count is 2
- **Priority**: HIGH

### R2. Concurrent upload to same album seq monotonicity

- **Given**: An album has 5 photos (seq 1-5)
- **When**: Three clients POST photos concurrently within 50ms
- **Then**: All three return 202 with seq values 6, 7, 8 (in any assignment order); no gaps, no duplicates
- **Priority**: HIGH

### R3. Concurrent DELETE and GET on same photo

- **Given**: A photo exists in completed status with accessible URL
- **When**: Client A DELETEs photo while Client B GETs it (parallel within 10ms)
- **Then**: Either DELETE wins (GET returns 404) or GET wins (DELETE returns 404/409); not both succeed with inconsistent state
- **Priority**: HIGH

### R4. Concurrent POST and DELETE on same photo

- **Given**: A photo is uploaded and returns photo_id P
- **When**: Background worker is processing while DELETE /photos/P is called
- **Then**: DELETE completes within 5s; subsequent GET returns 404; no orphan file remains in storage (URL returns 404 or 403)
- **Priority**: HIGH

### R5. Concurrent PUT same album_id

- **Given**: Album A does not exist
- **When**: Two clients PUT /albums/A with different titles within 10ms
- **Then**: Both return 200 or 201; subsequent GET returns exactly one title (last-write-wins acceptable); no partial field merge from both requests
- **Priority**: MEDIUM

### R6. Background worker status update race with DELETE

- **Given**: A photo is in processing status
- **When**: Background worker transitions to completed while DELETE is called (parallel)
- **Then**: DELETE completes within 5s; GET returns 404; no orphan completed photo or file remains
- **Priority**: HIGH

<!-- Category: async_invariants — Async / temporal invariants -->

### R7. Photo processing completes within timeout

- **Given**: A photo is uploaded and returns 202 with status=processing
- **When**: 30 seconds elapse
- **Then**: GET /photos/:photo_id returns status=completed or status=failed; not stuck in processing indefinitely
- **Priority**: HIGH

### R8. Completed photo URL is accessible

- **Given**: GET /photos/:photo_id returns status=completed with url field
- **When**: HTTP GET is issued to the url value
- **Then**: Response is 200 with image content; not 404 or 403 (file upload actually succeeded)
- **Priority**: HIGH

### R9. DELETE removes file within 5 seconds

- **Given**: A photo exists in completed status with accessible URL U
- **When**: DELETE /photos/:photo_id returns 200 or 204
- **Then**: Within 5 seconds, GET /photos/:photo_id returns 404 AND HTTP GET to URL U returns non-200 (404, 403, or error)
- **Priority**: HIGH

### R10. DELETE idempotency and consistency

- **Given**: A photo is deleted (DELETE returned 200/204)
- **When**: 5 seconds elapse after DELETE
- **Then**: GET /photos/:photo_id still returns 404; file URL still inaccessible; state does not revert
- **Priority**: MEDIUM

### R11. Photo status transitions are monotonic

- **Given**: A photo reaches status=completed
- **When**: Background worker retries or re-processes
- **Then**: Status remains completed; does not regress to processing (no status flip-flop)
- **Priority**: MEDIUM

### R12. Failed photo does not leave orphan file

- **Given**: A photo upload fails and reaches status=failed
- **When**: Storage is inspected
- **Then**: No file exists in storage for that photo_id (or file is marked for cleanup); no storage leak
- **Priority**: MEDIUM

<!-- Category: auth_boundaries — Authorization boundaries -->

*Category auth_boundaries marked N/A by drafter: The specification describes no authentication or authorization mechanism; all endpoints are publicly accessible and any caller can create, read, update, or delete any resource regardless of the 'owner' field value.*

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R13. Empty or zero-byte photo file

- **Given**: POST /albums/:album_id/photos accepts multipart file
- **When**: Photo field contains 0-byte file
- **Then**: 400 Bad Request or 422 Unprocessable Entity; not 500 or silent acceptance with processing failure
- **Priority**: HIGH

### R14. Oversize photo file

- **Given**: POST /albums/:album_id/photos accepts file upload
- **When**: Photo file is 100MB or 1GB
- **Then**: 413 Payload Too Large or 400 within reasonable time; not 500, timeout, or OOM crash
- **Priority**: HIGH

### R15. Non-image MIME type upload

- **Given**: POST /albums/:album_id/photos expects image
- **When**: Photo field contains .exe, .pdf, or .txt file
- **Then**: 400 or 415 Unsupported Media Type; not 500 or silent acceptance leading to processing failure
- **Priority**: MEDIUM

### R16. SQL injection in album_id

- **Given**: PUT or GET /albums/:album_id parses album_id from URL
- **When**: album_id is "'; DROP TABLE albums; --"
- **Then**: 400 Bad Request or 404 Not Found; not 500 or database error; no side effects on other albums
- **Priority**: HIGH

### R17. Oversize title or description

- **Given**: PUT /albums/:album_id accepts title and description strings
- **When**: title or description is 10MB string
- **Then**: 400 Bad Request or 413; not 500, OOM, or silent truncation without error
- **Priority**: HIGH

### R18. album_id mismatch between URL and body

- **Given**: PUT /albums/:album_id includes album_id in both URL and body
- **When**: URL has album_id=A but body has album_id=B
- **Then**: 400 Bad Request or ignore body field and use URL value; not create album B or 500
- **Priority**: MEDIUM

## Open Exploration

Although the specification includes an 'owner' field in album records, no authentication or authorization layer is described. Probe whether the implementation leaks cross-owner data or allows unauthorized modifications. Test the LIST endpoint under high album counts (1000+ albums) to verify it truly returns all albums without pagination bugs or memory issues. Investigate what happens when the background worker crashes mid-processing: does the system have retry logic, dead-letter queues, or does the photo remain stuck? Test concurrent DELETEs of the same photo to verify idempotency and that the file is removed exactly once. Finally, probe the health check endpoint during load tests to see if it remains responsive when the system is under stress, or if it shares resources that cause it to fail when queues back up.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*