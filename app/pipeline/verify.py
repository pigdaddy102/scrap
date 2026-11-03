"""이력정리 기사 기반 원본 추가 검색 (PRD v1.2 3.4).

흐름
  ensure_tasks(): single/multi 후보 없이 history 후보만 있는 그룹마다 verify_task(pending) 생성
  run_verification(search_fn, process_fn):
    pending 작업(최대 max_tasks_per_run)마다 검색 → 찾은 기사를 process_fn으로 일반 파이프라인 처리
    → select_all() → 그룹에 원본 후보가 생겼으면 found, 아니면 attempts += 1 (max_attempts 이상이면 not_found)
    → select_all() 다시 (not_found 그룹의 history를 adopted로)

search_fn과 process_fn은 주입한다. 실제 수집기 연결은 2-2단계(run.py)에서 한다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from app.config import AppConfig
from app.pipeline.select import select_all
from app.storage import db

ORIGINAL_TYPES = ("single", "multi")


@dataclass(frozen=True)
class VerifyQuery:
    """원본 추가 검색 조건 1건 (제강사·기간 단위)."""

    steelmaker: str
    aliases: tuple[str, ...]
    date_from: date
    date_to: date

    @property
    def keyword(self) -> str:
        # site_search 매체: 제강사 정식 이름 1단어 (두 단어 검색은 0건)
        return self.steelmaker

    def describe(self) -> dict:
        return {"site_search_keyword": self.keyword, "section_list_title_filter": list(self.aliases),
                "date_from": self.date_from.isoformat(), "date_to": self.date_to.isoformat()}


SearchFn = Callable[[VerifyQuery], Iterable[Any]]
ProcessFn = Callable[[Iterable[Any]], int]


@dataclass
class VerifySummary:
    tasks_run: int = 0
    found: int = 0
    not_found: int = 0
    still_pending: int = 0
    searches: int = 0
    articles_found: int = 0
    task_ids: list[int] = field(default_factory=list)


def build_query(cfg: AppConfig, steelmaker: str, apply_date: str) -> VerifyQuery:
    d = date.fromisoformat(apply_date)
    s = cfg.steelmakers.get(steelmaker)
    aliases = tuple(dict.fromkeys([s.name, *s.aliases]))
    return VerifyQuery(steelmaker, aliases, d - timedelta(days=cfg.settings.verify.search_window_days), d)


def _groups_with_types(conn: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in conn.execute("SELECT dedup_key, source_type FROM announcements"):
        out.setdefault(r["dedup_key"], set()).add(r["source_type"])
    return out


def ensure_tasks(conn: sqlite3.Connection, cfg: AppConfig) -> list[int]:
    """history 후보만 있는 그룹에 pending 작업을 만든다. 이미 작업이 있는 그룹은 건너뛴다."""
    created = []
    existing = {r["dedup_key"] for r in conn.execute("SELECT dedup_key FROM verify_tasks")}
    for key, types in sorted(_groups_with_types(conn).items()):
        if key in existing or not types <= {"history"}:
            continue
        hist = conn.execute(
            "SELECT id, steelmaker, apply_date FROM announcements WHERE dedup_key = ? AND source_type = 'history' "
            "ORDER BY id LIMIT 1", (key,)).fetchone()
        q = build_query(cfg, hist["steelmaker"], hist["apply_date"])
        created.append(db.insert_verify_task(
            conn, dedup_key=key, history_announcement_id=hist["id"], steelmaker=hist["steelmaker"],
            apply_date=hist["apply_date"], queries_json=q.describe(), status="pending", attempts=0))
    return created


def _original_article_id(conn: sqlite3.Connection, dedup_key: str) -> int | None:
    row = conn.execute(
        "SELECT article_id FROM announcements WHERE dedup_key = ? AND source_type IN ('single', 'multi') "
        "ORDER BY CASE status WHEN 'adopted' THEN 0 ELSE 1 END, id LIMIT 1", (dedup_key,)).fetchone()
    return row["article_id"] if row else None


def run_verification(conn: sqlite3.Connection, cfg: AppConfig, search_fn: SearchFn,
                     process_fn: ProcessFn) -> VerifySummary:
    summary = VerifySummary()
    ensure_tasks(conn, cfg)

    # 정규 수집으로 이미 원본이 들어온 pending 작업은 검색 없이 found
    for t in db.list_verify_tasks(conn, status="pending"):
        art = _original_article_id(conn, t["dedup_key"])
        if art is not None:
            db.update_verify_task(conn, t["id"], status="found", found_article_id=art, last_searched_at=db.now_kst())
            summary.found += 1

    tasks = db.list_verify_tasks(conn, status="pending")[: cfg.settings.verify.max_tasks_per_run]
    searched: set[VerifyQuery] = set()
    for t in tasks:
        q = build_query(cfg, t["steelmaker"], t["apply_date"])
        if q not in searched:  # 같은 제강사·기간은 한 실행에서 한 번만 검색
            searched.add(q)
            summary.searches += 1
            metas = list(search_fn(q))
            if metas:
                summary.articles_found += process_fn(metas)
        summary.task_ids.append(t["id"])
    summary.tasks_run = len(tasks)

    select_all(conn, cfg)

    max_attempts = cfg.settings.verify.max_attempts
    for t in tasks:
        art = _original_article_id(conn, t["dedup_key"])
        attempts = t["attempts"] + 1
        if art is not None:
            db.update_verify_task(conn, t["id"], status="found", found_article_id=art, attempts=attempts,
                                  last_searched_at=db.now_kst())
            summary.found += 1
        elif attempts >= max_attempts:
            db.update_verify_task(conn, t["id"], status="not_found", attempts=attempts, last_searched_at=db.now_kst())
            summary.not_found += 1
        else:
            db.update_verify_task(conn, t["id"], attempts=attempts, last_searched_at=db.now_kst())
            summary.still_pending += 1

    select_all(conn, cfg)
    return summary


def queries_of(task: sqlite3.Row) -> dict:
    return json.loads(task["queries_json"]) if task["queries_json"] else {}
