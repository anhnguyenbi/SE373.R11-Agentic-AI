"""Kiểm tra endpoint model thật trước khi chạy demo --live.

    python check_live.py

4 bước:
  1. Chat thường.
  2. Tool calling.
  3. Usage có phản ánh kích thước context không: gửi prompt ngắn và prompt dài ~6.000 token,
     input tokens báo về phải tăng tương ứng. Nếu không, số đo token của Lab 07 không dùng được.
  4. Tool output có tới model nguyên vẹn không: tool trả 200 dòng, đáp án nằm ở dòng cuối.
     Một số router (ví dụ 9Router bật "Compress tool output") cắt bớt output, làm sai B1/B2.
"""

import json
import sys

from common.llm import OpenAIChat, ProviderError, Tool, load_settings, run_agent


def main() -> int:
    settings = load_settings()
    print(f"Endpoint: {settings.base_url}\nModel:    {settings.model}")
    llm = OpenAIChat(settings, timeout=120)
    problems = []
    try:
        short = llm.chat([{"role": "user", "content": "Trả lời đúng một từ: OK"}])
        print(f"[1/4] Chat thường: {short.message.get('content')!r}, input_tokens={short.usage.input_tokens}")
        print(f"      usage gốc: {json.dumps(short.usage.raw[0], ensure_ascii=False)}")

        add = Tool("add", "Cộng hai số nguyên", {"type": "object", "properties": {
            "a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]},
            lambda a, b: str(a + b))
        run = run_agent(llm, [{"role": "user", "content": "Dùng tool add để tính 19 + 23 rồi trả lời kết quả."}], [add])
        used_tool = any(step["tool_calls"] for step in run.trace)
        print(f"[2/4] Tool calling: gọi tool={used_tool}, trả lời={run.answer!r}")
        if not used_tool:
            problems.append("Model không gọi tool: chọn model khác có hỗ trợ tool calling.")

        filler = " ".join(f"mục-{i:04d} dữ liệu đệm" for i in range(3000))  # ~6.000+ token
        long = llm.chat([{"role": "user", "content": filler + "\n\nBỏ qua phần trên, trả lời đúng một từ: OK"}])
        grew = long.usage.input_tokens - short.usage.input_tokens
        print(f"[3/4] Usage theo kích thước: ngắn={short.usage.input_tokens}, dài={long.usage.input_tokens} "
              f"(tăng {grew})")
        print(f"      usage gốc (dài): {json.dumps(long.usage.raw[0], ensure_ascii=False)}")
        if grew < 3000:
            problems.append("Input tokens router báo về gần như không đổi khi prompt dài thêm ~6.000 token. "
                            "Router không báo đúng usage (có thể do nén prompt hoặc tách phần cache), "
                            "nên số đo token của B1/B2 sẽ sai. Tắt các tính năng nén/Token Saver hoặc dùng provider khác.")

        lines = [f"dòng {i:03d}: status=ok latency_ms={i * 7 % 997}" for i in range(1, 200)]
        lines.append("dòng 200: MA_BI_MAT=ZX-4471")
        dump = Tool("read_log", "Đọc toàn bộ file log", {"type": "object", "properties": {}}, lambda: "\n".join(lines))
        run = run_agent(llm, [{"role": "user", "content": "Gọi read_log rồi cho biết giá trị MA_BI_MAT ở dòng cuối. "
                                                          "Chỉ trả lời giá trị đó."}], [dump])
        full_output = "ZX-4471" in run.answer
        print(f"[4/4] Tool output nguyên vẹn: {full_output}, trả lời={run.answer[:120]!r}")
        if not full_output:
            problems.append("Model không thấy dòng cuối của tool output: router đang cắt/nén tool output "
                            "(9Router: Token Saver → tắt 'Compress tool output (RTK)').")
    except ProviderError as exc:
        print(f"LỖI: {exc}")
        return 1

    if problems:
        print("\nCHƯA ỔN:")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\nKết nối ổn, có thể chạy: python run_all.py --live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
