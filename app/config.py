"""YAML 설정 3종(settings, media, steelmakers)을 pydantic 모델로 읽는다. (PRD v1.2 3.1.1, 3.1.3, 부록 A)"""

from __future__ import annotations

import os
import re
import warnings
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# .env가 있으면 읽는다. 이미 설정된 환경 변수가 우선한다.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------- media.yaml


class Selectors(BaseModel):
    item: str
    title: str
    link: str
    date: str


class MediaDefaults(BaseModel):
    request_interval_sec: float = 3
    timeout_sec: float = 15
    max_retries: int = 2
    max_pages_per_keyword: int = 5
    user_agent: str


class Media(BaseModel):
    id: str
    name: str
    order: int
    enabled: bool = True
    method: Literal["site_search", "section_list"]
    base_url: str
    search_url: str | None = None
    list_url: str | None = None
    keyword_encoding: str = "utf-8"
    single_keyword_only: bool = False
    parser: str
    selectors: Selectors
    page_size: int = 20
    max_pages: int | None = None  # None이면 defaults.max_pages_per_keyword
    # pages: page 파라미터로 넘김 / date_windows: 기간을 window_days 단위로 나눠 구간마다 1페이지(page 파라미터 없음)
    paging: Literal["pages", "date_windows"] = "pages"
    window_days: int = 7
    access: Literal["full", "partial", "mixed"] = "full"

    @model_validator(mode="after")
    def _check_urls(self) -> "Media":
        if self.method == "site_search" and not self.search_url:
            raise ValueError(f"{self.id}: site_search 매체는 search_url이 필요합니다")
        if self.method == "section_list" and not self.list_url:
            raise ValueError(f"{self.id}: section_list 매체는 list_url이 필요합니다")
        return self


class MediaConfig(BaseModel):
    defaults: MediaDefaults
    media: list[Media]

    @model_validator(mode="after")
    def _unique(self) -> "MediaConfig":
        ids = [m.id for m in self.media]
        orders = [m.order for m in self.media]
        if len(ids) != len(set(ids)):
            raise ValueError("media.yaml: id가 중복됩니다")
        if len(orders) != len(set(orders)):
            raise ValueError("media.yaml: order가 중복됩니다")
        return self

    def ordered(self, include_disabled: bool = False) -> list[Media]:
        """수집 순서(order 오름차순). 기본은 enabled 매체만."""
        items = [m for m in self.media if include_disabled or m.enabled]
        return sorted(items, key=lambda m: m.order)

    def get(self, media_id: str) -> Media:
        for m in self.media:
            if m.id == media_id:
                return m
        raise KeyError(media_id)

    def order_of(self, media_id: str) -> int:
        return self.get(media_id).order

    def max_pages(self, media_id: str) -> int:
        m = self.get(media_id)
        return m.max_pages if m.max_pages is not None else self.defaults.max_pages_per_keyword


# ---------------------------------------------------------- steelmakers.yaml


class Steelmaker(BaseModel):
    name: str
    short: str
    aliases: list[str] = Field(default_factory=list)
    plants: list[str] = Field(default_factory=list)
    color: str

    @field_validator("color")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError(f"색상은 #RRGGBB 형식이어야 합니다: {v}")
        return v


class GradeMapping(BaseModel):
    enabled: bool = False
    map: dict[str, str] = Field(default_factory=dict)

    def apply(self, grade: str) -> str:
        """등급명 매핑 (PRD 3.2.5 3번). 앞뒤 공백을 지운 뒤 완전 일치로 비교, 맵에 없으면 그대로."""
        g = grade.strip()
        if not self.enabled:
            return g
        return self.map.get(g, g)


class SteelmakerConfig(BaseModel):
    steelmakers: list[Steelmaker]
    grades_standard: list[str] = Field(default_factory=list)
    grade_mapping: GradeMapping = Field(default_factory=GradeMapping)

    def names(self) -> list[str]:
        """표시·정렬 순서대로 제강사 정식 이름."""
        return [s.name for s in self.steelmakers]

    def get(self, name: str) -> Steelmaker:
        for s in self.steelmakers:
            if s.name == name:
                return s
        raise KeyError(name)

    def plant_order(self, name: str) -> list[str]:
        return list(self.get(name).plants)

    def sort_index(self, name: str) -> int:
        return self.names().index(name)


