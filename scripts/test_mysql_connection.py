"""Verify MySQL connectivity using the project's .env settings.

This script is read-only: it connects, executes ``SELECT 1``, then closes the
connection.  It never prints the configured password.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    # Running ``python scripts/test_mysql_connection.py`` makes Python search
    # from ``scripts`` first.  Add the project root so ``app`` is available
    # regardless of the caller's current directory.
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    env_path = project_root / ".env"
    if not env_path.is_file():
        print(f"找不到 .env：{env_path}", file=sys.stderr)
        return 2
    load_dotenv(env_path)

    try:
        from app.services.mysql_instrument_repository import MySQLSettings
        settings = MySQLSettings.from_env()
        import mysql.connector
    except Exception as error:
        print(f"設定或 MySQL driver 載入失敗：{error}", file=sys.stderr)
        return 2

    try:
        # Verify the server and credentials first.  This still works before
        # POST /database/init has created the configured database.
        connection = mysql.connector.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            connection_timeout=10,
        )
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        connection.close()
    except mysql.connector.Error as error:
        print(
            "MySQL 連線失敗："
            f"{error.msg}（host={settings.host}、port={settings.port}、"
            f"user={settings.user}）",
            file=sys.stderr,
        )
        return 1

    try:
        connection = mysql.connector.connect(
            host=settings.host,
            port=settings.port,
            user=settings.user,
            password=settings.password,
            database=settings.database,
            connection_timeout=10,
        )
        connection.close()
    except mysql.connector.Error as error:
        if error.errno == 1049:  # ER_BAD_DB_ERROR
            print(
                "MySQL 伺服器與帳密連線成功，但資料庫尚未建立："
                f"{settings.database}。請先呼叫 POST /api/v1/database/init"
            )
            return 3
        print(f"MySQL 已連上伺服器，但無法使用資料庫 {settings.database}：{error.msg}", file=sys.stderr)
        return 1

    print(
        "MySQL 連線成功："
        f"host={settings.host}、port={settings.port}、"
        f"user={settings.user}、database={settings.database}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
