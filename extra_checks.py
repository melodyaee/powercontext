#!/usr/bin/env python3
"""Extra checks for the formally-required behaviours the public pack omits.

Covers: crash recovery, rollback-on-kill, invalid vectors, distance predicates,
SELECT *, complex WHERE, ANN filtering, and a data set larger than a cache.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROGRAM = Path(__file__).resolve().parent / "minivectordb.py"
DATA_DIR = Path(tempfile.mkdtemp(prefix="mvdb-extra-", dir="/tmp"))
PASS, FAIL = [], []


class Session:
    def __init__(self):
        self.proc = None
        self.seq = 0

    def start(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-u", str(PROGRAM), str(DATA_DIR)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1,
        )
        line = self.proc.stdout.readline()
        assert json.loads(line) == {"type": "ready"}, line
        self.seq = 0

    def request(self, sql=None, mode="exact", op="execute"):
        self.seq += 1
        payload = {"id": self.seq, "op": op}
        if sql is not None:
            payload.update(sql=sql, search_mode=mode)
        self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        response = json.loads(line)
        assert response["id"] == self.seq, response
        return response

    def shutdown(self):
        response = self.request(op="shutdown")
        assert response.get("ok") is True, response
        self.proc.stdin.close()
        assert self.proc.wait(timeout=30) == 0

    def kill(self):
        self.proc.kill()
        self.proc.wait(timeout=30)


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("PASS " if condition else "FAIL ") + name + ("" if condition else f": {detail}"), flush=True)


def expect_ok(response):
    return response.get("ok") is True, response


def main():
    session = Session()
    session.start()

    # --- crash durability: committed rows survive SIGKILL ------------------
    check("create table", expect_ok(session.request(
        "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), embedding VECTOR(2));"))[0])
    ok, response = expect_ok(session.request(
        "INSERT INTO docs VALUES (1,'a','[1,0]'), (2,'a','[2,0]'), (3,'b','[9,0]');"))
    check("multi-row insert", ok and response.get("affected_rows") == 3, response)
    session.kill()
    session.start()
    response = session.request("SELECT id FROM docs ORDER BY id;")
    check("committed rows survive hard kill",
          response.get("rows") == [[1], [2], [3]], response)

    # --- uncommitted transaction is rolled back on crash -------------------
    session.request("BEGIN;")
    session.request("INSERT INTO docs VALUES (4,'a','[4,0]');")
    session.request("DELETE FROM docs WHERE id = 1;")
    session.kill()
    session.start()
    response = session.request("SELECT id FROM docs ORDER BY id;")
    check("uncommitted changes vanish after crash",
          response.get("rows") == [[1], [2], [3]], response)

    # --- recovery must be repeatable ---------------------------------------
    session.request("BEGIN;")
    session.request("INSERT INTO docs VALUES (5,'a','[5,0]');")
    session.kill()
    session.start()
    session.kill()
    session.start()
    response = session.request("SELECT id FROM docs ORDER BY id;")
    check("recovery survives a second kill",
          response.get("rows") == [[1], [2], [3]], response)

    # --- invalid vectors ---------------------------------------------------
    for literal, label in (
        ("'[NaN,0]'", "NaN rejected"),
        ("'[Infinity,0]'", "Infinity rejected"),
        ("'[1e40,0]'", "float32 overflow rejected"),
        ("'[1]'", "dimension mismatch rejected"),
        ("'[1,2,3]'", "extra dimension rejected"),
    ):
        response = session.request(f"INSERT INTO docs VALUES (90,'x',{literal});")
        check(label, response.get("ok") is False and bool(
            response.get("error", {}).get("code")), response)
    response = session.request("SELECT id FROM docs ORDER BY id;")
    check("failed inserts leave no rows",
          response.get("rows") == [[1], [2], [3]], response)

    # --- projection / predicate coverage ----------------------------------
    response = session.request("SELECT * FROM docs ORDER BY id;")
    check("SELECT * expands vectors",
          response.get("columns") == ["id", "category", "embedding"]
          and response["rows"][0][2] == [1.0, 0.0], response)

    response = session.request(
        "SELECT id FROM docs WHERE id IN (1,3) AND category = 'b' ORDER BY id;")
    check("complex WHERE with IN/AND", response.get("rows") == [[3]], response)

    response = session.request(
        "SELECT id FROM docs WHERE L2_DISTANCE(embedding, '[0,0]') < 3 ORDER BY id;")
    check("L2_DISTANCE inside WHERE", response.get("rows") == [[1], [2]], response)

    response = session.request(
        "SELECT id, L2_DISTANCE(embedding, '[0,0]') AS d FROM docs "
        "WHERE category = 'a' ORDER BY d LIMIT 2;", mode="ann")
    check("ANN respects filter and returns unique ids",
          response.get("rows") == [[1, 1.0], [2, 2.0]], response)

    # --- transactions continue after a failed statement -------------------
    session.request("BEGIN;")
    session.request("UPDATE docs SET category = 'z' WHERE id = 1;")
    bad = session.request("INSERT INTO docs VALUES (2,'dup','[0,0]');")
    check("statement failure inside transaction", bad.get("ok") is False, bad)
    session.request("COMMIT;")
    response = session.request("SELECT id, category FROM docs ORDER BY id;")
    check("earlier successful change still commits",
          response.get("rows") == [[1, "z"], [2, "a"], [3, "b"]], response)

    # --- data larger than the page cache -----------------------------------
    session.request("CREATE TABLE big (id BIGINT PRIMARY KEY, payload VARCHAR(16), embedding VECTOR(4));")
    batch = 500
    for start in range(0, 20000, batch):
        rows = ",".join(
            f"({i},'p{i%97}','[{i%7},{i%5},{(i*3)%11},1]')"
            for i in range(start, start + batch)
        )
        response = session.request(f"INSERT INTO big VALUES {rows};")
        if response.get("ok") is not True:
            check("bulk insert", False, response)
            break
    else:
        check("20k rows beyond page cache", True)
    response = session.request("SELECT id FROM big ORDER BY id DESC LIMIT 1;")
    check("large-table query", response.get("rows") == [[19999]], response)
    session.shutdown()

    print(f"\nSUMMARY: {len(PASS)} passed, {len(FAIL)} failed", flush=True)
    if FAIL:
        print("FAILED: " + ", ".join(FAIL), flush=True)
    print(f"DATA_DIR={DATA_DIR}", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
