#!/usr/bin/env python3
"""Smoke benchmark mirroring the published workload shape (10k x 128d, K=10)."""
import json
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROGRAM = Path(__file__).resolve().parent / "minivectordb.py"
DATA_DIR = Path(tempfile.mkdtemp(prefix="mvdb-bench-", dir="/tmp"))
N, DIM, QUERIES, K = 10000, 128, 100, 10
random.seed(20261111)


class Session:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-u", str(PROGRAM), str(DATA_DIR)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        assert json.loads(self.proc.stdout.readline()) == {"type": "ready"}
        self.seq = 0

    def request(self, sql, mode="exact"):
        self.seq += 1
        self.proc.stdin.write(json.dumps(
            {"id": self.seq, "op": "execute", "sql": sql, "search_mode": mode},
            ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def close(self):
        self.seq += 1
        self.proc.stdin.write(json.dumps({"id": self.seq, "op": "shutdown"}) + "\n")
        self.proc.stdin.flush()
        self.proc.stdout.readline()
        self.proc.stdin.close()
        self.proc.wait(timeout=60)


def main():
    session = Session()
    create = "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), embedding VECTOR(128));"
    assert session.request(create)["ok"] is True

    started = time.monotonic()
    for start in range(0, N, 100):
        rows = ",".join(
            "(%d,'g%d','[%s]')" % (
                i, i % 10,
                ",".join("%.3f" % random.uniform(-1, 1) for _ in range(DIM)),
            )
            for i in range(start, start + 100)
        )
        response = session.request(f"INSERT INTO docs VALUES {rows};")
        assert response.get("ok") is True, response
    write_seconds = time.monotonic() - started

    session.request("CREATE VECTOR INDEX docs_vec ON docs(embedding);")
    probes = [("[" + ",".join("%.3f" % random.uniform(-1, 1) for _ in range(DIM)) + "]",
               i % 2 == 0) for i in range(QUERIES)]
    started = time.monotonic()
    for vector, filtered in probes:
        where = "WHERE category = 'g3' " if filtered else ""
        sql = (f"SELECT id, L2_DISTANCE(embedding, '{vector}') AS distance FROM docs "
               f"{where}ORDER BY distance LIMIT {K};")
        response = session.request(sql, mode="ann")
        assert response.get("ok") is True, response
        assert len(response["rows"]) <= K, response
        assert len({row[0] for row in response["rows"]}) == len(response["rows"]), response
    query_seconds = time.monotonic() - started
    session.close()

    print(f"WRITE {N} rows x {DIM}d: {write_seconds:.2f}s", flush=True)
    print(f"QUERY {QUERIES} TopK({K}) half filtered: {query_seconds:.2f}s "
          f"({query_seconds / QUERIES * 1000:.1f} ms/query)", flush=True)
    print(f"EXTRAPOLATED 1000 queries: {query_seconds * 10:.1f}s", flush=True)
    print(f"DATA_DIR={DATA_DIR}", flush=True)


if __name__ == "__main__":
    main()
