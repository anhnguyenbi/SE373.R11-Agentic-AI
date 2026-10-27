"""Lớp gọi model dùng chung cho Lab 07.

- OpenAIChat: gọi endpoint OpenAI-compatible (Chat Completions + tool calling) bằng thư viện chuẩn.
- ScriptedLLM: "model" giả lập theo kịch bản để chạy offline, test và demo không cần API key.
- run_agent: vòng lặp agent gọi tool cho tới khi model trả lời cuối, đồng thời cộng dồn usage.

Số input tokens khi chạy thật lấy từ `usage.prompt_tokens` mà provider trả về.
Khi chạy offline, token được ƯỚC LƯỢNG (≈ 4 ký tự / token) và luôn gắn cờ `estimated=True`.
"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

LAB_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- usage


@dataclass
class Usage:
    input_tokens: int = 0       # tổng input tokens, gồm cả phần đọc/ghi cache nếu provider tách riêng
    output_tokens: int = 0
    calls: int = 0
    estimated: bool = False
    cached_input_tokens: int = 0
    raw: list = field(default_factory=list)  # usage nguyên bản provider trả về, để kiểm tra lại
    estimated_input_tokens: int = 0          # ước lượng của chính client trên payload gửi đi
    undercounted_calls: int = 0              # số lần provider báo thấp hơn nhiều so với payload thật

    @property
    def provider_reliable(self) -> bool:
        return self.undercounted_calls == 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.calls += other.calls
        self.estimated = self.estimated or other.estimated
        self.cached_input_tokens += other.cached_input_tokens
        self.raw.extend(other.raw)
        self.estimated_input_tokens += other.estimated_input_tokens
        self.undercounted_calls += other.undercounted_calls

    def to_dict(self) -> dict:
        data = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "calls": self.calls,
            "estimated": self.estimated,
        }
        if not self.estimated:
            data["cached_input_tokens"] = self.cached_input_tokens
            data["estimated_input_tokens"] = self.estimated_input_tokens
            data["provider_usage_reliable"] = self.provider_reliable
            data["raw_usage"] = self.raw
        return data


def pick_token_source(usages: dict[str, "Usage"]) -> tuple[dict[str, int], str]:
    """Chọn MỘT nguồn số đo chung cho mọi mode để so sánh công bằng.

    Dùng usage của provider nếu mọi lần gọi đều đáng tin; nếu có lần nào provider báo thiếu
    (ví dụ bỏ phần cache), dùng ước lượng của client cho TẤT CẢ các mode.
    """
    if all(not u.estimated and u.provider_reliable for u in usages.values()):
        return {k: u.input_tokens for k, u in usages.items()}, "provider usage"
    if all(u.estimated for u in usages.values()):
        return {k: u.input_tokens for k, u in usages.items()}, "ước lượng offline"
    return {k: u.estimated_input_tokens for k, u in usages.items()}, \
        "ước lượng của client (provider báo thiếu phần context được cache)"


def usage_from_provider(usage: dict) -> "Usage":
    """Chuẩn hoá usage của provider.

    - Chuẩn OpenAI: prompt_tokens ĐÃ gồm prompt_tokens_details.cached_tokens.
    - Kiểu Anthropic (một số router chuyển tiếp nguyên): input_tokens KHÔNG gồm
      cache_read_input_tokens / cache_creation_input_tokens, nên phải cộng thêm.
    """
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    cache_read = int(usage.get("cache_read_input_tokens", 0) or 0)
    cache_write = int(usage.get("cache_creation_input_tokens", 0) or 0)
    details_cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0)
    if cache_read or cache_write:
        total, cached = prompt + cache_read + cache_write, cache_read
    elif details_cached > prompt:  # cached nằm ngoài prompt_tokens: router không theo chuẩn OpenAI
        total, cached = prompt + details_cached, details_cached
    else:
        total, cached = prompt, details_cached
    return Usage(total, int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0), 1,
                 estimated=False, cached_input_tokens=cached, raw=[usage])


def estimate_tokens(obj: Any) -> int:
    """Ước lượng thô số token: ~4 ký tự / token trên JSON của payload."""
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return max(1, math.ceil(len(text) / 4))


# --------------------------------------------------------------------------- settings


def _read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class Settings:
    api_key: str
    model: str
    base_url: str


def load_settings() -> Settings:
    """Đọc cấu hình từ biến môi trường, sau đó tới Lab_07/.env. Thiếu thì báo lỗi rõ ràng."""
    file_values = _read_dotenv(LAB_DIR / ".env")

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    settings = Settings(
        api_key=get("OPENAI_API_KEY"),
        model=get("MODEL_NAME"),
        base_url=get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
    )
    missing = [n for n, v in (("OPENAI_API_KEY", settings.api_key), ("MODEL_NAME", settings.model)) if not v]
    if missing:
        raise SystemExit(f"Thiếu cấu hình {', '.join(missing)}. Sao chép .env.example thành .env và điền giá trị.")
    return settings


# --------------------------------------------------------------------------- models


@dataclass
class ChatResult:
    message: dict
    usage: Usage


def _parse_sse(text: str) -> dict:
    """Ghép các chunk SSE (`data: {...}`) của Chat Completions thành một response thường."""
    content, tool_calls, usage, role = [], {}, None, "assistant"
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        chunk = json.loads(payload)
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}
            role = delta.get("role") or role
            if delta.get("content"):
                content.append(delta["content"])
            for call in delta.get("tool_calls") or []:
                slot = tool_calls.setdefault(call.get("index", len(tool_calls)),
                                             {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                slot["id"] = call.get("id") or slot["id"]
                fn = call.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
    message: dict[str, Any] = {"role": role, "content": "".join(content) or None}
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    return {"choices": [{"message": message}], "usage": usage or {}}


def parse_response_body(text: str) -> dict:
    """Đọc response JSON thường; nếu provider trả stream SSE thì tự ghép lại."""
    stripped = text.strip()
    if not stripped:
        raise ProviderError(200, "Provider trả về body rỗng.")
    if stripped.startswith("data:") or "\ndata:" in stripped[:2000]:
        return _parse_sse(stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        raise ProviderError(200, "Response không phải JSON. 300 ký tự đầu: " + stripped[:300]) from None


_HINTS = {
    400: "Request bị từ chối: thường do MODEL_NAME sai hoặc model không hỗ trợ tool calling.",
    401: "Sai hoặc thiếu OPENAI_API_KEY.",
    403: "Key không có quyền dùng model/endpoint này.",
    404: "Sai OPENAI_BASE_URL (cần kết thúc bằng /v1) hoặc sai MODEL_NAME.",
    429: "Bị giới hạn tốc độ/quota ở upstream, đợi một lúc rồi chạy lại.",
    503: "Router không có kết nối upstream khả dụng cho model này (tài khoản hết quota, bị tắt, "
         "hoặc model không được tài khoản hỗ trợ). Kiểm tra Providers / Console Log trên dashboard.",
}


class ProviderError(RuntimeError):
    def __init__(self, status: int, detail: str):
        hint = _HINTS.get(status, "")
        super().__init__(f"HTTP {status} từ provider. {hint}\nChi tiết: {detail}")
        self.status = status


class OpenAIChat:
    """Client tối giản cho /chat/completions (OpenAI-compatible)."""

    def _error_detail(self, exc) -> str:
        try:
            text = exc.read().decode("utf-8", "replace")[:600]
        except Exception:
            text = ""
        return text.replace(self.settings.api_key, "***") or "(không có nội dung)"

    def __init__(self, settings: Settings | None = None, timeout: float = 120.0):
        self.settings = settings or load_settings()
        self.timeout = timeout
        self.name = self.settings.model

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | None = None) -> ChatResult:
        body: dict[str, Any] = {"model": self.settings.model, "messages": messages, "stream": False}
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice
        request = urllib.request.Request(
            self.settings.base_url + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.settings.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = parse_response_body(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(exc.code, self._error_detail(exc)) from None
        except urllib.error.URLError as exc:
            raise ProviderError(0, f"Không kết nối được {self.settings.base_url}: {exc.reason}. "
                                   "Router/endpoint đã chạy chưa?") from None
        usage = data.get("usage") or {}
        if "prompt_tokens" not in usage and "input_tokens" not in usage:
            raise RuntimeError("Provider không trả usage.prompt_tokens nên không đo được input tokens.")
        message = data["choices"][0]["message"]
        message.setdefault("content", None)
        parsed = usage_from_provider(usage)
        # Một số router (ví dụ 9Router + Claude Code) bỏ phần context đã cache khỏi usage trả về.
        # Ghi thêm ước lượng của client để phát hiện và vẫn so sánh được.
        parsed.estimated_input_tokens = estimate_tokens({"messages": messages, "tools": tools or []})
        if parsed.estimated_input_tokens > 3000 and parsed.input_tokens < 0.5 * parsed.estimated_input_tokens:
            parsed.undercounted_calls = 1
        return ChatResult(
            message={k: v for k, v in message.items() if k in ("role", "content", "tool_calls")},
            usage=parsed,
        )


Policy = Callable[[list[dict], list[dict] | None], dict]


class ScriptedLLM:
    """Model giả lập: một hàm policy nhận messages và trả về assistant message.

    Dùng để chạy offline. Policy chỉ được nhìn thấy đúng những gì có trong context,
    giống như model thật, nên vẫn kiểm chứng được tác dụng của memory/compaction.
    """

    name = "scripted-offline"

    def __init__(self, policy: Policy):
        self.policy = policy

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             tool_choice: str | None = None) -> ChatResult:
        message = self.policy(messages, None if tool_choice == "none" else tools)
        message.setdefault("role", "assistant")
        message.setdefault("content", None)
        size = estimate_tokens({"messages": messages, "tools": tools or []})
        usage = Usage(size, estimate_tokens(message), 1, estimated=True, estimated_input_tokens=size)
        return ChatResult(message=message, usage=usage)


# --------------------------------------------------------------------------- tools + agent loop


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


def tool_call(name: str, arguments: dict, call_id: str) -> dict:
    """Tạo assistant message gọi tool (dùng trong policy offline)."""
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}


@dataclass
class AgentRun:
    answer: str
    messages: list[dict]
    trace: list[dict] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


def run_agent(llm, messages: list[dict], tools: list[Tool], max_steps: int = 12) -> AgentRun:
    registry = {t.name: t for t in tools}
    schemas = [t.schema() for t in tools] or None
    run = AgentRun(answer="", messages=list(messages))
    for step in range(1, max_steps + 1):
        result = llm.chat(run.messages, schemas)
        run.usage.add(result.usage)
        message = result.message
        run.messages.append(message)
        run.trace.append({"step": step, "input_tokens": result.usage.input_tokens,
                          "tool_calls": [c["function"]["name"] for c in message.get("tool_calls") or []]})
        calls = message.get("tool_calls") or []
        if not calls:
            run.answer = message.get("content") or ""
            return run
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                output = registry[name].fn(**args) if name in registry else f"Lỗi: không có tool {name}"
            except Exception as exc:  # lỗi tool được trả lại cho model, không làm sập vòng lặp
                output = f"Lỗi khi chạy tool {name}: {exc}"
            run.messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            run.trace[-1].setdefault("tool_output_chars", 0)
            run.trace[-1]["tool_output_chars"] += len(output)
    raise RuntimeError(f"Agent vượt quá {max_steps} bước mà chưa trả lời.")


def make_llm(live: bool, offline_policy: Policy):
    return OpenAIChat() if live else ScriptedLLM(offline_policy)
