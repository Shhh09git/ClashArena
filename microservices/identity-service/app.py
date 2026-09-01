"""
ClashArena - DISTRIBUTED architecture
--------------------------------------
identity-service: the single owner of user accounts and the only component
allowed to MINT a JWT. Every other service only VERIFIES tokens, locally,
using the shared SECRET_KEY - so no service has to call this one on the hot
path just to check who is making a request.

That split is the whole reason identity can be deployed and scaled on its
own: login traffic is bursty at the start of an event, but token
verification (which is pure CPU) happens inside each service.

Owns identity.db exclusively - no other service reads this schema. The one
piece of user data other services need (a display name for the
leaderboard) is exposed through GET /api/users/<id>, not through the
database.
"""

import os
import functools
import datetime

import jwt
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "DATABASE_URL", "sqlite:///identity.db"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
TOKEN_TTL_HOURS = int(os.environ.get("TOKEN_TTL_HOURS", "12"))

# Roles recognised by the platform. Kept here because identity-service is
# what writes the `role` claim; every other service only reads it.
VALID_ROLES = {"player", "organizer", "referee", "admin"}

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    # Never the password itself: a salted one-way hash. Length 255 because
    # pbkdf2 hashes are ~90 chars and scrypt ones are longer.
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="player")
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    def set_password(self, password):
        # pbkdf2:sha256 is pinned explicitly rather than left to Werkzeug's
        # default (scrypt in 3.x). scrypt is deliberately memory-hard, which
        # is good for security but makes login noticeably slow inside a
        # small container. pbkdf2:sha256 with a 16-byte random salt is the
        # right trade-off for this project, and being explicit means the
        # hash format does not silently change when Werkzeug is upgraded.
        self.password_hash = generate_password_hash(
            password, method="pbkdf2:sha256", salt_length=16
        )

    def check_password(self, password):
        # Constant-time comparison of the derived key happens inside
        # Werkzeug - never compare hashes with ==.
        return check_password_hash(self.password_hash, password)


def issue_token(user):
    """Mint a signed, stateless JWT for `user`.

    Claims are deliberately minimal - only what a downstream service needs
    to authorise a request without calling back here:

      sub  : user id (INTEGER - see the note below)
      role : used by require_auth(roles=[...]) in tournament-service
      name : convenience for clients, never used for authorisation
      iat  : issued-at
      exp  : expiry, enforced by PyJWT on decode

    NOTE on `sub` being an int: tournament-service uses request.user["sub"]
    directly as organizer_id / user_id, which are Integer columns, and
    compares it against integer player ids. PyJWT 2.10+ started rejecting
    non-string `sub` values, so every service pins PyJWT==2.9.0. If that pin
    is ever raised, `sub` must become a string here AND every consumer must
    cast it back with int() - otherwise winner_id checks silently fail on
    "1" != 1.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "sub": user.id,
        "role": user.role,
        "name": user.username,
        "iat": now,
        "exp": now + datetime.timedelta(hours=TOKEN_TTL_HOURS),
    }
    # HS256 = symmetric. Every service holds the same SECRET_KEY, so any of
    # them can verify a token but only this one is supposed to sign. Moving
    # to RS256 (private key here, public key everywhere else) is the obvious
    # hardening step and is listed as future work in docs/ARCHITECTURE.md.
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def require_auth(roles=None):
    """Same decorator contract as tournament-service, kept identical on
    purpose so the RBAC rule reads the same way in every service."""

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

    # Deliberately the same error and status for "no such user" and "wrong
    # password" - telling them apart would let an attacker enumerate valid
    # usernames.
    if user is None or not user.check_password(body.get("password") or ""):
        return jsonify({"error": "invalid credentials"}), 401

    return jsonify(
        {"token": issue_token(user), "role": user.role, "user_id": user.id}
    )


@app.get("/api/users/<int:uid>")
def get_user(uid):
    """Public read of the non-sensitive part of a profile.

    leaderboard-service calls exactly this to turn a winner_id from the
    event stream into a display name. It returns no hash, no email, no
    role-sensitive data - which is why it does not need a token.
    """
    user = User.query.get_or_404(uid)
    return jsonify({"id": user.id, "username": user.username, "role": user.role})


@app.get("/api/me")
@require_auth()
def me():
    """Lets a client confirm a token is still valid without a DB lookup on
    the caller's side. Also the smallest possible demo that the RBAC
    decorator works."""
    return jsonify(
        {
            "user_id": request.user["sub"],
            "username": request.user.get("name"),
            "role": request.user["role"],
        }
    )


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok", "service": "identity-service"})


with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001, debug=False)
