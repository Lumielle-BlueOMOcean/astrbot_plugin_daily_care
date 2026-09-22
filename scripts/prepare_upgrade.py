#!/usr/bin/env python3
"""Protect Daily Care data before AstrBot replaces the plugin directory.

AstrBot's plugin updater removes the old plugin directory before extracting a
successful update.  This helper must therefore run while the legacy database
still exists.  It uses the same SQLite backup and validation path as plugin
startup, but places the result in AstrBot's persistent plugin-data directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.database import (
    DatabaseMigrationError,
    _has_sqlite_sidecar,
    _is_valid_sqlite_database,
    prepare_data_dir,
)


PLUGIN_NAME = "astrbot_plugin_daily_care"
IMPORTANT_TABLES = ("care_targets", "care_events", "care_plans", "send_log", "kv")
MIGRATION_MANIFEST = ".daily_care_upgrade_manifest.json"


def _database_artifact_exists(database_path: Path) -> bool:
    return database_path.exists() or _has_sqlite_sidecar(database_path)


def _database_fingerprint(database_path: Path) -> dict[str, str]:
    """Hash the database and SQLite sidecars to detect post-backup writes."""
    result: dict[str, str] = {}
    for suffix in ("", "-wal", "-shm", "-journal"):
        path = Path(f"{database_path}{suffix}")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        result[suffix or "database"] = digest.hexdigest()
    return result


def _manifest_path(data_dir: Path) -> Path:
    return data_dir / MIGRATION_MANIFEST


def _read_manifest(data_dir: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(_manifest_path(data_dir).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(
        payload.get("source_fingerprint"), dict
    ):
        return None
    return payload


def _write_manifest(data_dir: Path, fingerprint: dict[str, str]) -> None:
    payload = {
        "format": 1,
        "source": "legacy plugin data/daily_care.db",
        "source_fingerprint": fingerprint,
    }
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{MIGRATION_MANIFEST}.",
        suffix=".tmp",
        dir=data_dir,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, _manifest_path(data_dir))
    except (OSError, TypeError) as exc:
        raise DatabaseMigrationError(f"无法写入升级保护记录: {exc}") from exc
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def prepare_upgrade(
    astrbot_root: str | os.PathLike,
    *,
    core_stopped: bool = False,
) -> Path:
    """Protect Daily Care data before replacing the plugin directory.

    Legacy migration is intentionally offline-only.  A caller must confirm
    that AstrBot Core is stopped so the old plugin cannot write after the
    consistent SQLite backup has completed.
    """
    root = Path(astrbot_root).expanduser().resolve()
    standard_dir = root / "data" / "plugin_data" / PLUGIN_NAME
    legacy_dir = root / "data" / "plugins" / PLUGIN_NAME / "data"
    standard_path = standard_dir / "daily_care.db"
    legacy_path = legacy_dir / "daily_care.db"
    standard_exists = _database_artifact_exists(standard_path)
    legacy_exists = _database_artifact_exists(legacy_path)

    if not standard_exists and not legacy_exists:
        raise DatabaseMigrationError(
            "未找到需要保护的 Daily Care 数据库；请核实 --astrbot-root 是否指向实际运行实例。"
            "全新安装无需运行此工具。"
        )

    if legacy_exists and not core_stopped:
        raise DatabaseMigrationError(
            "检测到插件目录中的旧版数据库。为避免一致性备份后继续写入，"
            "请先完整停止 AstrBot Core，并使用 --core-stopped 重新执行。"
        )

    if standard_exists and legacy_exists:
        data_dir = Path(prepare_data_dir(standard_dir, legacy_dir))
        manifest = _read_manifest(data_dir)
        if manifest is None:
            raise DatabaseMigrationError(
                "新旧 Daily Care 数据库同时存在，但没有可验证的升级保护记录；"
                "已停止以避免误判哪一份数据更新。"
            )
        if not _is_valid_sqlite_database(legacy_path):
            raise DatabaseMigrationError(f"旧版数据库不可验证: {legacy_path}")
        if manifest.get("source_fingerprint") != _database_fingerprint(legacy_path):
            raise DatabaseMigrationError(
                "检测到旧版数据库在保护后发生变化，可能存在新增写入；"
                "请停止升级并重新核对数据。"
            )
        return data_dir

    if standard_exists:
        return Path(prepare_data_dir(standard_dir, legacy_dir))

    before = _database_fingerprint(legacy_path)
    data_dir = Path(prepare_data_dir(standard_dir, legacy_dir))
    after = _database_fingerprint(legacy_path)
    if before != after:
        raise DatabaseMigrationError(
            "旧版数据库在一致性备份期间发生变化，未生成可确认的升级保护记录。"
        )
    _write_manifest(data_dir, after)
    return data_dir


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
    parser.add_argument(
        "--core-stopped",
        action="store_true",
        help="确认 AstrBot Core 已完整停止；旧版数据库迁移必须提供此选项。",
    )
    args = parser.parse_args()
    if not args.astrbot_root:
        parser.error("请提供 --astrbot-root 或设置 ASTRBOT_ROOT")

    try:
        root = Path(args.astrbot_root).expanduser().resolve()
        standard_path = root / "data" / "plugin_data" / PLUGIN_NAME / "daily_care.db"
        legacy_path = root / "data" / "plugins" / PLUGIN_NAME / "data" / "daily_care.db"
        standard_exists = _database_artifact_exists(standard_path)
        legacy_exists = _database_artifact_exists(legacy_path)
        data_dir = prepare_upgrade(
            args.astrbot_root,
            core_stopped=args.core_stopped,
        )
    except DatabaseMigrationError as exc:
        print(f"Daily Care 数据保护失败，已停止更新前准备：{exc}", file=sys.stderr)
        return 2

    database_path = data_dir / "daily_care.db"
    if database_path.exists():
        counts = _record_counts(database_path)
        print(f"Daily Care 数据已保护到：{database_path}")
        print("业务记录计数：" + ", ".join(f"{key}={value}" for key, value in counts.items()))
        if standard_exists and not legacy_exists:
            print("标准数据目录中的数据库已验证有效；本次未执行迁移，可按正常流程更新插件。")
        elif legacy_exists and not standard_exists:
            print("旧版数据库已完成一致性保护；请在 Core 保持停止期间继续替换插件文件。")
        else:
            print("已核对升级保护记录与旧库未发生变化；请勿在 Core 运行时继续旧库写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
