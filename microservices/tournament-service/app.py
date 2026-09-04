"""
ClashArena - DISTRIBUTED architecture
--------------------------------------
tournament-service: owns tournaments, registrations and matches.
Verifies JWTs issued by identity-service locally (shared SECRET_KEY,
no network call needed to validate a token -> keeps this service fast
and independently deployable).

When an organizer reports a match result, this service does NOT update
ratings itself. It publishes a "raw result" event onto the Redis stream
`raw-results`. This is the PRODUCER step of the kata's
producer -> transformer -> tester -> consumer result-ingestion pipeline.
"""

import os
import math
import json
import time
import uuid
import functools
import datetime

import jwt
import redis
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///tournaments.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

db = SQLAlchemy(app)
r = redis.Redis.from_url(REDIS_URL, decode_responses=True)


class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    game = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), default="registration")
    organizer_id = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Registration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"))
    user_id = db.Column(db.Integer)


class Match(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"))
    round = db.Column(db.Integer, nullable=False)
    slot = db.Column(db.Integer, nullable=False)
    player1_id = db.Column(db.Integer, nullable=True)
    player2_id = db.Column(db.Integer, nullable=True)
    winner_id = db.Column(db.Integer, nullable=True)
    status = db.Column(db.String(20), default="pending")
    score = db.Column(db.String(20), nullable=True)


def require_auth(roles=None):
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "missing bearer token"}), 401
            token = auth.split(" ", 1)[1]
            try:
                data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
            except jwt.PyJWTError:
                return jsonify({"error": "invalid or expired token"}), 401
            if roles and data.get("role") not in roles:
                return jsonify({"error": "forbidden: requires role in %s" % roles}), 403
            request.user = data
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def generate_bracket(tournament_id, player_ids):
    n = len(player_ids)
    size = 2 ** math.ceil(math.log2(max(n, 2)))
    padded = player_ids + [None] * (size - n)
    for slot in range(0, size, 2):
        p1, p2 = padded[slot], padded[slot + 1]
        if p2 is None and p1 is not None:
            status, winner = "bye", p1
        elif p1 is None and p2 is not None:
            status, winner = "bye", p2
        else:
            status, winner = "pending", None
        db.session.add(
            Match(
                tournament_id=tournament_id,
                round=1,
                slot=slot // 2,
                player1_id=p1,
                player2_id=p2,
                winner_id=winner,
                status=status,
            )
        )
    db.session.commit()


def advance_round_if_complete(tournament_id, round_num):
    matches = (
        Match.query.filter_by(tournament_id=tournament_id, round=round_num)
        .order_by(Match.slot)
        .all()
    )
    if not matches or any(m.winner_id is None for m in matches):
        return
    if len(matches) == 1:
        t = Tournament.query.get(tournament_id)
        t.status = "finished"
        db.session.commit()
        return
    winners = [m.winner_id for m in matches]
    next_round = round_num + 1
    for slot in range(0, len(winners), 2):
        db.session.add(
            Match(
                tournament_id=tournament_id,
                round=next_round,
                slot=slot // 2,
                player1_id=winners[slot],
                player2_id=winners[slot + 1],
                status="pending",
            )
        )
    db.session.commit()


@app.post("/api/tournaments")
@require_auth(roles=["organizer", "admin"])
def create_tournament():
    body = request.get_json(force=True)
    t = Tournament(name=body["name"], game=body["game"], organizer_id=request.user["sub"])
    db.session.add(t)
    db.session.commit()
    return jsonify({"id": t.id, "name": t.name, "status": t.status}), 201


@app.post("/api/tournaments/<int:tid>/register")
@require_auth()
def register_for_tournament(tid):
    t = Tournament.query.get_or_404(tid)
    if t.status != "registration":
        return jsonify({"error": "registration closed"}), 400
    db.session.add(Registration(tournament_id=tid, user_id=request.user["sub"]))
    db.session.commit()
    return jsonify({"status": "registered"}), 201


@app.post("/api/tournaments/<int:tid>/start")
@require_auth(roles=["organizer", "admin"])
def start_tournament(tid):
    t = Tournament.query.get_or_404(tid)
    player_ids = [reg.user_id for reg in Registration.query.filter_by(tournament_id=tid)]
    if len(player_ids) < 2:
        return jsonify({"error": "need at least 2 registered players"}), 400
    generate_bracket(tid, player_ids)
    t.status = "in_progress"
    db.session.commit()
    return jsonify({"status": "in_progress", "players": len(player_ids)})


@app.get("/api/tournaments/<int:tid>/bracket")
def get_bracket(tid):
    matches = Match.query.filter_by(tournament_id=tid).order_by(Match.round, Match.slot).all()
    return jsonify(
        [
            {
                "id": m.id, "round": m.round, "slot": m.slot,
                "player1_id": m.player1_id, "player2_id": m.player2_id,
                "winner_id": m.winner_id, "status": m.status, "score": m.score,
            }
            for m in matches
        ]
    )


@app.post("/api/matches/<int:mid>/result")
@require_auth(roles=["organizer", "admin"])
def report_result(mid):
    body = request.get_json(force=True)
    match = Match.query.get_or_404(mid)
    if match.status == "reported":
        return jsonify({"error": "result already recorded (no double-count)"}), 409
    winner_id = body["winner_id"]
    if winner_id not in (match.player1_id, match.player2_id):
        return jsonify({"error": "winner must be one of the two players"}), 400

    match.winner_id = winner_id
    match.score = body.get("score")
    match.status = "reported"
    db.session.commit()
    advance_round_if_complete(match.tournament_id, match.round)

    # --- PRODUCER step of the result-ingestion pipeline ---
    event = {
        "event_id": str(uuid.uuid4()),   # used downstream to de-duplicate
        "match_id": match.id,
        "tournament_id": match.tournament_id,
        "player1_id": match.player1_id,
        "player2_id": match.player2_id,
        "winner_id": winner_id,
        "reported_at": time.time(),
    }
    r.xadd("raw-results", {"payload": json.dumps(event)})

    return jsonify({"status": "reported", "winner_id": winner_id, "event_id": event["event_id"]})


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "tournament-service"})


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8002, debug=False)