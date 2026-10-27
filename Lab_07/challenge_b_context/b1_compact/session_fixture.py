"""Tạo một session debug dài, có nhiều tool output lớn (log), để thử compaction.

Kịch bản: POST /api/photos trả 500 với ảnh lớn. Agent đã đi qua nhiều lượt điều tra.
Thông tin quan trọng nằm rải rác ở đầu, giữa và cuối session; phần lớn token là log nhiễu.
"""

from __future__ import annotations

import json
import random

SYSTEM = ("Bạn là agent debug cho dịch vụ photo-api (Python, Flask, Pillow, chạy sau nginx + gunicorn). "
          "Bạn có tool read_log, read_file, run_command. Trả lời bằng tiếng Việt.")

TOOLS = [
    {"type": "function", "function": {"name": name, "description": desc,
                                      "parameters": {"type": "object", "properties": {"arg": {"type": "string"}},
                                                     "required": ["arg"]}}}
    for name, desc in (("read_log", "Đọc một file log"), ("read_file", "Đọc file mã nguồn"),
                       ("run_command", "Chạy lệnh shell trong repo"))
]

# Các dữ kiện phải còn giữ được sau compaction (dùng để chấm câu trả lời).
FACTS = {
    "loi_dang_dieu_tra": [["memoryerror"], ["resize_to_thumbnail", "image_service"]],
    "da_thu_nginx": [["client_max_body_size"]],
    "da_thu_timeout": [["timeout"], ["gunicorn", "120"]],
    "da_thu_memory_limit": [["1gi"]],
    "test_tai_hien": [["test_upload_large_photo"]],
    "buoc_tiep": [["draft"]],
    "rang_buoc_api": [["public", "mobile", "không được đổi api", "không đổi api"]],
    "rang_buoc_deadline": [["2.4", "thứ sáu", "friday"]],
}

_PATHS = ["/api/photos", "/api/albums", "/api/users/me", "/healthz", "/api/photos/thumb", "/static/app.js"]
_SERVICES = ["photo-api", "album-api", "auth", "worker"]


def _noise_lines(rng: random.Random, n: int, kind: str) -> list[str]:
    lines = []
    for i in range(n):
        ts = f"2026-10-24T09:{(i // 60) % 60:02d}:{i % 60:02d}Z"
        if kind == "nginx":
            path = rng.choice(_PATHS)
            status = rng.choice([200, 200, 200, 204, 304, 404])
            lines.append(f'10.0.{rng.randint(0, 9)}.{rng.randint(2, 250)} - - [{ts}] "GET {path} HTTP/1.1" '
                         f'{status} {rng.randint(120, 90000)} "-" "Mozilla/5.0" rt={rng.random():.3f}')
        elif kind == "gunicorn":
            lines.append(f"[{ts}] [{rng.randint(10, 99)}] [INFO] {rng.choice(_SERVICES)} handled request "
                         f"id={rng.getrandbits(48):012x} in {rng.randint(3, 900)}ms")
        else:
            lines.append(f"{ts} INFO {rng.choice(_SERVICES)} request_id={rng.getrandbits(48):012x} "
                         f"user={rng.randint(1000, 9999)} cache_hit={rng.choice(['true', 'false'])} "
                         f"latency_ms={rng.randint(1, 400)}")
    return lines


def _call(name: str, arg: str, cid: str) -> dict:
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function",
                            "function": {"name": name, "arguments": json.dumps({"arg": arg})}}]}


