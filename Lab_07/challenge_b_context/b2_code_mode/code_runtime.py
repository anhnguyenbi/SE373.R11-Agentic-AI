"""Runtime chạy code cho Code Mode.

Code chạy ở đâu: trong một tiến trình Python con riêng (`python -I`), thư mục làm việc là thư mục logs,
biến môi trường bị xoá (không lộ API key), có timeout và giới hạn độ dài output.
Chỉ stdout (đã cắt ngắn) quay lại context của model, nên raw log không đi vào context.

Lưu ý (slide 31): đây KHÔNG phải sandbox bảo mật. Code vẫn đọc được file mà user hệ điều hành đọc được.
Production cần container/VM, permission check, giới hạn CPU/RAM/network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

MAX_STDOUT = 2000
MAX_STDERR = 800
TIMEOUT_S = 10


def run_python(code: str, workdir: Path) -> str:
    try:
        proc = subprocess.run([sys.executable, "-I", "-c", code], cwd=workdir, capture_output=True,
                              text=True, encoding="utf-8", timeout=TIMEOUT_S,
                              env={"PATH": os.defpath, "PYTHONIOENCODING": "utf-8"})
    except subprocess.TimeoutExpired:
        return f"[timeout sau {TIMEOUT_S}s]"
    out = proc.stdout
    if len(out) > MAX_STDOUT:
        out = out[:MAX_STDOUT] + f"\n[... cắt bớt {len(proc.stdout) - MAX_STDOUT} ký tự; hãy in kết quả gọn hơn]"
    result = f"exit_code={proc.returncode}\nstdout:\n{out}"
    if proc.stderr:
        result += f"\nstderr:\n{proc.stderr[-MAX_STDERR:]}"
    return result
