"""MockFlightLLM - chat model giả lập để chạy OFFLINE và TÁI LẬP được.

Đây là một ``BaseChatModel`` thật của LangChain (hỗ trợ ``bind_tools`` và
tool_calls), nên ba agent chạy y hệt khi thay bằng model thật (OpenAI, Claude,
Gemini...). Nó đọc ngữ cảnh (CONSTRAINTS + observation) và ra quyết định như một
LLM "khá giỏi", nhưng có thể bị cài NHIỄU theo seed để mô phỏng đúng các failure
mode trong slide "Agent debugging":

    repeat            gọi lại y hệt hành động vừa thất bại  -> Lặp không tiến bộ
    hallucinate       bịa số hiệu chuyến / giá               -> Bịa đặt thông tin
    forget_constraint chọn chuyến rẻ nhất, quên khung giờ    -> Quên yêu cầu
    premature_finish  tuyên bố "đã đặt" khi chưa thanh toán  -> Tiêu chí hoàn thành chủ quan
    bad_format        gửi ngày "07/10/2026"                  -> Sai định dạng tham số
    bad_plan          kế hoạch thiếu bước check_seat/kiểm chứng (planner)

Vai trò được nhận diện qua dòng ``ROLE=...`` trong system prompt:
    react | planner | executor | replanner
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import PrivateAttr

from .constraints import Constraints, fmt_vnd

HALT_STATUSES = {"stopped", "pending_approval", "rejected_by_human"}


def estimate_tokens(text: str) -> int:
    """Ước lượng token ~ 4 ký tự/token (đủ để so sánh tương đối giữa các design)."""
    return max(1, len(text) // 4)


def _block(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.S)
    return m.group(1) if m else None


def _content(m: BaseMessage) -> str:
    c = m.content
    if isinstance(c, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in c)
    return str(c)


# =============================================================================
@dataclass
class Knowledge:
    """Những gì 'model' rút ra được từ observation trong ngữ cảnh."""

    c: Constraints
    events: list[dict[str, Any]]
    search: dict[int, dict] = field(default_factory=dict)
    search_args: dict[int, dict] = field(default_factory=dict)
    last_search_obs: dict[int, dict] = field(default_factory=dict)
    flight_leg: dict[str, int] = field(default_factory=dict)
    flights: dict[str, dict] = field(default_factory=dict)
    checked: dict[str, dict] = field(default_factory=dict)
    timeouts: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    holds: dict[int, tuple[str, str]] = field(default_factory=dict)
    paid: set[str] = field(default_factory=set)
    confirmed: set[str] = field(default_factory=set)
    halted: str | None = None
    goal: bool = False

    def __post_init__(self) -> None:
        for ev in self.events:
            tool, args, obs = ev["tool"], ev.get("args", {}), ev.get("obs", {})
            st = obs.get("status")
            if st in HALT_STATUSES:
                self.halted = obs.get("reason") or obs.get("hint") or st
            h = str(obs.get("harness", ""))
            if h.startswith("GOAL"):
                self.goal = True
            elif h.startswith("STOP"):
                self.halted = h
            if tool == "search_flights":
                leg = self.c.leg_of(str(args.get("origin", "")).upper(), str(args.get("destination", "")).upper(), args.get("date"))
                if leg is None:
                    continue
                self.last_search_obs[leg] = obs
                self.search_args[leg] = args
                if st == "ok":
                    self.search[leg] = obs
                    for f in obs["flights"]:
                        self.flight_leg[f["flight_no"]] = leg
                        self.flights[f["flight_no"]] = f
            elif tool == "check_seat":
                fno = args.get("flight_no")
                if st == "ok":
                    if obs.get("seats_left", 0) > 0:
                        self.checked[fno] = obs
                    else:
                        self.failed[fno] = "hết chỗ"
                elif obs.get("error") == "timeout":
                    self.timeouts[fno] = self.timeouts.get(fno, 0) + 1
                elif st in ("not_found", "denied"):
                    self.failed[fno] = st
            elif tool == "book_seat":
                fno = args.get("flight_no")
                if st == "ok":
                    leg = self.flight_leg.get(fno)
                    if leg is not None:
                        self.holds[leg] = (obs["booking_code"], fno)
                elif st in ("sold_out", "denied", "not_found"):
                    self.failed[fno] = st
            elif tool == "pay" and st == "ok":
                self.paid.add(args.get("booking_code"))
            elif tool == "get_booking" and st == "ok" and obs.get("state") == "confirmed":
                self.confirmed.add(args.get("booking_code"))

    def candidates(self, leg: int, *, ignore_constraints: bool = False, skip_timeouts: int = 2) -> list[dict]:
        obs = self.search.get(leg)
        if not obs:
            return []
        out = []
        for f in obs["flights"]:
            if f["flight_no"] in self.failed or self.timeouts.get(f["flight_no"], 0) >= skip_timeouts:
                continue
            if ignore_constraints or self.c.is_ok(leg, f):
                out.append(f)
        return sorted(out, key=lambda f: f["price"])

    def success_text(self) -> str:
        parts = []
        for i in range(len(self.c.legs)):
            code, fno = self.holds[i]
            f = self.flights.get(fno, {})
            parts.append(f"chặng {i + 1}: {fno} {f.get('origin')}→{f.get('destination')} {f.get('date')} "
                         f"{f.get('depart_time')}, mã {code}, giá {fmt_vnd(self.checked.get(fno, f).get('price'))}")
        return "Đặt vé thành công (harness đã kiểm chứng trạng thái booking) - " + " | ".join(parts)


@dataclass
class Action:
    kind: str  # call | final
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    text: str = ""
    noise: str = ""


# =============================================================================
class MockFlightLLM(BaseChatModel):
    noise: float = 0.0
    seed: int = 0
    _rng: random.Random = PrivateAttr()
    _focus: dict[int, str] = PrivateAttr(default_factory=dict)
    _call_id: int = PrivateAttr(default=0)
    noise_log: list[str] = []

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self._rng = random.Random(self.seed)
        self._focus = {}
        self.noise_log = []

    @property
    def _llm_type(self) -> str:
        return "mock-flight-llm"

    def bind_tools(self, tools: Any, **kwargs: Any):  # noqa: D401 - giống chữ ký của các chat model thật
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    # ------------------------------------------------------------------ noise
    def _noisy(self, kind: str) -> bool:
        if self.noise > 0 and self._rng.random() < self.noise:
            self.noise_log.append(kind)
            return True
        return False

    # ------------------------------------------------------------------ main
    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs: Any) -> ChatResult:
        system = "\n".join(_content(m) for m in messages if isinstance(m, SystemMessage))
        role_m = re.search(r"ROLE=(\w+)", system)
        role = role_m.group(1) if role_m else "react"
        all_text = "\n".join(_content(m) for m in messages)
        constraints = Constraints.model_validate_json(_block(all_text, "CONSTRAINTS"))

        if role == "react":
            events = self._events_from_messages(messages)
            act = self.decide(Knowledge(constraints, events), allow_noise=True)
            msg = self._to_ai_message(act)
        else:
            obs_json = _block(all_text, "OBSERVATIONS")
            events = json.loads(obs_json) if obs_json else []
            k = Knowledge(constraints, events)
            if role == "planner":
                payload = self.plan(constraints)
            elif role == "executor":
                step = json.loads(_block(all_text, "STEP"))
                payload = self.fill(step, k)
            elif role == "replanner":
                payload = self.replan(k)
            else:
                payload = {"error": f"unknown role {role}"}
            msg = AIMessage(content=json.dumps(payload, ensure_ascii=False))

        tools_chars = len(json.dumps(kwargs.get("tools", []), ensure_ascii=False))
        t_in = estimate_tokens(all_text) + tools_chars // 4
        t_out = estimate_tokens(_content(msg) + json.dumps(msg.tool_calls, ensure_ascii=False))
        msg.usage_metadata = {"input_tokens": t_in, "output_tokens": t_out, "total_tokens": t_in + t_out}
        msg.response_metadata = {"model_name": "mock-flight-llm", "role": role}
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _to_ai_message(self, act: Action) -> AIMessage:
        if act.kind == "call":
            self._call_id += 1
            thought = f"Suy luận: cần gọi {act.tool} để tiến tới mục tiêu."
            return AIMessage(content=thought, tool_calls=[{"name": act.tool, "args": act.args, "id": f"call_{self._call_id}", "type": "tool_call"}])
        return AIMessage(content=act.text)

    @staticmethod
    def _events_from_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
        pending: dict[str, tuple[str, dict]] = {}
        events = []
        for m in messages:
            if isinstance(m, AIMessage):
                for tc in m.tool_calls or []:
                    pending[tc["id"]] = (tc["name"], tc["args"])
            elif isinstance(m, ToolMessage):
                name, args = pending.get(m.tool_call_id, (m.name or "", {}))
                try:
                    obs = json.loads(_content(m))
                except json.JSONDecodeError:
                    obs = {"status": "error", "raw": _content(m)}
                events.append({"tool": name, "args": args, "obs": obs})
        return events

    # ================================================================== ReAct
    def decide(self, k: Knowledge, *, allow_noise: bool) -> Action:
        c = k.c
        noisy = self._noisy if allow_noise else (lambda _kind: False)
        if k.halted:
            return Action("final", text=f"Harness đã dừng: {k.halted}. Mình dừng tại đây và báo lại cho bạn.")
        if k.goal:
            if noisy("hallucinate"):
                code, fno = k.holds[0]
                return Action("final", text=f"Đặt vé thành công! Chuyến {fno}, ghế 5C, giá 1.200.000đ, mã {code}.", noise="hallucinate")
            return Action("final", text=k.success_text())

        for i, leg in enumerate(c.legs):
            if i in k.holds:
                code, fno = k.holds[i]
                if code in k.confirmed:
                    continue
                if code in k.paid:
                    return Action("call", "get_booking", {"booking_code": code})
                if noisy("premature_finish"):
                    return Action("final", text=f"Đã đặt vé thành công chuyến {fno}, mã {code}.", noise="premature_finish")
                return Action("call", "pay", {"booking_code": code, "method": c.payment_method})

            # --- cần search
            if i not in k.search:
                last = k.last_search_obs.get(i)
                date = leg.date
                if last is None and noisy("bad_format"):
                    y, m, d = leg.date.split("-")
                    date = f"{d}/{m}/{y}"
                return Action("call", "search_flights", {"origin": leg.origin, "destination": leg.destination, "date": date})
            s = k.search[i]
            if s["count"] == 0:
                if noisy("repeat"):
                    return Action("call", "search_flights", dict(k.search_args[i]), noise="repeat")
                near = ", ".join(s.get("nearby_dates", []))
                return Action("final", text=(f"Không có chuyến {leg.origin}→{leg.destination} ngày {leg.date}. "
                                             f"Ngày gần nhất có chuyến: {near}. Bạn có muốn đổi ngày không? Mình chưa đặt vé nào."))

            # --- chọn ứng viên (có thể 'quên' ràng buộc và bám vào chuyến rẻ nhất)
            focus = self._focus.get(i)
            if focus and (focus in k.failed):
                self._focus.pop(i)
                focus = None
            if focus is None and noisy("forget_constraint"):
                allc = k.candidates(i, ignore_constraints=True)
                if allc:
                    focus = self._focus[i] = allc[0]["flight_no"]
            retry_timeout = noisy("repeat") if any(k.timeouts.values()) else False
            cands = k.candidates(i, skip_timeouts=99 if retry_timeout else 2)
            target = focus or (cands[0]["flight_no"] if cands else None)
            if target is None:
                if any(k.timeouts.values()):
                    return Action("final", text=(f"Dịch vụ check_seat bị timeout liên tục cho chặng {leg.origin}→{leg.destination}. "
                                                 "Mình chưa đặt vé; bạn muốn thử lại sau hay chọn phương án khác?"))
                return Action("final", text=(f"Không có chuyến nào thoả ràng buộc cho chặng {leg.label()}. "
                                             "Mình chưa đặt vé. Bạn muốn nới khung giờ hoặc giá trần không?"))
            if target not in k.checked:
                return Action("call", "check_seat", {"flight_no": target}, noise="repeat" if retry_timeout else "")
            if noisy("hallucinate") and "VN999" not in k.failed:
                return Action("call", "book_seat", {"flight_no": "VN999", "passenger": c.passenger}, noise="hallucinate")
            return Action("call", "book_seat", {"flight_no": target, "passenger": c.passenger})

        return Action("final", text=k.success_text() if len(k.holds) == len(c.legs) else "Đã xong.")

    # ================================================================== Planner
    def plan(self, c: Constraints) -> dict[str, Any]:
        bad = self._noisy("bad_plan")
        steps: list[dict[str, Any]] = []
        for i, leg in enumerate(c.legs):
            n = i + 1
            steps.append({"tool": "search_flights", "leg": n, "args": {"origin": leg.origin, "destination": leg.destination, "date": leg.date},
                          "purpose": f"Tìm chuyến chặng {n}"})
            if not bad:
                steps.append({"tool": "check_seat", "leg": n, "args": {"flight_no": f"?chuyến rẻ nhất thoả ràng buộc chặng {n}"},
                              "purpose": f"Kiểm ghế/giá chuyến rẻ nhất hợp lệ chặng {n}", "foreach": True})
            steps.append({"tool": "book_seat", "leg": n, "args": {"flight_no": f"?chuyến đã check_seat còn ghế chặng {n}", "passenger": c.passenger},
                          "purpose": f"Giữ chỗ chặng {n}"})
            steps.append({"tool": "pay", "leg": n, "args": {"booking_code": f"?booking_code chặng {n}", "method": c.payment_method},
                          "purpose": f"Thanh toán chặng {n}"})
            if not bad:
                steps.append({"tool": "get_booking", "leg": n, "args": {"booking_code": f"?booking_code chặng {n}"},
                              "purpose": f"Kiểm chứng booking chặng {n}"})
        return {"steps": steps, "rationale": "Tìm → kiểm ghế → giữ chỗ → thanh toán → kiểm chứng, lần lượt từng chặng."}

    # ================================================================== Executor (điền tham số cho 1 bước)
    def fill(self, step: dict[str, Any], k: Knowledge) -> dict[str, Any]:
        tool, args = step["tool"], dict(step.get("args", {}))
        leg = int(step.get("leg", 1)) - 1
        if tool == "check_seat":
            if self._noisy("forget_constraint"):
                cands = k.candidates(leg, ignore_constraints=True)
            else:
                cands = k.candidates(leg, skip_timeouts=1)
            if not cands:
                return {"cannot_fill": "không còn ứng viên thoả ràng buộc trong observation"}
            return {"args": {"flight_no": cands[0]["flight_no"]}}
        if tool == "book_seat":
            if self._noisy("hallucinate"):
                return {"args": {"flight_no": "VN999", "passenger": k.c.passenger}}
            ok = [f for f in k.checked if k.flight_leg.get(f) == leg and f not in k.failed]
            if not ok:  # kế hoạch thiếu check_seat -> chọn thẳng từ search
                cands = k.candidates(leg)
                if not cands:
                    return {"cannot_fill": "không có chuyến hợp lệ"}
                ok = [cands[0]["flight_no"]]
            ok.sort(key=lambda f: k.checked.get(f, k.flights.get(f, {})).get("price", 0))
            return {"args": {"flight_no": ok[0], "passenger": k.c.passenger}}
        if tool in ("pay", "get_booking"):
            if leg not in k.holds:
                return {"cannot_fill": f"chưa có booking_code cho chặng {leg + 1}"}
            out = {"booking_code": k.holds[leg][0]}
            if tool == "pay":
                out["method"] = k.c.payment_method
            return {"args": out}
        return {"args": args}

    # ================================================================== Replanner (hybrid)
    def replan(self, k: Knowledge) -> dict[str, Any]:
        c = k.c
        if k.halted:
            return {"action": "finish", "reason": f"harness dừng: {k.halted}"}
        steps: list[dict[str, Any]] = []
        for i, leg in enumerate(c.legs):
            n = i + 1
            if i in k.holds:
                code, _ = k.holds[i]
                if code in k.confirmed:
                    continue
                if code not in k.paid:
                    steps.append({"tool": "pay", "leg": n, "args": {"booking_code": code, "method": c.payment_method}, "purpose": f"Thanh toán chặng {n}"})
                steps.append({"tool": "get_booking", "leg": n, "args": {"booking_code": code}, "purpose": f"Kiểm chứng booking chặng {n}"})
                continue
            if i not in k.search:
                steps.append({"tool": "search_flights", "leg": n, "args": {"origin": leg.origin, "destination": leg.destination, "date": leg.date},
                              "purpose": f"Tìm chuyến chặng {n}"})
                steps += self._tail(c, n)
                continue
            if k.search[i]["count"] == 0:
                if self._noisy("repeat"):
                    return {"action": "continue", "reason": "thử tìm lại", "steps": [
                        {"tool": "search_flights", "leg": n, "args": dict(k.search_args[i]), "purpose": f"Tìm lại chặng {n}"}]}
                near = ", ".join(k.search[i].get("nearby_dates", []))
                return {"action": "finish", "reason": f"không có chuyến ngày {leg.date}; ngày gần nhất {near}; cần người quyết định đổi ngày"}
            retry = self._noisy("repeat")
            cands = k.candidates(i, skip_timeouts=99 if retry else 2)
            if not cands:
                why = "check_seat timeout liên tục" if any(k.timeouts.values()) else "không còn chuyến thoả ràng buộc"
                return {"action": "finish", "reason": f"{why} cho chặng {n}; cần người quyết định"}
            target = cands[0]["flight_no"]
            if self._noisy("forget_constraint"):  # replanner cũng có thể quên ràng buộc
                allc = k.candidates(i, ignore_constraints=True)
                target = allc[0]["flight_no"] if allc else target
            elif self._noisy("hallucinate") and "VN999" not in k.failed:
                target = "VN999"
            if target not in k.checked:
                steps.append({"tool": "check_seat", "leg": n, "args": {"flight_no": target}, "purpose": f"Kiểm ghế {target} (chặng {n})"})
            steps.append({"tool": "book_seat", "leg": n, "args": {"flight_no": target, "passenger": c.passenger}, "purpose": f"Giữ chỗ chặng {n}"})
            steps += self._tail(c, n, after_book=True)
        if not steps:
            return {"action": "finish", "reason": "mọi chặng đã confirmed"}
        return {"action": "continue", "reason": "đổi hướng theo observation mới", "steps": steps}

    @staticmethod
    def _tail(c: Constraints, n: int, after_book: bool = False) -> list[dict[str, Any]]:
        tail = []
        if not after_book:
            tail.append({"tool": "check_seat", "leg": n, "args": {"flight_no": f"?chuyến rẻ nhất thoả ràng buộc chặng {n}"}, "foreach": True,
                         "purpose": f"Kiểm ghế chặng {n}"})
            tail.append({"tool": "book_seat", "leg": n, "args": {"flight_no": f"?chuyến đã check_seat còn ghế chặng {n}", "passenger": c.passenger},
                         "purpose": f"Giữ chỗ chặng {n}"})
        tail.append({"tool": "pay", "leg": n, "args": {"booking_code": f"?booking_code chặng {n}", "method": c.payment_method}, "purpose": f"Thanh toán chặng {n}"})
        tail.append({"tool": "get_booking", "leg": n, "args": {"booking_code": f"?booking_code chặng {n}"}, "purpose": f"Kiểm chứng chặng {n}"})
        return tail
