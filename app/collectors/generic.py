"""범용 본문 추출과 접근 제한 판정 (PRD v1.2 3.1.2)."""

from __future__ import annotations

import re

PAYWALL_RE = re.compile(r"유료회원\s*전용|로그인\s*또는\s*회원가입|유료회원만\s*열람|구독자\s*전용|로그인\s*후\s*(이용|열람)")
MIN_FULL_LENGTH = 200


def judge_access(text: str, http_status: int = 200) -> str:
    """full | partial | blocked. 200자 미만이거나 유료 안내 문구가 있으면 partial."""
    if http_status in (401, 403):
        return "blocked"
    if PAYWALL_RE.search(text or "") or len((text or "").strip()) < MIN_FULL_LENGTH:
        return "partial"
    return "full"


def extract_main_text(html: str) -> str:
    """매체 선택자로 본문을 못 찾았을 때의 대체 수단 (trafilatura)."""
    try:
        import trafilatura
    except ImportError:  # pragma: no cover
        return ""
    return trafilatura.extract(html, include_comments=False, include_tables=False) or ""
