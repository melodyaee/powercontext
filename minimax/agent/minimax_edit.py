#!/usr/bin/env python3
"""Call MiniMax with the current SDD context; save the real request/response for review."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXT = ROOT / "state" / "context-latest.md"
DEVLOG = ROOT / "devlog"
BASE = os.environ.get("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
MODEL = os.environ.get("MINIMAX_MODEL", "MiniMax-Text-01")
KEY = os.environ.get("MINIMAX_API_KEY", "")


def main() -> None:
    if not KEY:
        raise SystemExit("请先设置 MINIMAX_API_KEY（不要写进仓库）")
    context = CONTEXT.read_text(encoding="utf-8")
    task = "为 sample_project/eligibility.py 增加过期订单边界：expired 订单不得退款，并补齐测试。"
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "你是 SDD 开发助手。严格遵守给定规格，只修改必要文件，输出 unified diff。"},
            {"role": "user", "content": f"{task}\n\n{context}"},
        ],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        BASE + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    answer = data["choices"][0]["message"]["content"]
    DEVLOG.mkdir(exist_ok=True)
    (DEVLOG / "round-01.md").write_text(
        "# MiniMax 开发记录 · round 01\n\n"
        f"- model: `{MODEL}`\n- base_url: `{BASE}`\n- task: {task}\n\n"
        "## 输入上下文\n\n```\n" + context + "\n```\n\n"
        "## MiniMax 输出\n\n" + answer + "\n",
        encoding="utf-8",
    )
    print(answer)


if __name__ == "__main__":
    main()
