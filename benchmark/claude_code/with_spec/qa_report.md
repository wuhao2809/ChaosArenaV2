---

QA Evaluation Report — album_store

Target: http://naive-1570828662.us-west-2.elb.amazonaws.com

---

R1: Concurrent per-album seq assignment

R1: FAILED (confidence: HIGH)

How tested: 20 concurrent POST /albums/:album_id/photos requests fired simultaneously via ThreadPoolExecutor(max_workers=20) with a
threading.Barrier. Then polled all 20 resulting photo_ids via GET to look for seq values.

Evidence:
All 20 returned 202 Accepted. Response sample:
{"photo_id":"9e103c3c-d645-48ae-adc9-0315541e939f","status":"processing"}
{"photo_id":"cc54303b-6499-4c49-9ff4-7e0892080839","status":"processing"}

GET poll on all 20 photos — seq field is null/absent across all:
Poll: status=completed, seq=None (repeated for all 20)

Spec required vs. observed: The spec requires all 20 202 responses contain distinct positive integer seq values forming a contiguous
range N+1 through N+20. The seq field is entirely absent from both the 202 response bodies and from subsequent GET responses. No seq
values can be verified.

---

R2: Concurrent album upsert idempotency

R2: PASSED (confidence: HIGH)

How tested: 20 concurrent PUT /albums/qa-r2-eval-{ts} with identical bodies via ThreadPoolExecutor(max_workers=20).

Evidence:
All 20 returned HTTP 200
Status counts: [200]
GET /albums: exactly 1 record for the album_id
GET /albums/qa-r2-eval-1780468793: 200
{"album_id":"qa-r2-eval-1780468793","title":"Concurrent Album","description":"test","owner":"qa@test.com"}

---

R3: Delete-during-processing worker resurrection

R3: PASSED (confidence: HIGH)

How tested: Uploaded a photo, immediately issued DELETE before background worker could complete, then polled GET every 500ms for 10
seconds.

Evidence:
POST → 202 {"photo_id":"98b8e90c-82f6-4a09-ba4a-26b19d609844","status":"processing"}
DELETE immediately → 200
GET immediately → 404
[polled 10s every 500ms — all returned 404, no resurrection observed]

---

R4: Concurrent deletes of the same photo

R4: FAILED (confidence: HIGH)

How tested: Uploaded a photo and waited for completed. Then fired two concurrent DELETE requests using threading.Barrier(2) to
synchronize simultaneous dispatch.

Evidence:
Both DELETE responses: 200 {"status":"deleted"}
Results: [(200, '{"status":"deleted"}\n'), (200, '{"status":"deleted"}\n')]
No 5xx observed.

Spec required vs. observed: Spec requires exactly one 200/204 and exactly one 404. Both concurrent deletes returned 200. The service does
not detect the double-delete race.

---

R5: Photo processing completes within 30-second deadline

R5: FAILED (confidence: HIGH)

How tested: Uploaded a photo, polled GET every 500ms for up to 35 seconds, inspected status and seq at each poll.

Evidence:
POST → 202 {"photo_id":"a8ee8041-798d-4387-a45e-a62d7a09bc50","status":"processing"}
Poll 0: status=processing, seq=None
Poll 1: status=completed, seq=None
[No status regression observed]

Spec required vs. observed: Processing completes promptly (PASS). No status regression (PASS). However, the spec requires "the seq field
is present and unchanged at every poll." The seq field is null/absent at every poll, including on the final completed record. seq is
never assigned.

---

R6: Backing URL is live at the instant status flips to completed

R6: PASSED (confidence: HIGH)

How tested: Polled until completed, immediately fetched the url field within the same loop iteration (<1s).

Evidence:
GET photo → 200, status=completed
url = https://naive-photos-008209411721.s3.us-west-2.amazonaws.com/albums/.../photos/...?X-Amz-...
GET url → HTTP 200

---

R7: Metadata and file both gone within 5 seconds of DELETE

R7: PASSED (confidence: HIGH)

How tested: Captured URL from completed photo, issued DELETE, waited exactly 5 seconds, then checked both GET metadata and the backing
URL.

Evidence:
Photo status: completed
Backing URL fetched before delete → 200
DELETE → 200
[sleep 5s]
GET /albums/{a7}/photos/{pid} → 404
GET backing URL → 403
Elapsed: 5.2s

---

R8: seq field present synchronously in the 202 response body

R8: FAILED (confidence: HIGH)