# ------------------------------------------------------------ settings.yaml


class Period(BaseModel):
    default_from: date
    default_to: date
    daily_lookback_days: int = 3


class Prefilter(BaseModel):
    must_include_any: list[str] = Field(default_factory=list)
    exclude_if_title_has: list[str] = Field(default_factory=list)


class ClaudeCliSettings(BaseModel):
    executable: str = "claude"
    extra_args: list[str] = Field(default_factory=list)
    workdir: str | None = None
    check_auth: bool = True


class MockSettings(BaseModel):
    responses_dir: str = "tests/fixtures/mock_responses"


class LLMSettings(BaseModel):
    provider: Literal["claude_cli", "mock"] = "claude_cli"
    model: str
    max_input_chars: int = 6000
    timeout_sec: int = 180
    retries: int = 1
    max_attempts_per_article: int = 3
    claude_cli: ClaudeCliSettings = Field(default_factory=ClaudeCliSettings)
    mock: MockSettings = Field(default_factory=MockSettings)


class VerifySettings(BaseModel):
    max_tasks_per_run: int = 20
    max_attempts: int = 3
    search_window_days: int = 21


class MonitoringSettings(BaseModel):
    log_oldest_published: bool = True
    warn_on_possible_gap: bool = True


class PublishSettings(BaseModel):
    git_push: bool = True
    allowed_paths: list[str] = Field(default_factory=list)
    commit_message: str = "data: daily update {date}"


class AppSettings(BaseModel):
    mode: Literal["collector", "viewer"] = "collector"


class Settings(BaseModel):
    period: Period
    keywords: list[str]
    prefilter: Prefilter = Field(default_factory=Prefilter)
    llm: LLMSettings
    verify: VerifySettings = Field(default_factory=VerifySettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    publish: PublishSettings = Field(default_factory=PublishSettings)
    app: AppSettings = Field(default_factory=AppSettings)

    @field_validator("keywords")
    @classmethod
    def _single_word(cls, v: list[str]) -> list[str]:
        # PRD 3.1.3: 두 단어 검색은 0건이므로 공백이 들어간 키워드는 경고
        for kw in v:
            if re.search(r"\s", kw.strip()):
                warnings.warn(f"검색 키워드에 공백이 있습니다(두 단어 검색은 결과 0건): {kw!r}", stacklevel=2)
        return [kw.strip() for kw in v]


# -------------------------------------------------------------------- 묶음


class AppConfig(BaseModel):
    settings: Settings
    media: MediaConfig
    steelmakers: SteelmakerConfig


def load_settings(path: Path | None = None) -> Settings:
    s = Settings.model_validate(_load_yaml(path or CONFIG_DIR / "settings.yaml"))
    # 환경 변수가 설정 파일보다 우선 (PRD 3.6.3, .env.example)
    if os.environ.get("APP_MODE"):
        s.app.mode = os.environ["APP_MODE"]  # type: ignore[assignment]
        Settings.model_validate(s.model_dump())  # 값 검증
    if os.environ.get("LLM_PROVIDER"):
        s.llm.provider = os.environ["LLM_PROVIDER"]  # type: ignore[assignment]
        Settings.model_validate(s.model_dump())
    return s


def load_media(path: Path | None = None) -> MediaConfig:
    return MediaConfig.model_validate(_load_yaml(path or CONFIG_DIR / "media.yaml"))


def load_steelmakers(path: Path | None = None) -> SteelmakerConfig:
    return SteelmakerConfig.model_validate(_load_yaml(path or CONFIG_DIR / "steelmakers.yaml"))


@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    """프로젝트 기본 설정을 한 번만 읽어 캐시한다. 테스트에서는 get_config.cache_clear()."""
    return AppConfig(settings=load_settings(), media=load_media(), steelmakers=load_steelmakers())
