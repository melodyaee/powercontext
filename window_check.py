#!/usr/bin/env python3
"""Differential test: TopK window fast path vs the general full-scan path.

The same engine answers each query twice -- once with ORDER BY only (general
path, all matching rows) and once with ORDER BY ... LIMIT K (fast path). Their
first K rows must agree exactly, including distance ties and sparse filters.
"""
import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

PROGRAM = Path(sys.argv[1]).resolve()
DATA_DIR = Path(tempfile.mkdtemp(prefix="mvdb-window-", dir="/tmp"))
PASS, FAIL = [], []


class Session:
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-u", str(PROGRAM), str(DATA_DIR)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        assert json.loads(self.proc.stdout.readline()) == {"type": "ready"}
        self.seq = 0

    def request(self, sql):
        self.seq += 1
        self.proc.stdin.write(json.dumps(
            {"id": self.seq, "op": "execute", "sql": sql, "search_mode": "exact"},
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


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)) if not ok else ""),
          flush=True)


def insert_all(session, table, rows):
    for start in range(0, len(rows), 200):
        chunk = rows[start:start + 200]
        values = ",".join(
            "(%d,'%s','[%s]')" % (i, cat, ",".join(str(x) for x in vec))
            for i, cat, vec in chunk
        )
        response = session.request(f"INSERT INTO {table} VALUES {values};")
        assert response.get("ok"), response


def compare(session, table, target, where, k, label):
    base = f"SELECT id, L2_DISTANCE(v, '{target}') AS distance FROM {table}"
    full = session.request(f"{base} {where} ORDER BY distance;")
    fast = session.request(f"{base} {where} ORDER BY distance LIMIT {k};")
    if not (full.get("ok") and fast.get("ok")):
        check(label, False, {
            "full_ok": full.get("ok"), "fast_ok": fast.get("ok"),
            "error": full.get("error") or fast.get("error"),
        })
        return
    expected, got = full["rows"][:k], fast["rows"]
    ok = len(expected) == len(got)
    if ok:
        for e, g in zip(expected, got):
            if e[0] != g[0] or abs(e[1] - g[1]) > 1e-5:
                ok = False
                break
    check(label, ok, (expected[:3], got[:3]))


def main():
    session = Session()
    random.seed(20261111)
    for name in ("mixed", "ties", "sparse", "identical"):
        response = session.request(
            f"CREATE TABLE {name} (id BIGINT PRIMARY KEY, category VARCHAR(8), v VECTOR(4));"
        )
        assert response.get("ok"), response

    tables = {
        "mixed": [(i, "abc"[i % 3], [random.randint(-2, 2) for _ in range(4)])
                  for i in range(600)],
        "ties": [(i, "ab"[i % 2], [0, 0, 0, 0]) for i in range(1500)],
        "sparse": [(i, "rare" if i in (7, 311, 899) else "common",
                    [random.randint(-3, 3) for _ in range(4)]) for i in range(1000)],
        "identical": [(i, "z", [1, 2, 3, 4]) for i in range(800)],
    }
    for name, rows in tables.items():
        insert_all(session, name, rows)

    targets = ["[0,0,0,0]", "[1,-1,2,0]", "[2,2,2,2]", "[1,2,3,4]"]
    wheres = {
        "no_filter": "",
        "cat_a": "WHERE category = 'a'",
        "cat_rare": "WHERE category = 'rare'",
        "cat_none": "WHERE category = 'none'",
        "id_range": "WHERE id > 100 AND id < 260",
    }
    for name in tables:
        for where_name, where in wheres.items():
            for k in (1, 10, 25):
                target = targets[(len(PASS) + len(FAIL)) % len(targets)]
                compare(session, name, target, where, k, f"{name}/{where_name}/K{k}")
        compare(session, name, "[0,0,0,0]",
                "WHERE L2_DISTANCE(v, '[0,0,0,0]') < 3", 10,
                f"{name}/distance_predicate/K10")

    # Sparse filter on a large all-tied table: forces repeated window growth.
    response = session.request(
        "CREATE TABLE growth (id BIGINT PRIMARY KEY, category VARCHAR(8), v VECTOR(4));"
    )
    assert response.get("ok"), response
    insert_all(session, "growth",
               [(i, "hit" if i % 1000 == 0 else "miss", [0, 0, 0, 0])
                for i in range(10000)])
    compare(session, "growth", "[0,0,0,0]", "WHERE category = 'hit'", 10,
            "growth/1pct_filter/K10")
    compare(session, "growth", "[0,0,0,0]", "WHERE category = 'hit'", 25,
            "growth/1pct_filter/K25_exceeds_matches")

    session.close()
    print(f"\nSUMMARY: {len(PASS)} passed, {len(FAIL)} failed", flush=True)
    if FAIL:
        print("FAILED: " + ", ".join(FAIL[:15]), flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
