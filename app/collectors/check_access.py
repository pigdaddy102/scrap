"""매체별 접근 수준 확인 (PRD v1.2 3.1.2, C4). 회사 PC에서 실행해 media.yaml의 access 값을 확정한다.

  python -m app.collectors.check_access [--per-media 2]

매체마다 최근 목록에서 표본 기사 N건을 열어 HTTP 상태, 본문 길이, 유료 안내 감지 여부, access_level만 출력한다.
본문은 출력하거나 저장하지 않는다.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from app.collectors.base import HttpClient
from app.collectors.generic import PAYWALL_RE
from app.collectors.registry import build_collectors
from app.config import get_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.collectors.check_access")
    ap.add_argument("--per-media", type=int, default=2)
    ap.add_argument("--days", type=int, default=14, help="표본을 고를 최근 기간(일)")
    args = ap.parse_args(argv)

    cfg = get_config()
    http = HttpClient(cfg.media.defaults)
    today = date.today()
    print(f"접근 확인 {today} (표본 {args.per_media}건/매체, 본문은 출력하지 않음)")
    for c in build_collectors(cfg, http):
        kw = cfg.settings.keywords[0] if c.media.method == "site_search" else None
        c.max_pages = 1
        try:
            metas = c.list_articles(today - timedelta(days=args.days), today, kw).metas[: args.per_media]
        except Exception as e:
            print(f"- {c.media.name}: 목록 실패 {e}")
            continue
        levels = []
        for m in metas:
            fr = c.fetch_text(m.url)
            paywall = bool(PAYWALL_RE.search(fr.text))
            levels.append(fr.access_level)
            print(f"- {c.media.name} | HTTP {fr.http_status} | 본문 {len(fr.text)}자 | 유료 안내 {'있음' if paywall else '없음'} "
                  f"| {fr.access_level} | {m.url}")
        if levels:
            print(f"  → media.yaml 현재 access={c.media.access}, 표본 결과={sorted(set(levels))}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
