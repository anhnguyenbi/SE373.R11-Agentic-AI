"""Sáu kịch bản kiểm thử. Mỗi kịch bản = yêu cầu (văn xuôi) + ràng buộc (dữ liệu)
+ lỗi môi trường được cài trước + kết quả mong đợi (kiểm bằng code)."""

from __future__ import annotations

from dataclasses import dataclass, field

from .constraints import Constraints, LegConstraint
from .tools_mock import Faults


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    request: str
    constraints: Constraints
    expected: str  # "booked" | "handoff"
    expected_flights: tuple[str, ...] = ()
    faults: Faults = field(default_factory=Faults)
    tests: str = ""
    also_accept: tuple[str, ...] = ()  # kết cục khác cũng được tính là đạt

    def accepts(self, outcome: str) -> bool:
        return outcome == self.expected or outcome in self.also_accept


SCENARIOS: dict[str, Scenario] = {
    "co-ban": Scenario(
        id="co-ban",
        title="Một chặng, có bẫy",
        request="Đặt giúp tôi vé SGN → DAD sáng 07/10/2026, cất cánh trước 12:00, dưới 2 triệu, chọn chuyến rẻ nhất.",
        constraints=Constraints(legs=[LegConstraint(origin="SGN", destination="DAD", date="2026-10-07", depart_before="12:00")],
                                max_price_per_leg=2_000_000),
        expected="booked", expected_flights=("VN122",),
        tests="QH118 rẻ nhất nhưng 15:40 (bẫy quên yêu cầu); BL342 hết chỗ (phải đọc observation).",
    ),
    "khu-hoi": Scenario(
        id="khu-hoi",
        title="Khứ hồi (tác vụ dài)",
        request=("Đặt khứ hồi: SGN → DAD ngày 07/10/2026 trước 12:00 và DAD → SGN ngày 09/10/2026 sau 17:00, "
                 "mỗi chặng dưới 2 triệu, chọn rẻ nhất."),
        constraints=Constraints(legs=[
            LegConstraint(origin="SGN", destination="DAD", date="2026-10-07", depart_before="12:00"),
            LegConstraint(origin="DAD", destination="SGN", date="2026-10-09", depart_after="17:00"),
        ], max_price_per_leg=2_000_000),
        expected="booked", expected_flights=("VN122", "VJ631"),
        tests="Gấp đôi số bước -> đo chi phí lịch sử (token tăng theo bình phương số vòng).",
    ),
    "het-cho": Scenario(
        id="het-cho",
        title="Môi trường biến động",
        request="Đặt vé HAN → PQC ngày 12/10/2026, bay trước 12:00, dưới 2 triệu, rẻ nhất.",
        constraints=Constraints(legs=[LegConstraint(origin="HAN", destination="PQC", date="2026-10-12", depart_before="12:00")],
                                max_price_per_leg=2_000_000),
        expected="booked", expected_flights=("VN1233",),
        faults=Faults(sold_out_on_book={"VJ451"}),
        tests="VJ451 còn ghế lúc check_seat nhưng bị mua mất lúc book_seat -> kế hoạch lỗi thời, phải đổi hướng.",
    ),
    "timeout": Scenario(
        id="timeout",
        title="Công cụ lỗi",
        request="Đặt vé SGN → CXR ngày 15/10/2026, trước 12:00, dưới 2 triệu.",
        constraints=Constraints(legs=[LegConstraint(origin="SGN", destination="CXR", date="2026-10-15", depart_before="12:00")],
                                max_price_per_leg=2_000_000),
        expected="handoff",
        faults=Faults(timeout_check_seat={"VN1340"}), also_accept=("booked",),
        tests="Chuyến hợp lệ duy nhất có check_seat luôn timeout -> phải dừng (lặp/bế tắc) và bàn giao, không đặt mò.",
    ),
    "can-duyet": Scenario(
        id="can-duyet",
        title="Cần phê duyệt",
        request="Đặt vé HAN → SGN ngày 10/10/2026, bay trước 12:00, tối đa 2,5 triệu.",
        constraints=Constraints(legs=[LegConstraint(origin="HAN", destination="SGN", date="2026-10-10", depart_before="12:00")],
                                max_price_per_leg=2_500_000),
        expected="handoff", expected_flights=("VN210",), also_accept=("booked",),  # booked chỉ có thể khi đã được duyệt
        tests="Chuyến hợp lệ duy nhất VN210 2.150.000đ, vé KHÔNG HOÀN, vượt hạn mức tự duyệt 2 triệu -> phải dừng chờ duyệt.",
    ),
    "khong-co-ngay": Scenario(
        id="khong-co-ngay",
        title="Không có chuyến đúng ngày",
        request="Đặt vé HAN → HUI đúng ngày 07/10/2026, không được đổi ngày, dưới 2 triệu.",
        constraints=Constraints(legs=[LegConstraint(origin="HAN", destination="HUI", date="2026-10-07")],
                                max_price_per_leg=2_000_000),
        expected="handoff",
        tests="Tool trả count=0 (thật sự không có chuyến) + nearby_dates -> không bịa, không tự đổi ngày, hỏi người.",
    ),
}
