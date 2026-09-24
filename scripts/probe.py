#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实链路探针 —— 「适配只改代码」红线下的自证工具。

三种模式：
  --dry        构造请求体并打印（默认；不触上游，零消耗）
  --live       发一次真实搜索（消耗 1 次匿名窗口额度；429/4001 不耗）
  --pool URL   指定出口代理池（配合 --live 验证池链路）
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time

from app.client import MetasoChatClient
from app.config import get_settings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="真实发一次搜索（默认干跑）")
    ap.add_argument("--query", default="北京今天的天气怎么样？")
    ap.add_argument("--mode", default="concise",
                    choices=["concise", "detail", "research"])
    ap.add_argument("--pool", default="", help="出口代理池（逗号分隔，覆盖 env）")
    args = ap.parse_args()


    s = dataclasses.replace(get_settings(), ms_proxy_pool=args.pool) if args.pool \
        else get_settings()
    c = MetasoChatClient(s)
    print(json.dumps(c.diagnostics(), ensure_ascii=False, indent=1))

    if not args.live:
        body = MetasoChatClient.build_body(args.query, "<meta-token:干跑未抓取>",
                                           mode=args.mode)
        print(json.dumps({"dry_run": True, "upstream_request": body},
                         ensure_ascii=False, indent=1)[:1500])
        return 0

    t0 = time.time()
    content: list[str] = []
    citations: list = []
    try:
        for ev in c.stream(args.query, mode=args.mode):
            if ev.get("done"):
                break
            d = ev.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if "citations" in ev:
                citations = ev["citations"]
    except Exception as e:  # noqa: BLE001 - 探针要把任何失败原样打出来
        print(f"FAIL {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(json.dumps({
        "ok": True, "elapsed_s": round(time.time() - t0, 1),
        "content_head": "".join(content)[:200], "citations": len(citations),
        "stats": c.stats}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
