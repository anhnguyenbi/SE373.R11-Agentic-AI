"""Agent hỗ trợ lập trình có persistent memory về quy ước dự án."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.llm import Tool, run_agent, tool_call  # noqa: E402
from memory_store import MemoryStore  # noqa: E402

BASE_PROMPT = """Bạn là trợ lý lập trình cho dự án web "TaskBoard" (backend Python).
Mỗi session là một cuộc trò chuyện mới, bạn KHÔNG nhớ gì từ session trước ngoài phần Project memory bên dưới.

Quy tắc dùng memory:
1. Khi người dùng nêu một quy ước dự án mới, hoặc đổi một quy ước cũ, hãy gọi `remember_convention`
   cho TỪNG quy ước. Dùng topic ngắn dạng `nhom.ten` (ví dụ `api.json_case`, `testing.framework`,
   `git.commit_style`). Nếu là thay đổi quy ước đã có trong memory, dùng lại đúng topic đó và truyền `replaces_id`.
2. Khi viết code, JSON hay commit message, phải tuân theo các quy ước trong Project memory.
   Nếu memory chưa có quy ước liên quan, nói rõ là chưa có quy ước thay vì tự đoán.
3. Project memory chỉ chứa quy ước đang hiệu lực. Khi được hỏi quy ước TRƯỚC ĐÂY, gọi `memory_history`.
"""


def build_tools(store: MemoryStore, session_id: str) -> list[Tool]:
    def remember_convention(topic: str, content: str, replaces_id: str | None = None) -> str:
        entry = store.remember(topic, content, session_id, replaces_id)
        msg = f"Đã lưu {entry.id} `{entry.topic}`"
        return msg + (f", thay thế {entry.supersedes}." if entry.supersedes else ".")

    def memory_history(topic: str = "") -> str:
        rows = [{"id": e.id, "topic": e.topic, "content": e.content, "status": e.status,
                 "session": e.session, "superseded_by": e.superseded_by} for e in store.history(topic)]
        return json.dumps(rows, ensure_ascii=False) if rows else "Không có bản ghi nào."

    return [
        Tool("remember_convention",
             "Lưu hoặc cập nhật một quy ước dự án vào persistent memory.",
             {"type": "object",
              "properties": {"topic": {"type": "string", "description": "Khoá ngắn, ví dụ api.json_case"},
                             "content": {"type": "string", "description": "Nội dung quy ước, một câu"},
                             "replaces_id": {"type": "string", "description": "id quy ước cũ bị thay (nếu có)"}},
              "required": ["topic", "content"]},
             remember_convention),
        Tool("memory_history",
             "Xem lịch sử quy ước, gồm cả bản đã bị thay thế.",
             {"type": "object", "properties": {"topic": {"type": "string"}}},
             memory_history),
    ]


def build_system_prompt(store: MemoryStore | None) -> str:
    memory = store.render_for_context() if store else "## Project memory\n(memory bị tắt trong session này)"
    return BASE_PROMPT + "\n" + memory


def run_session(llm, store: MemoryStore | None, session_id: str, user_text: str) -> dict:
    """Mở một session mới: chỉ có system prompt (kèm memory) + 1 câu của người dùng."""
    system = build_system_prompt(store)
    tools = build_tools(store, session_id) if store else []
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user_text}]
    run = run_agent(llm, messages, tools)
    return {"session": session_id, "injected_memory": system[len(BASE_PROMPT):].strip(),
            "user": user_text, "answer": run.answer, "trace": run.trace,
            "usage": run.usage.to_dict(), "messages": run.messages}


# --------------------------------------------------------------------------- offline policy
# Model giả lập: chỉ đọc system prompt + hội thoại hiện tại (đúng những gì model thật thấy).
# Nó nhận ra vài quy ước bằng từ khoá. Mục đích là kiểm chứng phần harness memory, không thay model thật.

_PATTERNS = [
    ("api.json_case", r"camelCase", "Tên field JSON dùng camelCase (ví dụ fullName)."),
    ("api.json_case", r"snake_case", "Tên field JSON dùng snake_case (ví dụ full_name)."),
    ("testing.framework", r"pytest", "Test viết bằng pytest, đặt trong thư mục tests/."),
    ("git.commit_style", r"conventional commits", "Commit message theo Conventional Commits (feat:, fix:, ...)."),
]


def _memory_lines(system: str) -> dict[str, tuple[str, str]]:
    found = {}
    for m in re.finditer(r"- \[(m\d+)\] `([^`]+)`: (.+)", system):
        found[m.group(2)] = (m.group(1), m.group(3))
    return found


def offline_policy(messages: list[dict], tools) -> dict:
    system, user = messages[0]["content"], messages[1]["content"]
    memory = _memory_lines(system)
    tool_msgs = [m for m in messages if m["role"] == "tool"]
    low = user.lower()

    # 1) Người dùng nêu/đổi quy ước -> gọi tool ghi memory cho từng quy ước còn chưa ghi.
    if tools and ("quy ước" in low or "từ nay" in low) and "trước đây" not in low:
        wanted = []
        for topic, pattern, content in _PATTERNS:
            if re.search(pattern, user, re.I) and topic not in [w[0] for w in wanted]:
                wanted.append((topic, content))
        if len(tool_msgs) < len(wanted):
            topic, content = wanted[len(tool_msgs)]
            args = {"topic": topic, "content": content}
            if topic in memory:
                args["replaces_id"] = memory[topic][0]
            return tool_call("remember_convention", args, f"call_{len(tool_msgs) + 1}")
        return {"content": "Đã ghi nhớ: " + "; ".join(c for _, c in wanted)}

    # 2) Hỏi về quy ước trước đây -> tra lịch sử.
    if "trước đây" in low:
        if not tool_msgs and tools:
            return tool_call("memory_history", {"topic": "api.json_case"}, "call_h")
        rows = json.loads(tool_msgs[-1]["content"]) if tool_msgs and tool_msgs[-1]["content"].startswith("[") else []
        parts = [f"{r['id']} ({r['status']}, lưu ở {r['session']}): {r['content']}" for r in rows]
        return {"content": "Lịch sử quy ước JSON: " + " → ".join(parts) if parts else "Không có lịch sử."}

    # 3) Viết code/JSON theo quy ước trong memory.
    case = memory.get("api.json_case", ("", ""))[1].lower()
    commit = memory.get("git.commit_style", ("", ""))[1].lower()
    if "camelcase" in case:
        keys = ("id", "fullName", "createdAt")
    elif "snake_case" in case:
        keys = ("id", "full_name", "created_at")
    else:
        return {"content": "Memory chưa có quy ước đặt tên field JSON nên tôi chưa biết dùng kiểu nào. "
                           "Bạn muốn dùng snake_case hay camelCase?"}
    body = json.dumps({keys[0]: 42, keys[1]: "Nguyen Van A", keys[2]: "2026-10-27T10:00:00Z"},
                      ensure_ascii=False, indent=2)
    msg = "feat(user): trả về thông tin người dùng" if "conventional" in commit else "(chưa có quy ước commit)"
    return {"content": f"JSON response:\n```json\n{body}\n```\nCommit message: `{msg}`"}
