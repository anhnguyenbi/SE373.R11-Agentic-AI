"""Kiểm tra nhanh kết nối tới model thật trước khi chạy demo --live.

    python check_live.py

Gửi 2 request nhỏ: một câu chat thường và một câu có tool calling. In ra model, usage và lỗi (nếu có).
"""

import json
import sys

from common.llm import OpenAIChat, ProviderError, Tool, run_agent, load_settings


def main() -> int:
    settings = load_settings()
    print(f"Endpoint: {settings.base_url}\nModel:    {settings.model}")
    llm = OpenAIChat(settings, timeout=60)
    try:
        result = llm.chat([{"role": "user", "content": "Trả lời đúng một từ: OK"}])
        print(f"[1/2] Chat thường: {result.message.get('content')!r}, usage={result.usage.to_dict()}")
        add = Tool("add", "Cộng hai số nguyên", {"type": "object", "properties": {
            "a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]},
            lambda a, b: str(a + b))
        run = run_agent(llm, [{"role": "user", "content": "Dùng tool add để tính 19 + 23 rồi trả lời kết quả."}], [add])
        used_tool = any(step["tool_calls"] for step in run.trace)
        print(f"[2/2] Tool calling: gọi tool={used_tool}, trả lời={run.answer!r}, usage={json.dumps(run.usage.to_dict())}")
        if not used_tool:
            print("Model không gọi tool: hãy chọn model khác hỗ trợ tool calling.")
            return 1
    except ProviderError as exc:
        print(f"LỖI: {exc}")
        return 1
    print("Kết nối ổn, có thể chạy: python run_all.py --live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
