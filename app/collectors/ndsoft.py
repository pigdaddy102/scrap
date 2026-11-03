"""ndsoft CMS 공통 수집기 (PRD v1.2 3.1.1). 선택자는 media.yaml에서 읽는다 (구형 스킨, altlist 스킨 공통).

목록은 최신순이다. 페이지를 넘기다가 (1) 항목이 없거나 (2) 페이지 한도에 닿거나
(3) 페이지의 가장 오래된 기사가 date_from보다 이전이면 멈춘다.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from app.collectors.base import ArticleMeta, BaseCollector, FetchResult, ListResult, decode_html, to_iso_date
from app.collectors.generic import extract_main_text, judge_access

FULL_DATE_RE = re.compile(r"(20\d{2})[-.](\d{2})[-.](\d{2})")
SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{2})-(\d{2})\s+\d{2}:\d{2}")


def parse_list_date(text: str, date_to: date) -> str | None:
    """'섹션 | 기자 | 2026-10-29 15:30' → '2026-10-29'. 'MM-DD HH:MM'(연도 없음)은 date_to 기준으로 연도 추론."""
    m = FULL_DATE_RE.search(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = SHORT_DATE_RE.search(text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = date_to.year
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if d > date_to:  # 예: 조회 종료일이 1월인데 목록이 12월이면 전년도
            d = date(year - 1, month, day)
        return d.isoformat()
    return None


class NdsoftCollector(BaseCollector):
    def list_url(self, page: int, date_from: date, date_to: date, keyword: str | None) -> str:
        m = self.media
        template = m.search_url if m.method == "site_search" else m.list_url
        assert template
        kw = quote(keyword or "", encoding=m.keyword_encoding)
        path = template.format(keyword=kw, date_from=date_from.isoformat(), date_to=date_to.isoformat(), page=page)
        return urljoin(m.base_url, path)

    def parse_list(self, html: str, date_to: date) -> list[ArticleMeta]:
        s = self.media.selectors
        soup = BeautifulSoup(html, "html.parser")
        out: list[ArticleMeta] = []
        for node in soup.select(s.item):
            title_el = node.select_one(s.title)
            link_el = node.select_one(s.link)
            if title_el is None or link_el is None or not link_el.get("href"):
                continue
            date_el = node.select_one(s.date)
            summary_el = node.select_one(".list-summary, .lead, .altlist-summary")
            out.append(ArticleMeta(
                media_id=self.media.id, media_name=self.media.name,
                title=title_el.get_text(" ", strip=True),
                url=urljoin(self.media.base_url, link_el["href"]),
                published_date=parse_list_date(date_el.get_text(" ", strip=True), date_to) if date_el else None,
                summary=summary_el.get_text(" ", strip=True) if summary_el else "",
            ))
        return out

    def list_articles(self, date_from: date, date_to: date, keyword: str | None = None) -> ListResult:
        if self.media.method == "site_search" and not keyword:
            raise ValueError(f"{self.media.id}: site_search는 키워드가 필요합니다")
        if self.media.paging == "date_windows":
            return self._list_by_windows(date_from, date_to, keyword)
        result = ListResult()
        lo, hi = date_from.isoformat(), date_to.isoformat()
        for page in range(1, self.max_pages + 1):
            html = decode_html(self.http.get(self.list_url(page, date_from, date_to, keyword)))
            metas = self.parse_list(html, date_to)
            result.pages = page
            if not metas:
                break
            dated = [m.published_date for m in metas if m.published_date]
            for m in metas:
                m.search_keyword = keyword if keyword else (
                    f"section:{self._section_code()}" if self._section_code() else "list:all")
                if m.published_date is None or lo <= m.published_date <= hi:
                    result.metas.append(m)
            if dated:
                oldest = min(dated)
                result.oldest_published = oldest if result.oldest_published is None else min(
                    result.oldest_published, oldest)
                if oldest < lo:
                    break
            if page == self.max_pages:
                result.limit_hit = True
        return result

    def _list_by_windows(self, date_from: date, date_to: date, keyword: str | None) -> ListResult:
        """기간을 window_days 단위로 나눠 최신 구간부터 1페이지씩 조회 (page 파라미터 없음).

        구간 수는 max_pages까지. 다 덮지 못하면 limit_hit, 구간이 page_size만큼 차면 full_windows 증가.
        """
        result = ListResult()
        end = date_to
        tag = keyword if keyword else (f"section:{self._section_code()}" if self._section_code() else "list:all")
        while end >= date_from and result.pages < self.max_pages:
            start = max(date_from, end - timedelta(days=self.media.window_days - 1))
            html = decode_html(self.http.get(self.list_url(1, start, end, keyword)))
            metas = self.parse_list(html, end)
            result.pages += 1
            if len(metas) >= self.media.page_size:
                result.full_windows += 1
            for m in metas:
                m.search_keyword = tag
                if m.published_date is None or start.isoformat() <= m.published_date <= end.isoformat():
                    result.metas.append(m)
                    if m.published_date and (result.oldest_published is None
                                             or m.published_date < result.oldest_published):
                        result.oldest_published = m.published_date
            end = start - timedelta(days=1)
        result.limit_hit = end >= date_from
        return result

    def _section_code(self) -> str | None:
        m = re.search(r"sc_(?:sub_)?section_code=(\w+)", self.media.list_url or "")
        return m.group(1) if m else None

    def fetch_text(self, url: str) -> FetchResult:
        resp = self.http.get(url)
        html = decode_html(resp)
        soup = BeautifulSoup(html, "html.parser")
        body = soup.select_one("#article-view-content-div")
        if body is not None:
            for tag in body.select("script, style, figure, .view-editors, .view-copyright"):
                tag.decompose()
            text = re.sub(r"\s+", " ", body.get_text(" ", strip=True))
        else:
            text = extract_main_text(html)
        pub = soup.select_one('meta[property="article:published_time"]')
        og = soup.select_one('meta[property="og:description"]')
        return FetchResult(
            text=text,
            access_level=judge_access(text, resp.status_code),
            http_status=resp.status_code,
            published_at=pub.get("content") if pub else None,
            lead=og.get("content", "") if og else "",
        )


def published_date_of(fr: FetchResult, fallback: str | None) -> str | None:
    return to_iso_date(fr.published_at) or fallback
