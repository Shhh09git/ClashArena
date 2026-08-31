"""
ClashArena - DISTRIBUTED architecture
--------------------------------------
ingestion-service: TRANSFORMER + TESTER steps of the kata's
producer -> transformer -> tester -> consumer result pipeline.

- Transformer: reads raw events from the `raw-results` Redis Stream and
  normalises them into a canonical "match result" shape.
- Tester: validates the event (winner must be one of the two players) and
  de-duplicates by event_id (a Redis SET of seen ids) so a result can
  never be double-counted even if the producer retries.
- Valid, deduplicated events are published to the `validated-results`
  stream for leaderboard-service to consume.

Runs a background worker thread so this can be scaled horizontally
(docker compose up --scale ingestion-service=3) while a Redis consumer
group guarantees each raw event is only fully processed once.
"""

import os
import json
import time
import threading

import redis
from flask import Flask, jsonify

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CONSUMER_NAME = os.environ.get("HOSTNAME", "ingestion-worker")
GROUP = "ingestion-group"
STREAM_IN = "raw-results"
STREAM_OUT = "validated-results"
DEDUPE_SET = "seen-event-ids"

app = Flask(__name__)
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

_stats = {"processed": 0, "rejected": 0, "duplicates": 0}


def ensure_group():
    try:
        r.xgroup_create(STREAM_IN, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def transform(raw_payload: dict) -> dict:
    """Normalise the producer's payload into the canonical shape."""
    return {
        "event_id": raw_payload["event_id"],
        "match_id": raw_payload["match_id"],
        "tournament_id": raw_payload["tournament_id"],
        "winner_id": raw_payload["winner_id"],
        "loser_id": (
            raw_payload["player1_id"]
            if raw_payload["winner_id"] == raw_payload["player2_id"]
            else raw_payload["player2_id"]
        ),
        "processed_at": time.time(),
    }


def test_valid(raw_payload: dict) -> bool:
    """Validation: winner must be one of the two registered players."""
    winner = raw_payload.get("winner_id")
    return winner in (raw_payload.get("player1_id"), raw_payload.get("player2_id"))


def worker_loop():
    ensure_group()
    while True:
        try:
            resp = r.xreadgroup(GROUP, CONSUMER_NAME, {STREAM_IN: ">"}, count=10, block=5000)
        except Exception as exc:  # keep the worker alive across transient Redis hiccups
            print("ingestion worker error:", exc)
            time.sleep(2)
            continue

        if not resp:
            continue

        for _stream, messages in resp:
            for msg_id, fields in messages:
                raw = json.loads(fields["payload"])
                event_id = raw["event_id"]

                # TESTER: de-duplicate (exactly-once guarantee)
                if not r.sadd(DEDUPE_SET, event_id):
                    _stats["duplicates"] += 1
                    r.xack(STREAM_IN, GROUP, msg_id)
                    continue

                # TESTER: validate
                if not test_valid(raw):
                    _stats["rejected"] += 1
                    r.xack(STREAM_IN, GROUP, msg_id)
                    continue

                # TRANSFORMER
                clean = transform(raw)
                r.xadd(STREAM_OUT, {"payload": json.dumps(clean)})
                _stats["processed"] += 1
                r.xack(STREAM_IN, GROUP, msg_id)


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "ingestion-service", "stats": _stats})


threading.Thread(target=worker_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8003, debug=False)
