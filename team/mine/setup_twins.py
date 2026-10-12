#!/usr/bin/env python3
"""Create or update every Sarybulak twin in Ditto (idempotent PUT, parents first)."""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .model import build_twins

DITTO_URL = os.environ.get("DITTO_URL", "http://localhost:8080").rstrip("/")
DITTO_USER = os.environ.get("DITTO_USER", "ditto")
DITTO_PASSWORD = os.environ["DITTO_PASSWORD"]
AUTH = "Basic " + base64.b64encode(f"{DITTO_USER}:{DITTO_PASSWORD}".encode()).decode()


def put(thing_id, body):
    req = urllib.request.Request(f"{DITTO_URL}/api/2/things/{thing_id}", data=json.dumps(body).encode(),
                                 method="PUT", headers={"Authorization": AUTH, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def main():
    twins = build_twins()
    created = updated = 0
    for thing_id, body in twins.items():
        for attempt in range(30):
            try:
                status = put(thing_id, body)
                if status == 201:
                    created += 1
                else:
                    updated += 1
                break
            except urllib.error.HTTPError as exc:
                # Ditto answers 503/408 while its cluster warms up: retry.
                if exc.code in (408, 503, 502, 504) and attempt < 29:
                    time.sleep(3)
                    continue
                print(f"FAILED {thing_id}: HTTP {exc.code} {exc.read()[:300]!r}", file=sys.stderr)
                return 1
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == 29:
                    print(f"FAILED {thing_id}: {exc}", file=sys.stderr)
                    return 1
                time.sleep(3)
    print(f"{len(twins)} twins ready in {DITTO_URL} ({created} created, {updated} updated)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
