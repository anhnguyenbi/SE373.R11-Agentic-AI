"""Kiểm thử đường gọi model thật bằng một server OpenAI-compatible giả chạy local."""

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.llm import (OpenAIChat, ProviderError, Settings, Tool, Usage, parse_response_body,  # noqa: E402
                        pick_token_source, run_agent)


class FakeProvider(BaseHTTPRequestHandler):
    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body["model"] == "down-model":
            data = b'{"error":{"message":"No active connection for model, key sk-test"}}'
            self.send_response(503)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        FakeProvider.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        has_tool_result = any(m["role"] == "tool" for m in body["messages"])
        if has_tool_result:
            message = {"role": "assistant", "content": "Kết quả: 5"}
        else:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}]}
        prompt_tokens = 2000 if body["model"] == "cache-model" else 100 + len(body["messages"])
        payload = {"choices": [{"message": message}],
                   "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 7}}
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

    def test_http_error_is_explained_and_key_redacted(self):
        llm = OpenAIChat(Settings("sk-test", "down-model", f"http://127.0.0.1:{self.server.server_port}/v1"))
        with self.assertRaises(ProviderError) as ctx:
            llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(ctx.exception.status, 503)
        self.assertIn("No active connection", str(ctx.exception))
        self.assertNotIn("sk-test", str(ctx.exception))

    def test_undercounted_usage_falls_back_to_client_estimate(self):
        llm = OpenAIChat(Settings("sk-test", "cache-model", f"http://127.0.0.1:{self.server.server_port}/v1"))
        big = llm.chat([{"role": "user", "content": "x " * 40000}]).usage   # ~20k token nhưng router báo 2000
        small = llm.chat([{"role": "user", "content": "hi"}]).usage
        self.assertFalse(big.provider_reliable)
        self.assertTrue(small.provider_reliable)
        tokens, source = pick_token_source({"big": big, "small": small})
        self.assertIn("client", source)
        self.assertGreater(tokens["big"], 10 * tokens["small"])


class ParseBodyTest(unittest.TestCase):
    def test_sse_stream_is_assembled(self):
        chunks = [
            {"choices": [{"delta": {"role": "assistant", "tool_calls": [
                {"index": 0, "id": "c1", "function": {"name": "add", "arguments": "{\"a\": 2"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ", \"b\": 3}"}}]}}]},
            {"choices": [{"delta": {"content": "Xong"}}]},
            {"choices": [], "usage": {"prompt_tokens": 55, "completion_tokens": 4}},
        ]
        text = "\n\n".join("data: " + json.dumps(c) for c in chunks) + "\n\ndata: [DONE]\n"
        data = parse_response_body(text)
        msg = data["choices"][0]["message"]
        self.assertEqual(msg["content"], "Xong")
        self.assertEqual(msg["tool_calls"][0]["function"], {"name": "add", "arguments": '{"a": 2, "b": 3}'})
        self.assertEqual(data["usage"]["prompt_tokens"], 55)

    def test_empty_and_non_json_bodies_raise_clear_errors(self):
        with self.assertRaises(ProviderError):
            parse_response_body("   ")
        with self.assertRaises(ProviderError) as ctx:
            parse_response_body("<html>oops</html>")
        self.assertIn("<html>", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
