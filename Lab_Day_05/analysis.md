# Phân tích Lab 05 — Tra cứu chính sách đúng phiên bản

## Kết quả cuối cùng

Mã nguồn, đăng ký tool, skill, reference, tài liệu chính sách và bằng chứng của Block 1 đã được hoàn thành trong `Lab_Day_05`. Thư mục project mẫu được giữ nguyên. Stage 02 đạt cả năm tình huống kiểm tra: A, B, A sau đổi tên, B sau đổi tên và thiếu thông tin.

Đã đối chiếu **11 lượt chạy thực tế** qua 9Router. Tên model cấu hình trong manifest là `cc/claude-opus-5-5`; đây là định danh được gửi cho gateway, không phải bằng chứng độc lập về model phía sau gateway. Báo cáo dùng các trace trong `evidence/live/` có `mode: live-provider` ở manifest. Trace mock trong `evidence/` là bằng chứng kiểm tra cơ chế riêng, không được dùng thay kết quả thực tế.

Cả 11 lượt chạy đều hoàn tất về mặt thực thi. Về hành vi, stage 01 chưa hỏi lại trong trường hợp thiếu trạng thái kích hoạt; stage 02 đã xử lý đúng nhờ skill. Kết quả này được giữ nguyên trong báo cáo và trace.

Không thay nội dung system prompt, không ghi cố định tên file, nội dung chính sách hoặc đáp án trong tool. Mã Python không có token comment; docstring mô tả tool được giữ để LangChain tạo schema. Không thêm Bash hoặc script làm capability cho agent stage 02.

## Stage 00: thông tin và khả năng còn thiếu

Trace `evidence/live/20261007-192718_47ac88a4_turn01_30764364.jsonl` có request đầu tại dòng 2, danh sách tools rỗng, không có tool result. Dòng 4 là câu trả lời cuối.

Agent tính được 8 ngày từ dữ liệu câu hỏi, ghi nhận sản phẩm chưa kích hoạt, nhưng nói rõ chưa có chính sách và không thể tự đọc file. Agent yêu cầu cung cấp nội dung chính sách trước khi kết luận. Câu hỏi A đã đủ ba thông tin của khách hàng; phần còn thiếu là tài liệu chính sách của đơn vị, phạm vi hiệu lực các phiên bản và khả năng tìm/đọc tài liệu.

Không có bằng chứng agent đã đọc chính sách ở stage 00. Agent không khẳng định quyền hoàn tiền khi chưa có căn cứ, đúng mục tiêu xác định giới hạn.

## Tool `list_files`

Tool được cài đặt giống nhau ở stage 01 và stage 02:

- `_list(workspace, path)` tái sử dụng `_resolve` và `_error` của file tool hiện có.
- Liệt kê các mục trực tiếp, không duyệt đệ quy; sắp xếp theo tên.
- Thành công trả `ok`, `path`, `entries`. Mỗi mục có `name`, `path` tương đối workspace và `type` là `file` hoặc `directory`.
- Thư mục rỗng hợp lệ trả thành công với `entries: []`; đường dẫn không tồn tại hoặc là file trả lỗi riêng.
- Chặn đường dẫn tuyệt đối, `~`, `..` vượt workspace và symlink dẫn ra ngoài. Nếu một mục con là symlink ra ngoài, trả lỗi cho lần liệt kê, không trả kết quả thành công một phần.
- Symlink trong workspace được phép; đường dẫn mục trả về dùng tên alias tương đối workspace. Mục không phải file/directory hợp lệ trả `UNSUPPORTED_ENTRY`; lỗi truy cập trả `DIRECTORY_ACCESS_ERROR`.
- Tool được export trong `tools/__init__.py` và thêm vào `TOOLS` của `agent.py`. Kiểm thử agent xác nhận schema `path: string`, đối số bắt buộc `path` và tool result vào request tiếp theo.

### Kết quả gọi tool trực tiếp

Mỗi stage có 9 bản ghi đạt, đều dùng dữ liệu giả trong thư mục tạm. Bằng chứng: `evidence/stage-01-files_tool_checks.jsonl` và `evidence/stage-02-skills_tool_checks.jsonl`.

| Dòng | Trường hợp | Kết quả quan sát |
|---|---|---|
| 1 | Thư mục hợp lệ | `ok: true`; mục `a.md`, `nested`, `z.md` theo thứ tự; không chứa file bên trong `nested` |
| 2 | Đường dẫn file | `NOT_A_DIRECTORY` |
| 3 | Không tồn tại | `DIRECTORY_NOT_FOUND` |
| 4 | `..` vượt workspace | `PATH_OUTSIDE_WORKSPACE` |
| 5 | Đường dẫn tuyệt đối | `PATH_OUTSIDE_WORKSPACE` |
| 6 | Symlink tới thư mục ngoài workspace | `PATH_OUTSIDE_WORKSPACE` |
| 7 | Thư mục rỗng | `ok: true`, danh sách rỗng |
| 8 | Tìm lại sau đổi tên dữ liệu giả | Tìm thấy `data/renamed-41.md`, không dùng tên cũ |
| 9 | Đọc đường dẫn đã tìm | Đọc đúng nội dung dữ liệu giả ban đầu |

