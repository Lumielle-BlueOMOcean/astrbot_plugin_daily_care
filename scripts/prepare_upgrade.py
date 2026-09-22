#!/usr/bin/env python3
"""Protect Daily Care data before AstrBot replaces the plugin directory.

AstrBot's plugin updater removes the old plugin directory before extracting a
successful update.  This helper must therefore run while the legacy database
still exists.  It uses the same SQLite backup and validation path as plugin
startup, but places the result in AstrBot's persistent plugin-data directory.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.database import DatabaseMigrationError, prepare_data_dir


PLUGIN_NAME = "astrbot_plugin_daily_care"
IMPORTANT_TABLES = ("care_targets", "care_events", "care_plans", "send_log", "kv")


def prepare_upgrade(astrbot_root: str | os.PathLike) -> Path:
    """Protect the legacy database and return the persistent data directory."""
    root = Path(astrbot_root).expanduser().resolve()
    standard_dir = root / "data" / "plugin_data" / PLUGIN_NAME
    legacy_dir = root / "data" / "plugins" / PLUGIN_NAME / "data"
    return Path(prepare_data_dir(standard_dir, legacy_dir))


def _record_counts(database_path: Path) -> dict[str, int]:
    with sqlite3.connect(database_path) as conn:
        return {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in IMPORTANT_TABLES
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在 AstrBot 更新 Daily Care 前保护旧版 SQLite 数据。"
    )
    parser.add_argument(
        "--astrbot-root",
        default=os.environ.get("ASTRBOT_ROOT"),
        help="AstrBot 根目录；也可以通过 ASTRBOT_ROOT 提供。",
    )
    args = parser.parse_args()
    if not args.astrbot_root:
        parser.error("请提供 --astrbot-root 或设置 ASTRBOT_ROOT")

    try:
        data_dir = prepare_upgrade(args.astrbot_root)
    except DatabaseMigrationError as exc:
        print(f"Daily Care 数据保护失败，已停止更新前准备：{exc}", file=sys.stderr)
        return 2

    database_path = data_dir / "daily_care.db"
    if database_path.exists():
        counts = _record_counts(database_path)
        print(f"Daily Care 数据已保护到：{database_path}")
        print("业务记录计数：" + ", ".join(f"{key}={value}" for key, value in counts.items()))
    else:
        print("未发现旧版 Daily Care 数据库；未创建空数据库。")
    print("现在可以继续使用 AstrBot 的插件更新流程。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
