# Lab 03 · BTVN#3 - Agent đặt vé máy bay bằng LangChain + LangGraph

SE373 · Kỹ thuật xây dựng hệ thống Agentic AI · Buổi 03 *Agent fundamentals*

**Sinh viên:** Nguyễn Bi Anh · **MSSV:** 23520055

> Tìm hiểu LangChain, LangGraph → tạo tool mockup → viết lớp harness cho agent.
> (1) Đủ các lớp harness: ràng buộc là dữ liệu, tiêu chí hoàn thành kiểm bằng code, kiểm quyền, bàn giao.
> (2) Cài agent với 3 mẫu: ReAct, Plan-then-Execute, Lai. (3) Đánh giá hiệu quả 3 mẫu.

Báo cáo đầy đủ: [`report/Bao_cao_Lab03.docx`](report/Bao_cao_Lab03.docx)

## Chạy nhanh (không cần khoá API)

```bash
cd Lab_03
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python lab03.py list                                      # 6 kịch bản
python lab03.py demo --design react  --scenario co-ban    # trace V1, V2, ... như trong slide
python lab03.py demo --design pe     --scenario het-cho   # P&E gặp kế hoạch lỗi thời -> bàn giao
python lab03.py demo --design hybrid --scenario het-cho   # Lai lập lại kế hoạch -> đặt được
python lab03.py demo --design hybrid --scenario can-duyet --approve ask   # bạn đóng vai người duyệt
python lab03.py demo --design react  --scenario co-ban --noise 0.3 --seed 3   # model mắc lỗi, harness chặn
python lab03.py demo --design react  --scenario can-duyet --no-harness       # bỏ harness: trừ tiền vé không hoàn mà không duyệt
python lab03.py bench --seeds 30 --noise 0.15             # đánh giá 1.116 lần chạy -> results/ (~2 phút)
python tests/test_lab03.py                                 # kiểm thử harness
```

Mặc định agent dùng `MockFlightLLM`, một `BaseChatModel` thật của LangChain (có `bind_tools`, tool_calls) đọc
observation để quyết định và **cài lỗi theo seed** để mô phỏng các failure mode trong slide. Cách này chạy offline,
không tốn tiền và lần nào chạy cũng ra cùng kết quả. Muốn chạy với model thật: `pip install langchain-openai`, chép
`.env.example` thành `.env` rồi điền khoá, sau đó chạy `python lab03.py demo --llm openai:gpt-4o-mini ...`
(cũng dùng được `anthropic:...` hoặc `google_genai:...`).

## Cấu trúc

```
Lab_03/
├── lab03.py                    # CLI: list / demo / bench
├── flight_agent/
│   ├── tools_mock.py           # Tool mockup: search_flights → check_seat → book_seat → pay → get_booking (+ cài lỗi)
│   ├── constraints.py          # Harness (1): RÀNG BUỘC LÀ DỮ LIỆU - Constraints, Policy, Budget
│   ├── harness.py              # Harness (2)(3)(4): is_done, kiểm quyền + phê duyệt, Handoff, LoopDetector, ngân sách
│   ├── agents/react.py         # Mẫu 1: create_agent + HarnessMiddleware (before_model / after_model)
│   ├── agents/plan_execute.py  # Mẫu 2: LangGraph StateGraph planner → review → execute → finalize
│   ├── agents/hybrid.py        # Mẫu 3: planner → execute k bước → (đổi đáng kể?) → replanner
│   ├── agents/common.py        # schema Plan, duyệt kế hoạch, executor điền tham số
│   ├── mock_llm.py             # LLM giả lập có nhiễu (repeat, hallucinate, forget_constraint, ...)
│   ├── scenarios.py            # 6 kịch bản kiểm thử
│   ├── evaluate.py             # benchmark + CHẤM ĐỘC LẬP (audit trạng thái backend)
│   └── charts.py
├── results/                    # runs.csv, summary.json, noise_sweep.json, charts/*.png, traces/*.txt
├── report/                     # báo cáo .docx + script dựng báo cáo
└── tests/test_lab03.py
```

