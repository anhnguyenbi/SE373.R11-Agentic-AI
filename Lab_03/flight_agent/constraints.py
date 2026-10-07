"""Lớp harness #1 - RÀNG BUỘC LÀ DỮ LIỆU.

Yêu cầu của người dùng không chỉ nằm trong prompt (sẽ bị "trôi" khi lịch sử dài
ra - failure mode "Quên yêu cầu"), mà được ghi thành dữ liệu có kiểu và được
harness kiểm lại bằng code trước mỗi hành động có tác dụng phụ và trước khi
chốt kết quả.

Ba nhóm dữ liệu:
  * Constraints - ràng buộc của NGƯỜI DÙNG (chặng, ngày, khung giờ, giá trần).
  * Policy      - chính sách QUYỀN của tổ chức (hạn mức tự duyệt, vé không hoàn...).
  * Budget      - giới hạn cứng của VÒNG LẶP (bước, token, thời gian, tiền).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
HHMM = re.compile(r"^\d{2}:\d{2}$")


def fmt_vnd(amount: int | float | None) -> str:
    if amount is None:
        return "?"
    return f"{int(amount):,}".replace(",", ".") + "đ"


class LegConstraint(BaseModel):
    """Ràng buộc cho một chặng bay."""

    origin: str = Field(description="Mã sân bay đi, ví dụ SGN")
    destination: str = Field(description="Mã sân bay đến, ví dụ DAD")
    date: str = Field(description="Ngày bay YYYY-MM-DD")
    depart_after: str = Field(default="00:00", description="Cất cánh không sớm hơn HH:MM")
    depart_before: str = Field(default="23:59", description="Cất cánh trước HH:MM")

    @field_validator("date")
    @classmethod
    def _iso(cls, v: str) -> str:
        if not ISO_DATE.match(v):
            raise ValueError("date phải có dạng YYYY-MM-DD")
        return v

    @field_validator("depart_after", "depart_before")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not HHMM.match(v):
            raise ValueError("giờ phải có dạng HH:MM")
        return v

    def label(self) -> str:
        win = ""
        if self.depart_after != "00:00":
            win += f" sau {self.depart_after}"
        if self.depart_before != "23:59":
            win += f" trước {self.depart_before}"
        return f"{self.origin}→{self.destination} {self.date}{win}"


class Constraints(BaseModel):
    """Toàn bộ yêu cầu của người dùng dưới dạng dữ liệu kiểm được bằng code."""

    legs: list[LegConstraint]
    max_price_per_leg: int = Field(description="Giá trần mỗi chặng (VND)")
    passenger: str = "NGUYEN VAN A"
    payment_method: str = "corp_card"
    allow_change_date: bool = False
    prefer: str = "cheapest"  # tiêu chí chọn: rẻ nhất trong các chuyến hợp lệ

    # ---- kiểm một chuyến bay có thoả ràng buộc của chặng hay không ----
    def violations(self, leg_idx: int, flight: dict[str, Any]) -> list[str]:
        leg = self.legs[leg_idx]
        out: list[str] = []
        if flight.get("origin") != leg.origin or flight.get("destination") != leg.destination:
            out.append(
                f"sai chặng ({flight.get('origin')}→{flight.get('destination')} ≠ {leg.origin}→{leg.destination})"
            )
        if flight.get("date") != leg.date:
            if not self.allow_change_date:
                out.append(f"sai ngày ({flight.get('date')} ≠ {leg.date})")
        t = str(flight.get("depart_time", ""))
        if t and not (leg.depart_after <= t < leg.depart_before):
            out.append(f"sai giờ ({t} ngoài khung {leg.depart_after}-{leg.depart_before})")
        price = flight.get("price")
        if price is not None and int(price) > self.max_price_per_leg:
            out.append(f"vượt giá trần ({fmt_vnd(price)} > {fmt_vnd(self.max_price_per_leg)})")
        return out

    def is_ok(self, leg_idx: int, flight: dict[str, Any]) -> bool:
        return not self.violations(leg_idx, flight)

    def leg_of(self, origin: str, destination: str, date: str | None = None) -> int | None:
        for i, leg in enumerate(self.legs):
            if leg.origin == origin and leg.destination == destination and (date is None or leg.date == date):
                return i
        for i, leg in enumerate(self.legs):  # khớp chặng dù ngày sai định dạng
            if leg.origin == origin and leg.destination == destination:
                return i
        return None

    def to_prompt_block(self) -> str:
        """Ràng buộc được đưa vào system prompt dưới dạng JSON (dữ liệu, không phải văn xuôi)."""
        return "<CONSTRAINTS>" + json.dumps(self.model_dump(), ensure_ascii=False) + "</CONSTRAINTS>"

    def describe(self) -> str:
        legs = "; ".join(f"chặng {i + 1}: {leg.label()}" for i, leg in enumerate(self.legs))
        return f"{legs}; giá ≤ {fmt_vnd(self.max_price_per_leg)}/chặng"


class Policy(BaseModel):
    """Chính sách quyền hạn (lớp kiểm quyền đọc dữ liệu này, không hard-code trong prompt)."""

    read_tools: list[str] = ["search_flights", "check_seat", "get_booking"]
    write_tools: list[str] = ["book_seat", "pay"]
    auto_approve_limit: int = 2_000_000  # trên mức này phải có người duyệt
    nonrefundable_needs_approval: bool = True  # vé không hoàn luôn phải duyệt
    allowed_payment_methods: list[str] = ["corp_card"]
    require_check_before_book: bool = True  # phải có check_seat thành công trước book_seat
    require_seen_in_search: bool = True  # chuyến phải xuất hiện trong observation search

    @property
    def allowlist(self) -> list[str]:
        return self.read_tools + self.write_tools


class Budget(BaseModel):
    """Giới hạn cứng: bước · token · thời gian · chi phí (slide 'Ngân sách vòng lặp')."""

    max_tool_calls: int = 14
    max_llm_calls: int = 16
    max_tokens: int = 60_000
    max_seconds: float = 120.0
    max_cost_usd: float = 0.05
    usd_per_m_input: float = 0.15  # đơn giá giả định (≈ model nhỏ), dùng để ước lượng chi phí
    usd_per_m_output: float = 0.60

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return tokens_in / 1e6 * self.usd_per_m_input + tokens_out / 1e6 * self.usd_per_m_output
