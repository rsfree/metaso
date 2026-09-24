#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""铸造 metaso 匿名「浏览器指纹身份」—— 免登录频控绕过的钥匙（2026-09-24 实测依据）。

背景（当日差分实验结论，修正契约文档 §11.2 的旧结论）：
  metaso 的 429 **不是纯 IP 维度** —— 同一出口 IP、同一查询：
    · 纯 HTTP（token + JSESSIONID 齐全）          → 秒 429（新链路帧 code=429）
    · 真浏览器（完整 JS 指纹 cookie：tid/_c_WBKFRo 等）→ 正常出结果
  即：限流键里含**浏览器侧身份**。旧结论「换 token 无效 ⇒ IP 维度」只测了
  token/JSESSIONID 两个变量，没测指纹 cookie —— 本脚本补上这个变量。

做法（对齐 qwen/tools/ident_pool.py 的既定模式）：
  Playwright 起一个无痕 Chromium，访问 metaso.cn 首页让阿里云 FP JS 自然运行，
  收集**全量 cookie**（含指纹项），存成 JSON 供 METASO_COOKIE 使用。
  全程**不发任何搜索请求** —— 铸造是零额度动作。

产物（--out，默认 metaso/var/identity.json）：
  {"cookie": "k=v; k=v; ...", "minted_at": iso8601, "ua": "...", "notes": [...]}
  `cookie` 直接填进 METASO_COOKIE（biz-api 的 MetasoChatClient 已支持整串注入）。

用法：
  python metaso/tools/mint_identity.py                  # 无头
  python metaso/tools/mint_identity.py --headed         # 有头（无头被指纹脚本识破时用）
  python metaso/tools/mint_identity.py --out /tmp/x.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: 指纹/会话 cookie 清单 —— 只决定**验什么**，不决定怎么造
EXPECTED = ("aliyungf_tc", "JSESSIONID", "tid", "_c_WBKFRo", "_nb_ioWEgULi")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

#: 本机 Playwright 缓存里已有的 Chromium（与 playwright 1.62 要求的 revision 不一致，
#: 用 executable_path 直接指，省掉 ~150MB 下载；失效时先 `playwright install chromium`）
CHROMIUM_CANDIDATES = (
    "~/Library/Caches/ms-playwright/chromium-1228/chrome-mac-arm64/"
    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "~/Library/Caches/ms-playwright/chromium-1223/chrome-mac-arm64/"
    "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
)


def _pick_executable() -> str | None:
    for raw in CHROMIUM_CANDIDATES:
        p = Path(raw).expanduser()
        if p.exists():
            return str(p)
    return None


def mint(*, headed: bool = False, settle_ms: int = 9000) -> dict:
    from playwright.sync_api import sync_playwright  # noqa: PLC0415 可选依赖（暂缓启用）

    exe = _pick_executable()
    notes: list[str] = []
    with sync_playwright() as pw:
        kwargs = {"headless": not headed}
        if exe:
            kwargs["executable_path"] = exe
            notes.append(f"executable_path={Path(exe).parts[-4]}")
        else:
            notes.append("executable_path 未命中缓存，用 playwright 默认浏览器")
        browser = pw.chromium.launch(**kwargs)
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="zh-CN",
                timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            page.goto("https://metaso.cn/", wait_until="domcontentloaded",
                      timeout=45_000)
            # 指纹 JS 通常在加载后异步收集环境并回种 cookie：停留 + 轻微鼠标移动
            page.mouse.move(200, 200)
            page.wait_for_timeout(settle_ms // 3)
            page.mouse.move(500, 380)
            page.wait_for_timeout(settle_ms // 3)
            page.mouse.move(760, 520)
            page.wait_for_timeout(settle_ms // 3)

            cookies = ctx.cookies(["https://metaso.cn"])
        finally:
            browser.close()

    jar = {c["name"]: c["value"] for c in cookies}
    missing = [k for k in EXPECTED if k not in jar]
    if missing:
        notes.append(f"⚠️ 缺少预期 cookie：{missing}（无头被指纹脚本识破？试 --headed）")
    else:
        notes.append("预期 cookie 全部到手")

    return {
        "cookie": "; ".join(f"{k}={v}" for k, v in jar.items()),
        "cookie_names": sorted(jar),
        "minted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ua": UA,
        "headed": headed,
        "settle_ms": settle_ms,
        "notes": notes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--headed", action="store_true", help="有头模式（反检测更强）")
    ap.add_argument("--settle-ms", type=int, default=9000,
                    help="首页停留时长（等指纹 JS），默认 9000")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "var" / "identity.json"))
    args = ap.parse_args()

    t0 = time.time()
    data = mint(headed=args.headed, settle_ms=args.settle_ms)
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"铸造完成 {time.time() - t0:.1f}s -> {out}")
    print(f"  cookie 项：{', '.join(data['cookie_names'])}")
    for n in data["notes"]:
        print(f"  {n}")
    return 0 if not any(n.startswith("⚠️") for n in data["notes"]) else 2


if __name__ == "__main__":
    sys.exit(main())
