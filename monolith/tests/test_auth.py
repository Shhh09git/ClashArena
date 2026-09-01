"""
Tests for the identity / security module (ClashArena monolith).

docs/ARCHITECTURE.md section 8 asks each team member to add tests for their
owned module, and the architecture-characteristics document lists
"automating the duplicate-result check instead of doing it by hand" as
outstanding work. This file covers both:

  - password hashing        (never plaintext, salted, verifiable)
  - JWT signing             (claims, expiry, tamper resistance, alg confusion)
  - RBAC                    (401 vs 403, role enforcement on write endpoints)
  - result integrity        (a replayed result is refused and counted once)

Run from the monolith/ folder:

    pip install -r requirements.txt pytest
    python -m pytest tests/ -v

Every test runs against a throwaway in-memory SQLite database, so there is
no fixture cleanup and no dependency on Docker or Redis.
"""

import os
import sys
import datetime

import jwt
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["DATABASE_URL"] = "sqlite://"  # in-memory
os.environ["SECRET_KEY"] = "test-secret"

import app as monolith  # noqa: E402


@pytest.fixture
def client():
    monolith.app.config["TESTING"] = True
    with monolith.app.app_context():
        monolith.db.drop_all()
        monolith.db.create_all()
        with monolith.app.test_client() as c:
            yield c


