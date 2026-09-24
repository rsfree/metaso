#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""metaso-service · 秘塔 AI 搜索（metaso.cn）的免登录搜索对话服务。

对外只暴露一个契约面：`POST /v1/chat/completions`（对齐 OpenAI Chat Completions，
带 citations / reasoning_content 标准扩展）。上游契约与频控两层模型见 docs/UPSTREAM.md。
"""

__version__ = "0.1.0"
