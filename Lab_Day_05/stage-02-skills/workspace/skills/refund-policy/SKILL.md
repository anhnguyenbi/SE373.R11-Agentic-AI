---
name: refund-policy
description: Tra cứu chính sách hoàn tiền theo ngày mua khi khách hàng hỏi về điều kiện, thời hạn hoặc phí hoàn tiền; dùng tài liệu chính sách trong workspace làm căn cứ.
---

# Tra cứu chính sách hoàn tiền

Trước khi kết luận, cần có ngày mua, ngày yêu cầu hoàn tiền và trạng thái kích hoạt sản phẩm. Nếu thiếu hoặc không rõ thông tin nào, hỏi lại thông tin đó; không tự giả định trạng thái kích hoạt và chưa kết luận đủ hay không đủ điều kiện.

Dùng `list_files` tìm tài liệu trong `data/policies/`. Đường dẫn tool tương đối workspace, không thêm tiền tố `workspace/`. Đọc các tài liệu tìm được bằng `read_file`; nếu có thư mục con, liệt kê riêng khi cần tìm tài liệu liên quan. Không đoán tên file hoặc chọn phiên bản từ tên file: tên tài liệu có thể thay đổi.

Đọc phạm vi hiệu lực trong nội dung từng tài liệu và chọn chính sách theo **ngày mua**, kể cả quy định có bao gồm ngày bắt đầu hiệu lực hay không. Không chọn theo ngày yêu cầu hoàn tiền hoặc ngày hiện tại. Nếu không có chính sách khớp hoặc các tài liệu mâu thuẫn, nêu rõ phần chưa xác định và chưa kết luận.

Tính số ngày đã qua bằng chênh lệch ngày lịch giữa ngày yêu cầu và ngày mua, dùng đúng ngày trong câu hỏi. Bằng đúng giới hạn vẫn thỏa điều kiện thời gian. Nếu ngày yêu cầu trước ngày mua hoặc ngày không hợp lệ, hỏi lại để làm rõ.

Đối chiếu cả thời hạn và trạng thái kích hoạt với chính sách đã chọn. Chỉ nêu phí áp dụng khi đủ điều kiện; không tự tính số tiền nếu thiếu giá trị đơn hàng.

Đọc [mẫu trả lời](references/answer-template.md) bằng `read_file` tại `skills/refund-policy/references/answer-template.md` trước khi trả lời. Dẫn đúng đường dẫn tài liệu thực tế đã đọc trong cuộc trò chuyện hiện tại. Skill này chỉ dùng `list_files` và `read_file`.
