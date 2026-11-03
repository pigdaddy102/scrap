"""System/User 프롬프트와 few-shot (PRD v1.2 3.2.4).

few-shot 예시는 직접 작성한 가상 기사이며, 골든셋(tests/fixtures/synthetic)과 겹치지 않게 만든다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.config import AppConfig


@dataclass
class ArticleInput:
    """LLM에 넘기는 기사 1건. body는 메모리에서만 쓰고 저장하지 않는다."""

    media_name: str
    title: str
    published_date: str  # YYYY-MM-DD
    body: str = ""
    access_level: str = "full"
    key: str | None = None  # mock 응답 조회용 (fixture_id 또는 url_norm 해시)


def _ex(article_type: str, reason: str, announcements: list[dict]) -> str:
    return json.dumps({"article_type": article_type, "type_reason": reason, "announcements": announcements},
                      ensure_ascii=False)


FEW_SHOTS: list[tuple[str, str]] = [
    (
        "<published_date>2026-08-03</published_date>\n<title>한국철강, 6일부 철스크랩 구매가격 kg당 15원 인상</title>\n"
        "<body>한국철강이 오는 6일부터 철스크랩 구매가격을 전 등급 kg당 15원 인상한다. 다만 선반류는 이번 조정에서 제외된다.</body>",
        _ex("single", "한국철강 한 곳의 신규 인상 발표", [
            {"steelmaker": "한국철강", "apply_date": "2026-08-06", "apply_date_inferred": True,
             "items": [{"plant": None, "grade": "전 등급", "change_krw_per_kg": 15, "note": "선반류 제외"}],
             "confidence": 0.95}]),
    ),
    (
        "<published_date>2026-08-10</published_date>\n<title>동국제강, 공장별 철스크랩 매입가 차등 인하</title>\n"
        "<body>동국제강은 13일부 인천공장 생철류를 톤당 1만원, 포항공장 중량류를 톤당 5천원 인하한다고 밝혔다.</body>",
        _ex("single", "동국제강 한 곳의 공장별 발표(여러 공장이어도 single)", [
            {"steelmaker": "동국제강", "apply_date": "2026-08-13", "apply_date_inferred": True,
             "items": [{"plant": "인천", "grade": "생철류", "change_krw_per_kg": -10, "note": None},
                       {"plant": "포항", "grade": "중량류", "change_krw_per_kg": -5, "note": None}],
             "confidence": 0.9}]),
    ),
    (
        "<published_date>2026-08-17</published_date>\n<title>현대제철·한국특강, 20일부 철스크랩 가격 동시 인하</title>\n"
        "<body>현대제철과 한국특강이 20일부터 철스크랩 구매가격을 일제히 내린다. 현대제철은 전등급 kg당 10원, "
        "한국특강은 경량류만 kg당 5원 인하한다. 세아베스틸도 같은 날 인하에 동참한다.</body>",
        _ex("multi", "두 제강사의 신규 발표를 처음 함께 보도(세아베스틸은 대상 아님)", [
            {"steelmaker": "현대제철", "apply_date": "2026-08-20", "apply_date_inferred": True,
             "items": [{"plant": None, "grade": "전등급", "change_krw_per_kg": -10, "note": None}], "confidence": 0.95},
            {"steelmaker": "한국특강", "apply_date": "2026-08-20", "apply_date_inferred": True,
             "items": [{"plant": None, "grade": "경량류", "change_krw_per_kg": -5, "note": None}], "confidence": 0.95}]),
    ),
    (
        "<published_date>2026-08-31</published_date>\n<title>[8월 결산] 제강사 철스크랩 가격 3차례 인하</title>\n"
        "<body>8월 한 달간 제강사들은 철스크랩 가격을 잇달아 내렸다. 대한제강이 8월 7일부 전등급 kg당 10원을 내린 데 이어 "
        "포스코가 8월 24일부 포항 생철류를 kg당 10원 인하했다.</body>",
        _ex("history", "이미 보도된 여러 발표를 월말에 모아 정리", [
            {"steelmaker": "대한제강", "apply_date": "2026-08-07", "apply_date_inferred": False,
             "items": [{"plant": None, "grade": "전등급", "change_krw_per_kg": -10, "note": None}], "confidence": 0.9},
            {"steelmaker": "포스코", "apply_date": "2026-08-24", "apply_date_inferred": False,
             "items": [{"plant": "포항", "grade": "생철류", "change_krw_per_kg": -10, "note": None}], "confidence": 0.9}]),
    ),
    (
        "<published_date>2026-08-25</published_date>\n<title>현대제철, 9월 철스크랩 구매가격 동결</title>\n"
        "<body>현대제철은 9월 첫 주 철스크랩 구매가격을 현 수준에서 유지하기로 했다.</body>",
        _ex("none", "가격 변동이 없는 동결 발표는 수집하지 않음", []),
    ),
]


def build_system_prompt(cfg: AppConfig) -> str:
    sm = cfg.steelmakers
    maker_lines = []
    for s in sm.steelmakers:
        plants = ", ".join(s.plants) if s.plants else "공장 구분 없음"
        maker_lines.append(f"- {s.name} (별칭: {', '.join(s.aliases)}) — 공장: {plants}")
    shots = "\n\n".join(f"[예시 {i}]\n입력:\n{inp}\n출력:\n{out}" for i, (inp, out) in enumerate(FEW_SHOTS, 1))
    return f"""너는 철강 원료(철스크랩) 시장 기사 분석가다. 공개 기사에서 제강사의 철스크랩 구매(매입) 단가 발표만 정확히 추출한다.
