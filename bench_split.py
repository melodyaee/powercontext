#!/usr/bin/env python3
"""Split benchmark: time TopK queries with and without a WHERE filter separately."""
import random
import sys
import time
from pathlib import Path

D = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent)
sys.path.insert(0, D)
from bench import Session, N, DIM, QUERIES, K

s = Session()
r = s.request("CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), embedding VECTOR(128));")
assert r["ok"], r
random.seed(20261111)
for start in range(0, N, 100):
    rows = ",".join(
        "(%d,'g%d','[%s]')" % (
            i, i % 10,
            ",".join("%.3f" % random.uniform(-1, 1) for _ in range(DIM)),
        )
        for i in range(start, start + 100)
    )
    r = s.request(f"INSERT INTO docs VALUES {rows};")
    assert r["ok"], r
s.request("CREATE VECTOR INDEX docs_vec ON docs(embedding);")

vectors = [
    "[" + ",".join("%.3f" % random.uniform(-1, 1) for _ in range(DIM)) + "]"
    for _ in range(QUERIES)
]
plain = [
    f"SELECT id, L2_DISTANCE(embedding, '{v}') AS distance FROM docs "
    f"ORDER BY distance LIMIT {K};"
    for v in vectors
]
filtd = [
    f"SELECT id, L2_DISTANCE(embedding, '{v}') AS distance FROM docs "
    f"WHERE category = 'g3' ORDER BY distance LIMIT {K};"
    for v in vectors
]
for q in plain[:3] + filtd[:3]:
    r = s.request(q, "ann")
    assert r["ok"], r

t0 = time.monotonic()
for q in plain:
    r = s.request(q, "ann")
    assert r["ok"], r
t1 = time.monotonic()
for q in filtd:
    r = s.request(q, "ann")
    assert r["ok"], r
t2 = time.monotonic()

print(f"NO_FILTER {QUERIES} queries: {(t1 - t0) / QUERIES * 1000:.2f} ms/query", flush=True)
print(f"FILTERED  {QUERIES} queries: {(t2 - t1) / QUERIES * 1000:.2f} ms/query", flush=True)
print(f"AVERAGE              : {(t2 - t0) / (2 * QUERIES) * 1000:.2f} ms/query", flush=True)
s.close()
