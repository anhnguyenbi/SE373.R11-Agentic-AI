# Lab 05 — Tra cứu chính sách đúng phiên bản

Bản hoàn thiện stage 00–02 từ project được cung cấp. Stage 01 và stage 02 có tool `list_files`; stage 02 có skill `refund-policy`. Project gốc được giữ nguyên.

Đã có mã nguồn, skill, tài liệu chính sách và trace của 11 lượt chạy thực tế qua 9Router trong `evidence/live/`. Stage 02 đạt cả năm tình huống của bài. Stage 01 còn kết luận có điều kiện khi thiếu trạng thái kích hoạt; chi tiết và bằng chứng được ghi trong `analysis.md`. Bộ kiểm thử đạt 128 tests. Bản `Lab_Day_05_Nop_Bai.zip` loại cấu hình riêng và môi trường chạy khỏi bài nộp.

## Nội dung

| Thành phần | Vị trí |
|---|---|
| Stage không có tool | `stage-00-chat/` |
| Tool mới và đăng ký ở stage 01 | `stage-01-files/tools/files.py`, `tools/__init__.py`, `agent.py` |
| Tool mới và đăng ký ở stage 02 | `stage-02-skills/tools/files.py`, `tools/__init__.py`, `agent.py` |
| Skill và reference | `stage-02-skills/workspace/skills/refund-policy/` |
| Hai chính sách | `stage-01-files/workspace/data/policies/`, `stage-02-skills/workspace/data/policies/` |
| Bản khôi phục skill và dữ liệu | `fixtures/` của từng stage |
| Kết quả và giải thích cuối bài | `analysis.md` |
| Bằng chứng kiểm thử | `evidence/` |
| Chương trình kiểm tra và thu trace | `scripts/` |

## Cài đặt và chạy

Yêu cầu Python 3.11+ và uv. Chạy trong thư mục này:

```bash
uv sync --all-packages --locked
```

Cấu hình model có hỗ trợ tool calling bằng biến môi trường hoặc tạo `.env` riêng theo `.env.example` của stage cần chạy. Chỉ giữ cấu hình trên máy, loại `.env` khỏi bài nộp.

```bash
cd stage-02-skills
uv run streamlit run app.py
```

Trong panel **Tools được cấp** có `read_file`, `write_file`, `list_files`. Catalog có `weekly-report` và `refund-policy`. Dòng mô tả tĩnh của project mẫu vẫn giữ nguyên theo phạm vi thay đổi của đề; panel lấy schema từ danh sách đăng ký thực tế.

Sau khi đổi nội dung skill, tải lại trang và mở cuộc trò chuyện mới. Nhập câu hỏi A hoặc B như trong đề, không thêm tên skill, tên file hay thứ tự gọi tool.

## Kiểm thử không cần API key

Chạy mỗi stage riêng để tránh các module trùng tên giữa các stage:

```bash
cd stage-00-chat
uv run pytest -q
cd ../stage-01-files
uv run pytest -q
cd ../stage-02-skills
uv run pytest -q
```

Kết quả đã chạy: **17 + 51 + 60 = 128 tests đạt**.

Từ thư mục gốc, tái tạo bằng chứng tool và context:

```bash
uv run --package stage-02-skills python scripts/check_tools.py --stage stage-01-files
uv run --package stage-02-skills python scripts/check_tools.py --stage stage-02-skills
uv run --package stage-02-skills python scripts/check_context.py
```

`check_context.py` dùng `ScriptedChatModel` có sẵn trong project mẫu; chỉ kiểm tra truyền schema, skill, reference và tài liệu đã đổi tên vào context. Chương trình không đánh giá khả năng suy luận của model thực tế.

## Thu trace thực tế

Khi đã có cấu hình model, từ thư mục gốc chạy:

```bash
uv run --package stage-02-skills python scripts/run_scenarios.py --env-file /duong/dan/cau-hinh.env
```

Hoặc dùng biến môi trường đã được thiết lập:

```bash
uv run --package stage-02-skills python scripts/run_scenarios.py
```

Chương trình chạy A ở stage 00; chạy A, B, A sau đổi tên, B sau đổi tên và trường hợp thiếu trạng thái kích hoạt ở stage 01–02. Mỗi trường hợp có conversation mới và workspace tạm riêng. Các lần đổi tên giữ nguyên nội dung, không sửa dữ liệu bàn giao.

Trace, manifest và context được lưu vào `evidence/live/`. Manifest chứa tên trace, tên model, ánh xạ đổi tên, câu trả lời và inventory; không lưu credential. Có thể chọn một trường hợp:

```bash
uv run --package stage-02-skills python scripts/run_scenarios.py --env-file /duong/dan/cau-hinh.env --stage stage-02-skills --case B-renamed
```

Kết quả thực tế đã được đối chiếu; tên trace và số dòng bằng chứng có trong `analysis.md`. Nếu chạy lại bằng model khác, đối chiếu kết quả mới trước khi cập nhật báo cáo. Chương trình không thay câu trả lời bằng đáp án định sẵn.
