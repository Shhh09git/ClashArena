"""
ClashArena - DISTRIBUTED architecture
--------------------------------------
gateway: the single public entrypoint (port 8000). A thin reverse proxy,
nothing more.

Two architectural jobs:

1. It makes the distributed system present the SAME public API on the SAME
   port as the monolith, so both architectures can be exercised with one
   identical curl walkthrough. That is what makes the comparison in
   docs/ARCHITECTURE.md an apples-to-apples one.

2. It decouples clients from the backend split. Moving an endpoint to a
   different service, or splitting a service in two, changes ROUTES below
   and nothing on the client side.

What it deliberately does NOT do:

- It does not verify JWTs. Each service verifies for itself, locally, with
  the shared SECRET_KEY. Centralising auth here would make the gateway a
  single point of failure for authorisation and would mean a token could
  be trusted purely because it arrived through the gateway.
- It holds no state and no database.
- It has no hard dependency on any one backend. If tournament-service is
  down, /api/tournaments returns 503 while /api/leaderboard keeps working
  - which is exactly the fault-tolerance property the kata asks for
  (C2/C5), demonstrated in docs/ARCHITECTURE.md section 8.
"""

import os

import requests
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

IDENTITY_URL = os.environ.get("IDENTITY_URL", "http://identity-service:8001")
TOURNAMENT_URL = os.environ.get("TOURNAMENT_URL", "http://tournament-service:8002")
LEADERBOARD_URL = os.environ.get("LEADERBOARD_URL", "http://leaderboard-service:8004")
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "10"))

# Longest prefix wins, so ordering here is not load-bearing, but keeping it
# grouped by owning service makes the domain boundaries readable at a glance.
ROUTES = [
    ("/api/register", IDENTITY_URL),
    ("/api/login", IDENTITY_URL),
    ("/api/users", IDENTITY_URL),
    ("/api/me", IDENTITY_URL),
    ("/api/tournaments", TOURNAMENT_URL),
    ("/api/matches", TOURNAMENT_URL),
    ("/api/leaderboard", LEADERBOARD_URL),
]

# Headers that describe THIS hop and must not be copied to the next one.
# Forwarding Host breaks virtual-host routing; forwarding Content-Length or
# Transfer-Encoding after requests has re-encoded the body produces a
# malformed request.
HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
}


def resolve_upstream(path):
    """Map a public path onto the service that owns it (longest prefix)."""
    best = None
    for prefix, base in ROUTES:
        if path == prefix or path.startswith(prefix + "/"):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, base)
    return best[1] if best else None


def forwarded_headers():
    """Copy the client's headers minus hop-by-hop ones.

    Authorization passes through untouched - that is the important one.
    The gateway never reads, rewrites or validates the token; it just
    carries it to whichever service is going to verify the signature.
    """
    return {k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP}


@app.route(
    "/api/<path:subpath>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
def proxy(subpath):
    path = "/api/" + subpath
    base = resolve_upstream(path)
    if base is None:
        return jsonify({"error": "no route for %s" % path}), 404

    try:
        upstream = requests.request(
            method=request.method,
            url=base + path,
            headers=forwarded_headers(),
            params=request.args,
            data=request.get_data(),
            timeout=UPSTREAM_TIMEOUT,
            allow_redirects=False,
        )
    except requests.Timeout:
        return jsonify({"error": "upstream timed out", "upstream": base}), 504
    except requests.RequestException:
        # The upstream is down or unreachable. Fail only THIS route - every
        # other route still resolves to a healthy service.
        return jsonify({"error": "upstream unavailable", "upstream": base}), 503

    excluded = HOP_BY_HOP | {"content-encoding"}
    headers = [
        (k, v) for k, v in upstream.raw.headers.items() if k.lower() not in excluded
    ]
    return Response(upstream.content, status=upstream.status_code, headers=headers)


@app.get("/healthz")
def healthz():
    """Reports the gateway's own health plus a snapshot of each backend.

    Always 200 as long as the gateway itself is serving: a dead backend is
    reported in the body, not by failing the gateway's own probe. An
    orchestrator restarting the gateway because tournament-service is down
    would be exactly the wrong response.
    """
    backends = {}
    for name, base in (
        ("identity-service", IDENTITY_URL),
        ("tournament-service", TOURNAMENT_URL),
        ("leaderboard-service", LEADERBOARD_URL),
    ):
        try:
            resp = requests.get(base + "/healthz", timeout=2)
            backends[name] = "ok" if resp.ok else "unhealthy(%d)" % resp.status_code
        except requests.RequestException:
            backends[name] = "unreachable"
    return jsonify({"status": "ok", "service": "gateway", "backends": backends})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
