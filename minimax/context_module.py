#!/usr/bin/env python3
"""SDD context module: select active spec + related code/tests, persist task state."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "state" / "engineering.db"
CONTEXT_PATH = ROOT / "state" / "context-latest.md"
PC_URL = os.environ.get("POWERCONTEXT_URL", "http://127.0.0.1:8000").rstrip("/")
PC_SCOPE = os.environ.get("POWERCONTEXT_SCOPE_ID", "")
PC_TOKEN = os.environ.get("POWERCONTEXT_TOKEN", "")


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS spec (id TEXT PRIMARY KEY, title TEXT, body TEXT, active INTEGER DEFAULT 1)")
    con.execute("CREATE TABLE IF NOT EXISTS artifact (kind TEXT, path TEXT, body TEXT, PRIMARY KEY(kind,path))")
    con.execute("CREATE TABLE IF NOT EXISTS progress (step TEXT PRIMARY KEY, status TEXT, detail TEXT)")
    return con


def http_post(path: str, payload: dict) -> dict:
    headers = {"Content-Type": "application/json"}
    if PC_TOKEN:
        headers["Authorization"] = f"Bearer {PC_TOKEN}"
    req = urllib.request.Request(
        PC_URL + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"PowerContext {path} HTTP {e.code}: {e.read().decode(errors='replace')}") from e


def cmd_init(_: argparse.Namespace) -> None:
    con = db()
    spec = ROOT / "sample_project" / "spec.md"
    con.execute("INSERT OR REPLACE INTO spec VALUES (?,?,?,?)", (
        "SPEC-001", "退款资格边界", spec.read_text(encoding="utf-8"), 1,
    ))
    sample_dir = ROOT / "sample_project"
    for kind in ("code", "test"):
        for p in sample_dir.glob("*.py"):
            con.execute("INSERT OR REPLACE INTO artifact VALUES (?,?,?)", (kind, p.name, p.read_text(encoding="utf-8")))
    con.commit()
    print(f"initialized {DB_PATH}: spec=1, artifacts={con.execute('SELECT count(*) FROM artifact').fetchone()[0]}")


def build_context(task: str) -> str:
    con = db()
    lines = ["# 当前开发上下文", "", "## 任务", task, "", "## 适用规格（active=1）"]
    for sid, title, body in con.execute("SELECT id,title,body FROM spec WHERE active=1 ORDER BY id"):
        lines.append(f"### {sid} {title}\n{body}")
    lines.append("\n## 相关代码")
    for path, body in con.execute("SELECT path,body FROM artifact WHERE kind='code' ORDER BY path"):
        lines.append(f"### {path}\n```python\n{body}\n```")
    lines.append("\n## 回归测试")
    for path, body in con.execute("SELECT path,body FROM artifact WHERE kind='test' ORDER BY path"):
        lines.append(f"### {path}\n```python\n{body}\n```")
    lines.append("\n## 已完成事项")
    rows = list(con.execute("SELECT step,status,detail FROM progress ORDER BY step"))
    if rows:
        for step, status, detail in rows:
            lines.append(f"- {step}: {status} — {detail}")
    else:
        lines.append("- （空状态）")
    return "\n".join(lines)


def cmd_context(a: argparse.Namespace) -> None:
    text = build_context(a.task)
    CONTEXT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTEXT_PATH.write_text(text, encoding="utf-8")
    print(text)


def cmd_save(a: argparse.Namespace) -> None:
    con = db()
    con.execute("INSERT OR REPLACE INTO progress VALUES (?,?,?)", (a.step, a.status, a.detail))
    con.commit()
    if PC_SCOPE:
        try:
            http_post("/v1/memory/remember", {
                "scope_id": PC_SCOPE,
                "kind": "decision",
                "text": f"{a.step}: {a.status} — {a.detail}",
                "reason": "MiniMax SDD task progress",
            })
            print("saved to local state and PowerContext")
        except Exception as e:
            print(f"local state saved; PowerContext failed: {e}", file=sys.stderr)
    else:
        print("saved to local state (POWERCONTEXT_SCOPE_ID not set)")


def cmd_next(a: argparse.Namespace) -> None:
    con = db()
    local = "\n".join(f"- {s}: {st} — {d}" for s, st, d in con.execute("SELECT step,status,detail FROM progress ORDER BY step")) or "- （空状态）"
    remote = ""
    if PC_SCOPE:
        try:
            remote = (http_post("/v1/context/prepare", {
                "scope_id": PC_SCOPE, "query": a.task, "max_bytes": 4000,
            }).get("content") or "")
        except Exception as e:
            remote = f"（PowerContext 不可用：{e}）"
    print(build_context(a.task).replace("- （空状态）", f"- （本地状态）{local}\n- （PowerContext）{remote or 'empty'}", 1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(func=cmd_init)
    c = sub.add_parser("context"); c.add_argument("--task", required=True); c.set_defaults(func=cmd_context)
    s = sub.add_parser("save"); s.add_argument("--step", required=True); s.add_argument("--status", required=True); s.add_argument("--detail", default=""); s.set_defaults(func=cmd_save)
    n = sub.add_parser("next"); n.add_argument("--task", required=True); n.set_defaults(func=cmd_next)
    a = p.parse_args()
    a.func(a)


if __name__ == "__main__":
    main()
