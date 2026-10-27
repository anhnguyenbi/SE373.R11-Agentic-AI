"""Kiểm thử đường gọi model thật bằng một server OpenAI-compatible giả chạy local."""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.llm import OpenAIChat, Settings, Tool, run_agent  # noqa: E402


class FakeProvider(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeProvider.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        has_tool_result = any(m["role"] == "tool" for m in body["messages"])
        if has_tool_result:
            message = {"role": "assistant", "content": "Kết quả: 5"}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}]}
        payload = {"choices": [{"message": message}],
                   "usage": {"prompt_tokens": 100 + len(body["messages"]), "completion_tokens": 7}}
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class OpenAIChatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeProvider)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_tool_loop_and_usage_from_provider(self):
        FakeProvider.requests.clear()
        llm = OpenAIChat(Settings("sk-test", "fake-model", f"http://127.0.0.1:{self.server.server_port}/v1"))
        add = Tool("add", "cộng", {"type": "object", "properties": {}}, lambda a, b: str(a + b))
        run = run_agent(llm, [{"role": "user", "content": "2+3?"}], [add])
        self.assertEqual(run.answer, "Kết quả: 5")
        self.assertEqual(run.usage.calls, 2)
        self.assertEqual(run.usage.input_tokens, (100 + 1) + (100 + 3))  # lấy đúng từ usage.prompt_tokens
        self.assertFalse(run.usage.estimated)
        self.assertEqual(FakeProvider.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual(FakeProvider.requests[0]["auth"], "Bearer sk-test")
        self.assertEqual(FakeProvider.requests[1]["body"]["messages"][-1],
                         {"role": "tool", "tool_call_id": "c1", "content": "5"})


if __name__ == "__main__":
    unittest.main()
