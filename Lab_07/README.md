# Lab 07 · Memory và Context Management

SE373 · Kỹ thuật xây dựng hệ thống Agentic AI · Buổi 07 *Memory, Context Engineering & Prompt Caching*

**Sinh viên:** Nguyễn Bi Anh · **MSSV:** 23520055

> **Challenge A:** agent nhớ quy ước dự án qua nhiều session và cập nhật khi quy ước đổi.
> **Challenge B1:** session dài được compact, agent vẫn nhớ lỗi đang điều tra, cách đã thử, bước tiếp.
> **Challenge B2:** agent trả lời câu hỏi trên nhiều file log bằng Code Mode, raw log không đi vào context; đo input tokens qua usage.

Chỉ dùng thư viện chuẩn của Python (3.10+), không cần cài package.

## Chạy nhanh

```bash
cd Lab_07
python run_all.py                # 24 unit test + 3 demo, offline, không cần API key
```

Chạy với model thật (endpoint OpenAI-compatible có tool calling):

```bash
cp .env.example .env             # điền OPENAI_API_KEY, MODEL_NAME, (tùy chọn) OPENAI_BASE_URL
python check_live.py             # 5 bước: chat, tool calling, usage theo kích thước, tool output có bị cắt không, usage nhiều lượt
python challenge_a_memory/demo.py --live
python challenge_b_context/b1_compact/demo_b1.py --live
python challenge_b_context/b2_code_mode/demo_b2.py --live
# hoặc: python run_all.py --live
```

**Offline và live.** Chế độ offline dùng một model giả lập theo kịch bản (`ScriptedLLM`). Model giả lập chỉ đọc đúng những
gì nằm trong context, nên vẫn kiểm chứng được phần harness: memory có được inject không, checkpoint còn giữ dữ kiện không,
raw log có lọt vào context không. Token ở chế độ offline là **ước lượng** (≈ 4 ký tự/token) và được gắn cờ `estimated: true`.
Chế độ `--live` lấy input tokens từ `usage.prompt_tokens` mà provider trả về. Thiếu usage thì dừng chứ không tự ước lượng.
Minh chứng được lưu ở `evidence/offline/` hoặc `evidence/live/` trong từng thư mục challenge.

**Lưu ý khi chạy qua router (ví dụ 9Router).** Số đo của Lab 07 chỉ đúng khi router chuyển nguyên văn context tới model
và báo đúng `usage`. Hãy tắt các tính năng nén của router, ví dụ 9Router → Token Saver → *Compress tool output (RTK)*.
Tính năng này cắt log còn khoảng 10 dòng, làm B2 trả lời sai và B1 mất bằng chứng. `check_live.py` tự kiểm tra hai điều kiện này:
input tokens phải tăng khi prompt dài thêm vài nghìn token, và model phải đọc được dòng cuối của một tool output 200 dòng.
Usage gốc của provider được lưu trong `raw_usage` của mỗi `report.json`.

Một số router không tính phần context đã cache vào `usage` trả về. Ví dụ, qua 9Router + Claude Code, một request B2 raw
được dashboard ghi khoảng 80.000 input tokens, nhưng response chỉ báo khoảng 2.000. Vì vậy client luôn ghi thêm ước lượng
của chính nó (`estimated_input_tokens`). Nếu provider báo thấp hơn một nửa ước lượng, B1/B2 so sánh **tất cả** các mode bằng
ước lượng của client, và ghi nguồn số đo vào trường `token_source` của `report.json`.

## Cấu trúc

```
Lab_07/
├── common/llm.py                     # client OpenAI-compatible, model giả lập, vòng lặp agent + tool, cộng dồn usage
├── challenge_a_memory/
│   ├── memory_store.py               # memory JSON: active / superseded / retired
│   ├── agent.py                      # tools remember_convention, memory_history; inject memory vào system prompt
│   ├── demo.py                       # 5 session độc lập + 1 session đối chứng không có memory
│   └── tests/
├── challenge_b_context/
│   ├── b1_compact/                   # session_fixture.py, compaction.py, demo_b1.py
│   ├── b2_code_mode/                 # log_fixture.py, code_runtime.py, demo_b2.py
│   └── tests/
├── tests/test_llm.py                 # kiểm thử client với provider giả chạy local
├── check_live.py                     # kiểm tra kết nối model thật
└── run_all.py
```

## Challenge A: persistent memory

