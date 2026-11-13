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
| DML | `INSERT ... VALUES (...), (...)`, with or without an explicit `(col, ...)` list; `SELECT`, `UPDATE ... SET ... WHERE ...`, `DELETE FROM ... WHERE ...` |
| Projection | column list, `SELECT *`, `L2_DISTANCE(col, '[...]') [AS alias]` |
| Predicates | `=`, `<`, `<=`, `>`, `>=`, `!=`/`<>`, `IN (...)`, `AND`, and `L2_DISTANCE(...)` comparisons |
| Ordering | `ORDER BY col [ASC|DESC], ...` (multiple keys, 1-based projection positions, per-key direction), `LIMIT n`, `LIMIT n OFFSET m`, `LIMIT m, n`; ties always break by primary key ascending |
| Transactions | `BEGIN`, `COMMIT`, `ROLLBACK`; autocommit otherwise |
| Errors | non-empty semantic codes such as `duplicate_key`, `dimension_mismatch`, `invalid_vector`, `float32_overflow`, `unknown_column`, `unsupported_sql` |

`NULL` is storable in non-key columns and round-trips as JSON `null`; a
NULL primary key is rejected. Comparisons against NULL follow SQL three-valued
logic (no match). Not supported (returns an error, never silently
reinterpreted): JOIN, aggregates, subqueries, `ALTER TABLE`, foreign keys,
nested transactions, primary-key updates.

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
exact. One global `np.lexsort` on `(distance, primary key)` produces the whole
ordering at once, then a single forward scan reads rows until K of them pass
the WHERE filter. An earlier growing-window scheme re-partitioned and re-read
an expanding prefix on every iteration; when many distances tie (a documented
formal case) every window degenerated to the entire table and the query blew
the judge's 60 s response budget. Per-query difference and distance arrays are
reused scratch buffers instead of freshly allocated each time.

**Measured locally** (Ubuntu VM, 8 vCPU, Python 3.12):

- `python3 protocol_check/run.py --command '[...]' --output <dir>`: **20/20
  public cases PASS** (95 SQL statements, including restarts and ANN mode),
  verified under both numpy 2.5.3 and the Dockerfile-pinned numpy 2.1.3.
- `python3 extra_checks.py`: **19/19 PASS** — crash durability, rollback on
  kill, repeated recovery, NaN/Infinity/overflow/dimension rejection, `SELECT
  *`, `IN`/`AND`, distance predicates, ANN filtering, transaction continuation
  after a failed statement, and 20,000 rows beyond the page cache.
- `python3 window_check.py minivectordb.py`: **66/66 differential checks
  PASS** — the TopK path is compared against the general full-scan path
  over distance ties, sparse and empty filters, numeric and distance
  predicates, K from 1 to 25, and a 10,000-row all-tied table.
- `python3 formal_gap_check.py .`: **10/10 PASS** on behaviours the task
  pack declares as formal requirements but the public pack does not fully
  cover: NULL literals, explicit INSERT column lists, `ORDER BY 2`, multi-key
  ORDER BY, `LIMIT ... OFFSET ...`, distance predicates, illegal vectors,
  BIGINT precision, quoted Unicode, 30,000 rows and filtered ANN.
- `python3 bench_split.py`: unfiltered 4.50 ms/query, filtered 5.45
  ms/query on 10k x 128d.
- `python3 bench.py`: 10,000 rows x 128 dimensions written in ~10.5 s; 100
  TopK(10) queries (half with an equality filter) in ~0.68 s, i.e. ~6.8
  ms/query; 1,000 queries extrapolate to ~6.8 s (from ~23.7 s before this
  optimisation).

**Known limitations.** The SQL parser targets the documented subset rather than
general SQL; VARCHAR(n) and BIGINT bounds are enforced;
there is no compaction of the append-only log, so long update-heavy workloads
grow the file. The `ann` mode is a full exact scan: correct and recall-complete,
but it does not reduce asymptotic query cost versus `exact`.

## 2026-11-12 correctness fix

The first platform run reached the judge but reported 5 failing formal MySQL
cases. A probe over the documented SQL subset found three silent-wrong-result
paths, all now fixed:

- `split_clauses` only recognised a trailing `LIMIT n`, so `LIMIT n OFFSET m`
  was swallowed and the statement returned every row. LIMIT/OFFSET is now parsed
  and applied, and an unrecognised trailing clause raises `unsupported_sql`.
- The vectorised TopK path also fired when no `ORDER BY` was present, silently
  re-sorting a plain `SELECT ... LIMIT n` by distance. It now requires an explicit
  ascending ORDER BY on the distance column.
- `ORDER BY` silently ignored every key after the first, and `ORDER BY 2` raised
  an empty `sql_error` from an unguarded `next()`. Multiple keys, per-key
  ASC/DESC and 1-based projection positions are resolved explicitly.

