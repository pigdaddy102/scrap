"""기사 선택 기준 — 결정적 select_all() (PRD v1.2 3.3).

같은 dedup_key 그룹에서 1건만 채택한다. 정렬 키:
  1) 유형 single(1) < multi(2) < history(3)
  2) 기사 게재일 오름차순
  3) 매체 order 오름차순 (스틸데일리 마지막)
  4) collected_at 오름차순
history가 1순위이면(원본 없음) 원본 추가 검색이 not_found일 때만 adopted, 그 전에는 adopted_provisional.
채택이 바뀌면 이전 채택은 replaced, adoption_history에 기록. 매번 전체를 다시 계산하며 같은 입력이면 결과가 같다.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from app.config import AppConfig
from app.storage import db

TYPE_RANK = {"single": 1, "multi": 2, "history": 3}
ADOPTED = ("adopted", "adopted_provisional")
REASON_DUPLICATE = "동일 발표 중복(우선순위 낮음)"
REASON_HAS_ORIGINAL = "원본 기사 있음"
REASON_EARLIER = "동일 발표, 더 이른 기사 있음"


@dataclass
class SelectSummary:
    groups: int = 0
    adopted: int = 0
    provisional: int = 0
    replaced: list[tuple[int, int, str]] = field(default_factory=list)  # (old, new, reason)
    conflicts: int = 0


def _candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT a.*, ar.published_date AS published_date, ar.media_id AS media_id,
                  ar.collected_at AS collected_at
             FROM announcements a JOIN articles ar ON ar.id = a.article_id
            ORDER BY a.id"""
    ).fetchall()


def _verify_status(conn: sqlite3.Connection, dedup_key: str) -> str | None:
    row = conn.execute("SELECT status FROM verify_tasks WHERE dedup_key = ? ORDER BY id DESC LIMIT 1",
                       (dedup_key,)).fetchone()
    return row["status"] if row else None


def _replace_reason(prev: sqlite3.Row, best: sqlite3.Row) -> str:
    if prev["source_type"] == "multi" and best["source_type"] == "single":
        return "multi_to_single"
    if prev["source_type"] == "history" and best["source_type"] in ("single", "multi"):
        return "history_to_original"
    return "earlier_article"


def _set(conn: sqlite3.Connection, row: sqlite3.Row, status: str, reason: str | None) -> None:
    if row["status"] != status or row["reject_reason"] != reason:
        db.update_announcement(conn, row["id"], status=status, reject_reason=reason)


def select_all(conn: sqlite3.Connection, cfg: AppConfig) -> SelectSummary:
    media_order = {m.id: m.order for m in cfg.media.media}
    rows = _candidates(conn)
    groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        groups[r["dedup_key"]].append(r)

    def sort_key(r: sqlite3.Row) -> tuple:
        return (TYPE_RANK[r["source_type"]], r["published_date"] or "9999-12-31",
                media_order.get(r["media_id"], 10_000), r["collected_at"] or "", r["id"])

    summary = SelectSummary(groups=len(groups))
    for key in sorted(groups):
        group = sorted(groups[key], key=sort_key)
        best = group[0]
        if best["source_type"] == "history":
            target = "adopted" if _verify_status(conn, key) == "not_found" else "adopted_provisional"
        else:
            target = "adopted"

        prev = next((r for r in group if r["status"] in ADOPTED), None)
        if prev is None:
            db.insert_adoption_history(conn, dedup_key=key, old_announcement_id=None,
                                       new_announcement_id=best["id"], reason="initial")
        elif prev["id"] != best["id"]:
            reason = _replace_reason(prev, best)
            _set(conn, prev, "replaced", f"교체됨: {reason}")
            db.insert_adoption_history(conn, dedup_key=key, old_announcement_id=prev["id"],
                                       new_announcement_id=best["id"], reason=reason)
            summary.replaced.append((prev["id"], best["id"], reason))

        _set(conn, best, target, None)
        summary.adopted += target == "adopted"
        summary.provisional += target == "adopted_provisional"

        for r in group[1:]:
            if prev is not None and r["id"] == prev["id"] and prev["id"] != best["id"]:
                continue  # 방금 replaced 처리
            if r["status"] == "replaced":
                continue  # 교체 이력은 유지
            if r["source_type"] == "history" and best["source_type"] != "history":
                why = REASON_HAS_ORIGINAL
            elif r["source_type"] == best["source_type"]:
                why = REASON_EARLIER
            else:
                why = REASON_DUPLICATE
            _set(conn, r, "rejected", why)

    summary.conflicts = _mark_conflicts(conn, rows)
    return summary


def _mark_conflicts(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> int:
    """같은 제강사·적용일에 dedup_key가 2개 이상이면 그 후보 전부 conflict=1 (PRD 3.3.3)."""
    keys_by_date: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in rows:
        keys_by_date[(r["steelmaker"], r["apply_date"])].add(r["dedup_key"])
    conflicted = {k for k, keys in keys_by_date.items() if len(keys) > 1}
    for r in rows:
        flag = int((r["steelmaker"], r["apply_date"]) in conflicted)
        if r["conflict"] != flag:
            db.update_announcement(conn, r["id"], conflict=flag)
    return len(conflicted)
