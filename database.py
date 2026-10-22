"""用户资料的持久化存储；每次操作单独打开并关闭数据库连接。"""

import os
import sqlite3
import json
import uuid
import time
from contextlib import closing
from pathlib import Path

DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", str(Path(__file__).resolve().parent / "lookingfor.db"))
).expanduser().resolve()


def connect() -> sqlite3.Connection:
    # 多个标签页/后台任务会同时读写；WAL + busy_timeout 让它们排队而不是直接报 locked。
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def init_db() -> None:
    """应用启动时建表；重复执行不会清空已有用户。"""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = connect()
    try:
        # WAL 允许读请求不被写事务阻塞，适合页面刷新和后台任务同时访问。
        connection.execute("PRAGMA journal_mode = WAL")
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
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_jobs (
                    user_id INTEGER PRIMARY KEY REFERENCES users(github_id),
                    job_id TEXT NOT NULL,
                    snapshot_version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_until REAL NOT NULL,
                    error TEXT NOT NULL DEFAULT ''
                )
            """)
            connection.execute("""
                CREATE TABLE IF NOT EXISTS analysis_attempts (
                    user_id INTEGER NOT NULL REFERENCES users(github_id),
                    started_at REAL NOT NULL
                )
            """)
            connection.execute('CREATE INDEX IF NOT EXISTS attempts_user_time ON analysis_attempts(user_id, started_at)')
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
    """更新完整收藏；只有用于分析的内容变化时，才更换版本并清除分析。"""
    validate_user_id(user_id)
    if not isinstance(repositories, list) or not all(isinstance(repo, dict) for repo in repositories):
        raise ValueError("Stars must be a list of repositories.")
    content = json.dumps(repositories, ensure_ascii=False)
    with closing(connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")

        old_snapshot = connection.execute(
            """
            SELECT version, repositories_json
            FROM star_snapshots
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        unchanged = False

        if old_snapshot is not None:
            old_repositories = json.loads(old_snapshot[1])
            unchanged = (
                analysis_signature(old_repositories)
                == analysis_signature(repositories)
            )

        if unchanged:
            version = old_snapshot[0]
        else:
            version = uuid.uuid4().hex

        # 即使无需重新分析，也更新 Star 数等展示字段以及同步时间。
        connection.execute("""
            INSERT INTO star_snapshots (user_id, version, repositories_json)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                version = excluded.version,
                repositories_json = excluded.repositories_json,
                synced_at = CURRENT_TIMESTAMP
        """, (user_id, version, content))
        if not unchanged:
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


def analysis_signature(repositories: list[dict]) -> str:
    """生成用于判断是否需要重新分析的稳定表示，忽略顺序和热度变化。"""
    temp_list = []
    for repo in repositories:
        repo_id = repo.get("id")
        full_name = repo.get("full_name")
        description = repo.get("description")
        language = repo.get("language")
        topics = sorted(repo.get("topics") or [])
        temp_list.append({
            "id": repo_id,
            "full_name": full_name,
            "description": description,
            "language": language,
            "topics": topics
        })
    # 仓库 ID 唯一；整份列表构造完后只需要排序一次。
    temp_list.sort(key=lambda repo: repo["id"])
    # 将列表转换为 JSON 字符串，并确保键的顺序一致
    json_string = json.dumps(temp_list, ensure_ascii=False, sort_keys=True)
    return json_string


def claim_analysis(user_id: int, *, max_active: int = 2, cooldown: float = 60,
                   daily_limit: int = 10, lease_seconds: float = 180) -> dict:
    """短事务内完成查缓存、任务去重、全站并发和个人额度检查；不调用模型。"""
    validate_user_id(user_id)
    now = time.time()
    with closing(connect()) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("UPDATE analysis_jobs SET status = 'interrupted', error = 'interrupted' "
                           "WHERE status = 'running' AND lease_until <= ?", (now,))
        data = load_user_data(user_id)
        if not data or not data['repositories']:
            return {'status': 'empty'}
        if data['keywords']:
            return {'status': 'succeeded'}
        job = connection.execute('SELECT job_id FROM analysis_jobs WHERE user_id = ? AND status = ?',
                                 (user_id, 'running')).fetchone()
        if job:
            return {'status': 'running', 'job_id': job[0], 'joined': True}
        connection.execute('DELETE FROM analysis_attempts WHERE started_at <= ?', (now - 86400,))
        attempts = connection.execute('SELECT started_at FROM analysis_attempts WHERE user_id = ? ORDER BY started_at',
                                      (user_id,)).fetchall()
        if len(attempts) >= daily_limit:
            return {'status': 'limited', 'wait': max(1, attempts[0][0] + 86400 - now)}
        if attempts and now - attempts[-1][0] < cooldown:
            return {'status': 'limited', 'wait': max(1, attempts[-1][0] + cooldown - now)}
        active = connection.execute("SELECT COUNT(*) FROM analysis_jobs WHERE status = 'running'").fetchone()[0]
        if active >= max_active:
            return {'status': 'busy'}
        job_id = uuid.uuid4().hex
        connection.execute("""
            INSERT INTO analysis_jobs (user_id, job_id, snapshot_version, status, lease_until, error)
            VALUES (?, ?, ?, 'running', ?, '')
            ON CONFLICT(user_id) DO UPDATE SET job_id = excluded.job_id,
                snapshot_version = excluded.snapshot_version, status = 'running',
                lease_until = excluded.lease_until, error = ''
        """, (user_id, job_id, data['version'], now + lease_seconds))
        connection.execute('INSERT INTO analysis_attempts VALUES (?, ?)', (user_id, now))
        return {'status': 'running', 'job_id': job_id, 'joined': False, 'data': data}