**Thiết kế** (trả lời các gợi ý ở slide 15):

| Câu hỏi | Lựa chọn | Lý do |
|---|---|---|
| Lưu ở đâu | Một file JSON (`memory.json`) | Số quy ước của một dự án nhỏ; đọc, diff, kiểm tra bằng mắt được. Ghi qua file tạm rồi `os.replace` để không hỏng file. |
| Ai ghi | Agent tự gọi tool `remember_convention(topic, content, replaces_id?)` | Agent hiểu câu nói của người dùng là quy ước hay chỉ là câu hỏi. Harness chỉ kiểm tra dữ liệu (không rỗng, `replaces_id` phải đang hiệu lực, không ghi trùng). |
| Khi nào nạp | Inject quy ước **đang hiệu lực** vào system prompt lúc mở session | Memory nhỏ nên nạp hết là rẻ, và agent không cần nhớ phải đi tìm. Lịch sử chỉ tra khi cần qua `memory_history`. |
| Khi quy ước đổi | Bản mới **supersede** bản cũ cùng `topic` | Bản cũ không bị xoá mà chuyển sang `superseded` và trỏ `superseded_by`. Nó không còn được inject, nên không có hai quy ước mâu thuẫn trong context, nhưng vẫn truy vết được. |
| Kiểm chứng | Demo in ra khối memory được inject ở mỗi session | Lưu kèm `sN.json` (system prompt, trace, câu trả lời) và `report.json`. |

**Kịch bản demo.** Mỗi session tạo một danh sách messages mới và nạp lại memory từ đĩa.

| Session | Người dùng | Kỳ vọng |
|---|---|---|
| s1 | Nêu 3 quy ước: JSON snake_case, pytest, Conventional Commits | Agent gọi `remember_convention` 3 lần |
| s2 | Viết JSON response cho `GET /users/{id}` + commit message | **Chứng minh 1:** dùng `full_name`, `created_at`, commit `feat(...)` |
| s3 | "Từ nay đổi JSON sang camelCase" | Agent ghi `api.json_case` mới với `replaces_id=m1` |
| s4 | Lặp lại yêu cầu của s2 | **Chứng minh 2:** dùng `fullName`, `createdAt`; quy ước commit vẫn giữ |
| s5 | "Trước đây quy ước JSON là gì?" | Agent gọi `memory_history`, nêu cả bản cũ lẫn bản mới |
| control | Yêu cầu của s2 nhưng tắt memory | Agent không biết quy ước (đối chứng) |

Memory được inject ở s4:

```
## Project memory (quy ước đang hiệu lực)
- [m2] `testing.framework`: Test viết bằng pytest, đặt trong thư mục tests/. (lưu ở s1)
- [m3] `git.commit_style`: Commit message theo Conventional Commits (feat:, fix:, ...). (lưu ở s1)
- [m4] `api.json_case`: Tên field JSON dùng camelCase (ví dụ fullName). (lưu ở s3, thay cho m1)
```

Kết quả offline: cả 7 kiểm tra đều PASS (`challenge_a_memory/evidence/offline/report.json`).

## Challenge B1: compaction

**Session dài.** `session_fixture.py` dựng một phiên debug 8 lượt: `POST /api/photos` trả 500 với ảnh lớn. Mỗi lượt có tool output
dài hàng trăm dòng log. Dữ kiện quan trọng nằm rải rác: ràng buộc ở lượt 1, ba cách đã thử ở lượt 1, 3, 5, nguyên nhân
(`MemoryError` trong `resize_to_thumbnail`) ở lượt 5, test tái hiện ở lượt 7, bước tiếp ở lượt 8.

**Khi nào compact.** Trước mỗi lần gọi model, harness ước lượng kích thước context. Nếu vượt `0.7 × budget` (mặc định budget 24.000 token)
thì compact. Compact theo lô: đẩy mọi lượt cũ ra, giữ nguyên văn 2 lượt gần nhất, nên không phải compact sau từng lượt.

**Giữ gì, bỏ gì (selective compaction):**

- Giữ nguyên văn: system prompt và 2 lượt gần nhất. Không bao giờ cắt giữa cặp `tool_calls`/`tool`.
- Giữ ở dạng checkpoint JSON: `muc_tieu`, `loi_dang_dieu_tra`, `bang_chung` (≤ 6 dòng stacktrace), `da_thu` (từng cách kèm kết quả),
  `rang_buoc`, `file_lien_quan`, `buoc_tiep`. Checkpoint được **gộp dần** qua các lần compact.
