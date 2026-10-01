"""配置。数据库连接可用环境变量 DATABASE_URL 覆盖。"""
from __future__ import annotations

import os

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres@127.0.0.1:5432/spectrum",
)

# 跨域：开发期前端 Vite 默认端口
CORS_ORIGINS = os.environ.get(
    "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
).split(",")
