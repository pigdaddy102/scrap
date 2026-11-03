"""LLM 응답 모델과 JSON Schema (PRD v1.2 3.2.3).

- pydantic 모델로 응답을 검증한다.
- claude -p --json-schema 에 넘길 스키마는 build_json_schema()가 만든다.
  $ref 없이 펼친 형태이고, 제강사 목록은 steelmakers.yaml에서 enum으로 채운다.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

ArticleType = Literal["single", "multi", "history", "none"]
ARTICLE_TYPES: tuple[str, ...] = ("single", "multi", "history", "none")


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plant: str | None = None
    grade: str = Field(min_length=1)
    change_krw_per_kg: int
    note: str | None = None


class Announcement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steelmaker: str
    apply_date: date
    apply_date_inferred: bool = False
    items: list[Item] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class ArticleAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    article_type: ArticleType
    type_reason: str = ""
    announcements: list[Announcement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _none_has_no_announcements(self) -> "ArticleAnalysis":
        # PRD 3.2.3: none이면 announcements는 빈 배열
        if self.article_type == "none":
            self.announcements = []
        return self


def build_json_schema(steelmakers: Sequence[str]) -> dict:
    """claude -p --json-schema 용 스키마 (모든 필드 required, additionalProperties false)."""
    item = {
        "type": "object",
        "properties": {
            "plant": {"type": ["string", "null"], "description": "공장별 발표일 때만 공장명, 전사 공통이면 null"},
            "grade": {"type": "string", "description": "기사에 나온 등급 표현 그대로"},
            "change_krw_per_kg": {"type": "integer", "description": "원/kg, 인상 양수·인하 음수. 원/톤이면 1000으로 나눔"},
            "note": {"type": ["string", "null"], "description": "제외 등급 등 별도 표기, 없으면 null"},
        },
        "required": ["plant", "grade", "change_krw_per_kg", "note"],
        "additionalProperties": False,
    }
    announcement = {
        "type": "object",
        "properties": {
            "steelmaker": {"type": "string", "enum": list(steelmakers)},
            "apply_date": {"type": "string", "description": "YYYY-MM-DD"},
            "apply_date_inferred": {"type": "boolean"},
            "items": {"type": "array", "items": item, "minItems": 1},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["steelmaker", "apply_date", "apply_date_inferred", "items", "confidence"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "article_type": {"type": "string", "enum": list(ARTICLE_TYPES)},
            "type_reason": {"type": "string"},
            "announcements": {"type": "array", "items": announcement},
        },
        "required": ["article_type", "type_reason", "announcements"],
        "additionalProperties": False,
    }