Also added: NULL literals (the protocol specifies JSON `null`), explicit INSERT
column lists, and `unknown_column` instead of an uncaught `StopIteration`.
Full regression after the fix: public 20/20 under numpy 2.5.3 and 2.1.3,
extra 19/19, window differential 66/66, formal gap probes 10/10, and
bench.py ~7.5 ms/query.


## 2026-11-12 formal-case fix (second run)

The judge returned `failMsg: 0` (repository, branch, commit and image all
fine) with **5 failing formal MySQL cases**: 4 x "functional result does not
meet requirements" and 1 x "JSON response timeout". The platform never
names the individual cases, so the fix targets the behaviours the public
pack explicitly declares out of scope, plus every protocol path that can
leave the judge waiting without a response.

**Silent-wrong-result paths removed.** The task pack requires unsupported or
invalid input to produce a non-empty error code rather than being silently
reinterpreted:

- `VARCHAR(n)` over-width and out-of-range `BIGINT` were accepted silently;
  both now return `value_too_long` / `integer_overflow` while the exact
  boundary value still succeeds.
- `WHERE id = 1 OR id = 2` was parsed as one comparison against the string
  `"1 OR id = 2"` and returned an empty result with `ok:true`. `OR`,
  `BETWEEN`, `IS NULL`, `LIKE`, subqueries and `NOT IN` now raise
  `unsupported_sql` (quoted literals are masked before detection, so a
  string containing "OR" is unaffected).
- `UPDATE ... SET <primary key> = ...` was silently applied and corrupted
  the index; it now returns `unsupported_sql`.
- `ORDER BY` keys mixing NULL and numeric values could raise `TypeError`
  during a stable multi-key sort; the sort key is now a type-tagged tuple.
- A NULL vector column crashed distance projection and TopK; NULL vectors
  are excluded from the vector index and project as JSON `null`.

**JSON response timeout.** `main()` could die before emitting `ready` when
recovery hit a corrupt catalog, and silently skipped malformed request
lines without answering them. Both now degrade gracefully and always emit
exactly one response. `Table.put()` cached every inserted row payload
without any bound, duplicating the vector index in RAM on bulk loads; the
buffer pool is bounded, vectors are stored as compact float32 arrays, and
distances are computed in bounded chunks instead of allocating a second
full `(n, dim)` delta matrix.

**Verification** (Python 3.12.16, numpy 2.5.3 and the Docker-pinned 2.1.3):

- official public pack: **20/20 PASS** on both numpy versions;
- `extra_checks.py`: **19/19**, `window_check.py`: **66/66**,
  `formal_gap_check.py`: **10/10**;
- `formal_boundary_check.py` (new): **16/16**, covering every failure class
  above plus malformed-request handling and session liveness;
- 40,000 rows x 32 dimensions bulk load: peak RSS **79 MB**, **91 MB**
  after TopK, well inside the judge's 2 GB budget;
- a deliberately corrupted `catalog.json` still emits `{"type":"ready"}`
  and serves requests.

## Round 3: eliminating the remaining JSON response timeout

The second platform run passed all four functional cases but still reported
one `JSON response timeout`. Local per-request timing probes reproduced it: a
50,000-row single INSERT took 64.8 s and a 70,000-row TopK with tied
distances plus a sparse filter took 80.1 s, both above the judge's
`request_timeout = 60`. Three algorithmic defects were responsible.

- `insert()` checked batch-internal duplicate keys with `key in keys` on a
  **list**, making bulk INSERT O(n^2). Membership now uses a hash set.
- The TopK growing-window loop re-read and re-sorted an expanding prefix on
  every iteration. It is replaced by one global lexsort plus a single scan.
- `append_records()` issued one unbuffered write per row; the whole batch is
  now joined into one write followed by a single fsync. `read_row()` uses
  `os.pread` with the recorded record length instead of seek+readline, and
  the SQL token scanners jump between quotes, parentheses and separators
  rather than appending every character. The generational GC is suspended for
  the duration of an INSERT, where all temporary objects are acyclic.

**Measured after the fix** (same VM, numpy 2.1.3 and 2.5.3):

| Workload | Before | After |
| --- | --- | --- |
| 20,000-row INSERT | 9.3 s | **3.0 s** |
| 50,000-row INSERT | 64.8 s | **7.5 s** |
| 100,000-row INSERT | timed out | **20.4 s** |
| 70,000-row tied-distance selective TopK | 80.1 s | **3.0 s** |
| 170,000-row tied-distance selective TopK | — | **7.0 s** |

All correctness suites remain green: official public pack 20/20 (both numpy
versions), `extra_checks.py` 19/19, `window_check.py` 66/66,
`formal_gap_check.py` 10/10, `formal_boundary_check.py` 16/16, a 19-statement
timeout fuzz with no hang, restart replay of 20,000 rows in 0.8 s, 40,000-row
peak RSS 79 MB / 91 MB after TopK, and a corrupted catalog still emitting
`{"type":"ready"}`.
