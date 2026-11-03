"""Claude 호출 전 제목·요약 사전 필터 (PRD v1.2 3.1.3)."""

from __future__ import annotations

from app.config import Prefilter


def passes(title: str, summary: str, rules: Prefilter) -> bool:
    if any(word in title for word in rules.exclude_if_title_has):
        return False
    text = f"{title} {summary}"
    return any(word in text for word in rules.must_include_any) if rules.must_include_any else True
