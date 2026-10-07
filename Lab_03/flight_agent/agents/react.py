"""Mẫu 1 · ReAct: suy luận → hành động → quan sát → suy luận tiếp.

Dựng bằng ``langchain.agents.create_agent`` (API chuẩn của LangChain 1.x). Vòng
lặp model ⇄ tools là của framework; harness cắm vào qua middleware:

    before_model : harness đã dừng (lặp / bế tắc / ngân sách / chờ duyệt) -> nhảy END + bàn giao
    after_model  : ghi token/llm_calls (ngân sách); nếu model thôi gọi tool thì
                   KIỂM CÂU TRẢ LỜI bằng code (is_done + đối chiếu dữ liệu). Sai -> đẩy
                   feedback và nhảy lại model; vẫn sai -> thay bằng câu trả lời của harness.
    tools        : mọi tool là proxy đi qua ``FlightHarness.call`` (kiểm quyền trước khi chạy).
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, hook_config
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage

from ..harness import FlightHarness
from ..mock_llm import estimate_tokens

REACT_SYSTEM = """ROLE=react
Bạn là agent đặt vé máy bay theo mẫu ReAct. Mỗi vòng: suy luận ngắn, gọi đúng MỘT tool, đọc observation rồi quyết định tiếp.
Ràng buộc của người dùng là DỮ LIỆU dưới đây - kiểm lại trước mỗi hành động có tác dụng phụ:
{constraints}
Quy tắc:
- Luôn search_flights trước; chỉ dùng flight_no có trong observation. Không bịa giá, giờ, mã đặt chỗ.
- Chọn chuyến RẺ NHẤT thoả mọi ràng buộc (ngày, khung giờ, giá trần). check_seat trước book_seat.
- Sau book_seat: pay(booking_code, method) rồi get_booking để kiểm chứng.
- Tool trả status=invalid_param/error thì đọc hint để sửa; không gọi lại y hệt quá 1 lần.
- count=0 nghĩa là thật sự không có chuyến: KHÔNG tự đổi ngày, hãy dừng và hỏi người dùng.
- Gặp status=pending_approval/stopped thì dừng gọi tool và báo người dùng.
- Khi xong, trả lời ngắn gọn bằng tiếng Việt, chỉ nêu dữ liệu có trong observation."""


class HarnessMiddleware(AgentMiddleware):
    """Nối FlightHarness vào vòng lặp của create_agent."""

    def __init__(self, harness: FlightHarness, max_answer_retries: int = 2) -> None:
        super().__init__()
        self.harness = harness
        self.max_answer_retries = max_answer_retries
        self.retries = 0

    @hook_config(can_jump_to=["end"])
    def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        h = self.harness
        if h.enabled and h.stopped and not h.goal_reached:
            return {"jump_to": "end", "messages": [AIMessage(content=h.summary_answer())]}
        return None

    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        h = self.harness
        msg = state["messages"][-1]
        usage = getattr(msg, "usage_metadata", None) or {}
        prompt_chars = sum(len(str(m.content)) for m in state["messages"][:-1])
        h.record_llm("react", usage.get("input_tokens") or prompt_chars // 4,
                     usage.get("output_tokens") or estimate_tokens(str(msg.content)))
        if getattr(msg, "tool_calls", None):
            if h.enabled and h.stopped and not h.goal_reached:  # vừa chạm trần ngân sách model
                return {"jump_to": "end", "messages": [AIMessage(content=h.summary_answer())]}
            return None
        # model thôi gọi tool = model TỰ CHO là xong -> harness kiểm bằng code
        action, feedback = h.on_final_answer(str(msg.content), allow_retry=self.retries < self.max_answer_retries)
        if action == "retry":
            self.retries += 1
            return {"jump_to": "model", "messages": [HumanMessage(content=feedback)]}
        if action == "replace":
            return {"messages": [AIMessage(content=h.summary_answer())]}
        return None


def build_react_agent(model: BaseChatModel, harness: FlightHarness):
    middleware: list[Any] = []
    if harness.enabled:
        middleware = [
            HarnessMiddleware(harness),
            # trần cứng của framework (slide demo 01) - lưới an toàn thứ hai sau harness
            ModelCallLimitMiddleware(run_limit=harness.budget.max_llm_calls + 2, exit_behavior="end"),
        ]
    else:
        middleware = [_UsageOnly(harness)]
    return create_agent(
        model=model,
        tools=harness.langchain_tools(),
        system_prompt=REACT_SYSTEM.format(constraints=harness.constraints.to_prompt_block()),
        middleware=middleware,
    )


class _UsageOnly(AgentMiddleware):
    """Chế độ KHÔNG harness (ablation): chỉ đo token, không kiểm gì."""

    def __init__(self, harness: FlightHarness) -> None:
        super().__init__()
        self.harness = harness

    def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
        msg = state["messages"][-1]
        usage = getattr(msg, "usage_metadata", None) or {}
        self.harness.record_llm("react", usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        if not getattr(msg, "tool_calls", None):
            self.harness.final_answer = str(msg.content)
        return None


def run_react(model: BaseChatModel, harness: FlightHarness, request: str, recursion_limit: int = 150) -> str:
    agent = build_react_agent(model, harness)
    result = agent.invoke({"messages": [{"role": "user", "content": request}]}, {"recursion_limit": recursion_limit})
    answer = str(result["messages"][-1].content)
    if harness.enabled and not harness.stopped:
        harness.on_final_answer(answer, allow_retry=False)
    return answer
