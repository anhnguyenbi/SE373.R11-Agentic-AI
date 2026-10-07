"""Tool mockup: một "API hãng bay" giả lập, có trạng thái, không gọi mạng.

Năm tool đúng như ví dụ trong slide:
    search_flights -> check_seat -> book_seat -> pay -> get_booking

Mọi tool trả về JSON có ``status`` rõ ràng (slide "Tool phải trả kết quả rõ"):
    ok              có dữ liệu (kể cả count = 0 nghĩa là THẬT SỰ không có chuyến)
    invalid_param   gọi sai tham số, kèm ``param`` và ``hint`` để agent tự sửa
    sold_out        hết chỗ
    error           công cụ lỗi (ví dụ timeout), kèm ``hint``
    not_found       không có đối tượng được hỏi

``Faults`` cho phép cài lỗi môi trường để kiểm thử: hết chỗ đột ngột khi giữ chỗ
(môi trường biến động), check_seat luôn timeout (công cụ lỗi).
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

AIRPORTS = {
    "SGN": "TP.HCM (Tân Sơn Nhất)",
    "HAN": "Hà Nội (Nội Bài)",
    "DAD": "Đà Nẵng",
    "CXR": "Nha Trang (Cam Ranh)",
    "PQC": "Phú Quốc",
    "HUI": "Huế (Phú Bài)",
}

AIRLINES = {"VN": "Vietnam Airlines", "VJ": "Vietjet Air", "QH": "Bamboo Airways", "BL": "Pacific Airlines"}


def _f(no, org, dst, date, time, price, refundable, seats):
    return dict(
        flight_no=no, airline=AIRLINES[no[:2]], origin=org, destination=dst, date=date,
        depart_time=time, price=price, refundable=refundable, seats=seats,
    )


# Cơ sở dữ liệu tĩnh -> mỗi lần chạy đều ra cùng kết quả (tái lập được).
FLIGHTS: list[dict[str, Any]] = [
    # --- SGN -> DAD 2026-10-07 (kịch bản co-ban, khu-hoi) -------------------------
    _f("QH118", "SGN", "DAD", "2026-10-07", "15:40", 1_240_000, True, 9),   # rẻ nhất nhưng sau 12:00 (bẫy "quên yêu cầu")
    _f("BL342", "SGN", "DAD", "2026-10-07", "07:15", 1_320_000, True, 0),   # rẻ, đúng giờ nhưng HẾT CHỖ
    _f("VN122", "SGN", "DAD", "2026-10-07", "08:10", 1_450_000, True, 3),   # <- đáp án đúng
    _f("VJ604", "SGN", "DAD", "2026-10-07", "09:45", 1_590_000, False, 5),
    _f("VN134", "SGN", "DAD", "2026-10-07", "11:30", 2_190_000, True, 7),   # vượt giá trần
    # --- DAD -> SGN 2026-10-09 (chặng về của khu-hoi, phải bay sau 17:00) -------
    _f("BL343", "DAD", "SGN", "2026-10-09", "19:00", 990_000, True, 0),     # hết chỗ
    _f("VN135", "DAD", "SGN", "2026-10-09", "14:20", 1_180_000, True, 6),   # quá sớm
    _f("VJ631", "DAD", "SGN", "2026-10-09", "18:05", 1_290_000, True, 4),   # <- đáp án đúng
    _f("QH119", "DAD", "SGN", "2026-10-09", "20:30", 1_350_000, True, 2),
    # --- HAN -> PQC 2026-10-12 (kịch bản het-cho: môi trường biến động) ----------
    _f("VJ451", "HAN", "PQC", "2026-10-12", "07:00", 1_490_000, True, 1),   # còn 1 ghế, bị người khác mua mất khi book
    _f("VN1233", "HAN", "PQC", "2026-10-12", "08:45", 1_720_000, True, 5),  # <- đáp án đúng sau khi đổi hướng
    _f("QH1563", "HAN", "PQC", "2026-10-12", "10:40", 2_050_000, True, 3),  # vượt giá trần
    # --- HAN -> SGN 2026-10-10 (kịch bản can-duyet) -------------------------------
    _f("QH201", "HAN", "SGN", "2026-10-10", "08:30", 1_900_000, True, 0),   # hết chỗ
    _f("VN210", "HAN", "SGN", "2026-10-10", "06:00", 2_150_000, False, 4),  # hợp lệ nhưng không hoàn + vượt hạn mức tự duyệt
    _f("VJ121", "HAN", "SGN", "2026-10-10", "13:30", 1_600_000, True, 8),   # quá giờ
    # --- SGN -> CXR 2026-10-15 (kịch bản timeout) ---------------------------------
    _f("VN1340", "SGN", "CXR", "2026-10-15", "07:40", 1_350_000, True, 6),  # check_seat luôn timeout
    _f("VJ780", "SGN", "CXR", "2026-10-15", "13:00", 990_000, True, 9),     # quá giờ
    # --- HAN -> HUI: KHÔNG có ngày 2026-10-07, chỉ có 2026-10-08 (khong-co-ngay) --
    _f("VN1541", "HAN", "HUI", "2026-10-08", "07:30", 1_280_000, True, 5),
    _f("VJ571", "HAN", "HUI", "2026-10-08", "12:15", 1_090_000, True, 3),
]

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Faults:
    """Lỗi môi trường được cài trước cho từng kịch bản."""

    sold_out_on_book: set[str] = field(default_factory=set)  # book_seat -> sold_out (ghế bị mua mất)
    timeout_check_seat: set[str] = field(default_factory=set)  # check_seat luôn timeout


class FlightBackend:
    """Hệ thống đặt vé giả lập có trạng thái (ghế, booking, thanh toán)."""

    def __init__(self, flights: list[dict[str, Any]] | None = None, faults: Faults | None = None):
        self.flights = {f["flight_no"]: f for f in copy.deepcopy(flights or FLIGHTS)}
        self.faults = faults or Faults()
        self.bookings: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict]] = []  # nhật ký phía server (dùng để audit độc lập)
        self._counter = 0

    # ----------------------------------------------------------------- helpers
    def _code(self, flight_no: str) -> str:
        self._counter += 1
        raw = hashlib.sha1(f"{flight_no}-{self._counter}".encode()).hexdigest().upper()
        letters = [c for c in raw if c.isalnum()]
        return "".join(letters[:4])

    @staticmethod
    def _public(f: dict[str, Any]) -> dict[str, Any]:
        return {k: f[k] for k in ("flight_no", "airline", "origin", "destination", "date", "depart_time", "price", "refundable")}

    # ------------------------------------------------------------------- tools
    def search_flights(self, origin: str, destination: str, date: str) -> dict[str, Any]:
        self.calls.append(("search_flights", dict(origin=origin, destination=destination, date=date)))
        origin, destination = origin.strip().upper(), destination.strip().upper()
        for name, code in (("origin", origin), ("destination", destination)):
            if code not in AIRPORTS:
                return {"status": "invalid_param", "param": name, "value": code,
                        "hint": f"Dùng mã sân bay IATA: {', '.join(AIRPORTS)}"}
        if not ISO_DATE.match(date.strip()):
            return {"status": "invalid_param", "param": "date", "value": date,
                    "hint": "Dùng YYYY-MM-DD, ví dụ 2026-10-07"}
        hits = [self._public(f) for f in self.flights.values()
                if f["origin"] == origin and f["destination"] == destination and f["date"] == date]
        hits.sort(key=lambda x: x["depart_time"])
        out: dict[str, Any] = {"status": "ok", "count": len(hits), "flights": hits}
        if not hits:
            nearby = sorted({f["date"] for f in self.flights.values()
                             if f["origin"] == origin and f["destination"] == destination})
            out["note"] = "Không có chuyến nào đúng ngày này."
            out["nearby_dates"] = nearby
        return out

    def check_seat(self, flight_no: str) -> dict[str, Any]:
        self.calls.append(("check_seat", dict(flight_no=flight_no)))
        f = self.flights.get(flight_no)
        if f is None:
            return {"status": "not_found", "flight_no": flight_no, "hint": "Gọi search_flights để lấy flight_no hợp lệ."}
        if flight_no in self.faults.timeout_check_seat:
            return {"status": "error", "error": "timeout", "flight_no": flight_no,
                    "hint": "Dịch vụ kiểm tra ghế lỗi, thử lại sau."}
        return {"status": "ok", "flight_no": flight_no, "seats_left": f["seats"], "price": f["price"],
                "refundable": f["refundable"], "depart_time": f["depart_time"], "date": f["date"]}

    def book_seat(self, flight_no: str, passenger: str) -> dict[str, Any]:
        self.calls.append(("book_seat", dict(flight_no=flight_no, passenger=passenger)))
        f = self.flights.get(flight_no)
        if f is None:
            return {"status": "not_found", "flight_no": flight_no, "hint": "Gọi search_flights để lấy flight_no hợp lệ."}
        if flight_no in self.faults.sold_out_on_book:
            f["seats"] = 0  # ghế cuối vừa bị người khác mua
        if f["seats"] <= 0:
            return {"status": "sold_out", "flight_no": flight_no, "seats_left": 0,
                    "hint": "Chuyến đã hết chỗ, hãy chọn chuyến khác từ kết quả search."}
        f["seats"] -= 1
        code = self._code(flight_no)
        self.bookings[code] = dict(booking_code=code, flight_no=flight_no, passenger=passenger,
                                   state="held", paid=False, price=f["price"], date=f["date"],
                                   depart_time=f["depart_time"], origin=f["origin"],
                                   destination=f["destination"], refundable=f["refundable"])
        return {"status": "ok", "booking_code": code, "state": "held", "flight_no": flight_no, "price": f["price"]}

    def pay(self, booking_code: str, method: str) -> dict[str, Any]:
        self.calls.append(("pay", dict(booking_code=booking_code, method=method)))
        b = self.bookings.get(booking_code)
        if b is None:
            return {"status": "not_found", "booking_code": booking_code, "hint": "Mã đặt chỗ không tồn tại."}
        if b["paid"]:
            return {"status": "ok", "booking_code": booking_code, "paid": True, "note": "đã thanh toán trước đó"}
        b["paid"] = True
        b["state"] = "confirmed"
        return {"status": "ok", "booking_code": booking_code, "paid": True, "amount": b["price"]}

    def get_booking(self, booking_code: str) -> dict[str, Any]:
        self.calls.append(("get_booking", dict(booking_code=booking_code)))
        b = self.bookings.get(booking_code)
        if b is None:
            return {"status": "not_found", "booking_code": booking_code}
        return {"status": "ok", **copy.deepcopy(b)}


# ---------------------------------------------------------------------------
# Schema tham số cho tool calling (tên, mô tả, schema - slide "Tools và Loop")
class SearchArgs(BaseModel):
    origin: str = Field(description="Mã sân bay đi (IATA), ví dụ SGN")
    destination: str = Field(description="Mã sân bay đến (IATA), ví dụ DAD")
    date: str = Field(description="Ngày bay dạng YYYY-MM-DD")


class FlightArgs(BaseModel):
    flight_no: str = Field(description="Số hiệu chuyến lấy từ kết quả search_flights")


class BookArgs(BaseModel):
    flight_no: str = Field(description="Số hiệu chuyến đã check_seat thành công")
    passenger: str = Field(description="Tên hành khách")


class PayArgs(BaseModel):
    booking_code: str = Field(description="Mã giữ chỗ trả về từ book_seat")
    method: str = Field(description="Phương thức thanh toán, ví dụ corp_card")


class BookingArgs(BaseModel):
    booking_code: str = Field(description="Mã đặt chỗ")


TOOL_SPECS: dict[str, tuple[type[BaseModel], str]] = {
    "search_flights": (SearchArgs, "Tìm chuyến bay theo chặng và ngày. Trả status, count, flights[]. count=0 nghĩa là thật sự không có chuyến."),
    "check_seat": (FlightArgs, "Kiểm tra số ghế còn và giá hiện tại của một chuyến."),
    "book_seat": (BookArgs, "Giữ chỗ (tác dụng phụ). Trả booking_code ở trạng thái held."),
    "pay": (PayArgs, "Thanh toán một booking đang held (tác dụng phụ, chuyển tiền)."),
    "get_booking": (BookingArgs, "Đọc lại trạng thái booking để kiểm chứng (confirmed, paid, price)."),
}
