# MiniVectorDB

A single-process, single-client vector database for the 2026 OceanBase
competition playoff (MiniVectorDB, question 600081). Branch:
`2026-competition-round2`.

## Runtime

- Language: Python 3.12. One third-party dependency, `numpy==2.1.3`, used
  **only** as a general math library for batch distance arithmetic. No storage
  engine, SQL engine or vector-index library is used. Storage, query execution,
  the vector index, transactions and recovery are implemented in
  `minivectordb.py`.
- Build: `docker build -t minivectordb .`
- Start (the judge uses this form): `docker run -i --rm --mount type=bind,src=<data>,dst=/data minivectordb`
- The container runs `python3 -B -u /app/minivectordb.py /data` in the
  foreground. No port, no TTY, no network access at run time.
- Data files live under `/data`: `catalog.json` (table definitions) and
  `<table>.dat` (append-only committed-record log). `TMPDIR` is set to `/data`
  so temporary files stay inside the measured directory.
- Protocol: JSON Lines on stdin/stdout, one response per request, flushed
  immediately; logs go to stderr. See `STDIO_PROTOCOL.md` from the task pack.

## SQL subset

| Area | Supported |
| --- | --- |
| DDL | `CREATE TABLE` with `BIGINT`, `VARCHAR(n)`, `VECTOR(n)` and exactly one `BIGINT PRIMARY KEY`; `CREATE VECTOR INDEX name ON table(vector_column)` |
| DML | `INSERT ... VALUES (...), (...)`, `SELECT`, `UPDATE ... SET ... WHERE ...`, `DELETE FROM ... WHERE ...` |
| Projection | column list, `SELECT *`, `L2_DISTANCE(col, '[...]') [AS alias]` |
| Predicates | `=`, `<`, `<=`, `>`, `>=`, `!=`/`<>`, `IN (...)`, `AND`, and `L2_DISTANCE(...)` comparisons |
| Ordering | `ORDER BY col [ASC|DESC]`, `LIMIT n`; ties always break by primary key ascending |
| Transactions | `BEGIN`, `COMMIT`, `ROLLBACK`; autocommit otherwise |
| Errors | non-empty semantic codes such as `duplicate_key`, `dimension_mismatch`, `invalid_vector`, `float32_overflow`, `unsupported_sql` |

Not supported (returns an error, never silently reinterpreted): NULL, JOIN,
aggregates, subqueries, `ALTER TABLE`, foreign keys, nested transactions,
primary-key updates.

## End-to-end example from an empty data directory

Start the container, then send these JSON lines on stdin. Each line gets one
response line on stdout preceded by `{"type":"ready"}`.

```json
{"id":1,"op":"execute","sql":"CREATE TABLE docs (id BIGINT PRIMARY KEY, category VARCHAR(8), embedding VECTOR(2));","search_mode":"exact"}
{"id":2,"op":"execute","sql":"INSERT INTO docs VALUES (1,'a','[3,4]'), (2,'a','[0,2]');","search_mode":"exact"}
{"id":3,"op":"execute","sql":"SELECT id, L2_DISTANCE(embedding, '[0,0]') AS distance FROM docs WHERE category = 'a' ORDER BY distance LIMIT 10;","search_mode":"exact"}
{"id":4,"op":"execute","sql":"BEGIN;","search_mode":"exact"}
{"id":5,"op":"execute","sql":"UPDATE docs SET category = 'b' WHERE id = 1;","search_mode":"exact"}
{"id":6,"op":"execute","sql":"ROLLBACK;","search_mode":"exact"}
{"id":7,"op":"execute","sql":"SELECT id, category FROM docs ORDER BY id;","search_mode":"exact"}
{"id":8,"op":"shutdown"}
```

After `--restart` in the mysqltest driver (a normal shutdown followed by a new
process over the same `/data`), `SELECT id, category FROM docs ORDER BY id`
returns `[[1,"a"],[2,"a"]]` — the rollback survived the restart.

