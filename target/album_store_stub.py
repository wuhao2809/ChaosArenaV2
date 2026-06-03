"""v2 stub: an Album Store target service for ChaosArena evaluation.

Mirrors the v1 ChaosArena album-store schema (continuity with the v1 dataset
narrative). Designed as a single-file Flask app with controlled BUG_MODE
variants — each mode injects exactly one documented LLM-coauthored failure
pattern, so that tool primitives can be validated end-to-end and Eval Axis 1
(precision per R-category) has ground truth by construction.

Endpoints
---------
GET    /health                         → 200 {"status":"ok"}
PUT    /albums/:album_id               → 200/201 with album body
GET    /albums/:album_id               → 200 album / 404
GET    /albums                         → 200 array of albums
POST   /albums/:album_id/photos        → 202 {"photo_id","status":"processing"}
GET    /albums/:album_id/photos/:pid   → 200 with status (processing|completed|failed)
DELETE /albums/:album_id/photos/:pid   → 200 / 204

BUG_MODE variants (env var; one active at a time)
-------------------------------------------------
(unset)            All endpoints correct. Baseline.
race               Concurrent PUT /albums/:id silently drops ~30% of the writes
                   (last-writer-wins NOT enforced — one of two simultaneous
                   PUTs may produce a stale record).
async_orphan       DELETE /albums/:id/photos/:pid while photo is still
                   processing → the async worker still writes "completed" 3s
                   later, leaving an orphan record visible via GET.
oversize_500       POST /albums/:id/photos with body > 1MB returns 500 instead
                   of 413 (graceful client error).
read_after_delete  After DELETE, GET still returns the record ~30% of the
                   time for the first 5 seconds (stale read window).
auth_bypass        (Reserved for Week 3.) Requires header `X-User: <id>`;
                   any token returns ANY user's data — no authorization check.

Notes
-----
- "Race" here is a synthetic last-write-loss: simulating the *observable*
  effect of an unprotected critical section, not a real Python data race
  (CPython GIL makes that hard to demonstrate reliably). The agent detects
  it via parallel_n / barrier_concurrent observing missing records on
  read-back, which is identical to how it would detect a real race in
  student code.
- All async work is simulated via background threads with time.sleep, NOT
  a real worker queue. Sufficient for ChaosArena to exercise the temporal
  primitives.
"""

import os
import random
import threading
import time
import uuid
from typing import Any

from flask import Flask, jsonify, request


app = Flask(__name__)

BUG_MODE = os.environ.get("BUG_MODE", "").lower()


# ----------------------------------------------------------------------
# In-memory state
# ----------------------------------------------------------------------

albums: dict[str, dict] = {}
photos: dict[str, dict] = {}              # photo_id -> {album_id, status, ...}
deleted_photos: dict[str, float] = {}     # photo_id -> deletion timestamp (for read_after_delete + async_orphan)
deleted_photo_cache: dict[str, dict] = {} # photo_id -> stale record (used by read_after_delete only)
state_lock = threading.Lock()


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _maybe_lose_write() -> bool:
    """For BUG_MODE=race: with 30% probability, silently drop this write."""
    return BUG_MODE == "race" and random.random() < 0.30


def _stale_read_window_active(photo_id: str) -> bool:
    """For BUG_MODE=read_after_delete: within 5s of deletion, 30% chance to
    return the deleted record anyway."""
    if BUG_MODE != "read_after_delete":
        return False
    deleted_at = deleted_photos.get(photo_id)
    if deleted_at is None:
        return False
    if time.time() - deleted_at > 5.0:
        return False
    return random.random() < 0.30


def _process_photo_async(photo_id: str) -> None:
    """Simulate async worker: 3s later mark photo as completed.

    For BUG_MODE=async_orphan, deliberately ignore deletion: even if the
    photo was deleted while processing, write 'completed' anyway (orphan).
    """
    time.sleep(3.0)
    with state_lock:
        if BUG_MODE == "async_orphan":
            # Buggy worker: doesn't re-check existence before writing.
            photos[photo_id] = {
                **photos.get(photo_id, {"photo_id": photo_id}),
                "status": "completed",
                "url": f"https://stub/photos/{photo_id}.jpg",
            }
        else:
            # Correct worker: only update if still present.
            if photo_id in photos:
                photos[photo_id] = {
                    **photos[photo_id],
                    "status": "completed",
                    "url": f"https://stub/photos/{photo_id}.jpg",
                }


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health() -> Any:
    return jsonify({"status": "ok"}), 200


