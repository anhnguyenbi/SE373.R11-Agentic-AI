"""SE373 · BTVN#3 · Agent đặt vé máy bay bằng LangChain/LangGraph + harness.
Sinh viên: Nguyễn Bi Anh · MSSV: 23520055

Cách chạy (không cần khoá API, mặc định dùng model giả lập tái lập được):

    python lab03.py list                                   # xem 6 kịch bản
    python lab03.py demo --design react --scenario co-ban  # chạy 1 lần, in trace V1, V2, ...
    python lab03.py demo --design pe --scenario het-cho
    python lab03.py demo --design hybrid --scenario can-duyet --approve ask   # người duyệt thật (gõ y/n)
    python lab03.py demo --design react --scenario co-ban --noise 0.3 --seed 2  # cài lỗi model
    python lab03.py demo --design react --scenario can-duyet --no-harness      # ablation: bỏ harness
    python lab03.py bench --seeds 30 --noise 0.15          # đánh giá 3 mẫu -> results/

Dùng model thật: thêm --llm openai:gpt-4o-mini (hoặc anthropic:..., google_genai:...),
khoá API đặt trong .env (xem .env.example).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from flight_agent.agents import DESIGN_LABEL, DESIGNS
from flight_agent.evaluate import aggregate, benchmark, run_one, save
from flight_agent.harness import ApprovalRequest
from flight_agent.scenarios import SCENARIOS

HERE = os.path.dirname(os.path.abspath(__file__))


def make_approver(mode: str):
    if mode == "none":
        return None  # không có người trực -> dừng và bàn giao
    if mode == "yes":
        return lambda req: True
    if mode == "no":
        return lambda req: False

    def ask(req: ApprovalRequest) -> bool:
        print("\n[CẦN PHÊ DUYỆT]", req.question())
        return input("Duyệt? [y/N]: ").strip().lower() in {"y", "yes", "co", "có"}
    return ask


def cmd_list(_args) -> None:
    for sc in SCENARIOS.values():
        print(f"- {sc.id:14} [{sc.expected:7}] {sc.title}: {sc.request}\n  {'':14} kiểm tra: {sc.tests}")


def cmd_demo(args) -> None:
    sc = SCENARIOS[args.scenario]
    print(f"=== {DESIGN_LABEL[args.design]} · kịch bản {sc.id} · harness={'BẬT' if not args.no_harness else 'TẮT'} "
          f"· llm={args.llm} · noise={args.noise} seed={args.seed} ===")
    print("USER:", sc.request)
    print("CONSTRAINTS:", sc.constraints.describe(), "\n")
    res, harness, answer = run_one(args.design, sc, seed=args.seed, noise=args.noise, use_harness=not args.no_harness,
                                   llm=args.llm, approver=make_approver(args.approve), verbose=True)
    print("\n--- CÂU TRẢ LỜI CUỐI ---\n" + answer)
    print("\n--- HARNESS ---")
    rep = harness.report()
    rep.pop("handoff", None)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    print(f"\n--- CHẤM ĐỘC LẬP --- outcome={res.outcome} · đạt={res.passed} · {res.detail}")


def _print_table(rows, harness: bool, noisy: bool) -> None:
    print(f"\n## harness={'BẬT' if harness else 'TẮT'} · {'có nhiễu' if noisy else 'sạch (noise=0)'}")
    print(f"{'kịch bản':14} {'design':7} {'đạt':>5} {'k.an toàn':>9} {'LLM':>5} {'tool':>5} {'token':>7} {'can thiệp':>9}  kết cục")
    for r in rows:
        if r["harness"] != harness or r["noisy"] != noisy:
            continue
        print(f"{r['scenario']:14} {r['design']:7} {r['pass_rate'] * 100:4.0f}% {r['unsafe_rate'] * 100:8.0f}% {r['llm_calls']:5} "
              f"{r['tool_calls']:5} {r['tokens']:7.0f} {r['interventions']:9}  {r['outcomes']}")


def cmd_bench(args) -> None:
    from flight_agent import charts

    t0 = time.time()
    out_dir = os.path.join(HERE, args.out)
    res = benchmark(seeds=args.seeds, noise=args.noise)
    rows = aggregate(res)
    save(res, rows, out_dir)
    for h in (True, False):
        for n in (False, True):
            _print_table(rows, h, n)

    # --- độ nhạy theo mức nhiễu (có harness), gộp mọi kịch bản
    levels = [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]
    curve: dict[str, list[tuple[float, float]]] = {d: [] for d in DESIGNS}
    sweep = []
    for p in levels:
        for d in DESIGNS:
            rs = [run_one(d, sc, seed=s, noise=p)[0] for sc in SCENARIOS.values() for s in range(1, args.seeds + 1)]
            rate = sum(r.passed for r in rs) / len(rs)
            curve[d].append((p, rate))
            sweep.append({"noise": p, "design": d, "pass_rate": round(rate, 3),
                          "tokens": round(sum(r.tokens for r in rs) / len(rs), 1),
                          "llm_calls": round(sum(r.llm_calls for r in rs) / len(rs), 2)})
    with open(os.path.join(out_dir, "noise_sweep.json"), "w", encoding="utf-8") as f:
        json.dump(sweep, f, ensure_ascii=False, indent=1)
    print("\n## Độ nhạy theo nhiễu (harness BẬT, gộp 6 kịch bản)")
    for p in levels:
        print(f"noise={p:.2f}  " + "  ".join(f"{d}={next(x['pass_rate'] for x in sweep if x['noise'] == p and x['design'] == d) * 100:5.1f}%" for d in DESIGNS))

    # --- chi phí lịch sử: token đầu vào từng lần gọi model, kịch bản khứ hồi sạch
    growth = {}
    tdir = charts.ensure_dir(os.path.join(out_dir, "traces"))
    for sid, sc in SCENARIOS.items():
        for d in DESIGNS:
            _r, h, ans = run_one(d, sc)
            with open(os.path.join(tdir, f"{sid}__{d}.txt"), "w", encoding="utf-8") as f:
                f.write(f"# {DESIGN_LABEL[d]} · {sid} · noise=0\nUSER: {sc.request}\n\n{h.trace_text()}\n\nANSWER:\n{ans}\n")
            if sid == "khu-hoi":
                growth[d] = [e["tokens_in"] for e in h.events if e["kind"] == "llm"]
    with open(os.path.join(out_dir, "token_growth_khu_hoi.json"), "w", encoding="utf-8") as f:
        json.dump(growth, f, indent=1)

    cdir = charts.ensure_dir(os.path.join(out_dir, "charts"))
    charts.grouped_bar(rows, "pass_rate", f"Tỉ lệ đạt theo kịch bản (harness bật, nhiễu {args.noise:.0%})", "% lần chạy đạt",
                       os.path.join(cdir, "pass_rate_harness.png"), pct=True)
    charts.grouped_bar(rows, "pass_rate", f"Tỉ lệ đạt khi TẮT harness (nhiễu {args.noise:.0%})", "% lần chạy đạt",
                       os.path.join(cdir, "pass_rate_no_harness.png"), harness=False, pct=True)
    charts.grouped_bar(rows, "tokens", "Token trung bình mỗi lần chạy (harness bật, có nhiễu)", "token",
                       os.path.join(cdir, "tokens.png"))
    same = growth.get("pe") == growth.get("hybrid")
    plot = {k: v for k, v in growth.items() if not (same and k == "hybrid")}
    charts.token_growth(plot, os.path.join(cdir, "token_growth.png"), "Chi phí của lịch sử - kịch bản khứ hồi (noise=0)",
                        labels={"pe": "Plan-then-Execute = Lai (không phải replan)"} if same else None)
    charts.noise_curve(curve, os.path.join(cdir, "noise_curve.png"), "Độ bền khi model mắc lỗi nhiều hơn (harness bật)")
    print(f"\nĐã lưu {len(res)} lần chạy vào {out_dir} ({time.time() - t0:.1f}s)")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="SE373 Lab03 - Flight booking agent: ReAct vs Plan-then-Execute vs Hybrid")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    d = sub.add_parser("demo")
    d.add_argument("--design", choices=list(DESIGNS), default="react")
    d.add_argument("--scenario", choices=list(SCENARIOS), default="co-ban")
    d.add_argument("--llm", default="mock")
    d.add_argument("--noise", type=float, default=0.0)
    d.add_argument("--seed", type=int, default=0)
    d.add_argument("--approve", choices=["none", "ask", "yes", "no"], default="none",
                   help="none: không có người trực -> dừng & bàn giao; ask: hỏi qua bàn phím")
    d.add_argument("--no-harness", action="store_true", help="ablation: chạy agent không có harness")
    d.set_defaults(fn=cmd_demo)
    b = sub.add_parser("bench")
    b.add_argument("--seeds", type=int, default=30)
    b.add_argument("--noise", type=float, default=0.15)
    b.add_argument("--out", default="results")
    b.set_defaults(fn=cmd_bench)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