def make_user(client, username, password="hunter2", role="player"):
    resp = client.post(
        "/api/register",
        json={"username": username, "password": password, "role": role},
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()


def login(client, username, password="hunter2"):
    resp = client.post("/api/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["token"]


def auth(token):
    return {"Authorization": "Bearer " + token}


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def test_password_is_never_stored_in_plaintext(client):
    make_user(client, "alice", password="correct horse battery")
    with monolith.app.app_context():
        row = monolith.User.query.filter_by(username="alice").one()
        assert "correct horse battery" not in row.password_hash
        assert row.password_hash.startswith("pbkdf2:sha256:")


def test_identical_passwords_produce_different_hashes(client):
    """Proves the hash is salted. Without a per-user salt, two users with
    the same password would share a hash and one rainbow-table hit would
    compromise both."""
    make_user(client, "bob", password="samepassword")
    make_user(client, "carol", password="samepassword")
    with monolith.app.app_context():
        a = monolith.User.query.filter_by(username="bob").one().password_hash
        b = monolith.User.query.filter_by(username="carol").one().password_hash
        assert a != b


def test_wrong_password_and_unknown_user_are_indistinguishable(client):
    """No username enumeration: both cases must return the same status and
    the same body, or the endpoint tells an attacker which accounts exist."""
    make_user(client, "dave")
    wrong = client.post("/api/login", json={"username": "dave", "password": "nope"})
    missing = client.post("/api/login", json={"username": "ghost", "password": "nope"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.get_json() == missing.get_json()


def test_duplicate_username_is_rejected(client):
    make_user(client, "erin")
    resp = client.post(
        "/api/register", json={"username": "erin", "password": "hunter2"}
    )
    assert resp.status_code == 409


def test_weak_password_and_bad_role_are_rejected(client):
    assert client.post(
        "/api/register", json={"username": "x", "password": "123"}
    ).status_code == 400
    assert client.post(
        "/api/register",
        json={"username": "y", "password": "hunter2", "role": "superadmin"},
    ).status_code == 400


# ---------------------------------------------------------------------------
# JWT signing and verification
# ---------------------------------------------------------------------------


def test_token_carries_the_claims_downstream_services_read(client):
    """tournament-service reads request.user["sub"] as an integer id and
    request.user["role"] for RBAC. If either claim changes shape, the
    distributed version breaks silently."""
    user = make_user(client, "frank", role="organizer")
    token = login(client, "frank")
    claims = jwt.decode(token, "test-secret", algorithms=["HS256"])
    assert claims["sub"] == user["id"]
    assert isinstance(claims["sub"], int)
    assert claims["role"] == "organizer"
    assert "exp" in claims and "iat" in claims


def test_tampered_role_claim_is_rejected(client):
    """The core reason a JWT can be trusted: re-signing with a different key
    fails the HMAC check, so a player cannot promote themselves."""
    make_user(client, "grace", role="player")
    token = login(client, "grace")
    claims = jwt.decode(token, "test-secret", algorithms=["HS256"])
    claims["role"] = "admin"
    forged = jwt.encode(claims, "attacker-key", algorithm="HS256")

    resp = client.post(
        "/api/tournaments",
        json={"name": "Forged Cup", "game": "1v1"},
        headers=auth(forged),
    )
    assert resp.status_code == 401


def test_unsigned_alg_none_token_is_rejected(client):
    """Classic algorithm-confusion attack. Passing algorithms=["HS256"] to
    jwt.decode is what stops it; omitting that argument would let this in."""
    forged = jwt.encode({"sub": 1, "role": "admin"}, key="", algorithm="none")
    resp = client.post(
        "/api/tournaments",
        json={"name": "None Cup", "game": "1v1"},
        headers=auth(forged),
    )
    assert resp.status_code == 401


def test_expired_token_is_rejected(client):
    user = make_user(client, "heidi", role="organizer")
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    expired = jwt.encode(
        {"sub": user["id"], "role": "organizer", "exp": past},
        "test-secret",
        algorithm="HS256",
    )
    resp = client.post(
        "/api/tournaments",
        json={"name": "Stale Cup", "game": "1v1"},
        headers=auth(expired),
    )
    assert resp.status_code == 401


def test_missing_or_malformed_header_is_rejected(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Basic abc"}).status_code == 401


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


def test_player_cannot_create_a_tournament(client):
    """Authenticated but not authorised: 403, not 401. The distinction
    matters - the caller's identity is fine, the permission is not."""
    make_user(client, "ivan", role="player")
    resp = client.post(
        "/api/tournaments",
        json={"name": "Player Cup", "game": "1v1"},
        headers=auth(login(client, "ivan")),
    )
    assert resp.status_code == 403


def test_organizer_can_create_a_tournament(client):
    make_user(client, "judy", role="organizer")
    resp = client.post(
        "/api/tournaments",
        json={"name": "Judy Cup", "game": "1v1"},
        headers=auth(login(client, "judy")),
    )
    assert resp.status_code == 201


def test_public_read_surfaces_need_no_token(client):
    """Kata R7: leaderboards and brackets are public, read-heavy surfaces.
    Requiring auth here would break the spectator path entirely."""
    assert client.get("/api/leaderboard").status_code == 200
    assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# Result integrity - the "no double-count" property, automated
# ---------------------------------------------------------------------------


def _played_match(client):
    organizer = make_user(client, "org", role="organizer")
    p1 = make_user(client, "player1")
    p2 = make_user(client, "player2")
    otoken = login(client, "org")

    tid = client.post(
        "/api/tournaments",
        json={"name": "Integrity Cup", "game": "1v1"},
        headers=auth(otoken),
    ).get_json()["id"]

    for name in ("player1", "player2"):
        client.post(
            "/api/tournaments/%d/register" % tid, headers=auth(login(client, name))
        )
    client.post("/api/tournaments/%d/start" % tid, headers=auth(otoken))

    bracket = client.get("/api/tournaments/%d/bracket" % tid).get_json()
    return otoken, bracket[0]["id"], p1["id"], p2["id"], organizer["id"]


def test_replayed_result_is_refused_and_counted_once(client):
    """The kata's central integrity requirement (C3, R5), automated.

    Reporting the same match twice must leave the leaderboard identical to
    reporting it once. In the monolith the guard is a status check inside
    the same transaction; in the distributed version the same property is
    achieved by event_id de-duplication in ingestion-service.
    """
    otoken, match_id, winner_id, _loser_id, _org = _played_match(client)

    first = client.post(
        "/api/matches/%d/result" % match_id,
        json={"winner_id": winner_id, "score": "2-0"},
        headers=auth(otoken),
    )
    assert first.status_code == 200

    after_first = client.get("/api/leaderboard").get_json()

    replay = client.post(
        "/api/matches/%d/result" % match_id,
        json={"winner_id": winner_id, "score": "2-0"},
        headers=auth(otoken),
    )
    assert replay.status_code == 409

    assert client.get("/api/leaderboard").get_json() == after_first
    winner_row = next(r for r in after_first if r["rating"] == 1020)
    assert winner_row["wins"] == 1


def test_result_reporting_requires_organizer_role(client):
    otoken, match_id, winner_id, _loser, _org = _played_match(client)
    resp = client.post(
        "/api/matches/%d/result" % match_id,
        json={"winner_id": winner_id},
        headers=auth(login(client, "player1")),
    )
    assert resp.status_code == 403


def test_winner_must_be_one_of_the_two_players(client):
    otoken, match_id, _w, _l, organizer_id = _played_match(client)
    resp = client.post(
        "/api/matches/%d/result" % match_id,
        json={"winner_id": organizer_id},
        headers=auth(otoken),
    )
    assert resp.status_code == 400


def test_result_records_who_reported_it(client):
    """Kata R9 asks for an audit trail. Storing reported_by/reported_at is
    the minimum version of that."""
    otoken, match_id, winner_id, _l, organizer_id = _played_match(client)
    client.post(
        "/api/matches/%d/result" % match_id,
        json={"winner_id": winner_id},
        headers=auth(otoken),
    )
    with monolith.app.app_context():
        match = monolith.db.session.get(monolith.Match, match_id)
        assert match.reported_by == organizer_id
        assert match.reported_at is not None
