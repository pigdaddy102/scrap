"""기사 단위 중복 제거 (PRD v1.2 3.1.5 1~3단계).

1. URL 정규화 → url_norm
2. 이미 처리한 기사(articles.url_norm UNIQUE)는 건너뜀
3. 같은 매체·같은 게재일·정규화 제목이 같으면 먼저 수집한 1건만 남김
4. 서로 다른 매체의 같은 발표는 여기서 합치지 않는다 (발표 단위 dedup_key, 3.3)
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 추적용 파라미터 (값과 무관하게 제거)
TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src"}
TRACKING_PREFIXES = ("utm_",)
NDSOFT_VIEW_PATH = "/news/articleView.html"


def normalize_url(url: str) -> str:
    """URL을 중복 판별용 키로 정규화한다. 실제 접속용 URL이 아니라 비교용 키다.

    - 스킴은 https, 호스트는 소문자, 앞의 'www.' / 'm.' 제거 (PC·모바일·www 통일)
    - ndsoft 기사 URL(/news/articleView.html)은 idxno만 남김
    - 그 외: 추적 파라미터 제거, 쿼리 정렬, fragment 제거, 끝의 '/' 제거
    """
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    path = parts.path or "/"
    query = parse_qsl(parts.query, keep_blank_values=False)

    if path.lower() == NDSOFT_VIEW_PATH.lower():
        idxno = next((v for k, v in query if k.lower() == "idxno"), None)
        if idxno:
            return f"https://{host}{NDSOFT_VIEW_PATH}?idxno={idxno}"

    kept = sorted(
        (k, v) for k, v in query
        if k.lower() not in TRACKING_PARAMS and not k.lower().startswith(TRACKING_PREFIXES)
    )
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit(("https", host, path, urlencode(kept), ""))


_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)


def normalize_title(title: str) -> str:
    """공백·특수문자를 지우고 소문자로. '현대제철, 철스크랩 10원 인하…' → '현대제철철스크랩10원인하'"""
    return _NON_WORD.sub("", title).lower()


def _attr(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key)


def dedupe_batch(
    metas: Iterable[Any],
    is_known: Callable[[str], bool] = lambda url_norm: False,
) -> list[tuple[Any, str]]:
    """수집한 기사 목록에서 중복을 걸러 (기사, url_norm) 목록을 돌려준다. 입력 순서(=수집 순서)를 유지한다.

    metas: media_id, url, title, published_date 속성(또는 키)을 가진 객체
    is_known: url_norm이 이미 DB에 있으면 True (예: lambda u: db.article_exists(conn, u))
    """
    seen_urls: set[str] = set()
    seen_titles: set[tuple[str, str, str]] = set()
    result: list[tuple[Any, str]] = []
    for meta in metas:
        url_norm = normalize_url(_attr(meta, "url"))
        if url_norm in seen_urls or is_known(url_norm):
            continue
        title_key = (_attr(meta, "media_id"), str(_attr(meta, "published_date")), normalize_title(_attr(meta, "title")))
        if title_key in seen_titles:
            continue
        seen_urls.add(url_norm)
        seen_titles.add(title_key)
        result.append((meta, url_norm))
    return result
