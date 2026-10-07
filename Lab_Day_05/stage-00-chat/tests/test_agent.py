"""Agent thật (create_agent + middleware) với mock model: event stream và request snapshot."""

from langchain_core.messages import AIMessage, HumanMessage

from agent import TOOLS, build_agent, system_prompt
from tests.conftest import ScriptedChatModel


def stream_events(agent, messages):
    events, new_messages = [], []
    for mode, chunk in agent.stream({"messages": messages}, stream_mode=["updates", "custom"]):
        if mode == "custom" and chunk.get("observer"):
            events.append(chunk)
        elif mode == "updates":
            for update in chunk.values():
                if isinstance(update, dict) and update.get("messages"):
                    new_messages.extend(update["messages"])
    return events, new_messages


def test_no_tools_registered():
    assert TOOLS == []


def test_single_model_call_without_tool_events():
    model = ScriptedChatModel(responses=[AIMessage(content="Em có thể trò chuyện và giải thích.")])
    events, new_messages = stream_events(build_agent(model), [HumanMessage(content="Em có thể hỗ trợ gì?")])
    assert [e["event"] for e in events] == ["model_request", "model_response"]
    request = events[0]["data"]
    assert request["system_prompt"] == system_prompt()
    assert request["tools"] == []
    assert [m["role"] for m in request["messages"]] == ["user"]
    assert [m.content for m in new_messages] == ["Em có thể trò chuyện và giải thích."]
    assert len(model.received) == 1
