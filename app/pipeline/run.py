"""CLI 진입점 — 전체 흐름 조율 (PRD v1.2 3.6.1, 5.1).

  python -m app.pipeline.run                                    # 일일: 오늘 기준 최근 N일
  python -m app.pipeline.run --from 2026-10-01 --to 2026-11-08  # 기간 지정(백필)
  python -m app.pipeline.run --media snmnews --dry-run          # 매체 1개, DB 저장 없이 결과 출력
  python -m app.pipeline.run --reselect                         # 수집 없이 선택 로직만
  python -m app.pipeline.run --llm mock                         # LLM 제공자 임시 변경

종료 코드: 0 success, 2 partial, 1 failed, 3 다른 실행이 진행 중(락)
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from app.collectors.base import ArticleMeta, BaseCollector, HttpClient, RobotsDisallowed
from app.collectors.ndsoft import published_date_of
from app.collectors.registry import build_collectors
from app.config import PROJECT_ROOT, AppConfig, get_config
from app.export.formatter import export_adopted_csv, load_adopted_rows
from app.llm.client import LLMProvider, UsageLimitError, analyze_with_retry, get_provider
from app.llm.mock import key_for_url
from app.llm.prompts import ArticleInput
from app.pipeline.dedup import dedupe_batch
from app.pipeline.normalize import normalize_analysis
from app.pipeline.prefilter import passes
from app.pipeline.reconcile import reconcile
from app.pipeline.select import select_all
from app.pipeline.verify import VerifyQuery, run_verification
from app.storage import db

log = logging.getLogger("pipeline")
LOCK_PATH = PROJECT_ROOT / "data" / ".lock"
EXIT = {"success": 0, "partial": 2, "failed": 1}


class LockedError(Exception):
    pass


class RunLock:
    def __init__(self, path: Path = LOCK_PATH):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as e:
            raise LockedError(f"다른 실행이 진행 중입니다 (락 파일: {self.path})") from e
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


@dataclass
class Pipeline:
    conn: sqlite3.Connection
    cfg: AppConfig
    collectors: list[BaseCollector]
    provider: LLMProvider | None  # None이면 LLM 단계를 건너뜀 (로그인 필요 등)
    dry_run: bool = False
    max_llm: int | None = None
    stats: dict[str, Any] = field(default_factory=lambda: {"media": {}, "llm": {}, "errors": []})
    status: str = "success"
    llm_stopped: bool = False
    _llm_calls: int = 0
    _llm_errors: int = 0
    _llm_seconds: float = 0.0

    # ------------------------------------------------------------ ① 수집
    def collect(self, date_from: date, date_to: date) -> list[ArticleMeta]:
        metas: list[ArticleMeta] = []
        for c in self.collectors:
            m = c.media
            keywords: list[str | None] = list(self.cfg.settings.keywords) if m.method == "site_search" else [None]
            media_stats: dict[str, Any] = {}
            for kw in keywords:
                label = kw or ("list:all" if "section_code" not in (m.list_url or "") else "section")
                try:
                    res = c.list_articles(date_from, date_to, kw)
                except (RobotsDisallowed, Exception) as e:  # 매체 하나가 실패해도 계속
                    self.status = "partial"
                    media_stats[label] = {"error": str(e)[:200]}
                    self.stats["errors"].append(f"{m.id}/{label}: {e}")
                    log.warning("수집 실패 %s/%s: %s", m.id, label, e)
                    continue
                gap = bool(res.limit_hit and res.oldest_published and res.oldest_published > date_from.isoformat())
                gap = gap or res.full_windows > 0
                media_stats[label] = {"listed": len(res.metas), "pages": res.pages, "limit_hit": res.limit_hit,
                                      "oldest_published": res.oldest_published, "possible_gap": gap}
                if m.paging == "date_windows":
                    media_stats[label]["full_windows"] = res.full_windows
                log.info("수집 %s/%s: %d건, %d페이지, 가장 오래된 기사 %s%s", m.id, label, len(res.metas), res.pages,
                         res.oldest_published, " ⚠ 한도 도달로 누락 가능" if gap else "")
                metas.extend(res.metas)
            self.stats["media"][m.id] = media_stats
        return metas

    # ------------------------------------------------------------ ② 중복 제거 + 사전 필터 → 저장
    def register(self, metas: Iterable[ArticleMeta], found_by: str = "regular") -> list[int]:
        """신규 기사를 articles에 저장하고, 사전 필터를 통과한 기사 id 목록을 돌려준다."""
        queued = []
        for meta, url_norm in dedupe_batch(metas, is_known=lambda u: db.article_exists(self.conn, u)):
            ok = passes(meta.title, meta.summary, self.cfg.settings.prefilter)
            aid = db.insert_article(
                self.conn, media_id=meta.media_id, media_name=meta.media_name, title=meta.title, url=meta.url,
                url_norm=url_norm, published_date=meta.published_date, search_keyword=meta.search_keyword,
                found_by=found_by, process_status="pending" if ok else "prefiltered_out")
            if ok:
                queued.append(aid)
        return queued

    def retry_queue(self) -> list[int]:
        """이전 실행에서 pending(로그인 필요 등)이나 llm_error로 남은 기사."""
        rows = self.conn.execute(
            "SELECT id FROM articles WHERE process_status IN ('pending', 'llm_error') AND llm_attempts < ? ORDER BY id",
            (self.cfg.settings.llm.max_attempts_per_article,)).fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------------ ③④⑤ 본문 → LLM → 검증 → 후보 저장
    def process(self, article_ids: Iterable[int]) -> int:
        if self.provider is None or self.llm_stopped:
            return 0
        added = 0
        by_media = {c.media.id: c for c in self.collectors}
        for aid in article_ids:
            if self.llm_stopped or (self.max_llm is not None and self._llm_calls >= self.max_llm):
                break
            art = db.get(self.conn, "articles", aid)
            collector = by_media.get(art["media_id"])
            if collector is None:
                continue
            try:
                fr = collector.fetch_text(art["url"])
            except Exception as e:
                db.update_article(self.conn, aid, process_status="fetch_error", error_msg=str(e)[:300])
                self.stats["errors"].append(f"본문 실패 {art['url']}: {e}")
                continue
            pub = published_date_of(fr, art["published_date"])
            body = fr.text if fr.access_level == "full" else (fr.lead or fr.text)
            article = ArticleInput(art["media_name"], art["title"], pub or "", body, fr.access_level,
                                   key=key_for_url(art["url_norm"]))
            try:
                res = analyze_with_retry(self.provider, article, self.cfg.settings.llm.retries)
            except UsageLimitError as e:
                self.llm_stopped = True
                self.status = "partial"
                self.stats["errors"].append(f"사용량 한도: {e}")
                log.warning("사용량 한도로 LLM 처리를 멈춥니다: %s", e)
                db.update_article(self.conn, aid, published_date=pub, access_level=fr.access_level)
                break
            finally:
                del body  # 본문은 여기서 버린다 (저장하지 않음)
            self._llm_calls += 1
            self._llm_seconds += res.seconds
            update = {"published_date": pub, "access_level": fr.access_level, "llm_provider": res.provider,
                      "llm_model": res.model, "llm_attempts": art["llm_attempts"] + 1}
            if not res.ok:
                self._llm_errors += 1
                db.update_article(self.conn, aid, process_status="llm_error", error_msg=res.error, **update)
                continue
            norm = normalize_analysis(res.analysis, pub or date.today().isoformat(), self.cfg)
            db.update_article(self.conn, aid, process_status="done", article_type=norm.article_type,
                              type_reason=res.analysis.type_reason, error_msg=None, **update)
            for a in norm.announcements:
                db.insert_announcement(
                    self.conn, article_id=aid, steelmaker=a.steelmaker, apply_date=a.apply_date,
                    apply_date_inferred=int(a.apply_date_inferred), items_json=a.items, content_text=a.content_text,
                    direction=a.direction, source_type=norm.article_type, dedup_key=a.dedup_key,
                    confidence=a.confidence, needs_review=int(a.needs_review))
                added += 1
            if self.dry_run:
                print(f"  [{norm.article_type}] {pub} {art['media_name']} | {art['title']}\n      {art['url']}")
                for a in norm.announcements:
                    print(f"      → {a.steelmaker} {a.apply_date} {a.content_text}"
                          f"{'  ⚠ ' + '; '.join(a.review_reasons) if a.needs_review else ''}")
        return added

    # ------------------------------------------------------------ ⑥ 원본 추가 검색
    def search_for_verify(self, q: VerifyQuery) -> list[ArticleMeta]:
        found: list[ArticleMeta] = []
        for c in self.collectors:
            try:
                if c.media.method == "site_search":
                    found.extend(c.list_articles(q.date_from, q.date_to, q.keyword).metas)
                else:
                    metas = c.list_articles(q.date_from, q.date_to).metas
                    found.extend(m for m in metas if any(a in m.title for a in q.aliases))
            except Exception as e:
                self.stats["errors"].append(f"추가 검색 실패 {c.media.id}: {e}")
        return found

    def process_verified(self, metas: Iterable[ArticleMeta]) -> int:
        return self.process(self.register(metas, found_by="verify"))

    def finish_llm_stats(self) -> None:
        self.stats["llm"] = {"provider": getattr(self.provider, "name", None), "calls": self._llm_calls,
                             "errors": self._llm_errors, "stopped_by_limit": self.llm_stopped,
                             "avg_sec": round(self._llm_seconds / self._llm_calls, 1) if self._llm_calls else None}


def resolve_period(args: argparse.Namespace, cfg: AppConfig, today: date | None = None) -> tuple[str, date, date]:
    today = today or date.today()
    if args.date_from or args.date_to:
        d_from = date.fromisoformat(args.date_from) if args.date_from else cfg.settings.period.default_from
        d_to = date.fromisoformat(args.date_to) if args.date_to else today
        return "backfill", d_from, min(d_to, today)
    return "daily", today - timedelta(days=cfg.settings.period.daily_lookback_days), today


def build_provider(cfg: AppConfig, override: str | None, stats: dict) -> LLMProvider | None:
    name = override or cfg.settings.llm.provider
    if name == "claude_cli" and cfg.settings.llm.claude_cli.check_auth:
        from app.llm.claude_cli import check_auth
        if not check_auth(cfg.settings.llm.claude_cli.executable):
            stats["auth"] = "auth_required"
            log.warning("Claude Code 로그인이 필요합니다(claude auth login). LLM 단계를 건너뜁니다.")
            return None
        stats["auth"] = "logged_in"
    return get_provider(cfg, override)


def run(args: argparse.Namespace) -> int:
    cfg = get_config()
    if args.reselect:
        conn = db.connect()
        rid = db.start_run(conn, "reselect")
        s = select_all(conn, cfg)
        export_adopted_csv(conn, cfg)
        db.finish_run(conn, rid, "success", {"select": {"groups": s.groups, "adopted": s.adopted,
                                                        "provisional": s.provisional, "conflicts": s.conflicts}})
        print(f"재선택: 그룹 {s.groups}, 채택 {s.adopted}, 확인 중 {s.provisional}, 충돌 {s.conflicts}")
        return 0

    mode, d_from, d_to = resolve_period(args, cfg)
    if args.mode:
        mode = args.mode
    conn = db.connect(":memory:") if args.dry_run else db.connect()
    http = HttpClient(cfg.media.defaults)
    collectors = build_collectors(cfg, http, args.media)
    stats: dict[str, Any] = {"media": {}, "llm": {}, "errors": []}
    provider = build_provider(cfg, args.llm, stats)
    pipe = Pipeline(conn, cfg, collectors, provider, args.dry_run, args.max_llm, stats)
    run_id = None if args.dry_run else db.start_run(conn, mode, d_from.isoformat(), d_to.isoformat())
    print(f"실행: {mode} {d_from} ~ {d_to}, 매체 {[c.media.id for c in collectors]}, "
          f"LLM {getattr(provider, 'name', '건너뜀')}{' (dry-run)' if args.dry_run else ''}")

    try:
        metas = pipe.collect(d_from, d_to)
        queued = pipe.register(metas)
        retry = [] if args.dry_run else [i for i in pipe.retry_queue() if i not in set(queued)]
        stats["articles"] = {"listed": len(metas), "queued": len(queued), "retry": len(retry)}
        pipe.process(queued + retry)
        if provider is not None and not pipe.llm_stopped and not args.dry_run:
            v = run_verification(conn, cfg, pipe.search_for_verify, pipe.process_verified)
            stats["verify"] = {"tasks": v.tasks_run, "found": v.found, "not_found": v.not_found,
                               "pending": v.still_pending, "searches": v.searches}
        s = select_all(conn, cfg)
        stats["select"] = {"groups": s.groups, "adopted": s.adopted, "provisional": s.provisional,
                           "conflicts": s.conflicts, "replaced": len(s.replaced)}
        stats["reconcile"] = reconcile(conn).summary()
        if provider is None:
            pipe.status = "partial"
        pipe.finish_llm_stats()
        stats["http"] = {"requests": len(http.request_log),
                         "min_interval_sec": round(http.min_interval(), 2) if http.min_interval() else None}
        if args.dry_run:
            rows = load_adopted_rows(conn, cfg)
            print(f"\n채택 {len(rows)}건 (dry-run, 저장 안 함)")
            for r in rows:
                print("  " + r.to_line())
        else:
            export_adopted_csv(conn, cfg)
    except Exception as e:
        pipe.status = "failed"
        stats["errors"].append(f"실패: {e}")
        log.exception("파이프라인 실패")
    finally:
        if run_id is not None:
            db.finish_run(conn, run_id, pipe.status, stats)
        _write_request_log(http, args.dry_run)

    print(f"\n결과: {pipe.status} | 수집 {stats.get('articles')} | LLM {stats['llm']} | "
          f"선택 {stats.get('select')} | 대조 {stats.get('reconcile')}")
    for mid, ms in stats["media"].items():
        for kw, st in ms.items():
            print(f"  {mid}/{kw}: {st}")
    if stats["errors"]:
        print("오류:", *stats["errors"][:20], sep="\n  ")
    return EXIT[pipe.status]


def _write_request_log(http: HttpClient, dry_run: bool) -> None:
    """요청 URL·시각만 남긴다 (본문 없음). 9.5 접속 간격 확인용."""
    logs = PROJECT_ROOT / "logs"
    logs.mkdir(exist_ok=True)
    name = f"requests_{date.today():%Y%m%d}{'_dryrun' if dry_run else ''}.log"
    with open(logs / name, "a", encoding="utf-8") as f:
        for domain, url, t in http.request_log:
            f.write(f"{t:.3f}\t{domain}\t{url}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.pipeline.run")
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--media")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reselect", action="store_true")
    ap.add_argument("--llm", choices=["claude_cli", "mock"])
    ap.add_argument("--max-llm", type=int, default=None, help="이번 실행의 LLM 호출 최대 건수 (점검용)")
    ap.add_argument("--mode", choices=["daily", "backfill", "manual"], help="run_log 모드 강제 (화면 실행 버튼은 manual)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.dry_run:
            return run(args)
        with RunLock():
            return run(args)
    except LockedError as e:
        print(e)
        return 3


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
