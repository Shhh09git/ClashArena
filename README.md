# ClashArena — Software Architectures Project

Same esports-tournament domain (from our kata), built twice:

- [`monolith/`](monolith/) — one Flask app, one database.
- [`microservices/`](microservices/) — five services + Redis Streams,
  implementing the kata's own producer → transformer → tester → consumer
  result pipeline.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — full write-up: how to
  run both, the architecture characteristics that drove the design,
  diagrams, security, testing, and the trade-off conclusion.

Quick start:

```bash
cd monolith && docker compose up --build        # http://localhost:8000
# in another terminal
cd microservices && docker compose up --build   # http://localhost:8000
```
