"""Selective compaction cho session dài.

Khi nào kích hoạt: trước mỗi lần gọi model, ước lượng kích thước context. Nếu vượt
`trigger_ratio * budget_tokens` thì compact.

Giữ gì / bỏ gì (selective compaction):
- GIỮ nguyên văn: system prompt, `keep_turns` lượt gần nhất (để model còn ngữ cảnh tức thời).
- GIỮ dạng checkpoint có cấu trúc: mục tiêu, lỗi đang điều tra + bằng chứng then chốt, từng cách đã thử
  kèm kết quả, ràng buộc (giữ nguyên ý), file/test liên quan, bước tiếp theo.
- BỎ: raw tool output (log, danh sách test pass, code không liên quan), các lượt không đem lại kết luận.
- Không mất hẳn: phần bị bỏ được ghi nguyên văn vào archive.jsonl để tra lại khi cần.

Checkpoint được gộp dần: lần compact sau nhận checkpoint cũ + các lượt mới bị đẩy ra.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from common.llm import Usage, estimate_tokens  # noqa: E402

CHECKPOINT_FIELDS = {
    "muc_tieu": "Mục tiêu/vấn đề người dùng giao, một câu.",
    "loi_dang_dieu_tra": "Lỗi đang điều tra và nguyên nhân gốc đã tìm ra (nếu có).",
    "bang_chung": "Danh sách ngắn các dòng log/stacktrace then chốt (tối đa 6 dòng).",
    "da_thu": "Danh sách {cach, ket_qua} cho TỪNG cách đã thử, giữ đúng kết quả.",
    "rang_buoc": "Danh sách ràng buộc phải tuân thủ, giữ nguyên ý.",
    "file_lien_quan": "File, hàm, test liên quan.",
    "buoc_tiep": "Bước tiếp theo cụ thể đã thống nhất.",
}

COMPACT_PROMPT = (
    "Bạn là bộ nén context cho một agent debug. Đọc checkpoint hiện có và đoạn hội thoại sắp bị xoá khỏi context, "
    "rồi trả về DUY NHẤT một JSON object (không markdown) với các khoá sau:\n"
    + "\n".join(f"- {k}: {v}" for k, v in CHECKPOINT_FIELDS.items())
    + "\nQuy tắc: gộp với checkpoint cũ, không làm mất cách đã thử hay ràng buộc nào; bỏ log nhiễu, chỉ giữ dòng then chốt; "
      "không bịa thông tin không có trong hội thoại."
)

Summarizer = Callable[[dict | None, list[dict]], tuple[dict, Usage]]


def render_transcript(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        if m["role"] == "tool":
            parts.append(f"[tool output]\n{m['content']}")
        elif m.get("tool_calls"):
            for c in m["tool_calls"]:
                parts.append(f"[assistant gọi tool] {c['function']['name']}({c['function']['arguments']})")
        else:
            parts.append(f"[{m['role']}] {m.get('content') or ''}")
    return "\n\n".join(parts)


def checkpoint_message(checkpoint: dict) -> dict:
    return {"role": "user",
            "content": "[CHECKPOINT — tóm tắt phần đầu session đã được compact; nguyên văn lưu trong archive]\n"
                       + json.dumps(checkpoint, ensure_ascii=False, indent=2)}


class CompactingSession:
    def __init__(self, system: str, summarizer: Summarizer, budget_tokens: int = 24000,
                 trigger_ratio: float = 0.7, keep_turns: int = 2, archive_path: Path | None = None):
        self.system = system
        self.summarizer = summarizer
        self.budget_tokens = budget_tokens
        self.trigger_ratio = trigger_ratio
        self.keep_turns = keep_turns
        self.archive_path = archive_path
        self.turns: list[list[dict]] = []
        self.checkpoint: dict | None = None
        self.events: list[dict] = []
        self.summarizer_usage = Usage()

    def add_turn(self, turn: list[dict]) -> None:
        if not turn or turn[0]["role"] != "user":
            raise ValueError("mỗi lượt phải bắt đầu bằng user message")
        self.turns.append(turn)

    def context(self) -> list[dict]:
        messages = [{"role": "system", "content": self.system}]
        if self.checkpoint:
            messages.append(checkpoint_message(self.checkpoint))
        for turn in self.turns:
            messages.extend(turn)
        return messages

    def context_tokens(self) -> int:
        return estimate_tokens(self.context())

    def should_compact(self) -> bool:
        return (self.context_tokens() > self.trigger_ratio * self.budget_tokens
                and len(self.turns) > self.keep_turns)

    def maybe_compact(self) -> bool:
        """Gọi trước mỗi lần gọi model."""
        if not self.should_compact():
            return False
        self.compact()
        return True

    def compact(self) -> None:
        before = self.context_tokens()
        evicted = [m for turn in self.turns[:-self.keep_turns] for m in turn]
        if self.archive_path:
            with self.archive_path.open("a", encoding="utf-8") as f:
                for m in evicted:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
        self.checkpoint, usage = self.summarizer(self.checkpoint, evicted)
        self.summarizer_usage.add(usage)
        self.turns = self.turns[-self.keep_turns:]
        self.events.append({"compaction": len(self.events) + 1, "evicted_messages": len(evicted),
                            "context_tokens_before": before, "context_tokens_after": self.context_tokens()})


# --------------------------------------------------------------------------- summarizers


def _parse_json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        raise ValueError("model không trả JSON checkpoint")
    data = json.loads(match.group(0))
    missing = [k for k in CHECKPOINT_FIELDS if k not in data]
    if missing:
        raise ValueError(f"checkpoint thiếu khoá {missing}")
    return data


def llm_summarizer(llm) -> Summarizer:
    def summarize(previous: dict | None, evicted: list[dict]) -> tuple[dict, Usage]:
        user = ("Checkpoint hiện có:\n" + (json.dumps(previous, ensure_ascii=False) if previous else "(chưa có)")
                + "\n\nHội thoại sắp bị xoá khỏi context:\n" + render_transcript(evicted))
        result = llm.chat([{"role": "system", "content": COMPACT_PROMPT}, {"role": "user", "content": user}])
        return _parse_json_object(result.message.get("content") or ""), result.usage
    return summarize


def extract_state(messages: list[dict], previous: dict | None = None) -> dict:
    """Bộ trích xuất theo luật (dùng cho chế độ offline).

    Đọc các câu có nhãn "Thử:", "Kết quả:", "Phát hiện:", "Bước tiếp:", "Ràng buộc:" và các dòng stacktrace.
    Chỉ dùng thông tin có trong `messages` (+ checkpoint cũ), giống như model thật chỉ thấy context.
    """
    state = {k: (list(v) if isinstance(v, list) else v) for k, v in (previous or {}).items()}
    for key in ("bang_chung", "da_thu", "rang_buoc", "file_lien_quan"):
        state.setdefault(key, [])
    for key in ("muc_tieu", "loi_dang_dieu_tra", "buoc_tiep"):
        state.setdefault(key, "")

    for m in messages:
        text = m.get("content") or ""
        if m["role"] == "tool":
            for line in text.splitlines():
                if re.search(r"Traceback|Error\b|MemoryError|FAILED|File \"", line) and line.strip() not in state["bang_chung"]:
                    if len(state["bang_chung"]) < 6:
                        state["bang_chung"].append(line.strip())
            continue
        if m["role"] == "user":
            if text.startswith("[CHECKPOINT"):
                continue
            if not state["muc_tieu"]:
                state["muc_tieu"] = text.split(". Ràng buộc")[0]
            if "Ràng buộc:" in text:
                for item in text.split("Ràng buộc:", 1)[1].split(", và "):
                    item = item.strip().rstrip(".")
                    if item and item not in state["rang_buoc"]:
                        state["rang_buoc"].append(item)
            continue
        if m["role"] == "assistant":
            for attempt, result in re.findall(r"Thử: (.+?)\. Kết quả: (.+?)(?=$|\n)", text):
                state["da_thu"].append({"cach": attempt, "ket_qua": result})
            if found := re.search(r"Phát hiện: (.+?)\. Thử:", text):
                state["loi_dang_dieu_tra"] = found.group(1)
            if found := re.search(r"Bước tiếp: (.+)", text):
                state["buoc_tiep"] = found.group(1)
            for name in re.findall(r"tests/\S+::\w+|app/\S+\.py|image_service\.py", text):
                if name not in state["file_lien_quan"]:
                    state["file_lien_quan"].append(name)
    return state


def rule_summarizer(previous: dict | None, evicted: list[dict]) -> tuple[dict, Usage]:
    return extract_state(evicted, previous), Usage(estimated=True)
