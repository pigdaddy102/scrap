"""화면용 데이터 함수 — DB 조회, 필터, 달력 이벤트 변환 (PRD v1.3 3.5). UI와 분리해 단위 테스트한다."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import AppConfig
from app.export.formatter import OutputRow, format_apply_date
from app.pipeline.reconcile import ReconcileReport, reconcile

ADOPTED = ("adopted", "adopted_provisional")
SYMBOL = {"up": "▲", "down": "▼", "mixed": "◆"}
BORDER = {"up": "#d62728", "down": "#1f4fd6", "mixed": "#7f7f7f"}
DIRECTION_LABEL = {"up": "인상", "down": "인하", "mixed": "혼합"}
TYPE_LABEL = {"single": "단일 단가발표", "multi": "복수 제강사 발표", "history": "단가이력 정리", "none": "해당 없음"}
STATUS_LABEL = {"adopted": "채택", "adopted_provisional": "채택(원본 확인 중)", "rejected": "탈락",
                "replaced": "교체됨", "candidate": "후보"}


@dataclass
class Filters:
    date_from: date
    date_to: date
    steelmakers: set[str]
    date_basis: str = "apply"  # apply(적용일자) | published(기사발표일)
    directions: set[str] = field(default_factory=lambda: {"up", "down", "mixed"})
    include_history: bool = True


# ------------------------------------------------------------ 조회

ANN_SQL = """
SELECT a.id, a.article_id, a.steelmaker, a.apply_date, a.apply_date_inferred, a.items_json, a.content_text,
       a.direction, a.source_type, a.dedup_key, a.status, a.reject_reason, a.confidence, a.needs_review, a.conflict,
       ar.media_id, ar.media_name, ar.title, ar.url, ar.published_date, ar.collected_at, ar.article_type,
       ar.access_level
  FROM announcements a JOIN articles ar ON ar.id = a.article_id
