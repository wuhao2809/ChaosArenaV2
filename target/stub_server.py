"""Stub target service for ChaosArena MVP.

A minimal 4-endpoint REST service that the agent evaluates. Has two modes:
  - default (good): all endpoints behave correctly
  - BUG_MODE=race: POST /tasks fails ~30% of the time, simulating a
    concurrency-related fault. The actual failure here is a synthetic 500,
    standing in for the observable effect of a real race condition
    (lost writes, broken locks) in a production system. The agent
    detects it via parallel_n's error_rate signal — equivalent to how
    it would detect a genuine race in a student-built service.
"""

import os
import random
import threading
import uuid

from flask import Flask, jsonify, request

app = Flask(__name__)

BUG_MODE = os.environ.get("BUG_MODE", "").lower()
RACE_FAILURE_RATE = 0.30  # tunable; chosen so parallel_n(20) reliably surfaces errors

tasks: dict[str, dict] = {}
lock = threading.Lock()


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/tasks", methods=["POST"])
def create_task():
    if BUG_MODE == "race" and random.random() < RACE_FAILURE_RATE:
        return jsonify({"error": "internal server error"}), 500

    data = request.get_json(silent=True) or {}
    title = data.get("title", "")
    if not title:
        return jsonify({"error": "title required"}), 400

    task_id = uuid.uuid4().hex[:8]
    with lock:
        tasks[task_id] = {"task_id": task_id, "title": title}
    return jsonify({"task_id": task_id, "title": title}), 201


@app.route("/tasks/<task_id>", methods=["GET"])
def get_task(task_id):
    with lock:
        task = tasks.get(task_id)
    if task is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(task), 200


@app.route("/tasks", methods=["GET"])
def list_tasks():
    with lock:
        items = list(tasks.values())
    return jsonify(items), 200


if __name__ == "__main__":
    mode_label = f"BUG_MODE={BUG_MODE}" if BUG_MODE else "good"
    print(f"[stub] starting on :8080 (mode: {mode_label})")
    app.run(host="0.0.0.0", port=8080, threaded=True)
