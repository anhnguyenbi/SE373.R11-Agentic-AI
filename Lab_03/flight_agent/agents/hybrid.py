"""Mẫu 3 · Lai "ReAct + Plan": lập kế hoạch, thực thi k bước, rồi LẬP LẠI kế hoạch
dựa trên những gì vừa quan sát nếu observation đổi đáng kể.

    START → planner → review → execute ⟲ ─(k bước, không đổi đáng kể)─► execute
                                  │ đổi đáng kể / bước hỏng / hết bước mà chưa xong
                                  ▼
                               replanner (model đọc toàn bộ observation)
                                  │ continue            │ finish
                                  └──► execute          ▼
                                                     finalize → END

"Đổi đáng kể" được phát hiện bằng CODE (rẻ, xác định): bước hỏng (hết chỗ, timeout,
bị từ chối), search rỗng, hoặc giá ở check_seat/book_seat khác giá đã thấy ở search.
Chỉ khi đó mới tốn một lần gọi replanner; còn lại chạy như plan-then-execute.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from ..harness import FlightHarness, StopKind
from .common import TOOLS_DOC, Plan, PlanStep, ReplanDecision, execute_step, invoke_json, observations, review_plan
from .plan_execute import PLANNER_SYSTEM, _plain_answer

REPLANNER_SYSTEM = (
    "Bạn là REPLANNER của agent đặt vé (mẫu lai). Đọc CONSTRAINTS và toàn bộ OBSERVATIONS. "
    "Nếu còn cách đạt mục tiêu mà không vi phạm ràng buộc: action=continue và trả các bước CÒN LẠI (đã điền "
    "flight_no/booking_code cụ thể nếu biết). Không lặp lại hành động vừa thất bại. Nếu cần người quyết định "
    "(không có chuyến, công cụ lỗi liên tục, cần đổi ràng buộc): action=finish kèm reason.\nTools:\n" + TOOLS_DOC
)


class HybridState(TypedDict, total=False):
    plan: list[dict[str, Any]]
    idx: int
    since_check: int
    replans: int
    rejections: int
    feedback: str
    status: str
    trigger: str


def significant_change(step: PlanStep, res_obs: dict[str, Any] | None, ok: bool, harness: FlightHarness) -> str | None:
    if not ok:
        return f"bước {step.tool} không thành công ({(res_obs or {}).get('status')})"
    obs = res_obs or {}
    if step.tool in ("check_seat", "book_seat"):
        fno = obs.get("flight_no")
        seen = harness.seen.get(fno, (None, {}))[1].get("price") if fno else None
        if seen is not None and obs.get("price") not in (None, seen):
            return f"giá {fno} đổi từ {seen} sang {obs.get('price')}"
    return None


def build_hybrid(model: BaseChatModel, harness: FlightHarness, request: str, *, k: int = 2, max_replans: int = 3,
                 plan_approver: Callable[[Plan], bool] | None = None, verbose: bool = False):
    def say(msg: str) -> None:
        if verbose:
            print("   ", msg)

    def planner(state: HybridState) -> HybridState:
        human = f"Yêu cầu: {request}\n{harness.constraints.to_prompt_block()}"
        if state.get("feedback"):
            human += f"\nKế hoạch trước bị TỪ CHỐI vì: {state['feedback']}. Lập lại."
        plan = invoke_json(model, harness, "planner", PLANNER_SYSTEM, human, Plan)
        say(f"[PLAN] {[s.tool for s in plan.steps]}")
        harness.log("plan", steps=[s.model_dump() for s in plan.steps])
        return {"plan": [s.model_dump() for s in plan.steps], "idx": 0, "since_check": 0}

    def review(state: HybridState) -> HybridState:
        plan = Plan(steps=state["plan"])
        problems = review_plan(plan, harness) if harness.enabled else []
        if not problems and plan_approver is not None and not plan_approver(plan):
            problems = ["người duyệt từ chối kế hoạch"]
        harness.log("plan_review", ok=not problems, problems=problems)
        if problems:
            harness._bump("plan_rejected")
            return {"status": "rejected", "rejections": state.get("rejections", 0) + 1, "feedback": "; ".join(problems)}
        return {"status": "approved"}

    def after_review(state: HybridState) -> str:
        if state["status"] == "approved":
            return "execute"
        return "planner" if state.get("rejections", 0) < 3 and not harness.stopped else "finalize"

    def execute(state: HybridState) -> HybridState:
        step = PlanStep(**state["plan"][state["idx"]])
        res = execute_step(step, model, harness, source="hybrid", on_event=say)
        if res.halted:
            return {"status": "halted"}
        change = significant_change(step, res.obs, res.ok, harness)
        if change:
            say(f"[CHECK] observation đổi đáng kể: {change}")
            return {"status": "replan", "trigger": change}
        nxt, since = state["idx"] + 1, state.get("since_check", 0) + 1
        if nxt >= len(state["plan"]):
            return {"idx": nxt, "status": "plan_end"}
        if since >= k:
            say(f"[CHECK] sau {k} bước: không đổi đáng kể -> thực thi tiếp")
            since = 0
        return {"idx": nxt, "since_check": since, "status": "running"}

    def after_execute(state: HybridState) -> str:
        if harness.stopped or state["status"] == "halted":
            return "finalize"
        if state["status"] == "plan_end":
            return "finalize" if (harness.is_done()[0] or not harness.enabled) else "replanner"
        return "replanner" if state["status"] == "replan" else "execute"

    def replanner(state: HybridState) -> HybridState:
        if state.get("replans", 0) >= max_replans:
            return {"status": f"quá {max_replans} lần lập lại kế hoạch"}
        human = (f"Yêu cầu: {request}\n{harness.constraints.to_prompt_block()}\nLý do lập lại: {state.get('trigger', 'hết bước')}\n"
                 f"<OBSERVATIONS>{json.dumps(observations(harness), ensure_ascii=False)}</OBSERVATIONS>")
        d = invoke_json(model, harness, "replanner", REPLANNER_SYSTEM, human, ReplanDecision)
        harness.log("replan", action=d.action, reason=d.reason, steps=[s.tool for s in d.steps])
        say(f"[REPLAN] {d.action}: {d.reason} {[s.tool for s in d.steps]}")
        if d.action != "continue" or not d.steps:
            return {"status": f"replanner kết thúc: {d.reason}", "replans": state.get("replans", 0) + 1}
        return {"plan": [s.model_dump() for s in d.steps], "idx": 0, "since_check": 0, "status": "running",
                "replans": state.get("replans", 0) + 1}

    def after_replan(state: HybridState) -> str:
        return "execute" if state["status"] == "running" and not harness.stopped else "finalize"

    def finalize(state: HybridState) -> HybridState:
        if harness.enabled and not harness.stopped:
            if harness.is_done()[0]:
                harness.goal_reached = True
                harness.stop(StopKind.GOAL, "is_done() = True")
            else:
                harness.stop(StopKind.NEEDS_HUMAN, state.get("status", "dừng"))
        return {}

    g = StateGraph(HybridState)
    for name, fn in (("planner", planner), ("review", review), ("execute", execute), ("replanner", replanner), ("finalize", finalize)):
        g.add_node(name, fn)
    g.add_edge(START, "planner")
    g.add_edge("planner", "review")
    g.add_conditional_edges("review", after_review, {"execute": "execute", "planner": "planner", "finalize": "finalize"})
    g.add_conditional_edges("execute", after_execute, {"execute": "execute", "replanner": "replanner", "finalize": "finalize"})
    g.add_conditional_edges("replanner", after_replan, {"execute": "execute", "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


def run_hybrid(model: BaseChatModel, harness: FlightHarness, request: str, *, k: int = 2, plan_approver=None,
               verbose: bool = False, recursion_limit: int = 100) -> str:
    graph = build_hybrid(model, harness, request, k=k, plan_approver=plan_approver, verbose=verbose)
    graph.invoke({"plan": [], "idx": 0, "replans": 0, "rejections": 0}, {"recursion_limit": recursion_limit})
    answer = harness.summary_answer() if harness.enabled else _plain_answer(harness)
    harness.final_answer = answer
    return answer
