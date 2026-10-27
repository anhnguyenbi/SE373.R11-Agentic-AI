"""Persistent memory cho quy ước dự án, lưu trong một file JSON.

Thiết kế (trả lời các câu hỏi gợi ý ở slide 15):
- Lưu ở đâu: một file JSON. Số quy ước của một dự án nhỏ, đọc/diff bằng mắt được.
- Ai ghi: agent tự gọi tool `remember_convention` khi người dùng nêu hoặc đổi quy ước.
- Khi nào nạp: các quy ước ĐANG HIỆU LỰC được inject vào system prompt lúc mở session.
  Lịch sử (bản cũ) không inject, chỉ tra khi cần qua tool `memory_history`.
- Khi quy ước đổi: bản mới thay thế (supersede) bản cũ theo cùng `topic`. Bản cũ không bị xoá,
  chỉ đổi trạng thái thành `superseded` và trỏ tới bản thay thế, nên không còn bị inject
  nhưng vẫn truy vết được.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ACTIVE, SUPERSEDED, RETIRED = "active", "superseded", "retired"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class MemoryEntry:
    id: str
    topic: str
    content: str
    status: str
    session: str
    created_at: str
    supersedes: str | None = None
    superseded_by: str | None = None
    closed_at: str | None = None


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.entries: list[MemoryEntry] = []
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = [MemoryEntry(**e) for e in data.get("entries", [])]

    # ----------------------------------------------------------------- ghi

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "entries": [asdict(e) for e in self.entries]},
                             ensure_ascii=False, indent=2)
        # ghi ra file tạm rồi rename để không làm hỏng memory nếu tiến trình bị ngắt giữa chừng
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, self.path)

    def remember(self, topic: str, content: str, session: str, replaces_id: str | None = None) -> MemoryEntry:
        topic, content = topic.strip().lower(), content.strip()
        if not topic or not content:
            raise ValueError("topic và content không được rỗng")
        old = self.get(replaces_id) if replaces_id else self.current(topic)
        if replaces_id and (old is None or old.status != ACTIVE):
            raise ValueError(f"không có quy ước đang hiệu lực với id {replaces_id}")
        if old and old.content == content and old.topic == topic:
            return old  # không đổi gì, tránh tạo bản trùng
        entry = MemoryEntry(id=f"m{len(self.entries) + 1}", topic=topic, content=content,
                            status=ACTIVE, session=session, created_at=_now(),
                            supersedes=old.id if old else None)
        if old:
            old.status, old.superseded_by, old.closed_at = SUPERSEDED, entry.id, entry.created_at
        self.entries.append(entry)
        self.save()
        return entry

    def retire(self, topic: str) -> MemoryEntry | None:
        entry = self.current(topic)
        if entry:
            entry.status, entry.closed_at = RETIRED, _now()
            self.save()
        return entry

    # ----------------------------------------------------------------- đọc

    def get(self, entry_id: str | None) -> MemoryEntry | None:
        return next((e for e in self.entries if e.id == entry_id), None)

    def current(self, topic: str) -> MemoryEntry | None:
        topic = topic.strip().lower()
        return next((e for e in self.entries if e.topic == topic and e.status == ACTIVE), None)

    def active(self) -> list[MemoryEntry]:
        return [e for e in self.entries if e.status == ACTIVE]

    def history(self, topic: str | None = None) -> list[MemoryEntry]:
        if not topic:
            return list(self.entries)
        topic = topic.strip().lower()
        return [e for e in self.entries if e.topic == topic or topic in e.topic]

    def render_for_context(self) -> str:
        """Khối Markdown được inject vào system prompt khi mở session."""
        lines = ["## Project memory (quy ước đang hiệu lực)"]
        active = self.active()
        if not active:
            lines.append("(chưa có quy ước nào được lưu)")
        for e in active:
            note = f", thay cho {e.supersedes}" if e.supersedes else ""
            lines.append(f"- [{e.id}] `{e.topic}`: {e.content} (lưu ở {e.session}{note})")
        return "\n".join(lines)
