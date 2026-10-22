"""用户资料的持久化存储；每次操作单独打开并关闭数据库连接。"""

import sqlite3
import json
import uuid
from contextlib import closing
from pathlib import Path

DATABASE_PATH = Path(__file__).resolve().parent / "lookingfor.db"


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    """应用启动时建表；重复执行不会清空已有用户。"""
    connection = connect()
    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    github_id INTEGER PRIMARY KEY,
                    login TEXT NOT NULL,
                    name TEXT,
                    avatar_url TEXT
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS star_snapshots (
                    user_id INTEGER PRIMARY KEY REFERENCES users(github_id),
                    version TEXT NOT NULL,
                    repositories_json TEXT NOT NULL,
                    synced_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analyses (
                    user_id INTEGER PRIMARY KEY REFERENCES users(github_id),
                    snapshot_version TEXT NOT NULL,
                    keywords_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
    finally:
        connection.close()


def save_user(user_info: dict) -> dict:
    """按 GitHub ID 新增或更新资料，成功提交后返回可写入会话的资料。"""
    if not isinstance(user_info, dict):
        raise ValueError("GitHub user info must be a dictionary.")
    github_id = user_info.get("id")
    login = user_info.get("login")
    if type(github_id) is not int or github_id <= 0:
        raise ValueError("GitHub user ID must be a positive integer.")
    if not isinstance(login, str) or not login.strip():
        raise ValueError("GitHub username is missing.")

    name = user_info.get("name")
    avatar_url = user_info.get("avatar_url")
    user = {
        "id": github_id,
        "login": login,
        "name": name if isinstance(name, str) and name else login,
        "avatar_url": avatar_url if isinstance(avatar_url, str) else "",
    }

    connection = connect()
    try:
        with connection:
            # ? 占位符让 SQLite 处理参数，不把用户名等内容拼进 SQL。
            connection.execute("""
                INSERT INTO users (github_id, login, name, avatar_url)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(github_id) DO UPDATE SET
                    login = excluded.login,
                    name = excluded.name,
                    avatar_url = excluded.avatar_url
            """, (user["id"], user["login"], user["name"], user["avatar_url"]))
    finally:
        connection.close()

    return user


def validate_user_id(user_id: int) -> None:
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("A valid GitHub user ID is required.")


def save_star_snapshot(user_id: int, repositories: list[dict]) -> str:
    """整批同步成功后替换该用户的收藏，并使之前的分析失效。"""
    validate_user_id(user_id)
    if not isinstance(repositories, list) or not all(isinstance(repo, dict) for repo in repositories):
        raise ValueError("Stars must be a list of repositories.")
    content = json.dumps(repositories, ensure_ascii=False)
    version = uuid.uuid4().hex
    with closing(connect()) as connection, connection:
        connection.execute("""
            INSERT INTO star_snapshots (user_id, version, repositories_json)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                version = excluded.version,
                repositories_json = excluded.repositories_json,
                synced_at = CURRENT_TIMESTAMP
        """, (user_id, version, content))
        connection.execute("DELETE FROM analyses WHERE user_id = ?", (user_id,))
    return version


def load_user_data(user_id: int) -> dict | None:
    """一次查询取得同一用户、同一版本的收藏与关键词；未同步返回 None。"""
    validate_user_id(user_id)
    with closing(connect()) as connection:
        row = connection.execute("""
            SELECT s.version, s.repositories_json, a.keywords_json
            FROM star_snapshots AS s
            LEFT JOIN analyses AS a
                ON a.user_id = s.user_id AND a.snapshot_version = s.version
            WHERE s.user_id = ?
        """, (user_id,)).fetchone()
    if row is None:
        return None
    return {
        "version": row[0],
        "repositories": json.loads(row[1]),
        "keywords": json.loads(row[2]) if row[2] is not None else None,
    }


def save_analysis(user_id: int, snapshot_version: str, keywords: list[str]) -> bool:
    """仅当收藏版本仍匹配时保存；旧任务完成后返回 False，不覆盖新结果。"""
    validate_user_id(user_id)
    if not isinstance(keywords, list) or not keywords or not all(
        isinstance(keyword, str) and keyword.strip() for keyword in keywords
    ):
        raise ValueError("Keywords must be a nonempty list of strings.")
    with closing(connect()) as connection, connection:
        # 条件检查和写入是同一条 SQL，避免检查后恰好被另一任务更新。
        cursor = connection.execute("""
            INSERT INTO analyses (user_id, snapshot_version, keywords_json)
            SELECT user_id, version, ? FROM star_snapshots
            WHERE user_id = ? AND version = ?
            ON CONFLICT(user_id) DO UPDATE SET
                snapshot_version = excluded.snapshot_version,
                keywords_json = excluded.keywords_json,
                created_at = CURRENT_TIMESTAMP
        """, (json.dumps(keywords, ensure_ascii=False), user_id, snapshot_version))
        return cursor.rowcount == 1