@app.route("/albums/<album_id>", methods=["PUT"])
def put_album(album_id: str) -> Any:
    data = request.get_json(silent=True) or {}
    title = data.get("title", "")
    description = data.get("description", "")
    owner = data.get("owner", "")

    if not title:
        return jsonify({"error": "title required"}), 400

    new_record = {
        "album_id": album_id,
        "title": title,
        "description": description,
        "owner": owner,
    }

    with state_lock:
        existed = album_id in albums
        if _maybe_lose_write():
            # Silently "succeed" without updating storage. Simulates the
            # observable effect of a lost write under concurrency.
            return jsonify(new_record), 200 if existed else 201
        albums[album_id] = new_record

    return jsonify(new_record), 200 if existed else 201


@app.route("/albums/<album_id>", methods=["GET"])
def get_album(album_id: str) -> Any:
    with state_lock:
        record = albums.get(album_id)
    if record is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(record), 200


@app.route("/albums", methods=["GET"])
def list_albums() -> Any:
    with state_lock:
        items = list(albums.values())
    return jsonify(items), 200


@app.route("/albums/<album_id>/photos", methods=["POST"])
def upload_photo(album_id: str) -> Any:
    # BUG_MODE=oversize_500: 500 instead of 413 for large bodies.
    raw_len = int(request.headers.get("Content-Length", 0) or 0)
    if raw_len > 1_000_000:
        if BUG_MODE == "oversize_500":
            return jsonify({"error": "internal server error"}), 500
        return jsonify({"error": "payload too large"}), 413

    with state_lock:
        if album_id not in albums:
            return jsonify({"error": "album not found"}), 404
        photo_id = uuid.uuid4().hex[:8]
        photos[photo_id] = {
            "photo_id": photo_id,
            "album_id": album_id,
            "status": "processing",
        }

    # Kick off async "processing".
    threading.Thread(target=_process_photo_async, args=(photo_id,), daemon=True).start()

    return jsonify({"photo_id": photo_id, "status": "processing"}), 202


@app.route("/albums/<album_id>/photos/<photo_id>", methods=["GET"])
def get_photo(album_id: str, photo_id: str) -> Any:
    # BUG_MODE=read_after_delete: 30% chance to return the deleted record
    # for the first 5s after deletion (served from the stale cache).
    if _stale_read_window_active(photo_id):
        cached = deleted_photo_cache.get(photo_id)
        if cached is not None and cached.get("album_id") == album_id:
            return jsonify(cached), 200

    with state_lock:
        record = photos.get(photo_id)

    if record is None or record.get("album_id") != album_id:
        return jsonify({"error": "not found"}), 404
    return jsonify(record), 200


@app.route("/albums/<album_id>/photos/<photo_id>", methods=["DELETE"])
def delete_photo(album_id: str, photo_id: str) -> Any:
    with state_lock:
        record = photos.get(photo_id)
        if record is None or record.get("album_id") != album_id:
            return jsonify({"error": "not found"}), 404

        if BUG_MODE == "async_orphan":
            # Keep record in `photos` so the async worker re-writes it 3s
            # later as an orphan.
            deleted_photos[photo_id] = time.time()
        elif BUG_MODE == "read_after_delete":
            # Hard-delete from primary store, but cache for the stale-read
            # window so GETs may serve stale 30% of the time within 5s.
            deleted_photo_cache[photo_id] = record
            deleted_photos[photo_id] = time.time()
            del photos[photo_id]
        else:
            del photos[photo_id]

    return ("", 204)


# ----------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------

if __name__ == "__main__":
    label = f"BUG_MODE={BUG_MODE}" if BUG_MODE else "default (no bugs)"
    print(f"[album_store_stub] starting on :8080  (mode: {label})")
    app.run(host="0.0.0.0", port=8080, threaded=True)
