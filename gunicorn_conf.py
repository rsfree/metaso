"""gunicorn 配置：单 worker —— 节流闸门与冷却窗口是进程内状态，多 worker 会形同虚设。"""
import os

bind = f"{os.environ.get('HOST', '0.0.0.0')}:{os.environ.get('PORT', '8110')}"
workers = 1
worker_class = "uvicorn.workers.UvicornWorker"
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
