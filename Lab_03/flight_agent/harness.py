"""FlightHarness - lớp code nằm giữa model và tool có tác dụng phụ.

Model chỉ ĐỀ XUẤT tool call. Harness quyết định có thực thi hay không, ghi
observation, và xét điều kiện dừng sau mỗi vòng (slide "Checklist harness chạy
sau mỗi vòng"):

    #  chạy khi nào              kiểm gì                                kết thúc kiểu
    0  trước khi thực thi tool   quyền hạn của hành động sắp làm        Cần con người
    1  sau khi có observation    tiêu chí hoàn thành (bằng code)        Đạt mục tiêu
    2  sau khi có observation    (tool, args) có trùng vòng trước       Phát hiện lặp
    3  sau khi có observation    đại lượng tiến triển có nhúc nhích     Bế tắc
    4  sau khi có observation    vòng · token · thời gian · tiền        Hết ngân sách

Bốn lớp harness mà BTVN yêu cầu nằm ở đây:
    (1) ràng buộc là dữ liệu         -> ``Constraints`` / ``Policy`` / ``Budget`` (constraints.py)
    (2) tiêu chí hoàn thành bằng code -> ``FlightHarness.is_done`` + ``verify_answer``
    (3) kiểm quyền                    -> ``FlightHarness.check_permission`` (+ người duyệt)
    (4) bàn giao                      -> ``Handoff`` (trạng thái · đã thử · câu hỏi cụ thể)
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from pydantic import ValidationError

from .constraints import Budget, Constraints, Policy, fmt_vnd
from .tools_mock import TOOL_SPECS, FlightBackend


# =============================================================================
# Năm kiểu dừng
class StopKind(str, Enum):
    GOAL = "goal_reached"          # 1 · Đạt mục tiêu      -> trả kết quả      (bình thường)
    BUDGET = "budget_exceeded"     # 2 · Hết ngân sách     -> log & báo người  (bất thường)
    LOOP = "loop_detected"         # 3 · Phát hiện lặp     -> log & báo người  (bất thường)
    STALL = "stalled"              # 4 · Bế tắc            -> log & báo người  (bất thường)
    NEEDS_HUMAN = "needs_human"    # 5 · Cần con người     -> chờ phê duyệt    (bình thường)


STOP_LABEL = {
    StopKind.GOAL: "Đạt mục tiêu",
    StopKind.BUDGET: "Hết ngân sách",
    StopKind.LOOP: "Phát hiện lặp",
    StopKind.STALL: "Bế tắc",
    StopKind.NEEDS_HUMAN: "Cần con người",
}
NORMAL_STOPS = {StopKind.GOAL, StopKind.NEEDS_HUMAN}


# =============================================================================
class LoopDetector:
    """Bộ phát hiện lặp (theo slide 'Bộ phát hiện lặp').

    * Trùng action : cùng (tool, args) xuất hiện >= repeat_k lần trong cửa sổ gần.
    * Không tiến triển: đại lượng ``progress`` của bài toán đứng yên qua stall_n vòng.
    Gọi lại get_booking để chờ confirmed là polling hợp lệ nên được miễn.
    """

    def __init__(self, window: int = 6, repeat_k: int = 3, stall_n: int = 5, exempt: tuple[str, ...] = ("get_booking",)):
        self.recent: deque[str] = deque(maxlen=window)
        self.k, self.n, self.last, self.stall = repeat_k, stall_n, None, 0
        self.exempt = exempt

    def check(self, tool: str, args: dict[str, Any], progress: Any) -> str | None:
        verdict = None
        if tool not in self.exempt:
            fp = tool + "|" + repr(sorted(args.items()))
            if self.recent.count(fp) + 1 >= self.k:
                verdict = "LOOP"
            self.recent.append(fp)
        self.stall = self.stall + 1 if progress == self.last else 0
        self.last = progress
        if verdict is None and self.stall >= self.n:
            verdict = "STALL"
        return verdict


# =============================================================================
@dataclass
class ApprovalRequest:
    tool: str
    args: dict[str, Any]
    reasons: list[str]
    flight: dict[str, Any] | None = None

    def question(self) -> str:
        f = self.flight or {}
        what = f"{self.tool}({', '.join(f'{k}={v!r}' for k, v in self.args.items())})"
        extra = f" · chuyến {f.get('flight_no')} {f.get('depart_time', '')} · {fmt_vnd(f.get('price'))}" if f else ""
        return f"Duyệt hành động {what}{extra}? Lý do phải hỏi: {'; '.join(self.reasons)}."


Approver = Callable[[ApprovalRequest], bool]


@dataclass
class Handoff:
    """Bàn giao cho con người: người nhận phải trả lời được trong 30 giây."""

    reason: str
    status: list[str]
    side_effects: list[str]
    tried: list[str]
    question: str
    options: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"=== BÀN GIAO CHO NGƯỜI · {self.reason} ===", "Trạng thái:"]
        lines += [f"  - {s}" for s in self.status] or ["  - (chưa làm gì)"]
        lines.append("Tác dụng phụ đã xảy ra:")
        lines += [f"  - {s}" for s in self.side_effects] or ["  - không có"]
        lines.append("Những gì đã thử:")
        lines += [f"  - {s}" for s in self.tried] or ["  - (không có)"]
        lines.append(f"Câu hỏi cụ thể: {self.question}")
        if self.options:
            lines.append("Lựa chọn: " + " | ".join(self.options))
        return "\n".join(lines)


@dataclass
class Decision:
    kind: str  # allow | deny | ask
    reasons: list[str] = field(default_factory=list)
    hint: str = ""
    flight: dict[str, Any] | None = None


# =============================================================================
FLIGHT_RE = re.compile(r"\b[A-Z]{2}\d{3,4}\b")
CODE_RE = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{4}\b")
PRICE_RE = re.compile(r"(?<![\d.,])\d{1,3}(?:[.,]\d{3}){2,}(?![\d])|(?<![\d.,])\d{6,8}(?![\d])")
SUCCESS_CLAIM_RE = re.compile(r"(đặt vé thành công|đã đặt (xong|thành công|vé)|booked|đã xác nhận vé|hoàn tất đặt vé)", re.I)
NEGATION_RE = re.compile(r"(chưa|không thể|không được) (đặt|hoàn tất)", re.I)


class FlightHarness:
    def __init__(
        self,
        backend: FlightBackend,
        constraints: Constraints,
        policy: Policy | None = None,
        budget: Budget | None = None,
        *,
        approver: Approver | None = None,
        enabled: bool = True,
        loop_detector: LoopDetector | None = None,
        verbose: bool = False,
    ) -> None:
        self.backend = backend
        self.constraints = constraints
        self.policy = policy or Policy()
        self.budget = budget or Budget()
        self.approver = approver
        self.enabled = enabled
        self.loop = loop_detector or LoopDetector()
        self.verbose = verbose
        self.started = time.perf_counter()

        # --- state / trace
        self.events: list[dict[str, Any]] = []
        self.round = 0
        self.tool_calls = 0
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.stop_kind: StopKind | None = None
        self.stop_detail = ""
        self.goal_reached = False
        self.handoff: Handoff | None = None
        self.interventions: dict[str, int] = {}
        self.final_answer = ""

        # --- kiến thức harness tự rút ra từ observation (dùng để kiểm chứng chéo)
        self.seen: dict[str, tuple[int, dict[str, Any]]] = {}       # flight_no -> (leg, flight)
        self.searched_legs: dict[int, dict[str, Any]] = {}            # leg -> observation search cuối
        self.checked: dict[str, dict[str, Any]] = {}                  # flight_no -> check_seat ok
        self.holds: dict[str, dict[str, Any]] = {}                    # booking_code -> info
        self.leg_booking: dict[int, str] = {}                         # leg -> booking_code
        self.paid: set[str] = set()
        self.verified: set[str] = set()
        self.approved_flights: set[str] = set()
        self.failures: list[str] = []
        self.pending_action: ApprovalRequest | None = None

    # ------------------------------------------------------------------ logging
    def _bump(self, key: str) -> None:
        self.interventions[key] = self.interventions.get(key, 0) + 1

    def log(self, kind: str, **data: Any) -> None:
        ev = {"t": round(time.perf_counter() - self.started, 4), "kind": kind, **data}
        self.events.append(ev)
        if self.verbose:
            print(self.format_event(ev))

    @staticmethod
    def _short(obs: dict[str, Any]) -> str:
        st = obs.get("status")
        if st == "ok" and "flights" in obs:
            return f"ok · {obs['count']} chuyến" + (f" · gần nhất {obs.get('nearby_dates')}" if not obs["count"] else "")
        if st == "ok" and "seats_left" in obs:
            return f"ok · {obs['seats_left']} ghế · {fmt_vnd(obs['price'])}"
        if st == "ok" and obs.get("state") == "held":
            return f"ok · {obs['booking_code']} · held · {fmt_vnd(obs.get('price'))}"
        if st == "ok" and "paid" in obs and "state" not in obs:
            return "ok · paid"
        if st == "ok" and "state" in obs:
            return f"{obs['state']} · paid={obs.get('paid')} · {fmt_vnd(obs.get('price'))}"
        rest = {k: v for k, v in obs.items() if k not in ("status",)}
        return f"{st} {json.dumps(rest, ensure_ascii=False)[:150]}"

    def format_event(self, ev: dict[str, Any]) -> str:
        if ev["kind"] == "tool":
            args = ", ".join(repr(v) for v in ev["args"].values())
            return f"V{ev['round']:<3} {ev['tool']}({args})".ljust(52) + f" → {self._short(ev['obs'])}"
        if ev["kind"] == "llm":
            return f"      [model:{ev['role']}] in={ev['tokens_in']} out={ev['tokens_out']}"
        if ev["kind"] == "stop":
            return f"      [HARNESS DỪNG] {ev['label']} · {ev['detail']}"
        return f"      [{ev['kind']}] {json.dumps({k: v for k, v in ev.items() if k not in ('t', 'kind')}, ensure_ascii=False)[:220]}"

    def trace_text(self) -> str:
        return "\n".join(self.format_event(e) for e in self.events)

    # ------------------------------------------------------------------ budget
    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.started

    @property
    def cost_usd(self) -> float:
        return self.budget.cost(self.tokens_in, self.tokens_out)

    def check_budget(self) -> str | None:
        b = self.budget
        if self.tool_calls >= b.max_tool_calls:
            return f"tool_calls {self.tool_calls}/{b.max_tool_calls}"
        if self.llm_calls >= b.max_llm_calls:
            return f"llm_calls {self.llm_calls}/{b.max_llm_calls}"
        if self.tokens_in + self.tokens_out >= b.max_tokens:
            return f"tokens {self.tokens_in + self.tokens_out}/{b.max_tokens}"
        if self.elapsed >= b.max_seconds:
            return f"thời gian {self.elapsed:.1f}s/{b.max_seconds}s"
        if self.cost_usd >= b.max_cost_usd:
            return f"chi phí ${self.cost_usd:.4f}/${b.max_cost_usd}"
        return None

    def record_llm(self, role: str, tokens_in: int, tokens_out: int) -> None:
        """Gọi sau mỗi lần gọi model (mọi design đều báo về đây để đo chi phí)."""
        self.llm_calls += 1
        self.tokens_in += int(tokens_in)
        self.tokens_out += int(tokens_out)
        self.log("llm", role=role, tokens_in=int(tokens_in), tokens_out=int(tokens_out))
        if self.enabled and not self.stopped:
            over = self.check_budget()
            if over and not self.goal_reached:
                self.stop(StopKind.BUDGET, over)

    # ------------------------------------------------------------------ stop
    @property
    def stopped(self) -> bool:
        return self.stop_kind is not None

    def stop(self, kind: StopKind, detail: str, question: str | None = None, options: list[str] | None = None) -> None:
        if self.stopped:
            return
        self.stop_kind, self.stop_detail = kind, detail
        if kind != StopKind.GOAL:
            self._bump(kind.value)
            self.handoff = self.build_handoff(question, options)
        self.log("stop", stop=kind.value, label=STOP_LABEL[kind], detail=detail)

    # ------------------------------------------------------------------ (3) KIỂM QUYỀN
    def check_permission(self, tool: str, args: dict[str, Any]) -> Decision:
        """Chạy TRƯỚC khi thực thi. Trả allow / deny (kèm hint để agent tự sửa) / ask (cần người duyệt)."""
        p, c = self.policy, self.constraints
        if tool not in p.allowlist:
            return Decision("deny", [f"tool '{tool}' không nằm trong allowlist {p.allowlist}"], "Chỉ dùng các tool đã khai báo.")
        if tool in p.read_tools:
            return Decision("allow")

        if tool == "book_seat":
            fno = args["flight_no"]
            if p.require_seen_in_search and fno not in self.seen:
                return Decision("deny", [f"{fno} không có trong observation search nào (nghi bịa đặt)"],
                                "Gọi search_flights và chọn flight_no có thật trong kết quả.")
            leg, flight = self.seen[fno]
            v = c.violations(leg, flight)
            if v:
                return Decision("deny", [f"{fno} vi phạm ràng buộc: " + "; ".join(v)],
                                "Chọn chuyến khác thoả CONSTRAINTS (ngày, khung giờ, giá trần).", flight)
            if p.require_check_before_book and fno not in self.checked:
                return Decision("deny", [f"chưa check_seat thành công cho {fno}"], f"Gọi check_seat('{fno}') trước.", flight)
            if leg in self.leg_booking:
                return Decision("deny", [f"chặng {leg + 1} đã có booking {self.leg_booking[leg]}"], "Không giữ chỗ trùng.", flight)
            if args.get("passenger") != c.passenger:
                return Decision("deny", [f"passenger '{args.get('passenger')}' khác hồ sơ '{c.passenger}'"], f"Dùng passenger='{c.passenger}'.", flight)
            chk = self.checked[fno]
            reasons = []
            if chk["price"] > p.auto_approve_limit:
                reasons.append(f"giá {fmt_vnd(chk['price'])} vượt hạn mức tự duyệt {fmt_vnd(p.auto_approve_limit)}")
            if p.nonrefundable_needs_approval and not chk.get("refundable", True):
                reasons.append("vé không hoàn")
            if reasons and fno not in self.approved_flights:
                return Decision("ask", reasons, flight={**flight, **chk})
            return Decision("allow", flight=flight)

        if tool == "pay":
            code = args["booking_code"]
            if code not in self.holds:
                return Decision("deny", [f"booking {code} không do phiên này giữ chỗ"], "Chỉ thanh toán booking_code trả về từ book_seat.")
            if args.get("method") not in p.allowed_payment_methods or args.get("method") != c.payment_method:
                return Decision("deny", [f"phương thức '{args.get('method')}' không được phép"], f"Dùng method='{c.payment_method}'.")
            h = self.holds[code]
            seen_price = self.checked.get(h["flight_no"], {}).get("price")
            if seen_price is not None and h["price"] != seen_price and h["flight_no"] not in self.approved_flights:
                return Decision("ask", [f"giá giữ chỗ {fmt_vnd(h['price'])} khác giá đã thấy {fmt_vnd(seen_price)}"])
            return Decision("allow")
        return Decision("deny", ["không có luật cho tool này"])

    # ------------------------------------------------------------------ cửa vào duy nhất cho mọi tool call
    def call(self, tool: str, args: dict[str, Any], source: str = "agent") -> dict[str, Any]:
        self.round += 1
        args = dict(args or {})

        if self.enabled and self.stopped and not (self.goal_reached and tool == "get_booking"):
            obs = {"status": "stopped", "reason": f"{STOP_LABEL[self.stop_kind]}: {self.stop_detail}",
                   "hint": "Harness đã dừng vòng lặp. Không gọi thêm tool; hãy trả lời người dùng."}
            self.log("tool", round=self.round, tool=tool, args=args, obs=obs, source=source, executed=False)
            return obs

        # validate tên tool + schema tham số (chặn 'tool không tồn tại' và 'sai định dạng')
        spec = TOOL_SPECS.get(tool)
        if spec is None:
            obs = {"status": "invalid_param", "error": "unknown_tool", "tool": tool, "valid_tools": list(TOOL_SPECS)}
            self._bump("unknown_tool")
            return self._finish_call(tool, args, obs, executed=False, source=source)
        try:
            args = spec[0](**args).model_dump()
        except ValidationError as exc:
            obs = {"status": "invalid_param", "error": exc.errors(include_url=False)[0]["msg"], "hint": "Sửa tham số theo schema."}
            return self._finish_call(tool, args, obs, executed=False, source=source)

        # 0 · kiểm quyền (trước khi thực thi)
        if self.enabled:
            d = self.check_permission(tool, args)
            if d.kind == "deny":
                self._bump("permission_denied")
                obs = {"status": "denied", "reason": "; ".join(d.reasons), "hint": d.hint}
                return self._finish_call(tool, args, obs, executed=False, source=source)
            if d.kind == "ask":
                req = ApprovalRequest(tool, args, d.reasons, d.flight)
                self._bump("approval_requested")
                self.log("approval_request", question=req.question())
                if self.approver is None:
                    self.pending_action = req
                    self.stop(StopKind.NEEDS_HUMAN, "chờ phê duyệt: " + "; ".join(d.reasons),
                              question=req.question(), options=["Duyệt", "Từ chối", "Chọn chuyến khác"])
                    obs = {"status": "pending_approval", "reasons": d.reasons,
                           "hint": "Hành động cần người duyệt. Dừng lại và báo người dùng."}
                    return self._finish_call(tool, args, obs, executed=False, source=source)
                approved = bool(self.approver(req))
                self.log("approval_decision", approved=approved)
                if not approved:
                    self.stop(StopKind.NEEDS_HUMAN, "người duyệt TỪ CHỐI: " + "; ".join(d.reasons),
                              question="Người duyệt đã từ chối. Có muốn tìm phương án khác (nới khung giờ / ngày) không?",
                              options=["Tìm phương án khác", "Huỷ yêu cầu"])
                    obs = {"status": "rejected_by_human", "reasons": d.reasons}
                    return self._finish_call(tool, args, obs, executed=False, source=source)
                if tool == "book_seat":
                    self.approved_flights.add(args["flight_no"])
                elif tool == "pay":
                    self.approved_flights.add(self.holds[args["booking_code"]]["flight_no"])

        obs = getattr(self.backend, tool)(**args)
        return self._finish_call(tool, args, obs, executed=True, source=source)

    def _finish_call(self, tool: str, args: dict, obs: dict, *, executed: bool, source: str) -> dict[str, Any]:
        if executed:
            self.tool_calls += 1
            self._learn(tool, args, obs)
        self.log("tool", round=self.round, tool=tool, args=args, obs=obs, source=source, executed=executed)
        if not self.enabled or self.stopped:
            return obs

        # 1 · tiêu chí hoàn thành (bằng code, không tin lời model)
        done, _ = self.is_done()
        if done:
            self.goal_reached = True
            self.stop(StopKind.GOAL, "is_done() = True (get_booking confirmed · paid · đúng ràng buộc)")
            return {**obs, "harness": "GOAL_REACHED: tiêu chí hoàn thành đã thoả. Không gọi thêm tool, hãy trả lời người dùng."}
        # 2 + 3 · lặp / bế tắc
        verdict = self.loop.check(tool, args, self.progress())
        if verdict == "LOOP":
            self.stop(StopKind.LOOP, f"({tool}, {args}) trùng {self.loop.k} lần trong {self.loop.recent.maxlen} vòng gần nhất")
        elif verdict == "STALL":
            self.stop(StopKind.STALL, f"tiến triển đứng yên {self.loop.n} vòng (progress={self.progress()})")
        # 4 · ngân sách (kiểm CUỐI CÙNG, để không che mất chẩn đoán thật)
        if not self.stopped:
            over = self.check_budget()
            if over:
                self.stop(StopKind.BUDGET, over)
        if self.stopped:
            return {**obs, "harness": f"STOP {self.stop_kind.value}: {self.stop_detail}. Không gọi thêm tool."}
        return obs

    # ------------------------------------------------------------------ cập nhật kiến thức từ observation
    def _learn(self, tool: str, args: dict, obs: dict) -> None:
        st = obs.get("status")
        if tool == "search_flights" and st == "ok":
            leg = self.constraints.leg_of(args["origin"].upper(), args["destination"].upper(), args["date"])
            if leg is not None:
                self.searched_legs[leg] = obs
                for f in obs["flights"]:
                    self.seen[f["flight_no"]] = (leg, f)
        elif tool == "check_seat":
            if st == "ok" and obs["seats_left"] > 0:
                self.checked[args["flight_no"]] = obs
            else:
                self.failures.append(f"check_seat {args['flight_no']}: {obs.get('error') or ('hết chỗ' if st == 'ok' else st)}")
        elif tool == "book_seat":
            if st == "ok":
                leg = self.seen.get(args["flight_no"], (None, None))[0]
                self.holds[obs["booking_code"]] = {"flight_no": args["flight_no"], "leg": leg, "price": obs["price"]}
                if leg is not None:
                    self.leg_booking[leg] = obs["booking_code"]
            else:
                self.failures.append(f"book_seat {args['flight_no']}: {st}")
        elif tool == "pay" and st == "ok":
            self.paid.add(args["booking_code"])
        elif tool == "get_booking" and st == "ok" and obs.get("state") == "confirmed":
            self.verified.add(args["booking_code"])

    def progress(self) -> tuple[int, int, int, int, int]:
        """Đại lượng tiến triển của bài toán (dùng phát hiện bế tắc)."""
        return (len(self.searched_legs), len(self.checked), len(self.leg_booking), len(self.paid), len(self.verified))

    # ------------------------------------------------------------------ (2) TIÊU CHÍ HOÀN THÀNH BẰNG CODE
    def is_done(self) -> tuple[bool, list[str]]:
        """Vị từ chạy bằng code + kiểm chứng chéo, hoàn toàn độc lập với phán đoán của model.

        Với MỌI chặng: get_booking(code).state == "confirmed" and paid == True
                       and đúng chặng/ngày/khung giờ and price <= giá trần
                       and price == giá đã thấy ở check_seat (kiểm chứng chéo).
        """
        missing: list[str] = []
        for i, _leg in enumerate(self.constraints.legs):
            code = self.leg_booking.get(i)
            if code is None:
                missing.append(f"chặng {i + 1}: chưa có booking")
                continue
            b = self.backend.bookings.get(code)  # đọc trực tiếp hệ thống bên ngoài, không tin model
            if b is None:
                missing.append(f"chặng {i + 1}: booking {code} không tồn tại")
                continue
            if not (b["state"] == "confirmed" and b["paid"]):
                missing.append(f"chặng {i + 1}: {code} state={b['state']} paid={b['paid']}")
                continue
            v = self.constraints.violations(i, b)
            if v:
                missing.append(f"chặng {i + 1}: {code} vi phạm {v}")
                continue
            seen_price = self.checked.get(b["flight_no"], {}).get("price")
            if seen_price is not None and seen_price != b["price"] and b["flight_no"] not in self.approved_flights:
                missing.append(f"chặng {i + 1}: giá {b['price']} ≠ giá đã thấy {seen_price}")
        return (not missing, missing)

    def observed_values(self) -> tuple[set[str], set[str], set[int]]:
        flights, codes, prices = set(self.seen), set(self.holds), set()
        for _leg, f in self.seen.values():
            prices.add(int(f["price"]))
        for o in self.checked.values():
            prices.add(int(o["price"]))
        for h in self.holds.values():
            prices.add(int(h["price"]))
        return flights, codes, prices

    def verify_answer(self, text: str) -> list[str]:
        """Đối chiếu câu trả lời cuối với kết quả tool (chặn bịa đặt / tuyên bố sai)."""
        problems: list[str] = []
        flights, codes, prices = self.observed_values()
        for fno in set(FLIGHT_RE.findall(text)):
            if fno not in flights:
                problems.append(f"'{fno}' không có nguồn trong observation")
        for code in set(CODE_RE.findall(text)):
            if code not in codes and not FLIGHT_RE.fullmatch(code) and code not in self.constraints.model_dump_json():
                problems.append(f"mã '{code}' không có nguồn")
        for raw in set(PRICE_RE.findall(text)):
            val = int(re.sub(r"[.,]", "", raw))
            if 100_000 <= val <= 50_000_000 and val not in prices and val != self.constraints.max_price_per_leg \
                    and val != self.policy.auto_approve_limit:
                problems.append(f"giá '{raw}' không có nguồn")
        if SUCCESS_CLAIM_RE.search(text) and not NEGATION_RE.search(text) and not self.is_done()[0]:
            problems.append("tuyên bố đã đặt vé nhưng is_done() = False: " + "; ".join(self.is_done()[1]))
        return problems

    def on_final_answer(self, text: str, *, allow_retry: bool) -> tuple[str, str | None]:
        """Harness xét câu trả lời cuối. Trả (action, feedback): action ∈ {accept, retry, replace}."""
        self.final_answer = text
        if not self.enabled:
            return "accept", None
        problems = self.verify_answer(text)
        if problems:
            self._bump("answer_rejected")
            self.log("answer_check", ok=False, problems=problems)
            fb = ("HARNESS: câu trả lời KHÔNG được chấp nhận vì: " + " | ".join(problems)
                  + ". Chỉ dùng dữ liệu có trong observation; nếu chưa xong thì tiếp tục gọi tool.")
            if allow_retry and not self.stopped:
                return "retry", fb
            if not self.stopped:
                self.stop(StopKind.NEEDS_HUMAN, "câu trả lời của agent không qua kiểm chứng",
                          question="Agent không đưa ra được kết quả kiểm chứng được. Kiểm tra trạng thái ở trên và quyết định bước tiếp theo?")
            return "replace", None
        self.log("answer_check", ok=True)
        if not self.stopped:
            # agent tự dừng khi chưa đạt mục tiêu -> nó đang hỏi người dùng
            self.stop(StopKind.NEEDS_HUMAN, "agent dừng và hỏi người dùng", question=text.strip()[:400])
        return "accept", None

    # ------------------------------------------------------------------ (4) BÀN GIAO
    def build_handoff(self, question: str | None = None, options: list[str] | None = None) -> Handoff:
        status, side, tried = [], [], []
        for i, leg in enumerate(self.constraints.legs):
            obs = self.searched_legs.get(i)
            s = f"chặng {i + 1} ({leg.label()}): "
            if obs is None:
                s += "chưa tìm được kết quả hợp lệ"
            else:
                s += f"tìm thấy {obs['count']} chuyến"
                code = self.leg_booking.get(i)
                if code:
                    h = self.holds[code]
                    s += f"; đã giữ chỗ {h['flight_no']} mã {code}" + (" và đã thanh toán" if code in self.paid else " (chưa thanh toán)")
            status.append(s)
        for code, h in self.holds.items():
            if code in self.paid:
                side.append(f"đã TRỪ TIỀN {fmt_vnd(h['price'])} cho booking {code} ({h['flight_no']})")
            else:
                side.append(f"đang GIỮ CHỖ {code} ({h['flight_no']}, {fmt_vnd(h['price'])}) - chưa thanh toán, cần huỷ nếu không dùng")
        counts: dict[str, int] = {}
        for f in self.failures:
            counts[f] = counts.get(f, 0) + 1
        tried = [f + (f" (×{n})" if n > 1 else "") for f, n in counts.items()]
        denied = [e for e in self.events if e["kind"] == "tool" and e["obs"].get("status") == "denied"]
        for e in denied[-3:]:
            tried.append(f"bị harness chặn {e['tool']}({e['args']}): {e['obs']['reason']}")

        if question is None:
            question = self._default_question()
        return Handoff(reason=f"{STOP_LABEL[self.stop_kind]} - {self.stop_detail}" if self.stop_kind else "",
                       status=status, side_effects=side, tried=tried, question=question,
                       options=options or [])

    def _default_question(self) -> str:
        c = self.constraints
        for i, leg in enumerate(c.legs):
            obs = self.searched_legs.get(i)
            if obs is not None and obs["count"] == 0:
                near = ", ".join(obs.get("nearby_dates", [])) or "không có"
                return (f"Không có chuyến {leg.origin}→{leg.destination} ngày {leg.date}. Ngày gần nhất có chuyến: {near}. "
                        f"Đổi sang ngày đó hay huỷ yêu cầu?")
        sold = [f for f in self.failures if "sold_out" in f]
        if sold:
            alts = []
            for fno, (leg, f) in self.seen.items():
                if c.is_ok(leg, f) and not any(fno in x for x in self.failures):
                    alts.append(f"{fno} {f['depart_time']} {fmt_vnd(f['price'])}")
            alt = ", ".join(alts) or "không còn"
            return (f"{sold[-1].split(':')[0].replace('book_seat ', '')} vừa hết chỗ lúc giữ chỗ nên kế hoạch hết hiệu lực. "
                    f"Ứng viên hợp lệ còn lại: {alt}. Cho phép lập lại kế hoạch với ứng viên này?")
        timeouts = [f for f in self.failures if "timeout" in f]
        if timeouts:
            return f"Dịch vụ check_seat lỗi liên tục ({timeouts[-1]}). Thử lại sau 15 phút, hay cho phép chọn chuyến khác?"
        if self.stop_kind == StopKind.BUDGET:
            return f"Đã chạm trần ngân sách ({self.stop_detail}). Cấp thêm ngân sách để chạy tiếp, hay dừng ở trạng thái hiện tại?"
        return f"Agent dừng ở trạng thái trên ({self.stop_detail}). Nới ràng buộc nào (giờ, giá trần ≤ {fmt_vnd(c.max_price_per_leg)}) hay dừng?"

    # ------------------------------------------------------------------ kết quả
    def summary_answer(self) -> str:
        """Câu trả lời do harness sinh từ dữ liệu đã kiểm chứng (không thể bịa)."""
        done, _ = self.is_done()
        if done:
            parts = []
            for i in range(len(self.constraints.legs)):
                b = self.backend.bookings[self.leg_booking[i]]
                parts.append(f"chặng {i + 1}: {b['flight_no']} {b['origin']}→{b['destination']} {b['date']} {b['depart_time']}, "
                             f"mã {b['booking_code']}, {fmt_vnd(b['price'])}, confirmed & đã thanh toán")
            return "Đặt vé thành công - " + " | ".join(parts)
        return self.handoff.render() if self.handoff else "Chưa hoàn thành."

    def report(self) -> dict[str, Any]:
        done, missing = self.is_done()
        return {
            "stop_kind": self.stop_kind.value if self.stop_kind else None,
            "stop_detail": self.stop_detail,
            "is_done": done,
            "missing": missing,
            "tool_calls": self.tool_calls,
            "llm_calls": self.llm_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_s": round(self.elapsed, 4),
            "interventions": dict(self.interventions),
            "handoff": asdict(self.handoff) if self.handoff else None,
        }

    # ------------------------------------------------------------------ LangChain tools (proxy qua harness)
    def langchain_tools(self) -> list:
        from langchain_core.tools import StructuredTool

        tools = []
        for name, (schema, desc) in TOOL_SPECS.items():
            def _fn(_name=name, **kwargs):
                # observation chuẩn hoá thành JSON (structured output, không phải HTML/văn xuôi)
                return json.dumps(self.call(_name, kwargs, source="react"), ensure_ascii=False)
            tools.append(StructuredTool.from_function(func=_fn, name=name, description=desc, args_schema=schema))
        return tools
