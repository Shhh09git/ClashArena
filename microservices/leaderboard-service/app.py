"""
ClashArena - DISTRIBUTED architecture
--------------------------------------
leaderboard-service: CONSUMER step of the result pipeline. Reads only
already-validated, de-duplicated events from `validated-results` and
updates its OWN database (database-per-service). It never talks to
tournament-service's database directly -> the two can evolve, deploy
and scale independently, which is the whole point of doing this as
microservices instead of a monolith.
"""

import os
import json
import time
import threading

import redis
import requests
from flask import Flask, jsonify
from flask_sqlalchemy import SQLAlchemy

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
IDENTITY_URL = os.environ.get("IDENTITY_URL", "http://identity-service:8001")
CONSUMER_NAME = os.environ.get("HOSTNAME", "leaderboard-worker")
GROUP = "leaderboard-group"
STREAM_IN = "validated-results"

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///leaderboard.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


class RatingEntry(db.Model):
    user_id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=True)
    rating = db.Column(db.Integer, default=1000)
    wins = db.Column(db.Integer, default=0)
    losses = db.Column(db.Integer, default=0)


def resolve_username(user_id):
    try:
        resp = requests.get(f"{IDENTITY_URL}/api/users/{user_id}", timeout=2)
        if resp.ok:
            return resp.json().get("username")
    except requests.RequestException:
        pass
    return f"user-{user_id}"


def get_or_create(user_id):
    entry = RatingEntry.query.get(user_id)
    if entry is None:
        entry = RatingEntry(
            user_id=user_id,
            username=resolve_username(user_id),
            rating=1000,
            wins=0,
            losses=0,
        )
        db.session.add(entry)
        db.session.flush()
    return entry


def ensure_group():
    try:
        r.xgroup_create(STREAM_IN, GROUP, id="0", mkstream=True)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def worker_loop():
    with app.app_context():
        ensure_group()
        while True:
            try:
                resp = r.xreadgroup(GROUP, CONSUMER_NAME, {STREAM_IN: ">"}, count=10, block=5000)
            except Exception as exc:
                print("leaderboard worker error:", exc)
                time.sleep(2)
                continue
            if not resp:
                continue
            for _stream, messages in resp:
                for msg_id, fields in messages:
                    event = json.loads(fields["payload"])
                    winner = get_or_create(event["winner_id"])
                    winner.rating += 20
                    winner.wins += 1
                    if event.get("loser_id"):
                        loser = get_or_create(event["loser_id"])
                        loser.rating = max(0, loser.rating - 10)
                        loser.losses += 1
                    db.session.commit()
                    r.xack(STREAM_IN, GROUP, msg_id)


@app.get("/api/leaderboard")
def leaderboard():
    entries = RatingEntry.query.order_by(RatingEntry.rating.desc()).limit(50).all()
    return jsonify(
        [
            {"username": e.username, "rating": e.rating, "wins": e.wins, "losses": e.losses}
            for e in entries
        ]
    )


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "leaderboard-service"})


with app.app_context():
    db.create_all()

threading.Thread(target=worker_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8004, debug=False)
