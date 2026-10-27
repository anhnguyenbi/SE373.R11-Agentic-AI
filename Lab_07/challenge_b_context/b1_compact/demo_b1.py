"""Demo B1: compact một session debug dài rồi cho agent tiếp tục.

    python demo_b1.py            # offline: model giả lập, token ước lượng
    python demo_b1.py --live     # model thật, input tokens lấy từ usage của provider

So sánh 3 cách đưa context vào lần gọi tiếp theo:
  full      : giữ toàn bộ session (không compact)
  compacted : checkpoint + 2 lượt gần nhất (selective compaction)
  truncated : chỉ cắt bỏ phần đầu, giữ 2 lượt gần nhất, KHÔNG có checkpoint (đối chứng)
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from common.llm import make_llm  # noqa: E402
from compaction import CompactingSession, extract_state, llm_summarizer, rule_summarizer  # noqa: E402
from session_fixture import CONTINUE_QUESTION, FACTS, SYSTEM, TOOLS, build_turns  # noqa: E402


def offline_answer(messages: list[dict], tools) -> dict:
    """Model giả lập: chỉ trả lời từ những gì có trong context (checkpoint + các lượt còn lại)."""
    checkpoint = None
    for m in messages:
        if m["role"] == "user" and (m.get("content") or "").startswith("[CHECKPOINT"):
            checkpoint = json.loads(m["content"].split("\n", 1)[1])
    s = extract_state(messages[1:-1], checkpoint)
    unknown = "(không còn thông tin trong context)"
    tried = "; ".join(f"{a['cach']} → {a['ket_qua']}" for a in s["da_thu"]) or unknown
    return {"content": (f"(1) Lỗi: {s['loi_dang_dieu_tra'] or unknown}\n"
                        f"(2) Đã thử: {tried}\n"
                        f"(3) Ràng buộc: {'; '.join(s['rang_buoc']) or unknown}\n"
                        f"(4) Bước tiếp: {s['buoc_tiep'] or unknown} "
                        f"(file/test: {', '.join(s['file_lien_quan']) or unknown})")}


def score(answer: str) -> dict:
    low = answer.lower()
    return {fact: all(any(word in low for word in group) for group in groups) for fact, groups in FACTS.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--budget", type=int, default=24000, help="ngân sách context (token ước lượng)")
    args = parser.parse_args()

    out = Path(args.out) if args.out else HERE / "evidence" / ("live" if args.live else "offline")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    llm = make_llm(args.live, offline_answer)
    summarizer = llm_summarizer(llm) if args.live else rule_summarizer
    turns = build_turns()

    # 1) Mô phỏng agent chạy từng lượt; trước mỗi lần gọi model thì kiểm tra có cần compact không.
    session = CompactingSession(SYSTEM, summarizer, budget_tokens=args.budget, archive_path=out / "archive.jsonl")
    for turn in turns:
        session.add_turn(turn)
        if session.maybe_compact():
            print(f"compact #{len(session.events)}: {session.events[-1]}")
    session.maybe_compact()  # trước lần gọi tiếp theo

    full = [{"role": "system", "content": SYSTEM}] + [m for t in turns for m in t]
    contexts = {
        "full": full,
        "compacted": session.context(),
        "truncated": [{"role": "system", "content": SYSTEM}] + [m for t in turns[-2:] for m in t],
    }

    report = {"model": llm.name, "mode": "live" if args.live else "offline",
              "budget_tokens": args.budget, "trigger_ratio": session.trigger_ratio,
              "compactions": session.events, "summarizer_usage": session.summarizer_usage.to_dict(), "runs": {}}
    (out / "checkpoint.json").write_text(json.dumps(session.checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")

    for name, context in contexts.items():
        messages = context + [{"role": "user", "content": CONTINUE_QUESTION}]
        result = llm.chat(messages, TOOLS, tool_choice="none")
        answer = result.message.get("content") or ""
        facts = score(answer)
        report["runs"][name] = {"input_tokens": result.usage.input_tokens, "estimated": result.usage.estimated,
                                "messages": len(messages), "facts": facts, "facts_kept": sum(facts.values()),
                                "facts_total": len(facts), "answer": answer}
        (out / f"{name}.input.json").write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n===== {name}: input_tokens={result.usage.input_tokens}"
              f"{' (ước lượng)' if result.usage.estimated else ''}, giữ {sum(facts.values())}/{len(facts)} dữ kiện")
        print(answer)

    full_t, comp_t = report["runs"]["full"]["input_tokens"], report["runs"]["compacted"]["input_tokens"]
    report["reduction_percent"] = round(100 * (1 - comp_t / full_t), 2)
    report["passed"] = (all(report["runs"]["compacted"]["facts"].values()) and comp_t < full_t
                        and len(session.events) > 0)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nInput tokens: full {full_t} → compacted {comp_t} (giảm {report['reduction_percent']}%). "
          f"Truncated giữ {report['runs']['truncated']['facts_kept']}/{len(FACTS)} dữ kiện.")
    print(f"{'PASS' if report['passed'] else 'FAIL'} — minh chứng: {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