## Design and verification record

**Record layout.** `catalog.json` is rewritten atomically (temp file + fsync +
`os.replace` + directory fsync). Each table has an append-only log; one line is
either `{"k":<pk>,"v":<row>}` or `{"k":<pk>,"d":1}`. Vectors are stored as
float32 JSON numbers. Only a `pk -> (byte offset, length)` index and a compact
float32 vector index are held in memory. Row payloads are read on demand
through a bounded buffer pool, so correctness never depends on loading the
whole data set into RAM; recovery is a streaming, read-only replay of the log.

**Transactions.** Autocommit statements append all their records with one
`fsync` before the success response. An explicit transaction stages rows in
memory with an undo log, so reads (including vector searches) see its own
writes. `COMMIT` appends every staged record and fsyncs; `ROLLBACK` restores the
undo log. A statement that fails midway (for example a multi-row INSERT with a
duplicate key or a wrong-dimension vector) has staged nothing, so it leaves no
partial state while earlier successful statements in the same transaction
remain committable.

**Durability and recovery.** `synchronous` persistence is an `fsync` on the log
fd at commit. A hard kill before commit loses only staged rows; committed rows
are replayed from the log. Replay is idempotent and read-only, so killing the
process during recovery is safe. Normal shutdown rolls back any open
transaction, returns `{"ok":true}` and exits 0.

**Vector index.** `CREATE VECTOR INDEX` marks the compact float32 matrix for a
lazy rebuild from the currently visible rows. Because it is rebuilt on every
write and before the next query, inserts, updates, deletes and mode switches
are all reflected. `exact` and `ann` both compute exact distances here, so
Recall@K is 1.0 by construction while filtering, valid IDs and uniqueness are
preserved. Distance ties are ranked by primary key.

**TopK query.** Distances are still computed for every row, so recall remains
exact. Ordering and row reads, however, happen on a small nearest-neighbour
window: `np.partition` finds a distance boundary, every row tied at that
boundary is included, and only those candidates are sorted by
`(distance, primary key)`. The window grows until K rows pass the WHERE filter
or the whole table has been examined, which keeps sparse filters and massive
distance ties correct. Per-query difference and distance arrays are reused
scratch buffers instead of freshly allocated each time.

**Measured locally** (Ubuntu VM, 8 vCPU, Python 3.12):

- `python3 protocol_check/run.py --command '[...]' --output <dir>`: **20/20
  public cases PASS** (95 SQL statements, including restarts and ANN mode),
  verified under both numpy 2.5.3 and the Dockerfile-pinned numpy 2.1.3.
- `python3 extra_checks.py`: **19/19 PASS** — crash durability, rollback on
  kill, repeated recovery, NaN/Infinity/overflow/dimension rejection, `SELECT
  *`, `IN`/`AND`, distance predicates, ANN filtering, transaction continuation
  after a failed statement, and 20,000 rows beyond the page cache.
- `python3 window_check.py minivectordb.py`: **66/66 differential checks
  PASS** — the TopK window path is compared against the general full-scan path
  over distance ties, sparse and empty filters, numeric and distance
  predicates, K from 1 to 25, and a 10,000-row all-tied table.
- `python3 bench_split.py`: unfiltered 4.60 ms/query, filtered 5.38
  ms/query on 10k x 128d.
- `python3 bench.py`: 10,000 rows x 128 dimensions written in ~10.5 s; 100
  TopK(10) queries (half with an equality filter) in ~0.68 s, i.e. ~6.8
  ms/query; 1,000 queries extrapolate to ~6.8 s (from ~23.7 s before this
  optimisation).

**Known limitations.** The SQL parser targets the documented subset rather than
general SQL; `ORDER BY` accepts a single key; VARCHAR length is not enforced;
there is no compaction of the append-only log, so long update-heavy workloads
grow the file. The `ann` mode is a full exact scan: correct and recall-complete,
but it does not reduce asymptotic query cost versus `exact`.
