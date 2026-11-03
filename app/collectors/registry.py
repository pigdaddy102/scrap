"""media.yaml의 method/parser → 수집기 매핑 (PRD v1.2 3.1.1). enabled:false 매체는 건너뛴다."""

from __future__ import annotations

from app.collectors.base import BaseCollector, HttpClient
from app.collectors.ndsoft import NdsoftCollector
from app.config import AppConfig

PARSERS: dict[str, type[BaseCollector]] = {"ndsoft": NdsoftCollector, "ndsoft_altlist": NdsoftCollector}


def build_collectors(cfg: AppConfig, http: HttpClient, only: str | None = None) -> list[BaseCollector]:
    out = []
    for m in cfg.media.ordered():
        if only and m.id != only:
            continue
        cls = PARSERS.get(m.parser)
        if cls is None:
            raise ValueError(f"{m.id}: 지원하지 않는 parser '{m.parser}'")
        out.append(cls(m, cfg.media.defaults, http, cfg.media.max_pages(m.id)))
    if only and not out:
        raise ValueError(f"매체를 찾을 수 없거나 비활성: {only}")
    return out