Schema tool được lưu riêng tại `evidence/stage-01-files_list_files_schema.json` và `evidence/stage-02-skills_list_files_schema.json`.

## Skill `refund-policy`

Skill ở `stage-02-skills/workspace/skills/refund-policy/`, gồm `SKILL.md` và `references/answer-template.md`; có bản tương ứng trong fixtures để reset workspace không làm mất bài tập.

Frontmatter có `name` và `description` nêu nhiệm vụ, điều kiện sử dụng. Catalog chỉ nạp metadata ban đầu. Model tự đọc body bằng `read_file`, sau đó đọc reference; nội dung được quan sát trong request tiếp theo.

Skill yêu cầu đủ ngày mua, ngày yêu cầu và trạng thái kích hoạt trước khi kết luận; tìm tài liệu bằng `list_files` tại `data/policies/`; đọc nội dung bằng `read_file`; chọn phạm vi hiệu lực theo **ngày mua**. Skill không ghi cố định tên file chính sách. Reference quy định chính sách áp dụng, số ngày, kết luận và lý do, phí nếu đủ điều kiện, đường dẫn tài liệu làm căn cứ.

Ngày yêu cầu trong câu hỏi được dùng để tính chênh lệch ngày lịch. Bằng đúng giới hạn vẫn đạt điều kiện thời gian. Skill không yêu cầu Bash hoặc script.

## Kết quả từng trường hợp thực tế

Các tên trace trong bảng đều thuộc `evidence/live/`. Manifest cùng tên stage/trường hợp chứa câu hỏi, câu trả lời, inventory và ánh xạ đổi tên; `_context.json` chứa messages và snapshots đầy đủ.

| Stage | Trường hợp | Kết quả quan sát | Trace |
|---|---|---|---|
| stage-00-chat | A | Nêu thiếu chính sách và không có quyền đọc file; chưa kết luận. Đạt việc xác định giới hạn. | `20261007-192718_47ac88a4_turn01_30764364.jsonl` |
| stage-01-files | A-renamed | Tìm tên mới; 8 ngày; không được hoàn; dẫn edition-31.md. Đạt kết luận A. | `20261007-192753_fcf08678_turn01_b5b300be.jsonl` |
| stage-01-files | A | Chính sách cũ; 8 ngày; không được hoàn. Đạt kết luận A. | `20261007-192725_26eae8b1_turn01_6ca6d8da.jsonl` |
| stage-01-files | B-renamed | Tìm tên mới; 10 ngày; được hoàn, không phí; dẫn edition-32.md. Đạt kết luận B. | `20261007-192808_9ad4e594_turn01_3df2d5ed.jsonl` |
| stage-01-files | B | Chính sách mới; 10 ngày; được hoàn, không phí. Đạt kết luận B. | `20261007-192740_2a41f480_turn01_8cd2cf16.jsonl` |
| stage-01-files | missing-activation | Chưa đạt yêu cầu hỏi lại: trả lời đủ điều kiện với điều kiện chưa kích hoạt, không đặt câu hỏi xác nhận. | `20261007-192822_f8142432_turn01_7721c346.jsonl` |
| stage-02-skills | A-renamed | 8 ngày; không đủ điều kiện; dẫn data/policies/edition-31.md. Đạt. | `20261007-192901_71584c29_turn01_038f7406.jsonl` |
| stage-02-skills | A | Chính sách cũ; 8 ngày; không đủ điều kiện; dẫn đúng tài liệu. Đạt. | `20261007-192836_aead8b64_turn01_59399034.jsonl` |
| stage-02-skills | B-renamed | 10 ngày; đủ điều kiện; không phí; dẫn data/policies/edition-32.md. Đạt. | `20261007-192913_83d97ee4_turn01_dc818466.jsonl` |
| stage-02-skills | B | Chính sách mới; 10 ngày; đủ điều kiện; không phí; dẫn đúng tài liệu. Đạt. | `20261007-192851_ae5034d8_turn01_b8dd6224.jsonl` |
| stage-02-skills | missing-activation | Hỏi sản phẩm đã kích hoạt chưa; chưa kết luận và chưa khẳng định phí áp dụng. Đạt. | `20261007-192924_39cfe3d2_turn01_87dc31d9.jsonl` |

