# ClashArena — Monolith

A single Flask application. Identity, tournaments, matches and the
leaderboard are internal modules sharing **one** SQLite database. There is
no network hop between "services" — a match result and the resulting
rating update happen in the same request, in the same DB transaction.

## Run it

```bash
docker compose up --build
```

The API is now at `http://localhost:8000`.

(Or without Docker: `pip install -r requirements.txt && python app.py`)

## Try it (copy/paste into a terminal, one block at a time)

```bash
# 1. Register an organizer and two players
curl -s -X POST localhost:8000/api/register -H "Content-Type: application/json" \
  -d '{"username":"org1","password":"pass123","role":"organizer"}'
curl -s -X POST localhost:8000/api/register -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"pass123"}'
curl -s -X POST localhost:8000/api/register -H "Content-Type: application/json" \
  -d '{"username":"bob","password":"pass123"}'

# 2. Log in as the organizer, grab the token
TOKEN=$(curl -s -X POST localhost:8000/api/login -H "Content-Type: application/json" \
  -d '{"username":"org1","password":"pass123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 3. Create a tournament
curl -s -X POST localhost:8000/api/tournaments -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"name":"Weekly Cup","game":"ClashArena1v1"}'
# note the "id" in the response, e.g. 1

# 4. Log in as alice and bob, register them for tournament 1
ALICE=$(curl -s -X POST localhost:8000/api/login -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"pass123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s -X POST localhost:8000/api/tournaments/1/register -H "Authorization: Bearer $ALICE"

BOB=$(curl -s -X POST localhost:8000/api/login -H "Content-Type: application/json" \
  -d '{"username":"bob","password":"pass123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")
curl -s -X POST localhost:8000/api/tournaments/1/register -H "Authorization: Bearer $BOB"

# 5. Start the tournament (generates the bracket)
curl -s -X POST localhost:8000/api/tournaments/1/start -H "Authorization: Bearer $TOKEN"

# 6. See the bracket, find the match id, then report the result
curl -s localhost:8000/api/tournaments/1/bracket
curl -s -X POST localhost:8000/api/matches/1/result -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"winner_id":2,"score":"2-0"}'

# 7. Check the leaderboard
curl -s localhost:8000/api/leaderboard
```

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | /api/register | - | create account |
| POST | /api/login | - | get a JWT |
| POST | /api/tournaments | organizer/admin | create tournament |
| POST | /api/tournaments/:id/register | any | join a tournament |
| POST | /api/tournaments/:id/start | organizer/admin | generate bracket |
| GET | /api/tournaments/:id/bracket | - | view bracket |
| POST | /api/matches/:id/result | organizer/admin | report a result |
| GET | /api/leaderboard | - | top ratings |
| GET | /healthz | - | health check |
