"""수집 공통: ArticleMeta, FetchResult, HTTP 클라이언트(접속 간격·재시도·robots.txt), BaseCollector (PRD v1.2 3.1.2, 6장).

- 같은 도메인 연속 요청은 request_interval_sec(기본 3초) 이상 간격을 둔다.
- robots.txt에서 금지한 경로는 요청하지 않는다.
- 본문은 반환만 하고 저장하지 않는다. 요청 로그에는 URL과 시각만 남긴다.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable
from urllib.parse import urlsplit

import requests

from app.config import Media, MediaDefaults

log = logging.getLogger(__name__)


@dataclass
class ArticleMeta:
    media_id: str
    media_name: str
    title: str
    url: str
    published_date: str | None  # YYYY-MM-DD (목록 기준, 기사 페이지에서 확정)
    summary: str = ""
    search_keyword: str | None = None


@dataclass
class FetchResult:
    text: str
    access_level: str  # full | partial | blocked
    http_status: int
    published_at: str | None = None  # 기사 페이지 meta article:published_time (ISO)
    lead: str = ""  # og:description (partial일 때 리드문으로 사용)


@dataclass
class ListResult:
    metas: list[ArticleMeta] = field(default_factory=list)
    pages: int = 0
    limit_hit: bool = False
    oldest_published: str | None = None
    full_windows: int = 0  # date_windows 방식에서 page_size만큼 꽉 찬 구간 수 (누락 가능)


class RobotsDisallowed(Exception):
    pass


class HttpClient:
    """도메인별 접속 간격, 재시도(지수 백오프), robots.txt 확인을 하는 GET 전용 클라이언트."""

    def __init__(self, defaults: MediaDefaults, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.defaults = defaults
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": defaults.user_agent})
        self.sleep = sleep
        self.clock = clock
        self._last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self.request_log: list[tuple[str, str, float]] = []  # (domain, url, monotonic 시각)

    def _wait(self, domain: str) -> None:
        last = self._last.get(domain)
        if last is not None:
            gap = self.defaults.request_interval_sec - (self.clock() - last)
            if gap > 0:
                self.sleep(gap)
        self._last[domain] = self.clock()

    def _robots_for(self, scheme: str, domain: str) -> urllib.robotparser.RobotFileParser | None:
        if domain not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                self._wait(domain)
                self.request_log.append((domain, f"{scheme}://{domain}/robots.txt", self.clock()))
                r = self.session.get(f"{scheme}://{domain}/robots.txt", timeout=self.defaults.timeout_sec)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except requests.RequestException as e:
                log.warning("robots.txt 확인 실패(허용으로 간주): %s %s", domain, e)
                rp = None
            self._robots[domain] = rp
        return self._robots[domain]

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        rp = self._robots_for(parts.scheme or "https", parts.netloc)
        return True if rp is None else rp.can_fetch(self.defaults.user_agent, url)

    def get(self, url: str) -> requests.Response:
        if not self.allowed(url):
            raise RobotsDisallowed(url)
        domain = urlsplit(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.defaults.max_retries + 1):
            self._wait(domain)
            self.request_log.append((domain, url, self.clock()))
            try:
                r = self.session.get(url, timeout=self.defaults.timeout_sec)
                if r.status_code >= 500 or r.status_code == 429:
                    raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
                return r
            except requests.RequestException as e:
                last_exc = e
                if attempt < self.defaults.max_retries:
                    self.sleep(2 ** attempt * self.defaults.request_interval_sec)
        assert last_exc is not None
        raise last_exc

    def min_interval(self) -> float | None:
        """요청 로그 기준 같은 도메인 연속 요청의 최소 간격(초). 9.5 수집 테스트용."""
        best: float | None = None
        last: dict[str, float] = {}
        for domain, _, t in self.request_log:
            if domain in last:
                gap = t - last[domain]
                best = gap if best is None else min(best, gap)
            last[domain] = t
        return best


def decode_html(resp: requests.Response) -> str:
    """ndsoft 매체는 UTF-8. 헤더에 charset이 없을 때 requests가 ISO-8859-1로 추정하는 것을 막는다."""
    head = resp.content[:2048].lower()
    enc = "euc-kr" if b"charset=euc-kr" in head or b'charset="euc-kr"' in head else "utf-8"
    return resp.content.decode(enc, errors="replace")


class BaseCollector:
    def __init__(self, media: Media, defaults: MediaDefaults, http: HttpClient, max_pages: int):
        self.media = media
        self.defaults = defaults
        self.http = http
        self.max_pages = max_pages

    def list_articles(self, date_from: date, date_to: date, keyword: str | None = None) -> ListResult:
        raise NotImplementedError

    def fetch_text(self, url: str) -> FetchResult:
        raise NotImplementedError


def to_iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10] if len(value) >= 10 else None
