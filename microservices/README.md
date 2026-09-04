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
  ├── ingestion-service (8003)    — TRANSFORMS + TESTS (validates \&
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

## Try it

Same curl walkthrough as the monolith's README, just against port 8000 —
the gateway routes every request to the right service.

## What's actually distributed here

* **Independent deployability / modularity**: each folder here is its own
Docker image; you can rebuild and redeploy `leaderboard-service` without
touching `tournament-service`.
* **Database-per-service**: `identity.db`, `tournaments.db`,
`leaderboard.db` are separate SQLite files (separate containers/volumes
in production Postgres would be used, one instance per service).
* **Reliability (no loss, no double-count)**: `ingestion-service`
deduplicates every event by `event\_id` using a Redis SET before it's
ever counted, and Redis consumer groups (`XREADGROUP` / `XACK`) mean a
crashed worker doesn't lose in-flight messages — they get redelivered.
* **Elasticity/scalability**: `ingestion-service` and `leaderboard-service`
are pure stream consumers with no local state that depends on which
replica handles a message, so you can scale them horizontally:

```bash
  docker compose up --build --scale ingestion-service=3
  ```

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

\*\*Known limitation:\*\* `k8s/` currently only contains

`leaderboard-service.yaml` as a scaling example. Running

`kubectl apply -f k8s/` on a fresh minikube cluster will deploy a

`leaderboard-service` pod that can't actually reach Redis or

`identity-service`, since neither is deployed in the cluster — it'll

report healthy on `/healthz` while its background worker fails in a

loop. To fully run the distributed architecture on Kubernetes, you'd

also need `redis.yaml` and `identity-service.yaml` (same Deployment +

Service pattern as `leaderboard-service.yaml`) applied alongside it.

The Horizontal Pod Autoscaler also requires

`minikube addons enable metrics-server` first. Docker Compose is the

fully working way to run this project end to end; the Kubernetes

manifest demonstrates the scaling pattern the assignment asks for.)

