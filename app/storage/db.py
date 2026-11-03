"""SQLite 연결과 기본 CRUD (PRD v1.2 4장).

- DB 경로: 환경 변수 SCRAP_DB_PATH (상대 경로는 프로젝트 루트 기준), 기본값 data/scrap.db
- 쓰기 모드로 열면 테이블이 없을 때 schema.sql로 자동 생성한다.
- 읽기 전용 모드(readonly=True)는 Streamlit Cloud viewer 모드용이다 (PRD 3.6.3).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from app.config import PROJECT_ROOT

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "scrap.db"
TABLES = ("articles", "announcements", "adoption_history", "verify_tasks", "run_log")

KST = timezone(timedelta(hours=9))


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def get_db_path() -> Path:
    raw = os.environ.get("SCRAP_DB_PATH")
    if not raw:
        return DEFAULT_DB_PATH
    p = Path(raw)
    return p if p.is_absolute() else PROJECT_ROOT / p


def connect(path: str | Path | None = None, readonly: bool = False) -> sqlite3.Connection:
    db_path = Path(path) if path else get_db_path()
    if readonly:
        if not db_path.exists():
            raise FileNotFoundError(f"DB 파일이 없습니다: {db_path}")
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


def list_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


# ------------------------------------------------------------ 공통 헬퍼

_columns_cache: dict[str, set[str]] = {}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in TABLES:
        raise ValueError(f"알 수 없는 테이블: {table}")
    if table not in _columns_cache:
        _columns_cache[table] = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    return _columns_cache[table]


def _check_fields(conn: sqlite3.Connection, table: str, fields: Iterable[str]) -> None:
    unknown = set(fields) - _columns(conn, table)
    if unknown:
        raise ValueError(f"{table}에 없는 컬럼: {sorted(unknown)}")


def _encode(value: Any) -> Any:
    # dict/list는 JSON 문자열로 저장 (items_json, stats_json, queries_json)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def insert(conn: sqlite3.Connection, table: str, data: dict[str, Any]) -> int:
    _check_fields(conn, table, data)
    cols = list(data)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = conn.execute(sql, [_encode(data[c]) for c in cols])
    conn.commit()
    return int(cur.lastrowid)


def update(conn: sqlite3.Connection, table: str, row_id: int, data: dict[str, Any]) -> None:
    if not data:
        return
    _check_fields(conn, table, data)
    sets = ", ".join(f"{c} = ?" for c in data)
    conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*(_encode(v) for v in data.values()), row_id])
    conn.commit()


def get(conn: sqlite3.Connection, table: str, row_id: int) -> sqlite3.Row | None:
    _columns(conn, table)
    return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()


def select(conn: sqlite3.Connection, table: str, where: dict[str, Any] | None = None,
           order_by: str = "id") -> list[sqlite3.Row]:
    where = where or {}
    _check_fields(conn, table, where)
    _check_fields(conn, table, [order_by.split()[0]])
    sql = f"SELECT * FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(f"{c} = ?" for c in where)
    sql += f" ORDER BY {order_by}"
    return conn.execute(sql, list(where.values())).fetchall()


# ------------------------------------------------------------ articles (4.1)


def insert_article(conn: sqlite3.Connection, **fields: Any) -> int:
    """url_norm이 이미 있으면 sqlite3.IntegrityError."""
    fields.setdefault("collected_at", now_kst())
    return insert(conn, "articles", fields)


def get_article_by_url_norm(conn: sqlite3.Connection, url_norm: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM articles WHERE url_norm = ?", (url_norm,)).fetchone()


def article_exists(conn: sqlite3.Connection, url_norm: str) -> bool:
    return get_article_by_url_norm(conn, url_norm) is not None


def update_article(conn: sqlite3.Connection, article_id: int, **fields: Any) -> None:
    update(conn, "articles", article_id, fields)


# ------------------------------------------------------- announcements (4.2)


def insert_announcement(conn: sqlite3.Connection, **fields: Any) -> int:
    ts = now_kst()
    fields.setdefault("created_at", ts)
    fields.setdefault("updated_at", ts)
    return insert(conn, "announcements", fields)


def update_announcement(conn: sqlite3.Connection, announcement_id: int, **fields: Any) -> None:
    fields.setdefault("updated_at", now_kst())
    update(conn, "announcements", announcement_id, fields)


def list_announcements(conn: sqlite3.Connection, **where: Any) -> list[sqlite3.Row]:
    return select(conn, "announcements", where)


# ---------------------------------------------------- adoption_history (4.3)


def insert_adoption_history(conn: sqlite3.Connection, **fields: Any) -> int:
    fields.setdefault("changed_at", now_kst())
    return insert(conn, "adoption_history", fields)


# -------------------------------------------------------- verify_tasks (4.4)


def insert_verify_task(conn: sqlite3.Connection, **fields: Any) -> int:
    return insert(conn, "verify_tasks", fields)


def update_verify_task(conn: sqlite3.Connection, task_id: int, **fields: Any) -> None:
    update(conn, "verify_tasks", task_id, fields)


def list_verify_tasks(conn: sqlite3.Connection, **where: Any) -> list[sqlite3.Row]:
    return select(conn, "verify_tasks", where)


# ------------------------------------------------------------- run_log (4.5)


def start_run(conn: sqlite3.Connection, mode: str, date_from: str | None = None,
              date_to: str | None = None) -> int:
    return insert(conn, "run_log", {"started_at": now_kst(), "mode": mode,
                                    "date_from": date_from, "date_to": date_to})


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any]) -> None:
    update(conn, "run_log", run_id, {"ended_at": now_kst(), "status": status, "stats_json": stats})


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT 1").fetchone()


if __name__ == "__main__":
    # python -m app.storage.db : DB를 만들고 테이블 목록을 출력
    c = connect()
    print(f"DB: {get_db_path()}")
    print("tables:", ", ".join(list_tables(c)))
