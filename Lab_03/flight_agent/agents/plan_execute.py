"""Mẫu 2 · Plan-then-Execute: gọi model MỘT lần để sinh trọn kế hoạch, duyệt kế
hoạch, rồi thực thi từng bước theo đúng kế hoạch đó (KHÔNG lập lại kế hoạch).

    START → planner → review ──(từ chối, tối đa 2 lần)──► planner
                         │ đồng ý
                         ▼
                      execute ⟲ (từng bước)  ──(bước hỏng)──► finalize (bàn giao)
                         │ hết bước
                         ▼
                      finalize → END

* planner : 1 lần gọi model lớn (ngữ cảnh = yêu cầu + CONSTRAINTS).
* review  : "người duyệt" = harness (allowlist, thứ tự bước, không đổi ngày, ước lượng
            chi phí) + tuỳ chọn người thật duyệt kế hoạch.
* execute : executor "model nhỏ" chỉ điền chỗ trống '?...' từ observation; bước có
            foreach được thử lần lượt ứng viên. Bước hỏng = kế hoạch lỗi thời -> dừng.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from ..harness import FlightHarness, StopKind
from .common import TOOLS_DOC, Plan, execute_step, invoke_json, review_plan

PLANNER_SYSTEM = (
    "Bạn là PLANNER của agent đặt vé. Lập TRỌN kế hoạch một lần, có thứ tự, mỗi bước là một tool call. "
    "Tham số chưa biết lúc lập kế hoạch (flight_no, booking_code) ghi dạng chuỗi bắt đầu bằng '?' kèm mô tả. "
    "Bước check_seat đặt foreach=true để thử lần lượt ứng viên. Với mỗi chặng: search_flights → check_seat → "
    "book_seat → pay → get_booking. Ghi rõ trường leg. Tuân thủ CONSTRAINTS, không đổi ngày.\nTools:\n" + TOOLS_DOC
)


class PEState(TypedDict, total=False):
    plan: list[dict[str, Any]]
    idx: int
    rejections: int
    feedback: str
    status: str
    log: list[str]


def build_plan_execute(model: BaseChatModel, harness: FlightHarness, request: str,
                       plan_approver: Callable[[Plan], bool] | None = None, verbose: bool = False):
    def say(msg: str) -> None:
        if verbose:
            print("   ", msg)

    def planner(state: PEState) -> PEState:
        human = f"Yêu cầu: {request}\n{harness.constraints.to_prompt_block()}"
        if state.get("feedback"):
            human += f"\nKế hoạch trước bị TỪ CHỐI vì: {state['feedback']}. Lập lại."
        plan = invoke_json(model, harness, "planner", PLANNER_SYSTEM, human, Plan)
        say(f"[PLAN] {[s.tool for s in plan.steps]}")
        harness.log("plan", steps=[s.model_dump() for s in plan.steps])
        return {"plan": [s.model_dump() for s in plan.steps], "idx": 0}

    def review(state: PEState) -> PEState:
        plan = Plan(steps=state["plan"])
        problems = review_plan(plan, harness) if harness.enabled else []
        if not problems and plan_approver is not None and not plan_approver(plan):
            problems = ["người duyệt từ chối kế hoạch"]
        harness.log("plan_review", ok=not problems, problems=problems)
        if problems:
            harness._bump("plan_rejected")
            say(f"[REVIEW] từ chối: {problems}")
            return {"status": "rejected", "rejections": state.get("rejections", 0) + 1, "feedback": "; ".join(problems)}
        say("[REVIEW] đồng ý")
        return {"status": "approved"}

    def after_review(state: PEState) -> str:
        if state["status"] == "approved":
            return "execute"
        if harness.stopped:
            return "finalize"
        return "planner" if state.get("rejections", 0) < 3 else "finalize"

    def execute(state: PEState) -> PEState:
        from .common import PlanStep

        step = PlanStep(**state["plan"][state["idx"]])
        res = execute_step(step, model, harness, source="pe", on_event=say)
        if res.halted:
            return {"status": "halted"}
        if not res.ok:
            return {"status": f"plan_failed@{state['idx'] + 1}: {res.reason}"}
        nxt = state["idx"] + 1
        return {"idx": nxt, "status": "done" if nxt >= len(state["plan"]) else "running"}

    def after_execute(state: PEState) -> str:
        return "execute" if state["status"] == "running" and not harness.stopped else "finalize"

    def finalize(state: PEState) -> PEState:
        st = state.get("status", "")
        if harness.enabled and not harness.stopped:
            if harness.is_done()[0]:
                harness.goal_reached = True
                harness.stop(StopKind.GOAL, "is_done() = True")
            elif st == "rejected":
                harness.stop(StopKind.NEEDS_HUMAN, f"kế hoạch bị từ chối {state.get('rejections')} lần: {state.get('feedback')}")
            else:
                harness.stop(StopKind.NEEDS_HUMAN, f"kế hoạch lỗi thời, không lập lại kế hoạch ({st})")
        return {}

    g = StateGraph(PEState)
    g.add_node("planner", planner)
    g.add_node("review", review)
    g.add_node("execute", execute)
    g.add_node("finalize", finalize)
    g.add_edge(START, "planner")
    g.add_edge("planner", "review")
    g.add_conditional_edges("review", after_review, {"execute": "execute", "planner": "planner", "finalize": "finalize"})
    g.add_conditional_edges("execute", after_execute, {"execute": "execute", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


def run_plan_execute(model: BaseChatModel, harness: FlightHarness, request: str, *, plan_approver=None,
                     verbose: bool = False, recursion_limit: int = 80) -> str:
    graph = build_plan_execute(model, harness, request, plan_approver, verbose)
    graph.invoke({"plan": [], "idx": 0, "rejections": 0}, {"recursion_limit": recursion_limit})
    answer = harness.summary_answer() if harness.enabled else _plain_answer(harness)
    harness.final_answer = answer
    return answer


def _plain_answer(harness: FlightHarness) -> str:
    """Không harness: không có kiểm chứng; báo theo observation cuối cùng (có thể sai)."""
    pays = [e for e in harness.events if e["kind"] == "tool" and e["tool"] == "pay" and e["obs"].get("status") == "ok"]
    if pays:
        return f"Đặt vé thành công, mã {', '.join(e['args']['booking_code'] for e in pays)}."
    last = next((e for e in reversed(harness.events) if e["kind"] == "tool"), None)
    return "Không hoàn thành được kế hoạch." + (f" Bước cuối: {json.dumps(last['obs'], ensure_ascii=False)[:120]}" if last else "")