Stage 01 còn đưa ra cách tính ngày mua là ngày thứ nhất trong một số câu trả lời và đưa ra điều kiện hoàn khi chưa biết trạng thái kích hoạt. Kết luận A/B vẫn đúng, nhưng quy trình hỏi lại và cách trình bày chưa nhất quán. Stage 02 áp dụng skill: tính đúng 8/10 ngày và hỏi trạng thái kích hoạt trước khi kết luận ở trường hợp thiếu thông tin. Vì stage 01 là bản kiểm tra tool chưa có skill, không sửa prompt để che đi sự khác biệt này.

## Vị trí bằng chứng trong trace nộp bài

Số dòng dưới đây là số dòng vật lý của file JSONL; mỗi dòng chứa một event đầy đủ. Không chỉ kiểm tra `status: completed`, đã đọc câu trả lời, tool result và snapshot context.

### A

Trace: `evidence/live/20261007-192836_aead8b64_turn01_59399034.jsonl`.

| Dòng JSONL | Bằng chứng |
|---|---|
| 5 | `read_file` thành công, trả nội dung `skills/refund-policy/SKILL.md`. |
| 6 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`. |
| 10 | `read_file` thành công, trả nội dung `skills/refund-policy/references/answer-template.md`. |
| 11 | `list_files` thành công tại `data/policies`, trả policy-before-oct.md, policy-from-oct.md. |
| 12 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`, `skills/refund-policy/references/answer-template.md`. |
| 16 | `read_file` thành công, trả nội dung `data/policies/policy-from-oct.md`. |
| 17 | `read_file` thành công, trả nội dung `data/policies/policy-before-oct.md`. |
| 18 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`, `skills/refund-policy/references/answer-template.md`, `data/policies/policy-before-oct.md`, `data/policies/policy-from-oct.md`. |
| 20 | Câu trả lời cuối; Chính sách cũ; 8 ngày; không đủ điều kiện; dẫn đúng tài liệu. Đạt. |
### A sau đổi tên

Trace: `evidence/live/20261007-192901_71584c29_turn01_038f7406.jsonl`.

| Dòng JSONL | Bằng chứng |
|---|---|
| 5 | `read_file` thành công, trả nội dung `skills/refund-policy/SKILL.md`. |
| 6 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`. |
| 10 | `read_file` thành công, trả nội dung `skills/refund-policy/references/answer-template.md`. |
| 11 | `list_files` thành công tại `data/policies`, trả edition-31.md, edition-32.md. |
| 12 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`, `skills/refund-policy/references/answer-template.md`. |
| 16 | `read_file` thành công, trả nội dung `data/policies/edition-31.md`. |
| 17 | `read_file` thành công, trả nội dung `data/policies/edition-32.md`. |
| 18 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`, `skills/refund-policy/references/answer-template.md`, `data/policies/edition-31.md`, `data/policies/edition-32.md`. |
| 20 | Câu trả lời cuối; 8 ngày; không đủ điều kiện; dẫn data/policies/edition-31.md. Đạt. |
### B sau đổi tên

Trace: `evidence/live/20261007-192913_83d97ee4_turn01_dc818466.jsonl`.

| Dòng JSONL | Bằng chứng |
|---|---|
| 5 | `read_file` thành công, trả nội dung `skills/refund-policy/SKILL.md`. |
| 6 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`. |
| 10 | `read_file` thành công, trả nội dung `skills/refund-policy/references/answer-template.md`. |
| 11 | `list_files` thành công tại `data/policies`, trả edition-31.md, edition-32.md. |
| 12 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`, `skills/refund-policy/references/answer-template.md`. |
| 16 | `read_file` thành công, trả nội dung `data/policies/edition-31.md`. |
| 17 | `read_file` thành công, trả nội dung `data/policies/edition-32.md`. |
| 18 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`, `skills/refund-policy/references/answer-template.md`, `data/policies/edition-31.md`, `data/policies/edition-32.md`. |
| 20 | Câu trả lời cuối; 10 ngày; đủ điều kiện; không phí; dẫn data/policies/edition-32.md. Đạt. |
### Thiếu thông tin

Trace: `evidence/live/20261007-192924_39cfe3d2_turn01_87dc31d9.jsonl`.

| Dòng JSONL | Bằng chứng |
|---|---|
| 5 | `read_file` thành công, trả nội dung `skills/refund-policy/SKILL.md`. |
| 6 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`. |
| 10 | `read_file` thành công, trả nội dung `skills/refund-policy/references/answer-template.md`. |
| 11 | `list_files` thành công tại `data/policies`, trả policy-before-oct.md, policy-from-oct.md. |
| 12 | Request đã chứa tool message của `skills/refund-policy/SKILL.md`, `skills/refund-policy/references/answer-template.md`. |
| 14 | Câu trả lời cuối; Hỏi sản phẩm đã kích hoạt chưa; chưa kết luận và chưa khẳng định phí áp dụng. Đạt. |