How tested: Issued POST /albums/:album_id/photos and inspected the raw 202 response body directly.

Evidence:
HTTP/1.1 202 Accepted
{"photo_id":"942c99ec-356a-4480-9704-de3d857fe6cc","status":"processing"}

Spec required vs. observed: Spec requires seq to be a positive integer in the 202 body. The field is completely absent. Confirmed with
verbose curl and Python requests; additionally, GET responses also show seq=null at every point.

---

R9: GET /albums reflects all albums immediately after concurrent creates

R9: PASSED (confidence: HIGH)

How tested: 50 concurrent PUT /albums/{unique_id} requests, then immediately GET /albums and checked for all 50.

Evidence:
All 50 returned 200/201
GET /albums → all 50 album_ids present
Missing: 0 of 50

---

R10: Cross-album photo read — resource scoping

R10: PASSED (confidence: HIGH)

How tested: Uploaded photo to album A (waited for completed), then GET /albums/{B}/photos/{pid}.

Evidence:
GET /albums/{B}/photos/{pid_from_A} → 404 {"error":"not found"}
GET /albums/{A}/photos/{pid} → 200 (photo intact)

---

R11: Cross-album photo delete — resource scoping

R11: FAILED (confidence: HIGH)

How tested: Uploaded photo to album A (waited for completed), then DELETE /albums/{B}/photos/{pid} where B is a different existing album.

Evidence:
DELETE /albums/{B}/photos/{pid_from_A} → 200 {"status":"deleted"}
GET /albums/{A}/photos/{pid} → 200 (photo still exists in album A)

Spec required vs. observed: Spec requires DELETE /albums/B/photos/P to return 404 since P belongs to A, not B. The service returned 200
(claiming a successful delete), though the actual photo record was not deleted. This is a double bug: wrong status code AND the response
body is misleading.

---

R12: Photo upload with missing or empty photo field

R12: FAILED (confidence: HIGH)

How tested: Three variants tested against an existing album:

1. POST with application/x-www-form-urlencoded missing the photo field
2. POST with multipart photo field containing a zero-byte file
3. POST with multipart using wrong field name (notphoto)

Evidence:
Missing photo field (wrong content-type): 400 {"error":"bad multipart form"}
Zero-byte photo (multipart): 202 {"photo_id":"e9d4261e-...","status":"processing"}
→ Subsequently: status=completed (zero-byte file uploaded successfully)
Wrong field name: 400 {"error":"missing photo field"}

Spec required vs. observed: A zero-byte photo field must return 400. The service accepted it with 202 and processed it to completed.

---

R13: Photo upload to non-existent album

R13: FAILED (confidence: HIGH)

How tested: POST /albums/nonexist-album-{ts}/photos with a valid 10KB JPEG, where the album was never created.

Evidence:
POST /albums/nonexist-album-1780468793/photos → 202
{"photo_id":"4305ff0e-5a54-4570-ac74-7518a6e862f2","status":"processing"}

Subsequent GET /albums/nonexist-album-1780468793/photos/{pid}:
Poll 0: 200 {"album_id":"nonexist-album-1780468793","photo_id":"4305ff0e...","status":"processing"}
Poll 1: 200 ... status=processing
[persists; album auto-created as a side-effect]

Spec required vs. observed: Spec requires 404 with no orphan photo record created. The service returned 202 and created both a photo
record and implicitly created the album as a dangling reference.

---

R14: Oversize photo upload

R14: PASSED (confidence: HIGH)

How tested: Generated 55MB random binary, uploaded as image/jpeg multipart.

Evidence:
POST → 202 {"photo_id":"eba822ff-...","status":"processing"} (took 4.0s)
Later GET → {"status":"completed", "url":"..."}

Service handles large files and completes processing successfully.

---

R15: Wrong Content-Type for photo upload

R15: PASSED (confidence: HIGH)

How tested: POST /albums/:album_id/photos with Content-Type: application/json and JSON body.

Evidence:
POST (JSON body) → 400 {"error":"bad multipart form"}

---

R16: PUT with missing or null required fields

R16: FAILED (confidence: HIGH)

How tested: Three variants against a valid album_id:

1. PUT with body missing title field
2. PUT with "owner": null
3. PUT with empty JSON object {}

Evidence:
Missing title: 200 {"album_id":"qa-r16-...","title":"","description":"test","owner":"qa@test.com"}
Null owner: 200 {"album_id":"qa-r16-...","title":"Test","description":"test","owner":""}
Empty body: 200 {"album_id":"qa-r16-...","title":"","description":"","owner":""}

