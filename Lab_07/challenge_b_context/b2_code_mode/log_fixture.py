"""Sinh bộ log giả lập cho B2 và đáp án đúng (tính bằng code, không qua model)."""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from pathlib import Path

SERVICES = ["auth", "billing", "catalog", "checkout", "search", "shipping"]
ERROR_CODES = ["E1001", "E1042", "E2003", "E2210", "E3005", "E4040", "E5000", "E5031"]
WARN_CODES = ["W100", "W204", "W301"]
MESSAGES = ["db timeout", "upstream refused", "invalid token", "payment declined", "cache miss storm",
            "rate limited", "serialization failed", "disk quota"]
LINE_RE = re.compile(r"^\S+Z \S+ (INFO|WARN|ERROR) ")

QUESTION = ("Trong thư mục logs có nhiều file log dịch vụ. Hãy tìm 3 mã lỗi (trường code=...) xuất hiện nhiều nhất "
            "trên các dòng mức ERROR, tính gộp tất cả các file, kèm số lần xuất hiện. "
            "Trả lời theo dạng 'E1234: <số lần>', mỗi mã một dòng, xếp giảm dần.")


def generate_logs(directory: Path, lines_per_file: int = 250, seed: int = 2026) -> dict:
    rng = random.Random(seed)
    directory.mkdir(parents=True, exist_ok=True)
    # trọng số lệch để top 3 rõ ràng nhưng vẫn phải đếm mới biết
    weights = [rng.randint(1, 9) for _ in ERROR_CODES]
    counts: Counter = Counter()
    for i, service in enumerate(SERVICES):
        rows = []
        for n in range(lines_per_file):
            ts = f"2026-10-20T{8 + n // 3600:02d}:{(n // 60) % 60:02d}:{n % 60:02d}Z"
            level = rng.choices(["INFO", "WARN", "ERROR"], [6, 2, 2])[0]
            if level == "ERROR":
                code = rng.choices(ERROR_CODES, weights)[0]
                counts[code] += 1
            elif level == "WARN":
                code = rng.choice(WARN_CODES)
            else:
                code = "-"
            rows.append(f"{ts} {service} {level} code={code} req={rng.getrandbits(40):010x} "
                        f"latency_ms={rng.randint(1, 3000)} msg=\"{rng.choice(MESSAGES)}\"")
        (directory / f"service-{i}-{service}.log").write_text("\n".join(rows) + "\n", encoding="utf-8")

    ranked = counts.most_common()
    if len(ranked) > 3 and ranked[2][1] == ranked[3][1]:
        return generate_logs(directory, lines_per_file, seed + 1)  # tránh hoà ở vị trí thứ 3
    expected = {"top3": [[code, n] for code, n in ranked[:3]], "files": len(SERVICES),
                "lines_per_file": lines_per_file, "seed": seed}
    (directory.parent / "expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return expected


def contains_raw_log(text: str) -> bool:
    return any(LINE_RE.match(line) for line in text.splitlines())
