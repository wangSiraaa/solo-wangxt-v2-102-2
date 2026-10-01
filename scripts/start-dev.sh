#!/usr/bin/env bash
# 开发环境一键启动（本仓库验证环境：Debian 12，PostgreSQL 经 conda-forge 安装）。
# 生产部署请使用系统服务管理 PostgreSQL/uvicorn，并通过 DATABASE_URL 指定连接。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PGDATA="${PGDATA:-$HOME/pgdata}"
PGPORT="${PGPORT:-5432}"
PGSOCKET="${PGSOCKET:-/tmp}"
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg2://postgres@127.0.0.1:${PGPORT}/spectrum}"
export PYTHONPATH="$ROOT/backend${PYTHONPATH:+:$PYTHONPATH}"

# 1) 定位 PostgreSQL（conda 环境 pg 或系统安装）
PG_BIN="${PG_BIN:-}"
if [[ -z "$PG_BIN" && -x "$HOME/miniforge3/envs/pg/bin/pg_ctl" ]]; then
  PG_BIN="$HOME/miniforge3/envs/pg/bin"
fi
if [[ -z "${PG_BIN}" ]]; then
  echo "未找到 PostgreSQL。可: conda create -n pg postgresql -c conda-forge，或设置 PG_BIN。" >&2
  exit 1
fi
export PATH="$PG_BIN:$PATH"

# 2) 初始化并启动本地数据库簇（trust，仅限本机开发）
if [[ ! -s "$PGDATA/PG_VERSION" ]]; then
  initdb -D "$PGDATA" -U postgres --auth=trust >/dev/null
fi
if ! pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
  pg_ctl -D "$PGDATA" -l "$PGDATA/server.log" -o "-p $PGPORT -k $PGSOCKET" start
fi
sleep 1
createdb -h "$PGSOCKET" -p "$PGPORT" -U postgres spectrum 2>/dev/null || true

# 3) 建表 + 示例掩模 + 教学演示场景
python3 -m app.seed

# 4) 启动后端（前台）；前端请另开终端: ( cd frontend && npm run dev )
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
