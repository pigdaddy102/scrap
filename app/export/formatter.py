"""출력 형식 문자열과 CSV (PRD v1.2 3.5.6, 4.7).

한 줄 형식: 제강사명 | 적용일자 | 발표내용 | 기사발표일 | 기사링크  (헤더 없이 내용만)
출력 문자열은 Claude가 아니라 이 모듈만 만든다. UI와 CSV 내보내기도 이 모듈을 쓴다.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

SEP = " | "
HISTORY_PREFIX = "(이력정리기사) "  # v1.1 Q4: 접두 뒤에 공백 1칸
CSV_HEADER = ["제강사명", "적용일자", "발표내용", "기사발표일", "기사링크"]


def _to_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def format_apply_date(value: str | date) -> str:
    """'2026-10-14' → '26/10/14일부'"""
    d = _to_date(value)
    return f"{d:%y/%m/%d}일부"


def format_published_date(value: str | date) -> str:
    """'2026-10-01' → '2026-10-01' (YYYY-MM-DD 보장)"""
    return _to_date(value).isoformat()


def format_change(change_krw_per_kg: int) -> str:
    """부호는 항상 붙인다: 10 → '+10원/kg', -10 → '-10원/kg'"""
    return f"{int(change_krw_per_kg):+d}원/kg"


def _get(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)


def format_item(item: Any) -> str:
    """{'grade': '전등급', 'change_krw_per_kg': -10, 'note': '선반압축 제외'} → '전등급 -10원/kg [선반압축 제외]'"""
    text = f"{_get(item, 'grade')} {format_change(_get(item, 'change_krw_per_kg'))}"
    note = _get(item, "note")
    if note:
        text += f" [{note}]"
    return text


def _signature(items: Sequence[Any]) -> list[tuple]:
    return [(_get(i, "grade"), int(_get(i, "change_krw_per_kg")), _get(i, "note") or None) for i in items]


def format_content(items: Sequence[Any], plant_order: Sequence[str] | None = None) -> str:
    """발표내용 문자열.

    - 공장 구분이 없으면(plant 모두 None) 등급별 내용을 ', '로 연결
    - 설정된 모든 공장(plant_order)의 내용이 같으면 지역 표기 없이 한 번만 표기
      (plant_order가 없으면 등장한 공장이 2곳 이상이고 모두 같을 때). 일부 공장만 보도되면 지역 표기 유지
    - 다르면 '(공장) 내용'을 ' / '로 연결. 공장 순서는 plant_order(steelmakers.yaml) → 그 외는 등장 순
    - 공장 표기가 없는 공통 항목이 섞여 있으면 맨 앞에 지역 표기 없이 둔다
    """
    if not items:
        raise ValueError("items가 비어 있습니다")
    groups: dict[str | None, list[Any]] = {}
    for it in items:
        groups.setdefault(_get(it, "plant") or None, []).append(it)

    plants = [p for p in groups if p is not None]
    if not plants:
        return ", ".join(format_item(i) for i in groups[None])

    order = list(plant_order or [])
    appearance = {p: i for i, p in enumerate(plants)}
    plants.sort(key=lambda p: (order.index(p) if p in order else len(order), appearance[p]))

    covers_all = set(plants) == set(order) if order else True
    if None not in groups and len(plants) > 1 and covers_all:
        first = _signature(groups[plants[0]])
        if all(_signature(groups[p]) == first for p in plants[1:]):
            return ", ".join(format_item(i) for i in groups[plants[0]])

    parts = []
    if None in groups:
        parts.append(", ".join(format_item(i) for i in groups[None]))
    for p in plants:
        parts.append(f"({p}) " + ", ".join(format_item(i) for i in groups[p]))
    return " / ".join(parts)


def format_link(url: str, source_type: str | None = None) -> str:
    """이력정리 기사로 채택했으면 '(이력정리기사) ' 접두."""
    return f"{HISTORY_PREFIX}{url}" if source_type == "history" else url


@dataclass
class OutputRow:
    steelmaker: str
    apply_date: str | date
    items: Sequence[Any]
    published_date: str | date
    url: str
    source_type: str = "single"
    plant_order: Sequence[str] = field(default_factory=list)
    content_text: str | None = None  # 이미 만든 문자열이 있으면 그대로 사용 (announcements.content_text)

    def columns(self) -> list[str]:
        content = self.content_text or format_content(self.items, self.plant_order)
        return [
            self.steelmaker,
            format_apply_date(self.apply_date),
            content,
            format_published_date(self.published_date),
            format_link(self.url, self.source_type),
        ]

    def to_line(self) -> str:
        return SEP.join(self.columns())


def format_lines(rows: Iterable[OutputRow]) -> str:
    """텍스트 복사용. 헤더 없이 한 줄에 한 건."""
    return "\n".join(r.to_line() for r in rows)


def rows_to_csv(rows: Iterable[OutputRow], include_header: bool = False) -> bytes:
    """CSV 바이트 (utf-8-sig, Excel 호환). 기본은 헤더 없음."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    if include_header:
        writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow(r.columns())
    return buf.getvalue().encode("utf-8-sig")


def write_csv(path: str | Path, rows: Iterable[OutputRow], include_header: bool = False) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(rows_to_csv(rows, include_header))
    return p


# ---------------------------------------------------- DB → 출력 (PRD 4.7)

ADOPTED_STATUSES = ("adopted", "adopted_provisional")


def load_adopted_rows(conn, cfg) -> list[OutputRow]:
    """채택(adopted, adopted_provisional) 발표를 적용일자 오름차순, 제강사 설정 순서로."""
    import json

    rows = conn.execute(
        f"""SELECT a.steelmaker, a.apply_date, a.items_json, a.content_text, a.source_type,
                   ar.published_date, ar.url
              FROM announcements a JOIN articles ar ON ar.id = a.article_id
             WHERE a.status IN ({', '.join('?' for _ in ADOPTED_STATUSES)})""",
        ADOPTED_STATUSES,
    ).fetchall()
    names = cfg.steelmakers.names()
    out = [
        OutputRow(r["steelmaker"], r["apply_date"], json.loads(r["items_json"]), r["published_date"], r["url"],
                  r["source_type"], cfg.steelmakers.plant_order(r["steelmaker"]), r["content_text"])
        for r in rows
    ]
    out.sort(key=lambda o: (str(o.apply_date), names.index(o.steelmaker) if o.steelmaker in names else 99,
                            str(o.published_date), o.url))
    return out


def export_adopted_csv(conn, cfg, path: str | Path | None = None) -> Path:
    """data/exports/adopted.csv 를 다시 만든다 (헤더 없음, utf-8-sig)."""
    from app.config import PROJECT_ROOT

    target = Path(path) if path else PROJECT_ROOT / "data" / "exports" / "adopted.csv"
    return write_csv(target, load_adopted_rows(conn, cfg), include_header=False)