## Xác minh đổi tên và lịch sử mới

Trong các manifest sau đổi tên, ánh xạ là:

| Tên ban đầu | Tên trong lần kiểm tra |
|---|---|
| `policy-before-oct.md` | `edition-31.md` |
| `policy-from-oct.md` | `edition-32.md` |

Listing thực tế trả tên mới; `read_file` đọc đúng các đường dẫn mới; A dẫn `edition-31.md`, B dẫn `edition-32.md`. Kết luận giữ nguyên khi tên thay đổi. Nội dung không bị sửa khi đổi tên.

Cả 11 manifest có `conversation_id` khác nhau. Request đầu của mỗi trace chỉ có một message người dùng, không có tool result từ lịch sử trước. Các tool result của lần đổi tên không chứa tên file chính sách cũ. Điều này xác nhận agent tìm lại tài liệu trong conversation mới thay vì dùng kết quả đọc cũ.

## Kiểm thử và phạm vi kết luận

Bộ test tự động dùng model giả lập cho những test kiểm tra cơ chế agent; không gọi provider. Đã chạy lại sau khi thu trace thực tế, lưu log trong `evidence/stage-00-tests.txt`, `stage-01-tests.txt`, `stage-02-tests.txt`:

| Stage | Tests đạt | Tests lỗi |
|---|---:|---:|
| Stage 00 | 17 | 0 |
| Stage 01 | 51 | 0 |
| Stage 02 | 60 | 0 |
| Tổng | **128** | **0** |

Đã chạy lại 18 bản ghi kiểm tra tool trực tiếp ở hai stage, sử dụng dữ liệu giả. Validator skill đã báo `Skill is valid!`. Các test kiểm tra schema đăng ký, đường dẫn an toàn, symlink, thứ tự listing, catalog và việc đưa skill/reference/tài liệu đã đổi tên vào context.

`evidence/stage-02_context_mock.jsonl` là trace bổ sung dùng `ScriptedChatModel`, được đánh dấu `execution_mode: scripted-mock`. Bằng chứng suy luận và lựa chọn tool của bài nộp lấy từ các trace thực tế nêu ở trên.

Đối với mốc ngày mua 01/10/2026, bằng đúng giới hạn 7/14 ngày và sản phẩm đã kích hoạt: quy tắc đã được thể hiện trong tài liệu và skill, nhưng các trace thực tế hiện tại không chạy riêng những trường hợp biên này. Không suy rộng kết quả các tình huống đã chạy thành bảo đảm cho mọi câu hỏi hoặc mọi model.

## Thành phần bài nộp

Bản ZIP chứa source stage 00–02; tool và đăng ký ở stage 01–02; skill/refund-policy và reference; hai tài liệu chính sách trong workspace/fixtures; trace thực tế, context và manifest; bằng chứng kiểm tra tool; log test; `analysis.md` và hướng dẫn chạy.

Bản ZIP loại `.env`, môi trường `.venv`, cache Python/pytest, Git metadata và API key. Cấu hình riêng trên máy được giữ để có thể chạy lại. Các script thu trace chạy bên ngoài agent; không cấp thêm tool script hoặc shell cho stage 02.

## Trả lời câu hỏi cuối bài

**Vì sao cần tool để tìm file và skill để hướng dẫn chọn chính sách?**

Tool cung cấp khả năng thao tác thực tế: liệt kê những mục đang tồn tại và lấy đường dẫn hợp lệ để đọc. Skill cung cấp quy trình nghiệp vụ: xác định thông tin còn thiếu, đọc phạm vi hiệu lực, chọn theo ngày mua, tính số ngày và trình bày căn cứ. Có tool tìm file chưa đảm bảo agent áp dụng đúng phiên bản; có hướng dẫn nghiệp vụ nhưng không có tool cũng chưa giúp agent nhìn thấy các file trong workspace. Hai phần giải quyết hai nhu cầu bổ sung cho nhau.

**Nếu chưa có tool tìm file, sửa prompt có giải quyết yêu cầu đổi tên file không?**

Trong project mẫu, không. `read_file` chỉ đọc được đường dẫn đã biết; prompt không tạo ra khả năng liệt kê thư mục hoặc tự cập nhật tên file khi chúng thay đổi. Ghi cố định tên file trong prompt sẽ lỗi sau khi đổi tên, đồng thời trái giới hạn của bài. Đưa danh sách file vào prompt từ một quá trình quét bên ngoài cũng phải bổ sung khả năng truy cập hệ thống file, nên không còn là chỉ sửa prompt. Cần tool tìm file đã đăng ký, sau đó đọc nội dung và dùng skill để lựa chọn chính sách.
