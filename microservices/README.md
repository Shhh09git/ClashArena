# ClashArena — Microservices

Five independently deployable services behind a gateway, wired together
with Redis Streams for the result-ingestion pipeline the kata describes
(producer → transformer → tester → consumer):

```
client
  │
  ▼
gateway (8000)
  │
  ├── identity-service (8001)     — owns users, issues JWTs
  ├── tournament-service (8002)   — owns tournaments/matches, PRODUCES raw
  │                                 result events onto Redis stream
  │                                 `raw-results`
  ├── ingestion-service (8003)    — TRANSFORMS + TESTS (validates &
  │                                 de-duplicates) events, republishes to
  │                                 `validated-results`
  └── leaderboard-service (8004)  — CONSUMES validated events, owns its
                                    own ratings DB, serves GET /api/leaderboard
```

Each service owns its own SQLite database (database-per-service). They
never share a schema — the only coupling is (a) the JWT secret, used to
verify tokens without calling identity-service on every request, and
(b) the Redis streams.

## Run it

```bash
docker compose up --build
```

The public API is at `http://localhost:8000` (same paths as the monolith —
that's intentional, so the two architectures are easy to compare).

## Resetting between runs

Each service now keeps its database in a named Docker volume, so data
survives a restart:

    docker compose down       # stops containers, keeps all data
    docker compose up         # same users, tournaments and ratings

To start completely fresh — which you'll want before re-running the
curl walkthrough, since usernames are unique — remove the volumes too:

    docker compose down -v

## Try it

Same curl walkthrough as the monolith's README, just against port 8000 —
the gateway routes every request to the right service.

## What's actually distributed here

* **Independent deployability / modularity**: each folder here is its own
  Docker image; you can rebuild and redeploy `leaderboard-service` without
  touching `tournament-service`.
* **Database-per-service**: `identity.db`, `tournaments.db` and
  `leaderboard.db` are separate SQLite files. In production these would be
  separate Postgres instances, one per service.
* **Reliability (no loss, no double-count)**: `ingestion-service`
  de-duplicates every event by `event_id` using a Redis SET before it is
  ever counted. Because `event_id` is deterministic (`match-<id>-result`),
  a genuine replay of the same match is caught here as well as by
  `tournament-service`'s own status check. Redis consumer groups
  (`XREADGROUP` / `XACK`) mean a crashed worker's un-acknowledged messages
  sit pending in the stream rather than being lost outright — but we do
  not currently run a reclaim loop (`XAUTOCLAIM`), so those messages stay
  stuck until one is added. This is listed as future work in
  `docs/ARCHITECTURE.md` §9.
* **Elasticity / scalability**: `ingestion-service` is a pure stream
  consumer with no local state, so it scales horizontally without issue:

  ```bash
  docker compose up --build --scale ingestion-service=3
  ```

  `leaderboard-service`, however, does **not** currently scale correctly
  beyond one replica — each replica keeps its own local SQLite file, so
  scaling splits the leaderboard data across replicas instead of sharing
  it. See "Known limitation" in `docs/ARCHITECTURE.md` for the full
  explanation and how we would fix it.

See `k8s/leaderboard-service.yaml` for the equivalent on Kubernetes,
including a HorizontalPodAutoscaler.

## Run on Kubernetes (minikube) instead of Compose

```bash
minikube start
eval $(minikube docker-env)     # build images directly into minikube
docker build -t clasharena/leaderboard-service:latest ./leaderboard-service
docker build -t clasharena/identity-service:latest ./identity-service
docker build -t clasharena/tournament-service:latest ./tournament-service
docker build -t clasharena/ingestion-service:latest ./ingestion-service
docker build -t clasharena/gateway:latest ./gateway
kubectl apply -f k8s/
kubectl get pods -w
```

**Known limitation:** `k8s/` currently contains only
`leaderboard-service.yaml`, as a scaling example. Running
`kubectl apply -f k8s/` on a fresh minikube cluster deploys a
`leaderboard-service` pod that cannot reach Redis or `identity-service`,
since neither is deployed in the cluster — it will report healthy on
`/healthz` while its background worker fails in a loop.

To run the distributed architecture fully on Kubernetes you would also
need `redis.yaml`, `identity-service.yaml`, `tournament-service.yaml`,
`ingestion-service.yaml` and `gateway.yaml`, following the same
Deployment + Service pattern as `leaderboard-service.yaml`. The
HorizontalPodAutoscaler additionally requires
`minikube addons enable metrics-server`.

Docker Compose is the fully working way to run this project end to end;
the Kubernetes manifest demonstrates the scaling pattern the assignment
asks for.
