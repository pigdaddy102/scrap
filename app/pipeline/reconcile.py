"""이력정리 기사 대조 (PRD v1.2 3.5.7, 10장 인수 기준 1번).

history 기사에서 추출한 발표(제강사·적용일자·dedup_key)를 대조 목록으로 삼아 채택 목록과 비교한다.
  원본 확인   : 같은 제강사·적용일에 원본(single/multi)으로 채택된 발표가 있고 dedup_key도 같음
  내용 불일치 : 원본 채택은 있으나 dedup_key가 다름
  원본 미확인 : 원본 채택이 없고 이력정리 기사 자체가 채택(또는 확인 중)
  누락        : 같은 제강사·적용일의 채택 발표가 없음
누락률 = (원본 미확인 + 누락) / 대조 대상
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

CONFIRMED = "원본 확인"
MISMATCH = "내용 불일치"
UNCONFIRMED = "원본 미확인"
MISSING = "누락"
ADOPTED = ("adopted", "adopted_provisional")


@dataclass
class ReconcileEntry:
    steelmaker: str
    apply_date: str
    dedup_key: str
    content_text: str
    history_article_id: int
    result: str


@dataclass
class ReconcileReport:
    entries: list[ReconcileEntry] = field(default_factory=list)

    def count(self, result: str) -> int:
        return sum(e.result == result for e in self.entries)

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def gap_rate(self) -> float:
        return (self.count(UNCONFIRMED) + self.count(MISSING)) / self.total if self.total else 0.0

    def summary(self) -> dict:
        return {"대조 대상": self.total, CONFIRMED: self.count(CONFIRMED), MISMATCH: self.count(MISMATCH),
                UNCONFIRMED: self.count(UNCONFIRMED), MISSING: self.count(MISSING),
                "누락률": round(self.gap_rate, 4)}


def reconcile(conn: sqlite3.Connection) -> ReconcileReport:
    refs = conn.execute(
        """SELECT steelmaker, apply_date, dedup_key, MIN(content_text) AS content_text, MIN(article_id) AS article_id
             FROM announcements WHERE source_type = 'history'
            GROUP BY steelmaker, apply_date, dedup_key
            ORDER BY apply_date, steelmaker, dedup_key"""
    ).fetchall()
    adopted = conn.execute(
        f"SELECT steelmaker, apply_date, dedup_key, source_type FROM announcements "
        f"WHERE status IN ({', '.join('?' for _ in ADOPTED)})", ADOPTED).fetchall()
    by_date: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for a in adopted:
        by_date.setdefault((a["steelmaker"], a["apply_date"]), []).append(a)

    report = ReconcileReport()
    for ref in refs:
        same_date = by_date.get((ref["steelmaker"], ref["apply_date"]), [])
        originals = [a for a in same_date if a["source_type"] in ("single", "multi")]
        if any(a["dedup_key"] == ref["dedup_key"] for a in originals):
            result = CONFIRMED
        elif originals:
            result = MISMATCH
        elif same_date:
            result = UNCONFIRMED
        else:
            result = MISSING
        report.entries.append(ReconcileEntry(ref["steelmaker"], ref["apply_date"], ref["dedup_key"],
                                             ref["content_text"], ref["article_id"], result))
    return report
