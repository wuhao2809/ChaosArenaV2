# QA Test Report — Album Store Service Tokens: 15k

**Target:** `http://naive-1570828662.us-west-2.elb.amazonaws.com`  
**Spec:** v1-album-store  
**Date:** 2026-06-02

---

## Bugs Found

### BUG-1 (Critical) — `seq` field missing from POST /albums/:id/photos response

**Description:** The 202 response from the photo upload endpoint omits the `seq` field entirely. The spec mandates it must be present and assigned synchronously in the POST handler.

**Request:**

```
POST /albums/test-album-1780462985/photos
Content-Type: multipart/form-data
[binary PNG body]
```

**Actual response (202):**

```json
{ "photo_id": "b86eb552-2105-4f95-aa9e-9038526d9d6c", "status": "processing" }
```

**Expected response (202):**

```json
{
  "photo_id": "b86eb552-2105-4f95-aa9e-9038526d9d6c",
  "seq": 1,
  "status": "processing"
}
```

**Spec violation (§2.5):**

> The 202 response body must include `"seq"` — a positive integer, monotonically increasing and unique within each album, assigned **synchronously in the POST handler**.

**Impact:** Scenario S10 (Per-Album Photo Sequence, 15 pts) will score 0. This also breaks any client that depends on the `seq` from the accept response.

---

### BUG-2 (Critical) — `seq` field missing from GET /albums/:id/photos/:photo_id response

**Description:** The photo status endpoint never returns the `seq` field, at any lifecycle stage (`processing` or `completed`).

**Request:**

```
GET /albums/test-album-1780462985/photos/b86eb552-2105-4f95-aa9e-9038526d9d6c
```

**Actual response (200, completed):**

```json
{
  "album_id": "test-album-1780462985",
  "photo_id": "b86eb552-2105-4f95-aa9e-9038526d9d6c",
  "status": "completed",
  "url": "https://naive-photos-008209411721.s3.us-west-2.amazonaws.com/..."
}
```

**Expected response:**

```json
{
  "photo_id": "...",
  "album_id": "...",
  "seq": 1,
  "status": "completed",
  "url": "https://..."
}
```

**Spec violation (§2.6):**

> `seq` must also be present in `GET /albums/:album_id/photos/:photo_id` at **all** lifecycle stages.

**Impact:** Reinforces S10 failure. Verified across 15+ photo uploads in both sequential and concurrent tests — `seq` is consistently absent.

---

### BUG-3 (Minor) — DELETE returns 200 for non-existent photo/album

**Description:** Deleting a photo that does not exist (wrong photo_id or non-existent album) returns 200 with `{"status":"deleted"}` instead of 404.

**Request:**

```
DELETE /albums/test-album/photos/00000000-0000-0000-0000-000000000000
```

**Actual response:**

```json
HTTP/1.1 200 OK
{"status":"deleted"}
```

**Request (non-existent album):**

```
DELETE /albums/nonexistent-album/photos/00000000-0000-0000-0000-000000000000
```

**Actual response:**

```json
HTTP/1.1 200 OK
{"status":"deleted"}
```

**Spec violation:** The spec says "After a successful DELETE" the photo must not be found — implying a deletion only succeeds when something was actually deleted. Returning success for a no-op DELETE misrepresents the operation.

**Impact:** Low direct scoring impact (spec doesn't explicitly require a 404 for phantom deletes), but misleads clients and may mask real errors.

---

## Correct Behaviors Verified

| #   | What was tested                                                 | Observed result                                                                            |
| --- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 1   | `GET /health` status code and body                              | 200, `{"status":"ok"}`, `Content-Type: application/json` ✅                                |
| 2   | `PUT /albums/:id` creates album with all fields                 | 200, response echoes all 4 fields ✅                                                       |
| 3   | `GET /albums/:id` retrieves stored album                        | 200, all 4 fields match exactly ✅                                                         |
| 4   | `GET /albums/nonexistent` returns 404                           | `{"error":"not found"}` ✅                                                                 |
| 5   | `GET /albums` returns array format with `album_id` on each item | Bare JSON array, all items have `album_id` ✅                                              |
| 6   | `GET /albums` list grows as albums are created                  | Created 5 concurrent albums; all appeared ✅                                               |
| 7   | `POST /albums/:id/photos` returns 202 immediately               | 170ms accept latency even for 1.4 MB image ✅                                              |
| 8   | Photo transitions to `completed` within 30s                     | Always completed in < 5s in tests ✅                                                       |
| 9   | Completed photo response includes `url`                         | Present, points to S3 pre-signed URL ✅                                                    |
| 10  | Photo URL returns 200 when fetched                              | S3 URL returns 200 before delete ✅                                                        |
| 11  | `DELETE` completes within 5 seconds                             | ~154ms observed ✅                                                                         |
| 12  | `GET` photo after delete returns 404                            | `{"error":"not found"}` ✅                                                                 |
| 13  | Photo file URL no longer returns 200 after delete               | Returns 403 (object removed from S3) ✅                                                    |
| 14  | `PUT` idempotency — same ID twice = one record                  | List shows exactly one entry; second PUT updates fields ✅                                 |
| 15  | Round-trip data integrity (PUT fields = GET fields)             | All 4 fields preserved exactly ✅                                                          |
| 16  | Concurrent `PUT /albums` — no lost writes                       | 5 concurrent creates all visible in list ✅                                                |
| 17  | Delete photo while still in `processing` state                  | DELETE 200, subsequent GET returns 404 ✅                                                  |
| 18  | Delete middle photo — adjacent photos unaffected                | Photos 1 and 3 remained accessible after deleting photo 2 ✅                               |
| 19  | Large photo (1.4 MB) upload accepted and processed              | 202 in 170ms, completed in ~2s ✅                                                          |
| 20  | `GET /albums/nonexistent-photo` returns 404                     | `{"error":"not found"}` ✅                                                                 |
| 21  | `Content-Type: application/json` on all endpoints               | Verified on GET /health, GET /albums/:id, PUT /albums/:id, POST /photos, DELETE /photos ✅ |

---

## Overall Verdict: FAIL

The service correctly implements all core album CRUD operations, async photo upload with proper 202 acceptance, photo deletion with file cleanup, data integrity, and idempotency. These cover scenarios S1–S9 cleanly.

However, the `seq` field — required by the spec in **both** the `POST /photos` 202 response and the `GET /photos/:id` response at every lifecycle stage — is completely absent from all responses tested. This is a single but systematic implementation gap that will cause **S10 (Per-Album Photo Sequence, 15 pts)** to score zero. Because S10 is non-critical it does not block the load tests, so the service should still earn partial credit, but it cannot achieve full correctness marks under the current implementation.
