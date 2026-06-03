# album_store — System Spec (drafted)

## Description

Album Store is a photo-album REST API where users organize photos into named albums. Albums are created/updated via PUT with required title validation. Photos are uploaded asynchronously (202 response) and processed in the background, transitioning from 'processing' to 'completed' or 'failed'. Photos can be deleted, and the service must prevent background workers from resurrecting deleted photos. The API is unauthenticated; all callers have equivalent access.

*This spec was drafted by ChaosArena's `spec_drafter` from a natural-language description. A TA should review and edit before running an evaluation.*

## Required Test Categories

<!-- Category: race_conditions — Race-condition tests (concurrent operations on shared state) -->

### R1. Concurrent PUT to same album (last-write-lost)

- **Given**: Album A exists with title 'Original'
- **When**: Two clients PUT /albums/A within 10ms, one with title 'Version1', the other with title 'Version2'
- **Then**: After both complete, GET /albums/A returns exactly one of the two titles (last write wins); the other title is not silently lost or merged. Both PUT requests return 200.
- **Priority**: HIGH

### R2. Concurrent PUT to different albums

- **Given**: No albums exist
- **When**: Two clients PUT /albums/A and PUT /albums/B simultaneously with different titles
- **Then**: Both return 201; GET /albums returns both albums with correct titles; neither is lost
- **Priority**: HIGH

### R3. Concurrent POST photos to same album

- **Given**: Album A exists
- **When**: Two clients POST /albums/A/photos within 10ms
- **Then**: Both return 202 with distinct photo_ids; GET /albums/A/photos (if such endpoint exists) or individual GETs show both photos exist
- **Priority**: HIGH

### R4. DELETE photo during background processing

- **Given**: Photo P in album A has status 'processing'
- **When**: Client DELETEs /albums/A/photos/P while background worker is actively processing it
- **Then**: DELETE returns 200 or 204; subsequent GET /albums/A/photos/P returns 404 immediately and continues to return 404 indefinitely (worker must not resurrect photo by writing 'completed' status)
- **Priority**: HIGH

<!-- Category: async_invariants — Async / temporal invariants -->

### R5. Deleted photo stays deleted (no resurrection)

- **Given**: Photo P in album A has status 'processing'
- **When**: Client DELETEs /albums/A/photos/P, then waits 60 seconds for background worker to finish
- **Then**: GET /albums/A/photos/P returns 404 at all times after the DELETE; status never transitions to 'completed'
- **Priority**: HIGH

<!-- Category: auth_boundaries — Authorization boundaries -->

*Category auth_boundaries marked N/A by drafter: The spec describes no authentication or authorization mechanism; all callers have equivalent access to all albums and photos. The 'owner' field exists but is not enforced.*

<!-- Category: edge_cases — Edge cases (input validation, oversize, error semantics) -->

### R6. Empty or missing title

- **Given**: Client attempts to create album
- **When**: PUT /albums/A with title='' (empty string) or with title field missing entirely
- **Then**: 400 Bad Request with explanatory message; not 201, not 500
- **Priority**: HIGH

### R7. Oversize photo upload

- **Given**: Album A exists
- **When**: POST /albums/A/photos with payload larger than 1 MB (e.g., 1.5 MB)
- **Then**: 413 Payload Too Large; not 500, not 202 with silent truncation
- **Priority**: HIGH

### R8. Photo upload to non-existent album

- **Given**: Album Z does not exist
- **When**: POST /albums/Z/photos
- **Then**: 404 Not Found; not 202, not 500
- **Priority**: HIGH

### R9. Photo access via wrong album_id

- **Given**: Photo P exists in album A
- **When**: GET /albums/B/photos/P (where B ≠ A)
- **Then**: 404 Not Found; photo details are not leaked
- **Priority**: HIGH

## Open Exploration

The spec includes an 'owner' field in albums but describes no authentication or authorization layer, which is architecturally suspicious. Probe whether the owner field is validated, whether it can be changed arbitrarily, and whether any implicit access-control assumptions exist in the implementation. Additionally, explore what happens when an album is deleted while photos are still processing (album-level cascade behavior is unspecified). Test the /health endpoint under load to confirm the 100ms SLA holds during concurrent photo uploads. Investigate whether photo_ids are predictable or sequential (potential enumeration risk even without auth). Finally, confirm that LIST /albums scales gracefully and does not leak deleted albums or photos.

## Out of Scope

(The drafter does not infer Out-of-Scope items. The TA should add any explicit exclusions during review.)

---

*Drafter notes for the TA reviewer: The drafter is required by construction to produce sections for race / async / auth / edge. Categories marked N/A include the drafter's stated justification. Verify the justification before accepting; chaos-engineering value is highest in categories the drafter chose to populate.*