"""claude -p subprocess 호출 (PRD v1.2 3.2.1).

호출 규격
  claude -p --output-format json --json-schema <스키마> --model <모델>
            --system-prompt-file <파일> --tools "" --no-session-persistence
  stdin = User 프롬프트(UTF-8 바이트), cwd = 프로젝트 밖 작업 폴더

- 시스템 프롬프트는 파일로 넘긴다. Windows의 claude.CMD는 cmd.exe를 거쳐 명령줄 8,191자 제한이 있기 때문이다.
- --bare는 쓰지 않는다 (bare 모드는 ANTHROPIC_API_KEY만 받아 claude.ai 로그인을 못 씀).
- 입출력은 항상 UTF-8 바이트로 다룬다 (Windows 기본 cp949면 한글이 깨짐).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from app.config import LLMSettings
from app.llm.errors import LLMCallError, UsageLimitError
from app.llm.prompts import ArticleInput, build_user_prompt

Runner = Callable[..., subprocess.CompletedProcess]

# 사용량 한도로 보고 이번 실행을 멈출 문구. 일시적 과부하(overloaded 등)는 일반 오류로 재시도한다.
USAGE_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|limit exceeded|too many requests",
                            re.IGNORECASE)


def resolve_executable(name: str = "claude") -> str:
    exe = shutil.which(name)
    if not exe:
        raise LLMCallError(f"Claude Code 실행 파일을 찾을 수 없습니다: {name}", kind="not_installed")
    return exe


def parse_cli_output(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> tuple[dict, str | None]:
    """claude -p --output-format json 출력에서 (structured_output, 모델 ID)를 꺼낸다.

    오류면 LLMCallError, 사용량 한도면 UsageLimitError.
    """
    text = stdout.decode("utf-8", errors="replace").strip()
    err_text = stderr.decode("utf-8", errors="replace")
    try:
        data: dict[str, Any] = json.loads(text) if text else {}
    except json.JSONDecodeError as e:
        if USAGE_LIMIT_RE.search(text + err_text):
            raise UsageLimitError((text or err_text)[:300]) from e
        raise LLMCallError(f"JSON 파싱 실패 (rc={returncode}): {(text or err_text)[:300]}", kind="parse") from e

    if not data:
        if USAGE_LIMIT_RE.search(err_text):
            raise UsageLimitError(err_text[:300])
        raise LLMCallError(f"빈 응답 (rc={returncode}): {err_text[:300]}", kind="empty")

    if data.get("is_error") or data.get("subtype") != "success":
        detail = str(data.get("result") or data.get("subtype") or err_text)[:300]
        if USAGE_LIMIT_RE.search(detail + err_text):
            raise UsageLimitError(detail)
        raise LLMCallError(f"claude -p 오류: {detail}", kind="cli_error")

    structured = data.get("structured_output")
    if not isinstance(structured, dict):
        raise LLMCallError("structured_output이 없습니다 (스키마 출력 실패)", kind="no_structured_output")

    model = next(iter(data.get("modelUsage") or {}), None)
    return structured, model


class ClaudeCliProvider:
    name = "claude_cli"

    def __init__(self, settings: LLMSettings, system_prompt: str, schema: dict,
                 runner: Runner = subprocess.run):
        self.settings = settings
        self.model = settings.model
        self.system_prompt = system_prompt
        self.schema_json = json.dumps(schema, ensure_ascii=False)
        self.runner = runner
        self._workdir = Path(settings.claude_cli.workdir) if settings.claude_cli.workdir \
            else Path(tempfile.gettempdir()) / "scrap_price_llm"
        self._workdir.mkdir(parents=True, exist_ok=True)
        self._prompt_file = self._workdir / "system_prompt.txt"
        self._prompt_file.write_text(system_prompt, encoding="utf-8")
        self._exe: str | None = None

    def command(self) -> list[str]:
        if self._exe is None:
            self._exe = resolve_executable(self.settings.claude_cli.executable)
        return [
            self._exe, "-p",
            "--output-format", "json",
            "--json-schema", self.schema_json,
            "--model", self.model,
            "--system-prompt-file", str(self._prompt_file),
            *self.settings.claude_cli.extra_args,
        ]

    def analyze(self, article: ArticleInput) -> tuple[dict, str | None]:
        """(structured_output dict, 모델 ID). 검증은 client.analyze_with_retry가 한다."""
        prompt = build_user_prompt(article, self.settings.max_input_chars)
        try:
            proc = self.runner(self.command(), input=prompt.encode("utf-8"), capture_output=True,
                               timeout=self.settings.timeout_sec, cwd=str(self._workdir))
        except subprocess.TimeoutExpired as e:
            raise LLMCallError(f"시간 초과 ({self.settings.timeout_sec}초)", kind="timeout") from e
        return parse_cli_output(proc.stdout or b"", proc.stderr or b"", proc.returncode)


def check_auth(executable: str = "claude", runner: Runner = subprocess.run) -> bool:
    """`claude auth status --json`의 loggedIn 값. 확인할 수 없으면 False."""
    try:
        proc = runner([resolve_executable(executable), "auth", "status", "--json"], capture_output=True, timeout=60)
        data = json.loads((proc.stdout or b"").decode("utf-8", errors="replace"))
    except (LLMCallError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return False
    return bool(data.get("loggedIn"))