Spec required vs. observed: Spec requires 400 for all three cases. The service accepts all of them with 200 and stores partial/empty
records.

---

R17: album_id mismatch between URL path and request body

R17: PASSED (confidence: HIGH)

How tested: PUT /albums/{X} with body {"album_id": "{Y}", "title": "...", "owner": "..."}.

Evidence:
PUT /albums/qa-r17-x-{ts} body={"album_id":"qa-r17-y-{ts}",...} → 200
{"album_id":"qa-r17-x-1780468793","title":"Mismatch",...}
GET /albums/qa-r17-x-{ts} → 200 (correct record under X)
GET /albums/qa-r17-y-{ts} → 404 (Y was never created)

URL path value used authoritatively; body album_id silently ignored.

---

R18: Second DELETE on already-deleted photo

R18: FAILED (confidence: HIGH)

How tested: Uploaded photo, waited for completed, issued DELETE three times sequentially.

Evidence:
First DELETE: 200 {"status":"deleted"}
Second DELETE: 200 {"status":"deleted"}
Third DELETE: 200 {"status":"deleted"}

Spec required vs. observed: Spec requires the second DELETE to return 404. The service returns 200 for every DELETE call regardless of
whether the resource exists. DELETE is effectively idempotent-but-wrong (always 200 instead of 404 on subsequent calls).

---

Additional Bugs Found During Open Exploration

Bug A — seq field completely missing system-wide. The seq field is never assigned: absent from 202 responses, absent from GET photo
responses (always null), and absent after completed. This is a systemic implementation gap, not a race condition.

Bug B — Non-existent album auto-creation on photo upload. When POST /albums/{nonexistent}/photos is called, the service creates the album
implicitly as a side-effect, returning 202 and persisting both album and photo records. This violates referential integrity.

Bug C — Zero-byte photo silently accepted and processed. A zero-byte file passes through the upload pipeline and reaches completed
status. No validation at the boundary.

Bug D — DELETE always returns 200. All DELETE requests return {"status":"deleted"} regardless of whether the resource ever existed, was
already deleted, or belongs to a different album. This masks double-deletes (R4) and repeated deletes (R18).

Bug E — Cross-album DELETE returns 200 with misleading body. DELETE /albums/B/photos/P (P belongs to A) returns 200 {"status":"deleted"}
but doesn't actually delete P from A. The response is both wrong status code and a false claim.

Bug F — PUT accepts empty/null required fields. No field validation on title or owner; empty string and null are silently accepted,
stored, and returned.

Bug G — GET /albums/{nonexistent} correctly returns 404 {"error":"not found"} (correct behavior, noted for completeness).

Bug H — Pre-signed URL TTL is 3600s. The S3 URLs expire after 1 hour. For long-running test suites this may cause URL checks to fail on
older photos.

---

Final Verdict

R1: FAILED (confidence: HIGH)
R2: PASSED (confidence: HIGH)
R3: PASSED (confidence: HIGH)
R4: FAILED (confidence: HIGH)
R5: FAILED (confidence: HIGH)
R6: PASSED (confidence: HIGH)
R7: PASSED (confidence: HIGH)
R8: FAILED (confidence: HIGH)
R9: PASSED (confidence: HIGH)
R10: PASSED (confidence: HIGH)
R11: FAILED (confidence: HIGH)
R12: FAILED (confidence: HIGH)
R13: FAILED (confidence: HIGH)
R14: PASSED (confidence: HIGH)
R15: PASSED (confidence: HIGH)
R16: FAILED (confidence: HIGH)
R17: PASSED (confidence: HIGH)
R18: FAILED (confidence: HIGH)

Overall verdict: FAIL

Reasoning: The service fails 9 of 18 required tests and has 8 distinct bugs. The most severe is the complete absence of the seq field
throughout the entire system — it is never present in 202 responses (R8 fail), never present in GET responses (R5 fail), and therefore
the concurrent seq-uniqueness guarantee in R1 is entirely untestable. Compounded by: DELETE always returns 200 regardless of resource
state (causes R4 and R18 failures); cross-album DELETE leaks 200 responses without enforcing album scoping (R11); photo upload to
non-existent albums silently creates orphan records (R13); zero-byte photos are accepted (R12); and PUT performs no validation of
required fields (R16). The basic async flow (R2, R3, R5 timing, R6, R7, R9, R10) works correctly, but the seq contract, delete semantics,
input validation, and resource scoping are all broken.