## Harness: từng lớp nằm ở đâu

| Lớp (theo slide) | Cài đặt |
|---|---|
| Ràng buộc là dữ liệu | `Constraints` (chặng, ngày, khung giờ, giá trần), `Policy` (allowlist, hạn mức tự duyệt 2 triệu, vé không hoàn phải duyệt), `Budget` (tool call, lần gọi model, token, giây, USD). Đưa vào prompt dạng `<CONSTRAINTS>{json}</CONSTRAINTS>` **và** được harness kiểm lại bằng code. |
| Tiêu chí hoàn thành kiểm bằng code | `FlightHarness.is_done()` đọc trực tiếp backend: mọi chặng `state=="confirmed" and paid` đúng ràng buộc, giá khớp với giá đã thấy ở `check_seat` (kiểm chứng chéo). `verify_answer()` đối chiếu số hiệu chuyến, mã, giá trong câu trả lời với observation; nếu model tuyên bố "đã đặt" mà `is_done()` sai thì câu trả lời bị trả lại. |
| Kiểm quyền | `check_permission()` chạy **trước** khi thực thi: tool ngoài allowlist, chuyến không có trong observation (nghi bịa), vi phạm ràng buộc, chưa `check_seat`, đặt trùng, sai phương thức trả tiền → `denied` kèm `hint`; vượt hạn mức hoặc vé không hoàn → `ask` (người duyệt). |
| Bàn giao | `Handoff`: trạng thái (đã làm tới đâu), tác dụng phụ (giữ chỗ, đã trừ tiền), những gì đã thử, câu hỏi cụ thể, các lựa chọn. |
| 5 điều kiện dừng | Thứ tự sau mỗi vòng: kiểm quyền (trước) → đạt mục tiêu → lặp → bế tắc → ngân sách (kiểm cuối cùng). `LoopDetector` theo đúng code trong slide (window=6, repeat_k=3, stall_n=5, miễn `get_booking` vì polling hợp lệ). |

## Kết quả chính (`python lab03.py bench`, 30 seed × nhiễu 15% + 1 lần chạy sạch / ô)

| Design | Đạt (harness bật) | Không an toàn (harness bật) | Đạt (tắt harness) | Không an toàn (tắt harness) | LLM call TB | Token TB |
|---|---|---|---|---|---|---|
| ReAct | 97,8% | 0% | 55,6% | 44,4% | 6,9 | 7.990 |
| Plan-then-Execute | 68,3% | 0% | 17,2% | 23,3% | 4,0 | 3.040 |
| Lai | 100% | 0% | 44,5% | 28,3% | 5,5 | 4.624 |

* **Harness là thứ làm agent an toàn**: bật harness thì 0 lần trừ tiền sai và 0 lần tuyên bố sai trên 1.116 lần chạy.
  Tắt harness thì 23–44% số lần chạy có nhiễu kết thúc không an toàn, và kịch bản `can-duyet` trừ tiền vé không hoàn
  ở 100% số lần chạy sạch của cả 3 mẫu.
* **ReAct** linh hoạt nhất trong ba mẫu đơn nhưng đắt nhất: mỗi vòng nạp lại toàn bộ lịch sử, nên token khứ hồi gấp
  khoảng 1,7 lần P&E, và đôi khi chạm trần 16 lần gọi model.
* **Plan-then-Execute** rẻ nhất và kế hoạch duyệt được trước khi chạy, nhưng gãy khi môi trường đổi
  (`het-cho`: 0%) hoặc khi một bước đầu sai.
* **Lai** lấy được cả hai ưu điểm: chỉ tốn một lần gọi replanner khi observation "đổi đáng kể" (code phát hiện).
  Đổi lại, trace nhiều loại node hơn nên khó debug hơn.

> Lưu ý: số liệu đo trên LLM giả lập có mô hình lỗi giả định, nên chỉ để **so sánh tương đối** giữa các kiến trúc.
> Muốn có số liệu tuyệt đối, chạy lại với `--llm` là model thật.