def build_turns(seed: int = 7, noise: int = 220) -> list[list[dict]]:
    """Trả về danh sách lượt; mỗi lượt bắt đầu bằng một user message."""
    rng = random.Random(seed)
    turns: list[list[dict]] = []

    def turn(user: str, tool: str, arg: str, output: list[str], answer: str) -> None:
        cid = f"call_{len(turns) + 1:02d}"
        turns.append([{"role": "user", "content": user}, _call(tool, arg, cid),
                      {"role": "tool", "tool_call_id": cid, "content": "\n".join(output)},
                      {"role": "assistant", "content": answer}])

    nginx = _noise_lines(rng, noise, "nginx")
    nginx.insert(noise // 2, '10.0.3.17 - - [2026-10-24T09:01:55Z] "POST /api/photos HTTP/1.1" 500 182 '
                             '"-" "Mozilla/5.0" rt=4.112 upstream=127.0.0.1:8000')
    turn("Người dùng báo POST /api/photos trả 500 khi upload ảnh khoảng 8MB trở lên. "
         "Ràng buộc: KHÔNG được đổi API public /api/photos (mobile app đang dùng), và phải fix trước release 2.4 vào thứ Sáu.",
         "read_log", "/var/log/nginx/access.log", nginx,
         "Giả thuyết 1: nginx chặn body lớn. Thử: tăng client_max_body_size lên 20m. "
         "Kết quả: vẫn 500; access log cho thấy upstream trả 500 chứ không phải 413, nên không phải do nginx.")

    turn("Xem metrics hệ thống lúc lỗi.", "run_command", "kubectl top pods -n photos --containers",
         _noise_lines(rng, noise, "app"), "Metrics bình thường, CPU thấp. Chưa có kết luận.")

    gunicorn = _noise_lines(rng, noise, "gunicorn")
    gunicorn.insert(noise // 3, "[2026-10-24T09:01:55Z] [41] [ERROR] Error handling request /api/photos")
    turn("Xem tiếp log gunicorn.", "read_log", "/var/log/gunicorn/error.log", gunicorn,
         "Giả thuyết 2: worker bị timeout. Thử: tăng gunicorn --timeout từ 30 lên 120. "
         "Kết quả: vẫn 500 sau khoảng 4 giây, không phải timeout.")

    turn("Có request nào khác lỗi tương tự không?", "run_command", "grep -c ' 500 ' /var/log/nginx/access.log",
         _noise_lines(rng, noise, "nginx"), "Chỉ các request upload ảnh lớn bị 500.")

    app = _noise_lines(rng, noise, "app")
    app[noise // 2: noise // 2] = [
        "2026-10-24T09:01:55Z ERROR photo-api Traceback (most recent call last):",
        '  File "app/routes/photos.py", line 41, in upload_photo',
        "    thumb = image_service.resize_to_thumbnail(file.stream)",
        '  File "app/services/image_service.py", line 88, in resize_to_thumbnail',
        "    img = Image.open(stream).convert('RGB')",
        "MemoryError",
    ]
    turn("Kiểm tra log ứng dụng photo-api.", "read_log", "/var/log/photo-api/app.log", app,
         "Phát hiện: MemoryError trong image_service.resize_to_thumbnail (image_service.py:88) khi "
         "Image.open(...).convert('RGB') giải nén toàn bộ ảnh 48MP. Thử: tăng memory limit container từ 512Mi lên 1Gi. "
         "Kết quả: ảnh 8MB qua được nhưng ảnh 12MB vẫn lỗi, nên chỉ dời ngưỡng chứ không sửa gốc.")

    turn("Tìm thêm dấu vết trong log worker.", "read_log", "/var/log/photo-api/worker.log",
         _noise_lines(rng, noise, "app"), "Log worker không liên quan tới lỗi upload.")

    pytest_out = [f"tests/test_albums.py::test_case_{i} PASSED" for i in range(noise // 2)]
    pytest_out += ["tests/test_upload.py::test_upload_large_photo FAILED",
                   "E   MemoryError", "=== 1 failed, %d passed in 12.31s ===" % (noise // 2)]
    turn("Viết test tái hiện lỗi rồi chạy test suite.", "run_command", "pytest -q", pytest_out,
         "Đã viết test tái hiện tests/test_upload.py::test_upload_large_photo (dùng ảnh 12MB, 48MP); hiện đang FAIL đúng như lỗi thật.")

    source = [f"{i:>3}  # ... code không liên quan ..." for i in range(1, 80)]
    source += [" 85  def resize_to_thumbnail(stream, size=(1024, 1024)):",
               " 86      if size is None:",
               " 87          size = (1024, 1024)",
               " 88      img = Image.open(stream).convert('RGB')",
               " 89      img.thumbnail(size)",
               " 90      return img"]
    turn("Đọc code resize_to_thumbnail rồi đề xuất bước tiếp theo.", "read_file", "app/services/image_service.py", source,
         "Bước tiếp: gọi img.draft('RGB', size) ngay sau Image.open để Pillow giảm độ phân giải lúc decode JPEG, "
         "rồi mới thumbnail(); giữ nguyên request/response của /api/photos. Sau đó chạy lại test_upload_large_photo "
         "và hạ memory limit về 512Mi để chắc fix thật.")
    return turns


CONTINUE_QUESTION = ("Tiếp tục công việc. Ở bước này KHÔNG gọi tool; chỉ dựa vào thông tin đang có trong hội thoại. "
                     "Trước khi làm, hãy nêu ngắn gọn: (1) lỗi đang điều tra và nguyên nhân đã tìm ra, "
                     "(2) những cách đã thử và kết quả từng cách, (3) ràng buộc phải tuân thủ, (4) bước tiếp theo cụ thể.")
