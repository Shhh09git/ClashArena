"""
ClashArena - MONOLITHIC architecture
------------------------------------
One Flask application, one SQLite database, one transaction boundary.

Everything the distributed version spreads across five services lives here
as four clearly-marked sections of a single module:

    SECTION 1 - Identity    accounts, password hashing, JWT, RBAC
    SECTION 2 - Tournaments creation, registration, bracket generation
    SECTION 3 - Matches     result reporting and round advancement
    SECTION 4 - Leaderboard ratings

The architectural point of this version is SIMPLICITY. Reporting a result
updates the match row AND both players' ratings inside ONE database
transaction, so the leaderboard can never be stale relative to the bracket,
and "no double-count" is guaranteed by a uniqueness rule in the same
transaction rather than by a de-duplication step in a pipeline.

The price is that it scales and deploys as one unit: a spectator read burst
(kata U10/C1) forces you to scale the write path along with it, and any
change - even to the rating formula - redeploys the whole application.

Public API is identical to the distributed version and also served on port
8000, so one curl walkthrough exercises either architecture. In a larger
codebase each section below would become a Flask blueprint in its own
module; kept in one file here because being readable top-to-bottom in one
sitting IS the characteristic this architecture is meant to demonstrate.
"""

import os
import math
import functools
import datetime

import jwt
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///clasharena.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "12"))

VALID_ROLES = {"player", "organizer", "referee", "admin"}

WIN_DELTA = 20
LOSS_DELTA = 10
STARTING_RATING = 1000

db = SQLAlchemy(app)


# ---------------------------------------------------------------------------
# MODELS - one schema, shared by every section.
#
# This is the clearest single difference from the distributed version. Here
# rating/wins/losses are COLUMNS ON THE USER, so a leaderboard read is one
# indexed query over one table. In microservices/ the same data lives in
# leaderboard-service's own RatingEntry table, populated from an event
# stream, precisely because it must be scalable and deployable on its own.
# ---------------------------------------------------------------------------


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="player")
    rating = db.Column(db.Integer, nullable=False, default=STARTING_RATING)
    wins = db.Column(db.Integer, nullable=False, default=0)
    losses = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def set_password(self, password):
        # pbkdf2:sha256 pinned explicitly rather than left to Werkzeug's
        # default (scrypt in 3.x), which is memory-hard and slow in a small
        # container. Being explicit also means the stored hash format does
        # not change silently on a Werkzeug upgrade.
        self.password_hash = generate_password_hash(
            password, method="pbkdf2:sha256", salt_length=16
        )

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Tournament(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    game = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(20), default="registration")
    organizer_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)


