"""Demo B2: trả lời câu hỏi trên nhiều file log theo 2 cách và đo input tokens.

    python demo_b2.py                 # offline: model giả lập, token ước lượng
    python demo_b2.py --live          # model thật, input tokens lấy từ usage của provider
    python demo_b2.py --lines 400     # log dài hơn

raw  : agent có tool read_log, đọc từng file -> toàn bộ log đi vào context.
code : agent chỉ có tool run_python, viết script xử lý log -> chỉ kết quả cuối đi vào context.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from collections import Counter
from functools import partial
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))

from code_runtime import run_python  # noqa: E402
from common.llm import Tool, make_llm, run_agent, tool_call  # noqa: E402
from log_fixture import QUESTION, contains_raw_log, generate_logs  # noqa: E402

SYSTEM = {
    "raw": "Bạn là agent phân tích log. Dùng list_logs và read_log để đọc file, rồi trả lời chính xác.",
    "code": ("Bạn là agent phân tích log ở chế độ Code Mode. Bạn KHÔNG đọc file trực tiếp; hãy dùng run_python để chạy "
             "một script Python trong thư mục logs (cwd), xử lý toàn bộ file và CHỈ in kết quả cuối cùng, gọn. "
             "Tuyệt đối không in dòng log thô. Mỗi dòng log có dạng: "
             "'<timestamp> <service> <LEVEL> code=<CODE> req=... latency_ms=... msg=\"...\"'."),
}

COUNT_SCRIPT = """\
import collections, glob, re
counts = collections.Counter()
for path in glob.glob('*.log'):
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = re.match(r'\\S+ \\S+ ERROR code=(\\S+)', line)
            if m:
                counts[m.group(1)] += 1
for code, n in counts.most_common(3):
    print(f'{code}: {n}')
"""


def build_tools(mode: str, logs: Path) -> list[Tool]:
    def list_logs() -> str:
        return "\n".join(sorted(p.name for p in logs.glob("*.log")))

    def read_log(name: str) -> str:
        path = (logs / name).resolve()
        if path.parent != logs.resolve() or not path.exists():
            return f"Không có file {name}"
        return path.read_text(encoding="utf-8")

    tools = [Tool("list_logs", "Liệt kê các file log.", {"type": "object", "properties": {}}, list_logs)]
    if mode == "raw":
        tools.append(Tool("read_log", "Đọc toàn bộ nội dung một file log.",
                          {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
                          read_log))
    else:
        tools.append(Tool("run_python", "Chạy code Python trong thư mục logs; chỉ stdout (tối đa 2000 ký tự) được trả về.",
                          {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
                          partial(lambda code, d: run_python(code, d), d=logs)))
    return tools


def offline_policy(mode: str):
    """Model giả lập. Ở chế độ raw nó đếm trên log nằm trong context; ở chế độ code nó viết script."""
    def policy(messages: list[dict], tools) -> dict:
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if mode == "code":
            if not tool_msgs:
                return tool_call("run_python", {"code": COUNT_SCRIPT}, "call_1")
            stdout = tool_msgs[-1]["content"].split("stdout:\n", 1)[-1]
            return {"content": stdout.strip()}
        if not tool_msgs:
            return tool_call("list_logs", {}, "call_0")
        files = tool_msgs[0]["content"].splitlines()
        if len(tool_msgs) - 1 < len(files):
            i = len(tool_msgs) - 1
            return tool_call("read_log", {"name": files[i]}, f"call_{i + 1}")
        counts = Counter(m.group(1) for t in tool_msgs[1:]
                         for m in re.finditer(r"^\S+ \S+ ERROR code=(\S+)", t["content"], re.M))
        return {"content": "\n".join(f"{c}: {n}" for c, n in counts.most_common(3))}
    return policy


def grade(answer: str, expected: dict) -> bool:
    found = re.findall(r"(E\d{4})\D{0,12}?(\d+)", answer)
    got = [[code, int(n)] for code, n in found][:3]
    return got == expected["top3"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", default=None)
    parser.add_argument("--lines", type=int, default=250, help="số dòng mỗi file log")
    args = parser.parse_args()

    out = Path(args.out) if args.out else HERE / "evidence" / ("live" if args.live else "offline")
    if out.exists():
        shutil.rmtree(out)
    logs = out / "logs"
    expected = generate_logs(logs, args.lines)
    report = {"mode": "live" if args.live else "offline", "expected": expected["top3"],
              "log_bytes": sum(p.stat().st_size for p in logs.glob("*.log")), "runs": {}}

    for mode in ("raw", "code"):
        llm = make_llm(args.live, offline_policy(mode))
        report["model"] = llm.name
        messages = [{"role": "system", "content": SYSTEM[mode]}, {"role": "user", "content": QUESTION}]
        run = run_agent(llm, messages, build_tools(mode, logs), max_steps=expected["files"] + 6)
        context_text = "\n".join(m.get("content") or "" for m in run.messages)
        report["runs"][mode] = {
            "correct": grade(run.answer, expected),
            "input_tokens_total": run.usage.input_tokens,
            "input_tokens_last_call": run.trace[-1]["input_tokens"],
            "llm_calls": run.usage.calls,
            "estimated": run.usage.estimated,
            "raw_log_in_context": contains_raw_log(context_text),
            "answer": run.answer,
            "trace": run.trace,
        }
        (out / f"{mode}.messages.json").write_text(json.dumps(run.messages, ensure_ascii=False, indent=1),
                                                   encoding="utf-8")
        r = report["runs"][mode]
        print(f"\n===== {mode}: {r['llm_calls']} lần gọi model, tổng input_tokens={r['input_tokens_total']}"
              f"{' (ước lượng)' if r['estimated'] else ''}, raw log trong context: {r['raw_log_in_context']}, "
              f"đúng: {r['correct']}")
        print(run.answer)

    raw_t, code_t = report["runs"]["raw"]["input_tokens_total"], report["runs"]["code"]["input_tokens_total"]
    report["reduction_percent"] = round(100 * (1 - code_t / raw_t), 2)
    report["passed"] = (report["runs"]["code"]["correct"] and not report["runs"]["code"]["raw_log_in_context"]
                        and code_t < raw_t)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nĐáp án đúng: {expected['top3']}")
    print(f"Tổng input tokens: raw {raw_t} → code {code_t} (giảm {report['reduction_percent']}%)")
    print(f"{'PASS' if report['passed'] else 'FAIL'} — minh chứng: {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
