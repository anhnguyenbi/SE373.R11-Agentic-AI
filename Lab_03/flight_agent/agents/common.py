"""Phần dùng chung cho Plan-then-Execute và Lai: schema kế hoạch, gọi model ra
JSON có kiểm Pydantic, duyệt kế hoạch (harness) và executor từng bước."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from ..harness import FlightHarness
from ..mock_llm import HALT_STATUSES, estimate_tokens
from ..tools_mock import TOOL_SPECS


# ----------------------------------------------------------------------------- schema
class PlanStep(BaseModel):
    tool: str = Field(description="Tên tool")
    leg: int = Field(default=1, description="Số thứ tự chặng (1-based) mà bước này phục vụ")
    args: dict[str, Any] = Field(description="Tham số; giá trị bắt đầu bằng '?' là chỗ trống executor điền từ observation")
    purpose: str = ""
    foreach: bool = Field(default=False, description="True: thử lần lượt ứng viên tới khi thành công (tối đa 3)")


class Plan(BaseModel):
    steps: list[PlanStep]
    rationale: str = ""


class FillResult(BaseModel):
    args: dict[str, Any] | None = None
    cannot_fill: str | None = None


class ReplanDecision(BaseModel):
    action: str = Field(description="continue | finish")
    reason: str = ""
    steps: list[PlanStep] = Field(default_factory=list)


TOOLS_DOC = "\n".join(f"- {n}({', '.join(s.model_fields)}): {d}" for n, (s, d) in TOOL_SPECS.items())


def _text(content: Any) -> str:
    if isinstance(content, list):
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content)


def invoke_json(model: BaseChatModel, harness: FlightHarness, role: str, system: str, human: str,
                schema: type[BaseModel], retries: int = 1) -> BaseModel:
    """Gọi model, ép ra JSON đúng schema (structured output) và báo token cho harness."""
    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    sys = (f"ROLE={role}\n{system}\nChỉ trả về DUY NHẤT một JSON object hợp lệ, không markdown, "
           f"theo JSON Schema: {schema_json}")
    messages = [SystemMessage(content=sys), HumanMessage(content=human)]
    last_err = ""
    for _ in range(retries + 1):
        resp = model.invoke(messages)
        usage = getattr(resp, "usage_metadata", None) or {}
        text = _text(resp.content).strip()
        harness.record_llm(role, usage.get("input_tokens") or estimate_tokens(sys + human),
                           usage.get("output_tokens") or estimate_tokens(text))
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        m = re.search(r"\{.*\}", cleaned, flags=re.S)
        try:
            return schema.model_validate_json(m.group(0) if m else cleaned)
        except (ValidationError, AttributeError) as exc:
            last_err = str(exc)[:300]
            messages.append(HumanMessage(content=f"JSON không hợp lệ: {last_err}. Trả lại đúng schema."))
    raise RuntimeError(f"{role}: model không trả JSON đúng schema ({last_err})")


def observations(harness: FlightHarness, compact: bool = True) -> list[dict[str, Any]]:
    """Lịch sử tool (đã thực thi hoặc bị chặn) để đưa vào ngữ cảnh."""
    out = []
    for e in harness.events:
        if e["kind"] != "tool":
            continue
        obs = e["obs"]
        if compact and obs.get("status") == "ok" and "flights" in obs:
            obs = {**obs, "flights": [{k: f[k] for k in ("flight_no", "date", "depart_time", "price", "refundable", "origin", "destination")}
                                      for f in obs["flights"]]}
        out.append({"tool": e["tool"], "args": e["args"], "obs": obs})
    return out


# ----------------------------------------------------------------------------- duyệt kế hoạch (harness)
def review_plan(plan: Plan, harness: FlightHarness) -> list[str]:
    """'Người duyệt' tự động cho kế hoạch: chạy TRƯỚC khi có tác dụng phụ.

    Ưu thế quyết định của plan-then-execute: kế hoạch nhìn thấy được trước khi chạy,
    nên duyệt được và ước lượng chi phí được.
    """
    p, c, b = harness.policy, harness.constraints, harness.budget
    problems: list[str] = []
    for s in plan.steps:
        if s.tool not in p.allowlist:
            problems.append(f"tool '{s.tool}' ngoài allowlist")
    for i, leg in enumerate(c.legs):
        n = i + 1
        seq = [s.tool for s in plan.steps if s.leg == n]
        need = ["search_flights", "check_seat", "book_seat", "pay", "get_booking"] if p.require_check_before_book else \
               ["search_flights", "book_seat", "pay", "get_booking"]
        it = iter(seq)
        if not all(t in it for t in need):
            problems.append(f"chặng {n}: thiếu/sai thứ tự bước, cần {' → '.join(need)}, kế hoạch có {seq}")
        for s in plan.steps:
            if s.tool == "search_flights" and s.args.get("origin") == leg.origin and s.args.get("destination") == leg.destination \
                    and s.args.get("date") not in (leg.date, None) and not c.allow_change_date:
                problems.append(f"chặng {n}: kế hoạch tự đổi ngày {s.args.get('date')} ≠ {leg.date}")
    est_calls = len(plan.steps) + sum(2 for s in plan.steps if s.foreach)
    if est_calls > b.max_tool_calls:
        problems.append(f"ước lượng {est_calls} tool call > ngân sách {b.max_tool_calls}")
    return problems


# ----------------------------------------------------------------------------- executor
@dataclass
class StepResult:
    ok: bool
    halted: bool = False
    reason: str = ""
    obs: dict[str, Any] | None = None


def step_ok(tool: str, obs: dict[str, Any]) -> bool:
    if obs.get("status") != "ok":
        return False
    if tool == "search_flights":
        return obs.get("count", 0) > 0
    if tool == "check_seat":
        return obs.get("seats_left", 0) > 0
    return True


EXECUTOR_SYSTEM = (
    "Bạn là EXECUTOR (model nhỏ) của agent đặt vé. Chỉ điền tham số cụ thể cho MỘT bước của kế hoạch, "
    "lấy giá trị từ OBSERVATIONS; không lập kế hoạch mới, không bịa flight_no/booking_code. "
    "Tuân thủ CONSTRAINTS. Nếu không điền được thì trả cannot_fill kèm lý do.\nTools:\n" + TOOLS_DOC
)


def execute_step(step: PlanStep, model: BaseChatModel, harness: FlightHarness, source: str,
                 on_event: Callable[[str], None] | None = None) -> StepResult:
    tries = 3 if step.foreach else 2
    last: dict[str, Any] | None = None
    for attempt in range(tries):
        args = dict(step.args)
        if any(isinstance(v, str) and v.startswith("?") for v in args.values()):
            human = (f"{harness.constraints.to_prompt_block()}\n<STEP>{step.model_dump_json()}</STEP>\n"
                     f"<OBSERVATIONS>{json.dumps(observations(harness), ensure_ascii=False)}</OBSERVATIONS>")
            fill = invoke_json(model, harness, "executor", EXECUTOR_SYSTEM, human, FillResult)
            if fill.cannot_fill or not fill.args:
                return StepResult(False, reason=f"executor không điền được: {fill.cannot_fill}", obs=last)
            args = fill.args
        obs = harness.call(step.tool, args, source=source)
        last = obs
        if on_event:
            on_event(f"{step.tool}({args}) -> {obs.get('status')}")
        if obs.get("status") in HALT_STATUSES or (harness.enabled and harness.stopped and not harness.goal_reached):
            return StepResult(False, halted=True, reason=obs.get("reason", "harness dừng"), obs=obs)
        if step_ok(step.tool, obs):
            return StepResult(True, obs=obs)
        transient = obs.get("error") == "timeout"
        if not step.foreach and not (transient and attempt == 0):
            break
    st = (last or {}).get("status")
    why = (last or {}).get("error") or ("0 ghế" if st == "ok" and step.tool == "check_seat" else "count=0" if st == "ok" else st)
    return StepResult(False, reason=f"bước {step.tool}({', '.join(map(str, args.values()))}) → {why}", obs=last)
