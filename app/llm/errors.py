"""LLM 호출 오류 종류."""


class LLMCallError(Exception):
    """재시도 대상 오류 (응답 오류, 파싱 실패, 시간 초과 등)."""

    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind


class UsageLimitError(LLMCallError):
    """사용량 한도. 재시도하지 않고 이번 실행의 LLM 처리를 멈춘다 (PRD 3.2.1)."""

    def __init__(self, message: str):
        super().__init__(message, kind="usage_limit")
