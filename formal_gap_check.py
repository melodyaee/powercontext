"""Probe the MiniVectorDB implementation against every behaviour the official
task pack declares as a FORMAL requirement but the 20 public cases do not fully
cover. Prints PASS/FAIL per probe; never assumes an expected error is correct.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

REPO = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
PY = sys.executable
SCRIPT = os.path.join(REPO, "minivectordb.py")


class Session:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.proc = subprocess.Popen(
            [PY, "-B", "-u", SCRIPT, data_dir],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        first = self.proc.stdout.readline()
        assert json.loads(first) == {"type": "ready"}, first
        self.ident = 0

    def request(self, sql, mode="exact"):
        self.ident += 1
        self.proc.stdin.write(json.dumps(
            {"id": self.ident, "op": "execute", "sql": sql, "search_mode": mode}
        ) + "\n")
        self.proc.stdin.flush()
        return json.loads(self.proc.stdout.readline())

    def shutdown(self):
        self.ident += 1
        self.proc.stdin.write(json.dumps({"id": self.ident, "op": "shutdown"}) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        self.proc.wait(timeout=20)
        return reply, self.proc.returncode

    def kill(self):
        self.proc.kill()
        self.proc.wait(timeout=10)


def run_case(name, fn):
    with tempfile.TemporaryDirectory(prefix="mvdb-gap-") as data:
        try:
            ok, detail = fn(data)
        except Exception as exc:  # noqa: BLE001
            ok, detail = False, f"EXCEPTION {type(exc).__name__}: {exc}"
    print(f"{'PASS' if ok else 'FAIL'} {name} :: {detail}", flush=True)
    return bool(ok)


def new_session(data):
    return Session(data)


DDL = "CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(32), embedding VECTOR(3));"


def probe_basic_variants(data):
    s = new_session(data)
    out = []
    for sql in [
        DDL,
        "INSERT INTO docs VALUES (1,'a','[1,2,3]');",
        "insert into docs values (2,'a','[4,5,6]');",          # lower case
        "SELECT * FROM docs;",                                  # select star
        "SELECT id FROM docs WHERE category = 'a';",
        "SELECT id FROM docs WHERE id IN (1,2);",
        "SELECT id FROM docs WHERE id > 0 AND id < 3;",
        "SELECT id FROM docs ORDER BY id DESC LIMIT 1;",
        "SELECT id FROM docs ORDER BY id LIMIT 1;",
        "SELECT id FROM docs LIMIT 0;",
        "SELECT id, category FROM docs ORDER BY category, id;",  # multi-column order
        "UPDATE docs SET category = 'b' WHERE id = 1;",
        "DELETE FROM docs WHERE id = 2;",
        "DELETE FROM docs;",
        "SELECT COUNT(*) FROM docs;",                            # must error, not crash
    ]:
        r = s.request(sql)
        out.append((sql[:52], r.get("ok"), (r.get("error") or {}).get("code")))
    s.shutdown()
    bad = [x for x in out if x[1] is None]
    return not bad, json.dumps(out, ensure_ascii=False)


def probe_null(data):
    s = new_session(data)
    s.request(DDL)
    r = s.request("INSERT INTO docs VALUES (1, NULL, '[1,2,3]');")
    detail = json.dumps(r, ensure_ascii=False)
    s.shutdown()
    return r.get("ok") is True, f"NULL literal -> {detail}"


def probe_alias_and_distance(data):
    s = new_session(data)
    s.request(DDL)
    s.request("INSERT INTO docs VALUES (1,'a','[3,4,0]'), (2,'a','[0,0,0]');")
    out = []
    for sql in [
        "SELECT id, L2_DISTANCE(embedding, '[0,0,0]') AS d FROM docs ORDER BY d LIMIT 2;",
        "SELECT id FROM docs WHERE L2_DISTANCE(embedding, '[0,0,0]') < 6 ORDER BY id;",
        "SELECT id, L2_DISTANCE(embedding, '[0,0,0]') FROM docs ORDER BY 2 LIMIT 2;",
    ]:
        out.append((sql[:56], s.request(sql)))
    s.shutdown()
    return all(r.get("ok") for _, r in out), json.dumps(out, ensure_ascii=False, default=str)[:900]


def probe_illegal_vectors(data):
    s = new_session(data)
    s.request(DDL)
    vectors = ["[nan,1,2]", "[Infinity,1,2]", "[1e40,1,2]", "[1,2]", "[1,2,3,4]",
               "['x',1,2]", "[true,1,2]", "[]", "[1,2,3]"]
    out = []
    for i, v in enumerate(vectors, 1):
        r = s.request(f"INSERT INTO docs VALUES ({i},'a','{v}');")
        out.append((v, r.get("ok"), (r.get("error") or {}).get("code")))
    s.shutdown()
    # first 8 must fail, last must succeed
    good = all(not x[1] for x in out[:8]) and out[8][1] is True
    return good, json.dumps(out, ensure_ascii=False)


def probe_shutdown_rollback(data):
    s = new_session(data)
    s.request("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
    s.request("INSERT INTO t VALUES (1,'committed');")
    s.request("BEGIN;")
    s.request("INSERT INTO t VALUES (2,'pending');")
    s.shutdown()
    s2 = new_session(data)
    r = s2.request("SELECT id, v FROM t ORDER BY id;")
    s2.shutdown()
    ok = r.get("ok") is True and r.get("rows") == [[1, "committed"]]
    return ok, json.dumps(r, ensure_ascii=False)


def probe_crash_durability(data):
    s = new_session(data)
    s.request("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
    s.request("INSERT INTO t VALUES (1,'durable');")
    s.request("BEGIN;")
    s.request("INSERT INTO t VALUES (2,'uncommitted');")
    s.kill()
    s2 = new_session(data)
    r = s2.request("SELECT id, v FROM t ORDER BY id;")
    s2.shutdown()
    ok = r.get("ok") is True and r.get("rows") == [[1, "durable"]]
    return ok, json.dumps(r, ensure_ascii=False)


def probe_bigint(data):
    s = new_session(data)
    s.request("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8));")
    big = 9007199254740993
    s.request(f"INSERT INTO t VALUES ({big},'x');")
    r = s.request("SELECT id FROM t;")
    s.shutdown()
    ok = r.get("rows") == [[big]] and isinstance(r["rows"][0][0], int)
    return ok, json.dumps(r, ensure_ascii=False)


def probe_unicode_quotes(data):
    s = new_session(data)
    s.request("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(64));")
    out = []
    for i, value in enumerate(["你好,世界", "O'Brien", 'semi;colon', "quote\"inside"], 1):
        escaped = value.replace("'", "''")
        r = s.request(f"INSERT INTO t VALUES ({i},'{escaped}');")
        out.append(r.get("ok"))
    r = s.request("SELECT id, v FROM t ORDER BY id;")
    s.shutdown()
    ok = all(out) and len(r.get("rows", [])) == 4
    return ok, json.dumps(r, ensure_ascii=False)


def probe_large_data(data):
    s = new_session(data)
    s.request("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8), embedding VECTOR(16));")
    for start in range(0, 30000, 500):
        rows = ",".join(
            f"({i},'g{i % 10}','[{','.join('0.1' for _ in range(16))}]')"
            for i in range(start, start + 500)
        )
        r = s.request(f"INSERT INTO t VALUES {rows};")
        if not r.get("ok"):
            s.shutdown()
            return False, f"insert failed at {start}: {r}"
    q = "SELECT id FROM t WHERE v = 'g3' ORDER BY id LIMIT 5;"
    r = s.request(q)
    s.shutdown()
    ok = r.get("ok") is True and len(r.get("rows", [])) == 5
    return ok, f"30000 rows, rows={r.get('rows')}, err={r.get('error')}"


def probe_ann_filter(data):
    s = new_session(data)
    s.request("CREATE TABLE t (id BIGINT PRIMARY KEY, v VARCHAR(8), embedding VECTOR(2));")
    for i in range(1, 101):
        s.request(f"INSERT INTO t VALUES ({i},'g{i % 2}','[{i % 7},{(i * 3) % 11}]');")
    s.request("CREATE VECTOR INDEX idx ON t(embedding);")
    r = s.request(
        "SELECT id, L2_DISTANCE(embedding, '[3,3]') AS d FROM t "
        "WHERE v = 'g1' ORDER BY d LIMIT 10;", "ann")
    s.shutdown()
    rows = r.get("rows", [])
    ids = [x[0] for x in rows]
    ok = r.get("ok") is True and len(ids) == 10 and len(set(ids)) == 10 and all(i % 2 == 1 for i in ids)
    return ok, json.dumps(r, ensure_ascii=False)[:300]


PROBES = [
    ("basic_sql_variants", probe_basic_variants),
    ("null_literal", probe_null),
    ("alias_and_distance_predicate", probe_alias_and_distance),
    ("illegal_vectors", probe_illegal_vectors),
    ("shutdown_rollback", probe_shutdown_rollback),
    ("crash_durability", probe_crash_durability),
    ("bigint_precision", probe_bigint),
    ("unicode_and_quotes", probe_unicode_quotes),
    ("large_data_30k", probe_large_data),
    ("ann_filter_unique_ids", probe_ann_filter),
]


def main():
    results = [run_case(name, fn) for name, fn in PROBES]
    print(f"SUMMARY {sum(results)}/{len(results)} PASS", flush=True)


if __name__ == "__main__":
    main()
