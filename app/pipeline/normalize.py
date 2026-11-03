"""LLM 추출 결과 검증 (PRD v1.2 3.2.5).

1. 화이트리스트: 대상 제강사·공장이 아니면 버림
2. 타당성: 적용일 범위(게재일 −10일~+45일), |변동폭| ≤ 100, confidence ≥ 0.7 — 어긋나면 needs_review
   변동폭 0(동결) 항목은 버림. 남는 발표가 없으면 기사를 none으로 바꿈
3. 등급 매핑 (steelmakers.yaml grade_mapping, 원문은 grade_raw)
4. 공장 통합: 설정된 모든 공장의 내용이 같으면 plant=None으로 합침
5. dedup_key (SHA-1), 6. content_text (formatter), direction
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.config import AppConfig
from app.export.formatter import format_content
from app.llm.schema import ArticleAnalysis

log = logging.getLogger(__name__)

APPLY_DATE_MIN_OFFSET = -10  # 게재일 기준 (일)
APPLY_DATE_MAX_OFFSET = 45
MAX_ABS_CHANGE = 100  # 원/kg
MIN_CONFIDENCE = 0.7


@dataclass
class NormalizedAnnouncement:
    steelmaker: str
    apply_date: str
    apply_date_inferred: bool
    items: list[dict]
    content_text: str
    direction: str
    dedup_key: str
    confidence: float
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)


@dataclass
class NormalizeResult:
    article_type: str
    announcements: list[NormalizedAnnouncement]
    warnings: list[str] = field(default_factory=list)


def _plant_rank(plants: list[str], plant: str | None) -> int:
    if plant is None:
        return -1
    return plants.index(plant) if plant in plants else len(plants)


def merge_plants(items: list[dict], plants: list[str]) -> list[dict]:
    """설정된 모든 공장의 (grade, change, note) 목록이 같으면 plant=None 한 벌로 합친다."""
    if not plants or any(i["plant"] is None for i in items):
        return items
    by_plant: dict[str, list[tuple]] = {}
    for i in items:
        by_plant.setdefault(i["plant"], []).append((i["grade"], i["change_krw_per_kg"], i["note"]))
    if set(by_plant) != set(plants):
        return items
    signatures = list(by_plant.values())
    if all(s == signatures[0] for s in signatures[1:]):
        first = plants[0]
        return [{**i, "plant": None} for i in items if i["plant"] == first]
    return items


def compute_dedup_key(steelmaker: str, apply_date: str, items: list[dict], plants: list[str]) -> str:
    parts = sorted(
        (_plant_rank(plants, i["plant"]), i["plant"] or "", i["grade"], int(i["change_krw_per_kg"]), i["note"] or "")
        for i in items
    )
    payload = json.dumps([steelmaker, apply_date, [p[1:] for p in parts]], ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def direction_of(items: list[dict]) -> str:
    changes = [int(i["change_krw_per_kg"]) for i in items]
    if all(c > 0 for c in changes):
        return "up"
    if all(c < 0 for c in changes):
        return "down"
    return "mixed"


def normalize_analysis(analysis: ArticleAnalysis, published_date: str | date, cfg: AppConfig) -> NormalizeResult:
    sm = cfg.steelmakers
    pub = published_date if isinstance(published_date, date) else date.fromisoformat(published_date)
    valid_makers = set(sm.names())
    out: list[NormalizedAnnouncement] = []
    warnings: list[str] = []

    for ann in analysis.announcements:
        if ann.steelmaker not in valid_makers:
            warnings.append(f"대상 아닌 제강사 제외: {ann.steelmaker}")
            continue
        plants = sm.plant_order(ann.steelmaker)
        reasons: list[str] = []
        items: list[dict] = []
        for it in ann.items:
            plant = it.plant.strip() if it.plant else None
            if plant is not None and plant not in plants:
                warnings.append(f"설정에 없는 공장 제외: {ann.steelmaker} {plant}")
                continue
            if it.change_krw_per_kg == 0:
                warnings.append(f"동결 항목 제외: {ann.steelmaker} {it.grade}")
                continue
            if abs(it.change_krw_per_kg) > MAX_ABS_CHANGE:
                reasons.append(f"변동폭 {it.change_krw_per_kg}원/kg이 ±{MAX_ABS_CHANGE} 초과")
            note = it.note.strip() if it.note and it.note.strip() else None
            items.append({
                "plant": plant,
                "grade": sm.grade_mapping.apply(it.grade),
                "grade_raw": it.grade,
                "change_krw_per_kg": int(it.change_krw_per_kg),
                "note": note,
            })
        if not items:
            continue

        items = merge_plants(items, plants)
        items.sort(key=lambda i: _plant_rank(plants, i["plant"]))  # 안정 정렬: 공장 순서, 같은 공장 안은 기사 순서

        offset = (ann.apply_date - pub).days
        if not APPLY_DATE_MIN_OFFSET <= offset <= APPLY_DATE_MAX_OFFSET:
            reasons.append(f"적용일이 게재일 기준 {offset:+d}일 (허용 {APPLY_DATE_MIN_OFFSET}~+{APPLY_DATE_MAX_OFFSET})")
        if ann.confidence < MIN_CONFIDENCE:
            reasons.append(f"신뢰도 {ann.confidence:.2f} < {MIN_CONFIDENCE}")

        apply_iso = ann.apply_date.isoformat()
        out.append(NormalizedAnnouncement(
            steelmaker=ann.steelmaker,
            apply_date=apply_iso,
            apply_date_inferred=ann.apply_date_inferred,
            items=items,
            content_text=format_content(items, plants),
            direction=direction_of(items),
            dedup_key=compute_dedup_key(ann.steelmaker, apply_iso, items, plants),
            confidence=ann.confidence,
            needs_review=bool(reasons),
            review_reasons=reasons,
        ))

    article_type = analysis.article_type if out else "none"
    for w in warnings:
        log.info(w)
    return NormalizeResult(article_type=article_type, announcements=out, warnings=warnings)
