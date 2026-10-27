"""Chạy toàn bộ kiểm thử và 3 demo của Lab 07.

    python run_all.py           # offline, không cần API key
    python run_all.py --live    # chạy thêm 3 demo với model thật (cần .env)
"""

import subprocess
import sys
from pathlib import Path

LAB = Path(__file__).resolve().parent
STEPS = [
    ("Unit test: LLM client", [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], LAB),
    ("Unit test: Challenge A", [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
     LAB / "challenge_a_memory"),
    ("Unit test: Challenge B", [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"],
     LAB / "challenge_b_context"),
]


def main() -> int:
    live = "--live" in sys.argv
    demos = [("Demo A", "challenge_a_memory/demo.py"), ("Demo B1", "challenge_b_context/b1_compact/demo_b1.py"),
             ("Demo B2", "challenge_b_context/b2_code_mode/demo_b2.py")]
    steps = list(STEPS)
    for name, script in demos:
        steps.append((name + " (offline)", [sys.executable, "-B", str(LAB / script)], LAB))
        if live:
            steps.append((name + " (live)", [sys.executable, "-B", str(LAB / script), "--live"], LAB))
    if live:
        print("\n########## Kiểm tra kết nối model thật", flush=True)
        if subprocess.run([sys.executable, "-B", str(LAB / "check_live.py")], cwd=LAB).returncode != 0:
            print("Bỏ qua các demo --live vì chưa kết nối được model (xem lỗi ở trên).")
            steps = [s for s in steps if not s[0].endswith("(live)")]
            live_failed = True
        else:
            live_failed = False
    failed = ["Kết nối model thật"] if live and live_failed else []
    for name, cmd, cwd in steps:
        print(f"\n########## {name}", flush=True)
        if subprocess.run(cmd, cwd=cwd).returncode != 0:
            failed.append(name)
    print("\n" + ("TẤT CẢ ĐỀU PASS" if not failed else f"FAIL: {', '.join(failed)}"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
