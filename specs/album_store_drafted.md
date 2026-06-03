# AlbumStore — System Spec (drafted)

## Description

A photo-album REST API where users organize photos into named albums. Albums are created or updated via PUT with a required title field. Photos are uploaded asynchronously via POST (202 response) and processed in the background, transitioning from 'processing' to 'completed' or 'failed'. Photos can be deleted, and the service must ensure deleted photos are never resurrected by background workers. The service handles concurrent writes correctly, with last-write-wins semantics for album updates and no lost writes.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent PUT to same album

- **Given**: Album A exists with title 'Original'
- **When**: Two clients PUT /albums/A with titles 'Title1' and 'Title2' within 10ms
- **Then**: Both return 200; subsequent GET /albums/A shows exactly one of 'Title1' or 'Title2' (last-write-wins); no corrupted merge or lost write

### R2. Concurrent photo uploads to same album

- **Given**: Album A exists
- **When**: Two clients POST /albums/A/photos within 10ms
- **Then**: Both return 202 with distinct photo_ids; both photos exist and are processing; no lost upload

### R3. Concurrent delete of same photo

- **Given**: Photo P exists in album A
- **When**: Two clients DELETE /albums/A/photos/P within 10ms
- **Then**: Exactly one returns 200 or 204; the other returns 404; subsequent GET returns 404

### R4. Delete photo while background worker completes it

- **Given**: Photo P is uploaded and status is 'processing'
- **When**: Client DELETEs /albums/A/photos/P while background worker is marking it 'completed' (simulate with tight timing or worker delay)
- **Then**: DELETE returns 200/204; subsequent GET /albums/A/photos/P returns 404 forever; photo is not resurrected with 'completed' status

<!-- Category: async_invariants — Async / temporal invariants -->

### R5. Photo processing completes within 30 seconds

- **Given**: A small photo (<100KB) is uploaded via POST /albums/A/photos
- **When**: 30 seconds elapse
- **Then**: GET /albums/A/photos/<photo_id> returns status 'completed' or 'failed' (not stuck in 'processing')

### R6. Deleted photo never resurrected by worker

- **Given**: Photo P is uploaded (status 'processing') and immediately deleted
- **When**: Background worker attempts to mark P as 'completed' after the delete; wait 30 seconds
- **Then**: GET /albums/A/photos/P returns 404 at all times after delete; status never becomes 'completed'

### R7. Completed photo status is stable

- **Given**: Photo P has status 'completed' with a url
- **When**: GET /albums/A/photos/P is called multiple times over 15 seconds
- **Then**: Status remains 'completed' with the same url; no regression to 'processing' or 'failed'

<!-- Category: auth_boundaries — Authorization boundaries -->

*Category auth_boundaries marked N/A by drafter: The specification describes an 'owner' field in album records but does not define any authentication or authorization mechanism. All clients can access all albums and photos without restriction. (Probe during Open Exploration whether the owner field is intended to enforce access control.)*

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R8. Empty title rejected

- **Given**: PUT /albums/A with body {"title": ""}
- **When**: Request is sent
- **Then**: 400 Bad Request; album is not created or updated

### R9. Missing title rejected

- **Given**: PUT /albums/A with body {"description": "No title"}
- **When**: Request is sent
- **Then**: 400 Bad Request; album is not created or updated

### R10. Oversize photo rejected

- **Given**: Album A exists
- **When**: POST /albums/A/photos with payload >1MB
- **Then**: 413 Payload Too Large; not 500 or 202

### R11. Script tag in title

- **Given**: PUT /albums/A with title '<script>alert(1)</script>'
- **When**: Request is sent
- **Then**: 201 or 200; title is stored as plain text or sanitized; not 500

### R12. Negative album_id

- **Given**: No album with id -1 exists
- **When**: GET /albums/-1
- **Then**: 404 Not Found or 400 Bad Request; not 500

### R13. Photo upload to non-existent album

- **Given**: Album Z does not exist
- **When**: POST /albums/Z/photos
- **Then**: 404 Not Found; not 202 or 500

### R14. Delete photo from wrong album

- **Given**: Photo P exists in album A
- **When**: DELETE /albums/B/photos/P (where B != A)
- **Then**: 404 Not Found; photo P in album A is unchanged

## Open Exploration

Beyond the required categories, testers should probe: (1) whether the 'owner' field is intended to enforce access control (currently unenforced); (2) whether album deletion is supported and, if so, what happens to photos in a deleted album (orphaned? cascade-deleted?); (3) whether the /health endpoint remains responsive under load (e.g., during heavy photo processing); (4) whether photo URLs expire or are permanent; (5) whether the service enforces any rate limits on uploads or album creation; (6) whether concurrent DELETE album + POST photo to that album produces a consistent outcome; and (7) whether the background worker has retry logic for transient failures and whether retries respect the deleted-photo invariant.

## Out of Scope

- Long title validation (10000+ chars): stub accepts any non-empty title; no length limit is defined.
- Zero-byte or non-image file type validation: stub accepts any payload ≤1MB and marks it "completed"; file-type rejection is not part of the spec.
- Authentication and authorization enforcement: the `owner` field is stored but not used for access control in this version.
- Pagination of `/albums`.
- Album deletion.

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*