"""mock 응답 제공자 (PRD v1.2 3.2.1). 개발·단위 테스트·시드 데이터용.

responses_dir/{key}.json 을 돌려준다. key는 ArticleInput.key (fixture_id 또는 url_norm 해시).
파일이 없으면 none 응답.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.config import PROJECT_ROOT
from app.llm.prompts import ArticleInput

NONE_RESPONSE = {"article_type": "none", "type_reason": "mock: 응답 없음", "announcements": []}


def key_for_url(url_norm: str) -> str:
    return hashlib.sha1(url_norm.encode("utf-8")).hexdigest()[:16]


class MockProvider:
    name = "mock"
    model = "mock"

    def __init__(self, responses_dir: str | Path):
        p = Path(responses_dir)
        self.responses_dir = p if p.is_absolute() else PROJECT_ROOT / p

    def analyze(self, article: ArticleInput) -> tuple[dict, str | None]:
        if article.key:
            path = self.responses_dir / f"{article.key}.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8")), self.model
        return dict(NONE_RESPONSE), self.model