결과는 지정된 JSON 스키마로만 출력한다.

## 대상 제강사 (이 6곳만 추출)
{chr(10).join(maker_lines)}
별칭은 정식 이름으로 바꾼다. 대상이 아닌 회사(세아베스틸, 세아창원특수강, 태웅, YK스틸 등)는 출력하지 않는다.

## 기사 유형
- single: 한 제강사의 단가 인상·인하가 기사의 주내용. 한 제강사의 여러 공장 발표도 single이다(multi 아님).
- multi: 한 기사가 2곳 이상 제강사의 새 단가발표를 함께 처음 보도. 대상 제강사가 1곳뿐이어도 비대상 회사와 함께 새 발표를 보도하면 multi다.
- history: 기간 중 이미 나온 여러 발표를 모아 정리·회고하거나 주간·월간 시황에서 되짚는 기사.
- none: 단가 발표와 무관(제품 판매가격, 수입 스크랩 오퍼·해외 가격, 일반 시황, 실적, 인사, 입고 중단 등),
  대상 제강사가 없는 기사, 그리고 가격 변동이 없는 동결·유지 발표.

보조 규칙:
- 기사 주제가 한 제강사 발표이고 다른 제강사의 이전 발표를 곁들여 언급하면 single로 보고 곁들인 언급은 추출하지 않는다.
- 철스크랩 구매 가격이 아닌 것(철근·형강 판매가격 등)은 none이다.

## 추출 규칙
- apply_date: 단가 적용 시작일(YYYY-MM-DD). '14일부', '11월 2일부'처럼 연도나 월이 빠지면 게재일 기준으로 추론하고 apply_date_inferred=true.
- plant: 공장별 발표일 때만 공장명(위 공장 목록의 이름). 전사 공통이면 null.
- grade: 기사에 나온 등급 표현을 그대로 쓴다(예: '생철류', '그 외 등급', '전 등급'). 바꾸거나 합치지 않는다.
  등급 언급 없이 일괄 조정하면 '전등급'으로 쓴다.
- change_krw_per_kg: 정수, 원/kg. 인상은 양수, 인하는 음수. '톤당 1만원'처럼 원/톤이면 1000으로 나눈다(톤당 1만원 = 10).
- note: 발표 내용 중 제외 등급이나 적용 조건처럼 가격 표기에 덧붙일 내용만 짧게 쓴다(예: '선반압축 제외'). 없으면 null.
  다음은 note에 쓰지 않는다: 적용 공장 범위(plant로 표현), 조정에서 빠진 다른 공장, 본문 열람 여부·추론 근거
  (confidence로 표현), 배경 설명.
- 한 발표에서 등급별·공장별 변동폭이 다르면 items를 여러 개로 나눈다.
- confidence: 0~1. 본문 일부만 있거나(access_level=partial) 추론이 들어가면 0.7 이하.

## 금지
- 기사에 없는 수치를 지어내지 않는다. 불확실하면 confidence를 낮춘다.
- 근거 문장(본문 인용)을 출력하지 않는다.

## 예시
{shots}
"""


def build_user_prompt(article: ArticleInput, max_input_chars: int = 6000) -> str:
    body = (article.body or "")[:max_input_chars]
    return (
        "<article>\n"
        f"<media>{article.media_name}</media>\n"
        f"<published_date>{article.published_date}</published_date>\n"
        f"<access_level>{article.access_level}</access_level>\n"
        f"<title>{article.title}</title>\n"
        f"<body>{body}</body>\n"
        "</article>\n"
        "위 기사를 분석해 지정된 JSON 스키마로 결과를 출력하라."
    )