- Bỏ: raw log, danh sách test PASS, code không liên quan, các lượt không có kết luận.
- Không mất hẳn: phần bị đẩy ra được ghi nguyên văn vào `archive.jsonl`.

Ở chế độ live, checkpoint do model viết theo prompt `COMPACT_PROMPT`. Ở chế độ offline, một bộ trích xuất theo luật đóng vai đó.

**Đo.** Cùng câu hỏi "tiếp tục công việc: lỗi gì, đã thử gì, ràng buộc, bước tiếp?" được hỏi với 3 kiểu context.
Câu trả lời được chấm theo 8 dữ kiện:

| Context | Input tokens (offline, ước lượng) | Dữ kiện còn giữ |
|---|---:|---:|
| full (không compact) | 36.559 | 8/8 |
| **compacted** (checkpoint + 2 lượt cuối) | **14.048** (−61,6%) | **8/8** |
| truncated (chỉ cắt đầu, không checkpoint) | 2.549 | 2/8 |

Cắt bỏ đơn thuần rẻ nhất nhưng làm mất ràng buộc và các cách đã thử. Khi đó agent dễ thử lại cách cũ hoặc đổi API public.

## Challenge B2: Code Mode

**Dữ liệu.** `log_fixture.py` sinh 6 file log dịch vụ (mặc định 250 dòng/file, khoảng 140 KB), có cả dòng INFO/WARN/ERROR.
Đáp án đúng (top 3 mã lỗi ở các dòng ERROR) được tính bằng code và lưu ở `expected.json`.

**Hai cách:**

- `raw`: agent có `list_logs` + `read_log`, đọc từng file, nên toàn bộ log đi vào context và tích luỹ qua mỗi lần gọi.
- `code`: agent chỉ có `list_logs` + `run_python`, viết script đếm và **chỉ in kết quả cuối**.

**Code chạy ở đâu.** `code_runtime.py` chạy code trong tiến trình con `python -I`. Thư mục làm việc là thư mục logs. Biến môi trường
bị xoá, nên API key không lộ. Timeout 10 giây, stdout cắt ở 2.000 ký tự. Đây **không phải sandbox bảo mật** (slide 31):
production cần container/VM, permission check và giới hạn tài nguyên.

**Kết quả offline** (tổng input tokens qua mọi lần gọi model trong một lượt hỏi):

| Mode | Số lần gọi model | Tổng input tokens (ước lượng) | Raw log trong context | Đúng |
|---|---:|---:|:---:|:---:|
| raw | 8 | 130.337 | có | ✔ |
| **code** | **2** | **701** (−99,5%) | **không** | ✔ |

Harness tự kiểm tra "raw log không đi vào context" bằng cách quét mọi message của lượt chạy code mode,
tìm dòng có định dạng log (`contains_raw_log`).

## Kiểm thử

| Bộ test | Nội dung |
|---|---|
| `tests/test_llm.py` | Client gọi đúng `/chat/completions`, gửi `Authorization`, chạy vòng tool, lấy input tokens từ `usage.prompt_tokens`; lỗi HTTP được giải thích và che key |
| `challenge_a_memory/tests` | Persist qua instance mới, supersede giữ lịch sử, context chỉ có quy ước đang hiệu lực, chống ghi trùng, `replaces_id` sai bị chặn, demo PASS |
| `challenge_b_context/tests` | Ngưỡng compact, giữ lượt mới nhất + archive phần cũ, không cắt cặp tool call, checkpoint gộp đủ 3 cách đã thử, runtime xoá env / timeout / cắt output, đáp án fixture đúng, demo B1/B2 PASS |

## Giới hạn

- Số liệu trong README là offline (token ước lượng, model giả lập). Hãy chạy `--live` để có số đo usage thật của provider.
  Kết quả live phụ thuộc model, nên cần đọc `report.json` chứ không nên chỉ nhìn PASS/FAIL.
- Chấm câu trả lời B1 dựa trên từ khoá nên chỉ là kiểm tra thô; vẫn nên đọc câu trả lời trong `report.json`.
- Memory của Challenge A là một file JSON cho một dự án. Nếu nhiều agent ghi đồng thời thì cần khoá file hoặc chuyển sang SQLite.
