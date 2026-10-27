"""Demo Challenge A: chạy 5 session độc lập + 1 session đối chứng không có memory.

    python demo.py                      # offline, model giả lập
    python demo.py --live               # model thật qua endpoint OpenAI-compatible (.env)
    python demo.py --out evidence/run1  # chọn thư mục lưu minh chứng

Mỗi session bắt đầu với danh sách messages MỚI, chỉ có system prompt (kèm memory được inject)
và một câu người dùng. Không có hội thoại nào được mang từ session trước sang.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from agent import offline_policy, run_session  # noqa: E402
from common.llm import make_llm  # noqa: E402
import memory_store  # noqa: E402
from memory_store import SUPERSEDED, MemoryStore  # noqa: E402

TASK = ("Viết JSON response mẫu cho API GET /users/{id}, gồm id, họ tên đầy đủ và thời điểm tạo. "
        "Kèm commit message cho thay đổi này.")

SESSIONS = [
    ("s1", "Quy ước dự án: tên field JSON dùng snake_case; test viết bằng pytest; "
           "commit message theo Conventional Commits.", True),
    ("s2", TASK, True),
    ("s3", "Từ nay team đổi quy ước: tên field JSON dùng camelCase để khớp frontend. "
           "Các quy ước khác giữ nguyên.", True),
    ("s4", TASK, True),
    ("s5", "Trước đây quy ước đặt tên field JSON là gì, và bây giờ là gì?", True),
    ("control", TASK, False),
]

SNAKE = re.compile(r"\b(full_name|created_at)\b")
CAMEL = re.compile(r"\b(fullName|createdAt)\b")
CONVENTIONAL = re.compile(r"\b(feat|fix|chore|refactor|docs|test)(\([^)]*\))?!?:", re.I)


def json_part(text: str) -> str:
    """Chỉ chấm phần JSON trong câu trả lời; lời giải thích có thể nhắc cả hai kiểu đặt tên."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    return "\n".join(blocks) if blocks else text


def check(results: dict, store: MemoryStore) -> dict:
    s5 = results["s5"]["answer"]
    s2_full, s4_full = results["s2"]["answer"], results["s4"]["answer"]
    s2, s4 = json_part(s2_full), json_part(s4_full)
    history = store.history("api.json_case")
    checks = {
        "proof1_new_session_uses_snake_case": bool(SNAKE.search(s2)) and not CAMEL.search(s2),
        "proof1_new_session_uses_conventional_commit": bool(CONVENTIONAL.search(s2_full)),
        "proof2_after_change_uses_camel_case": bool(CAMEL.search(s4)) and not SNAKE.search(s4),
        "proof2_other_conventions_kept": bool(CONVENTIONAL.search(s4_full)),
        "old_convention_superseded_not_deleted": any(e.status == SUPERSEDED for e in history),
        "only_new_convention_injected_in_s4": "camelcase" in results["s4"]["injected_memory"].lower()
                                              and "snake_case" not in results["s4"]["injected_memory"],
        "history_answer_mentions_both": "snake" in s5.lower() and "camel" in s5.lower(),
    }
    control = json_part(results["control"]["answer"])
    return {
        "checks": checks,
        "passed": all(checks.values()),
        # Đối chứng chỉ mang tính tham khảo: không có memory thì agent không biết quy ước mới nhất.
        "control_without_memory_used_camel_case": bool(CAMEL.search(control)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="gọi model thật thay vì model giả lập")
    parser.add_argument("--out", default=None, help="thư mục minh chứng")
    args = parser.parse_args()

    out = Path(args.out) if args.out else HERE / "evidence" / ("live" if args.live else "offline")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    llm = make_llm(args.live, offline_policy)
    if not args.live:
        # offline: dùng thời gian cố định để file minh chứng không đổi giữa các lần chạy
        memory_store._now = lambda: "2026-10-27T00:00:00+00:00"
    store = MemoryStore(out / "memory.json")
    results = {}
    for session_id, text, use_memory in SESSIONS:
        # nạp lại từ đĩa ở mỗi session để chứng minh memory là persistent, không nằm trong biến Python
        session_store = MemoryStore(store.path) if use_memory else None
        result = run_session(llm, session_store, session_id, text)
        results[session_id] = result
        (out / f"{session_id}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n===== {session_id} {'(KHÔNG có memory)' if not use_memory else ''}")
        print("--- memory được inject vào context:")
        print(result["injected_memory"])
        print(f"--- user: {text}")
        print(f"--- agent: {result['answer']}")

    report = {"model": llm.name, "mode": "live" if args.live else "offline", **check(results, MemoryStore(store.path))}
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n===== Kết quả kiểm chứng")
    for name, ok in report["checks"].items():
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"  (đối chứng) session không có memory dùng camelCase: {report['control_without_memory_used_camel_case']}")
    print(f"Minh chứng: {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
