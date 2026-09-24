#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""/llms.txt 与 / 落地页的 4 条防漂移门禁（fleet 约定，见 skill llms-txt-fleet-convention）。"""
from __future__ import annotations

import dataclasses

from app import llms_txt
from app.client import METASO_MODELS
from app.config import get_settings
from app.errors import ServiceError


def test_llms_txt_public_shape(api):
    """门禁①：公开性与形态 —— markdown 以 `# 服务名` 开头；落地页是 HTML；都免鉴权。"""
    r = api.get("/llms.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# metaso-service")
    assert "Bearer" in r.text or "鉴权" in r.text  # 鉴权口径必须出现
    html = api.get("/")
    assert html.status_code == 200
    assert html.headers["content-type"].startswith("text/html")
    assert "metaso-service" in html.text


def test_capability_table_matches_registry(api):
    """门禁②：与注册表对账 —— 每个模型 id 都在正文；注册计数 == len(注册表)。"""
    text = api.get("/llms.txt").text
    for m in METASO_MODELS:
        assert f"`{m.model}`" in text, m.model
    assert f"注册 {len(METASO_MODELS)} 项" in text
    assert f"本部署可用 {len(METASO_MODELS)} 项" in text  # conftest 环境 ready=True


def test_error_table_derived_no_ghost_codes():
    """门禁③：错误表与异常类双向一致 —— 表从类派生，且覆盖全部子类（无幽灵/无遗漏）。"""
    def subclasses(cls):
        for sub in cls.__subclasses__():
            yield sub
            yield from subclasses(sub)

    settings = get_settings()
    text = llms_txt.render_llms_txt(settings)
    rows = llms_txt._error_rows()
    table_codes = {code for code, *_ in rows}
    derived_codes = {cls.code for cls in subclasses(ServiceError)}
    assert table_codes == derived_codes, "错误表必须与 errors.py 异常类同源"
    for code, _, _, _, meaning in rows:
        assert f"`{code}`" in text
        assert meaning, "每个错误行都要有含义（来自类 docstring）"


def test_counts_change_with_ready():
    """门禁④：计数随开关变化 —— 未启用时可用数必须归零（防两套文案都说满）。"""
    ready = dataclasses.replace(get_settings(), ms_enabled=True, ms_cookie="")
    frozen = dataclasses.replace(get_settings(), ms_enabled=False, ms_cookie="")
    total = len(METASO_MODELS)
    assert f"本部署可用 {total} 项" in llms_txt.render_llms_txt(ready)
    assert "本部署可用 0 项" in llms_txt.render_llms_txt(frozen)
    assert f"本部署可用 {total} 项" not in llms_txt.render_llms_txt(frozen)
