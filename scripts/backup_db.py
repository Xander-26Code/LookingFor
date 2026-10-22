"""创建 SQLite 一致性备份；建议每天通过 cron 运行。

用法：
    python3 scripts/backup_db.py

环境变量：
    DATABASE_PATH   默认 ./lookingfor.db
    BACKUP_DIR      默认 ./backups
    BACKUP_KEEP     保留最近几个备份，默认 7；设为 0 不清理
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import database  # noqa: E402


def main() -> None:
    backup_dir = Path(os.getenv("BACKUP_DIR", str(ROOT / "backups"))).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"lookingfor-{time.strftime('%Y%m%d-%H%M%S')}.db"
    database.backup_to(target)

    keep = int(os.getenv("BACKUP_KEEP", "7"))
    if keep > 0:
        backups = sorted(backup_dir.glob("lookingfor-*.db"))
        for old in backups[:-keep]:
            old.unlink()
    print(target)


if __name__ == "__main__":
    main()
