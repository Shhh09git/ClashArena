# ClashArena — Software Architectures Project

An online esports tournament platform, built twice to compare architectural
styles: as a **monolith** and as **microservices**. Same domain, same public
API shape, two different ways of building it — see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full comparison.

**Status:** ✅ Both architectures working end to end · **Course:** Software
Architectures (CM90) · **Team:** Daniil Glazunov, Shattyk Kuziyeva

---

## 🚀 Quick start

```bash
# Monolith — one Flask app, one database
cd monolith && docker compose up --build
# → http://localhost:8000

# Microservices — 5 services + Redis, in another terminal
cd microservices && docker compose up --build
# → http://localhost:8000
```

Full walkthroughs (register → create tournament → report result →
leaderboard) are in each folder's own README.

## 📚 Documentation

| Document | What's in it |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Full write-up: both architectures, diagrams, security, testing, trade-off conclusion |
| [`docs/ClashArena-Architecture-Characteristics.pdf`](docs/ClashArena-Architecture-Characteristics.pdf) | Which architecture characteristics we selected, quantified targets, kata references |
| [`docs/Kata-updated.pdf`](docs/Kata-updated.pdf) | The original kata, with every requirement/user/context item numbered for reference |
| [`Kata-Esports-Tournament-Platform.pdf`](Kata-Esports-Tournament-Platform.pdf) | The original kata as first submitted |

## 🏗️ Architecture overview

**Monolith** — one process, one database:

Client → Flask app (auth, tournaments, matches, leaderboard) → SQLite


**Microservices** — five services behind a gateway, wired with Redis
Streams implementing the kata's own producer → transformer → tester →
consumer result pipeline:


Each service owns its own database. See `docs/ARCHITECTURE.md` §5 for the
full diagrams.

## 🛠️ Technology stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11, Flask |
| Database | SQLite (one shared file in the monolith; one per service in microservices) |
| Auth | JWT (PyJWT, HS256), Werkzeug password hashing |
| Messaging | Redis Streams (microservices only) — consumer groups for exactly-once processing |
| Containerisation | Docker, Docker Compose |
| Orchestration | Kubernetes manifests (`microservices/k8s/`), runnable on minikube |

## 📁 Project structure

clasharena/
├── README.md # this file
├── Kata-Esports-Tournament-Platform.pdf
├── docs/
│ ├── ARCHITECTURE.md
│ ├── ClashArena-Architecture-Characteristics.pdf
│ └── Kata-updated.pdf
├── monolith/
│ ├── app.py
│ ├── Dockerfile
│ ├── docker-compose.yml
│ └── README.md
└── microservices/
├── gateway/
├── identity-service/
├── tournament-service/
├── ingestion-service/
├── leaderboard-service/
├── k8s/
├── docker-compose.yml
└── README.md


## 👥 Team & responsibilities

Matches the kata's stated ownership split:

| Member | Owns | Architectural focus |
|---|---|---|
| Daniil Glazunov | `identity-service`, auth/RBAC in `monolith` and `tournament-service` | Identity, integrity & security |
| Shattyk Kuziyeva | `tournament-service`, `ingestion-service`, `leaderboard-service`, `k8s/` | Tournament format & scalability/elasticity |

## ✅ What's been verified

- Full flow tested end to end on both architectures (register → tournament
  → bracket → result → leaderboard).
- A real bug found and fixed in `leaderboard-service` (`NoneType` rating on
  new players) — see `docs/ARCHITECTURE.md` §7 and
  `microservices/evidence-bugfix.txt`.
- Fault tolerance: leaderboard reads keep working if `tournament-service`
  goes down.
- Reliability: replaying the same match result doesn't double-count it.