class Registration(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournament.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    __table_args__ = (
        # Integrity, enforced by the database rather than by application
        # code: a player cannot occupy two bracket slots in one tournament.
        db.UniqueConstraint("tournament_id", "user_id", name="uq_one_entry_per_player"),
    )


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
    reported_by = db.Column(db.Integer, nullable=True)
    reported_at = db.Column(db.DateTime, nullable=True)


# ---------------------------------------------------------------------------
# SECTION 1 - IDENTITY: hashing, token issuing, RBAC
# ---------------------------------------------------------------------------


def issue_token(user):
    """Mint a signed, stateless JWT.

    Identical claim shape to the distributed version's identity-service, so
    a token from either architecture is structurally the same thing.
    `sub` is an INTEGER user id; PyJWT is pinned to 2.9.0 across the project
    because 2.10+ rejects a non-string `sub`.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    return jwt.encode(
        {
            "sub": user.id,
            "role": user.role,
            "name": user.username,
            "iat": now,
            "exp": now + datetime.timedelta(hours=TOKEN_TTL_HOURS),
        },
        SECRET_KEY,
        algorithm="HS256",
    )


def require_auth(roles=None):
    """Authentication + RBAC in one decorator.

    Authentication is the signature check: jwt.decode re-computes the HMAC
    over header.payload with SECRET_KEY and refuses anything that does not
    match, so a client cannot edit its own role claim. It also enforces
    `exp` and raises on an expired token.

    Authorisation is the role check that follows. Note the order: an invalid
    token is 401 (who are you?), a valid token without the right role is 403
    (I know who you are, you may not do this).

    `algorithms=["HS256"]` is not optional. Omitting it lets an attacker
    present a token with alg "none" and have it accepted unsigned.
    """

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


@app.post("/api/register")
def register():
    body = request.get_json(force=True, silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") or "player"

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if len(password) < 6:
        return jsonify({"error": "password must be at least 6 characters"}), 400
    if role not in VALID_ROLES:
        return jsonify({"error": "role must be one of %s" % sorted(VALID_ROLES)}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "username already taken"}), 409

    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return jsonify({"id": user.id, "username": user.username, "role": user.role}), 201


@app.post("/api/login")
def login():
    body = request.get_json(force=True, silent=True) or {}
    user = User.query.filter_by(username=(body.get("username") or "").strip()).first()
    # Same response for unknown user and wrong password - otherwise the
    # endpoint becomes a username enumeration oracle.
    if user is None or not user.check_password(body.get("password") or ""):
        return jsonify({"error": "invalid credentials"}), 401
    return jsonify({"token": issue_token(user), "role": user.role, "user_id": user.id})


@app.get("/api/users/<int:uid>")
def get_user(uid):
    user = User.query.get_or_404(uid)
    return jsonify({"id": user.id, "username": user.username, "role": user.role})


@app.get("/api/me")
@require_auth()
def me():
    return jsonify(
        {
            "user_id": request.user["sub"],
            "username": request.user.get("name"),
            "role": request.user["role"],
        }
    )


# ---------------------------------------------------------------------------
# SECTION 2 - TOURNAMENTS: creation, registration, bracket generation
# ---------------------------------------------------------------------------


def generate_bracket(tournament_id, player_ids):
    """Single-elimination bracket, padded up to the next power of two.

    Byes are resolved immediately: a player with no opponent advances, which
    is what makes an odd number of entrants work without a special case
    later in advance_round_if_complete.
    """
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


@app.post("/api/tournaments")
@require_auth(roles=["organizer", "admin"])
def create_tournament():
    body = request.get_json(force=True, silent=True) or {}
    if not body.get("name") or not body.get("game"):
        return jsonify({"error": "name and game are required"}), 400
    t = Tournament(
        name=body["name"], game=body["game"], organizer_id=request.user["sub"]
    )
    db.session.add(t)
    db.session.commit()
    return jsonify({"id": t.id, "name": t.name, "status": t.status}), 201


@app.post("/api/tournaments/<int:tid>/register")
@require_auth()
def register_for_tournament(tid):
    t = Tournament.query.get_or_404(tid)
    if t.status != "registration":
        return jsonify({"error": "registration closed"}), 400
    user_id = request.user["sub"]
    if Registration.query.filter_by(tournament_id=tid, user_id=user_id).first():
        return jsonify({"error": "already registered"}), 409
    db.session.add(Registration(tournament_id=tid, user_id=user_id))
    db.session.commit()
    return jsonify({"status": "registered"}), 201


@app.post("/api/tournaments/<int:tid>/start")
@require_auth(roles=["organizer", "admin"])
def start_tournament(tid):
    t = Tournament.query.get_or_404(tid)
    if t.status != "registration":
        return jsonify({"error": "tournament already started"}), 409
    player_ids = [r.user_id for r in Registration.query.filter_by(tournament_id=tid)]
    if len(player_ids) < 2:
        return jsonify({"error": "need at least 2 registered players"}), 400
    generate_bracket(tid, player_ids)
    t.status = "in_progress"
    db.session.commit()
    return jsonify({"status": "in_progress", "players": len(player_ids)})


@app.get("/api/tournaments/<int:tid>/bracket")
def get_bracket(tid):
    matches = (
        Match.query.filter_by(tournament_id=tid)
        .order_by(Match.round, Match.slot)
        .all()
    )
    return jsonify(
        [
            {
                "id": m.id,
                "round": m.round,
                "slot": m.slot,
                "player1_id": m.player1_id,
                "player2_id": m.player2_id,
                "winner_id": m.winner_id,
                "status": m.status,
                "score": m.score,
            }
            for m in matches
        ]
    )


# ---------------------------------------------------------------------------
# SECTION 3 + 4 - MATCHES and LEADERBOARD
#
# These are one section in the monolith on purpose: reporting a result and
# updating ratings are the SAME transaction. That is the simplicity win, and
# it is exactly what the distributed version gives up.
# ---------------------------------------------------------------------------


def advance_round_if_complete(tournament_id, round_num):
    matches = (
        Match.query.filter_by(tournament_id=tournament_id, round=round_num)
        .order_by(Match.slot)
        .all()
    )
    if not matches or any(m.winner_id is None for m in matches):
        return
    if len(matches) == 1:
        Tournament.query.get(tournament_id).status = "finished"
        db.session.commit()
        return
    winners = [m.winner_id for m in matches]
    for slot in range(0, len(winners), 2):
        db.session.add(
            Match(
                tournament_id=tournament_id,
                round=round_num + 1,
                slot=slot // 2,
                player1_id=winners[slot],
                player2_id=winners[slot + 1],
                status="pending",
            )
        )
    db.session.commit()


@app.post("/api/matches/<int:mid>/result")
@require_auth(roles=["organizer", "admin"])
def report_result(mid):
    body = request.get_json(force=True, silent=True) or {}
    match = Match.query.get_or_404(mid)

    # INTEGRITY GUARD - the monolith's answer to "no double-count".
    # A second report of the same match is refused outright. In the
    # distributed version this same guarantee needs an event_id and a
    # de-duplication set in ingestion-service, because the result travels
    # through a stream that can redeliver.
    if match.status == "reported":
        return jsonify({"error": "result already recorded (no double-count)"}), 409

    winner_id = body.get("winner_id")
    if winner_id not in (match.player1_id, match.player2_id):
        return jsonify({"error": "winner must be one of the two players"}), 400

    match.winner_id = winner_id
    match.score = body.get("score")
    match.status = "reported"
    match.reported_by = request.user["sub"]          # audit trail (kata R9)
    match.reported_at = datetime.datetime.utcnow()

    # Ratings update in the SAME transaction as the match row.
    loser_id = (
        match.player1_id if winner_id == match.player2_id else match.player2_id
    )
    winner = User.query.get(winner_id)
    if winner is not None:
        winner.rating += WIN_DELTA
        winner.wins += 1
    if loser_id is not None:
        loser = User.query.get(loser_id)
        if loser is not None:
            loser.rating = max(0, loser.rating - LOSS_DELTA)
            loser.losses += 1

    # One commit: match result and both ratings land together or not at all.
    db.session.commit()

    advance_round_if_complete(match.tournament_id, match.round)
    return jsonify({"status": "reported", "winner_id": winner_id})


@app.get("/api/leaderboard")
def leaderboard():
    users = User.query.order_by(User.rating.desc()).limit(50).all()
    return jsonify(
        [
            {"username": u.username, "rating": u.rating, "wins": u.wins, "losses": u.losses}
            for u in users
        ]
    )


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "clasharena-monolith"})


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