def renew_analysis(user_id: int, job_id: str, lease_seconds: float = 180) -> bool:
    with closing(connect()) as connection, connection:
        cursor = connection.execute("UPDATE analysis_jobs SET lease_until = ? WHERE user_id = ? "
                                    "AND job_id = ? AND status = 'running' AND lease_until > ?",
                                    (time.time() + lease_seconds, user_id, job_id, time.time()))
        return cursor.rowcount == 1


def finish_analysis(user_id: int, job_id: str, keywords: list[str] | None, error: str = '') -> None:
    """任务所有权和版本检查、保存结果、结束任务在同一事务内完成。"""
    with closing(connect()) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        job = connection.execute("SELECT snapshot_version FROM analysis_jobs WHERE user_id = ? "
                                 "AND job_id = ? AND status = 'running' AND lease_until > ?",
                                 (user_id, job_id, time.time())).fetchone()
        if job is None:
            return
        current = connection.execute('SELECT version FROM star_snapshots WHERE user_id = ?', (user_id,)).fetchone()
        status = 'failed'
        if not current or current[0] != job[0]:
            status = 'stale'
        elif not error and keywords and all(isinstance(k, str) and k.strip() for k in keywords):
            connection.execute("""
                INSERT INTO analyses (user_id, snapshot_version, keywords_json) VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET snapshot_version = excluded.snapshot_version,
                    keywords_json = excluded.keywords_json, created_at = CURRENT_TIMESTAMP
            """, (user_id, job[0], json.dumps(keywords, ensure_ascii=False)))
            status = 'succeeded'
        connection.execute('UPDATE analysis_jobs SET status = ?, error = ? WHERE user_id = ? AND job_id = ?',
                           (status, error or ('empty_result' if status == 'failed' else ''), user_id, job_id))


def get_analysis_job(user_id: int) -> dict | None:
    validate_user_id(user_id)
    with closing(connect()) as connection, connection:
        connection.execute("UPDATE analysis_jobs SET status = 'interrupted', error = 'interrupted' "
                           "WHERE user_id = ? AND status = 'running' AND lease_until <= ?", (user_id, time.time()))
        row = connection.execute('SELECT job_id, status, error FROM analysis_jobs WHERE user_id = ?', (user_id,)).fetchone()
    return {'job_id': row[0], 'status': row[1], 'error': row[2]} if row else None


def ping() -> None:
    """健康检查用：数据库可连接且能执行查询。失败时抛 sqlite3.Error。"""
    with closing(connect()) as connection:
        connection.execute("SELECT 1").fetchone()


def delete_user_data(user_id: int) -> bool:
    """删除指定用户的收藏、分析、任务和账号行；不会影响其他用户。"""
    validate_user_id(user_id)
    with closing(connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        exists = connection.execute(
            "SELECT 1 FROM users WHERE github_id = ?", (user_id,)
        ).fetchone()
        if exists is None:
            return False
        # 先删子表，兼容旧库没有 ON DELETE CASCADE 的情况。
        for table in ("analysis_attempts", "analysis_jobs", "analyses", "star_snapshots"):
            connection.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM users WHERE github_id = ?", (user_id,))
        return True


def backup_to(destination: Path) -> Path:
    """用 SQLite 在线备份 API 生成一致性快照，包含 WAL 中尚未合并的数据。"""
    destination = Path(destination).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect()) as source, closing(sqlite3.connect(destination)) as target:
        source.backup(target)
    return destination