"""


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["items"] = json.loads(d.pop("items_json") or "[]")
    return d


def load_adopted(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(ANN_SQL + " WHERE a.status IN (?, ?)", ADOPTED).fetchall()
    return [_row(r) for r in rows]


def load_announcement(conn: sqlite3.Connection, ann_id: int) -> dict[str, Any] | None:
    r = conn.execute(ANN_SQL + " WHERE a.id = ?", (ann_id,)).fetchone()
    return _row(r) if r else None


def load_group(conn: sqlite3.Connection, dedup_key: str) -> list[dict[str, Any]]:
    """같은 발표 그룹의 모든 후보 (채택 포함)."""
    rows = conn.execute(ANN_SQL + " WHERE a.dedup_key = ? ORDER BY ar.published_date, a.id", (dedup_key,)).fetchall()
    return [_row(r) for r in rows]


def load_adoption_history(conn: sqlite3.Connection, dedup_key: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT h.changed_at, h.reason, h.old_announcement_id, h.new_announcement_id,
                  oa.url AS old_url, oa.media_name AS old_media, na.url AS new_url, na.media_name AS new_media
             FROM adoption_history h
             LEFT JOIN announcements o ON o.id = h.old_announcement_id LEFT JOIN articles oa ON oa.id = o.article_id
             LEFT JOIN announcements n ON n.id = h.new_announcement_id LEFT JOIN articles na ON na.id = n.article_id
            WHERE h.dedup_key = ? ORDER BY h.id""", (dedup_key,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ 필터 (달력·목록·출력 공통)


def basis_date(row: dict[str, Any], basis: str) -> str | None:
    return row["published_date"] if basis == "published" else row["apply_date"]


def apply_filters(rows: list[dict[str, Any]], f: Filters) -> list[dict[str, Any]]:
    lo, hi = f.date_from.isoformat(), f.date_to.isoformat()
    out = []
    for r in rows:
        d = basis_date(r, f.date_basis)
        if not d or not lo <= d <= hi:
            continue
        if r["steelmaker"] not in f.steelmakers or r["direction"] not in f.directions:
            continue
        if not f.include_history and r["source_type"] == "history":
            continue
        out.append(r)
    return out


def sort_rows(rows: list[dict[str, Any]], cfg: AppConfig) -> list[dict[str, Any]]:
    """적용일자 오름차순, 제강사 설정 순서 (PRD 3.5.5)."""
    names = cfg.steelmakers.names()
    return sorted(rows, key=lambda r: (r["apply_date"], names.index(r["steelmaker"]) if r["steelmaker"] in names
                                       else 99, r["published_date"] or "", r["id"]))


def to_output_rows(rows: list[dict[str, Any]], cfg: AppConfig) -> list[OutputRow]:
    return [OutputRow(r["steelmaker"], r["apply_date"], r["items"], r["published_date"], r["url"], r["source_type"],
                      cfg.steelmakers.plant_order(r["steelmaker"]), r["content_text"])
            for r in sort_rows(rows, cfg)]


# ------------------------------------------------------------ 달력 이벤트


def representative_change(row: dict[str, Any]) -> str:
    if row["direction"] == "mixed":
        return "혼합"
    changes = [int(i["change_krw_per_kg"]) for i in row["items"]]
    top = max(changes, key=abs) if changes else 0
    return f"{top:+d}"


def flags(row: dict[str, Any]) -> list[str]:
    out = []
    if row["status"] == "adopted_provisional":
        out.append("원본 확인 중")
    if row["needs_review"]:
        out.append("검토 필요")
    if row["conflict"]:
        out.append("충돌")
    if row["apply_date_inferred"]:
        out.append("적용일 추론")
    if row.get("access_level") == "partial":
        out.append("본문 일부만 확보")
    if row["source_type"] == "history":
        out.append("이력정리기사 채택")
    return out


def event_title(row: dict[str, Any], cfg: AppConfig) -> str:
    short = cfg.steelmakers.get(row["steelmaker"]).short
    title = f"{SYMBOL[row['direction']]} {short} {representative_change(row)}"
    if row["status"] == "adopted_provisional":
        title += " ⏳"
    if row["needs_review"] or row["conflict"]:
        title += " ⚠"
    return title


def to_events(rows: list[dict[str, Any]], cfg: AppConfig, basis: str = "apply") -> list[dict[str, Any]]:
    events = []
    for r in sort_rows(rows, cfg):
        classes = [f"ev-{r['direction']}"]
        if r["source_type"] == "history":
            classes.append("ev-history")
        if r["status"] == "adopted_provisional":
            classes.append("ev-provisional")
        if r["needs_review"] or r["conflict"]:
            classes.append("ev-review")
        events.append({
            "id": str(r["id"]),
            "title": event_title(r, cfg),
            "start": basis_date(r, basis),
            "allDay": True,
            "backgroundColor": cfg.steelmakers.get(r["steelmaker"]).color,
            "borderColor": BORDER[r["direction"]],
            "textColor": "#ffffff",
            "classNames": classes,
            "extendedProps": {"ann_id": r["id"], "steelmaker": r["steelmaker"], "content": r["content_text"]},
        })
    return events


def detail(row: dict[str, Any]) -> dict[str, Any]:
    """상세 카드용 필드."""
    return {
        "제강사": row["steelmaker"],
        "적용일자": format_apply_date(row["apply_date"]),
        "발표내용": row["content_text"],
        "구분": DIRECTION_LABEL[row["direction"]],
        "매체": row["media_name"],
        "기사 제목": row["title"],
        "기사발표일": row["published_date"],
        "수집일시": (row["collected_at"] or "")[:16].replace("T", " "),
        "기사 유형": TYPE_LABEL.get(row["source_type"], row["source_type"]),
        "채택 상태": STATUS_LABEL.get(row["status"], row["status"]),
    }


# ------------------------------------------------------------ 데이터 상태 탭


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM run_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["stats"] = json.loads(d.pop("stats_json") or "{}")
        out.append(d)
    return out


def media_stats_rows(run: dict[str, Any], cfg: AppConfig) -> list[dict[str, Any]]:
    """실행 1건의 매체·키워드별 누락 점검 (PRD 3.1.4)."""
    out = []
    names = {m.id: m.name for m in cfg.media.media}
    for mid, per_kw in (run.get("stats", {}).get("media") or {}).items():
        for kw, st in per_kw.items():
            failed = "error" in st
            out.append({"매체": names.get(mid, mid), "키워드/목록": kw,
                        "수집": "-" if failed else str(st.get("listed", "")),
                        "페이지": "-" if failed else str(st.get("pages", "")),
                        "한도 도달": "" if failed else ("예" if st.get("limit_hit") else "아니오"),
                        "가장 오래된 기사": st.get("oldest_published") or "-",
                        "누락 가능": "⚠" if st.get("possible_gap") else "", "오류": st.get("error", "")})
    return out


def pending_verify(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT steelmaker, apply_date, attempts, last_searched_at FROM verify_tasks "
                        "WHERE status = 'pending' ORDER BY apply_date").fetchall()
    return [dict(r) for r in rows]


def review_list(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(ANN_SQL + " WHERE (a.needs_review = 1 OR a.conflict = 1) AND a.status IN (?, ?) "
                        "ORDER BY a.apply_date, a.steelmaker", ADOPTED).fetchall()
    return [{"제강사": r["steelmaker"], "적용일자": r["apply_date"], "발표내용": r["content_text"],
             "검토 필요": "⚠" if r["needs_review"] else "", "충돌": "⚠" if r["conflict"] else "",
             "매체": r["media_name"], "기사": r["url"]} for r in rows]


def reconcile_report(conn: sqlite3.Connection) -> ReconcileReport:
    return reconcile(conn)


def articles_list(conn: sqlite3.Connection, limit: int = 1000) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT published_date, media_name, title, article_type, process_status, access_level, url "
        "FROM articles ORDER BY published_date DESC, id DESC LIMIT ?", (limit,)).fetchall()
    return [{"게재일": r["published_date"], "매체": r["media_name"], "제목": r["title"],
             "유형": TYPE_LABEL.get(r["article_type"], r["article_type"] or ""), "처리 상태": r["process_status"],
             "접근": r["access_level"] or "", "링크": r["url"]} for r in rows]
