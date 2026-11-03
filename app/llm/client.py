"""LLM 제공자 인터페이스, 제공자 선택, 재시도 (PRD v1.2 3.2.1).

provider.analyze(article) -> (structured dict, model)  를 호출하고 pydantic으로 검증한다.
오류·검증 실패는 retries회 재시도, 그래도 실패하면 ok=False (호출 측에서 llm_error 처리).
사용량 한도(UsageLimitError)는 재시도하지 않고 그대로 올린다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from pydantic import ValidationError

from app.config import AppConfig
from app.llm.errors import LLMCallError, UsageLimitError
from app.llm.prompts import ArticleInput, build_system_prompt
from app.llm.schema import ArticleAnalysis, build_json_schema

__all__ = ["LLMProvider", "AnalysisResult", "get_provider", "analyze_with_retry", "UsageLimitError"]


class LLMProvider(Protocol):
    name: str
    model: str

    def analyze(self, article: ArticleInput) -> tuple[dict, str | None]: ...


@dataclass
class AnalysisResult:
    ok: bool
    analysis: ArticleAnalysis | None
    provider: str
    model: str | None
    attempts: int
    seconds: float
    error: str | None = None


def get_provider(cfg: AppConfig, override: str | None = None) -> LLMProvider:
    name = override or cfg.settings.llm.provider
    if name == "mock":
        from app.llm.mock import MockProvider
        return MockProvider(cfg.settings.llm.mock.responses_dir)
    if name == "claude_cli":
        from app.llm.claude_cli import ClaudeCliProvider
        return ClaudeCliProvider(cfg.settings.llm, build_system_prompt(cfg), build_json_schema(cfg.steelmakers.names()))
    raise ValueError(f"알 수 없는 LLM 제공자: {name} (claude_cli | mock)")


def analyze_with_retry(provider: LLMProvider, article: ArticleInput, retries: int = 1) -> AnalysisResult:
    start = time.monotonic()
    last_error: str | None = None
    model: str | None = getattr(provider, "model", None)
    for attempt in range(1, retries + 2):
        try:
            raw, model = provider.analyze(article)
            analysis = ArticleAnalysis.model_validate(raw)
            return AnalysisResult(True, analysis, provider.name, model, attempt, time.monotonic() - start)
        except UsageLimitError:
            raise
        except ValidationError as e:
            last_error = f"스키마 검증 실패: {e.error_count()}건 — {e.errors()[0]['msg']}"
        except LLMCallError as e:
            last_error = f"{e.kind}: {e}"
    return AnalysisResult(False, None, provider.name, model, retries + 1, time.monotonic() - start, last_error)
