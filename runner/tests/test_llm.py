import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from dark import config, llm


class Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/models":
            self._send(200, {"data": [{"id": "a"}, {"id": "b"}]})
        elif self.path == "/healthz":
            self._send(200, {"ok": True, "key": True, "models": ["c"]})
        elif self.path == "/healthz-nokey":
            self._send(200, {"ok": True, "key": False, "models": ["c"]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        Handler.calls.append(body)
        model = body.get("model")
        if model == "rate":
            self._send(429, {"error": "rate limit"})
        elif model == "boom":
            self._send(500, {"error": "kaput"})
        elif model == "weird":
            self._send(200, {"nochoices": True})
        else:
            self._send(200, {"choices": [{"message": {"content": "hi", "reasoning": "xx"},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 7, "completion_tokens": 3}})


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def provider(self, **kw):
        d = dict(name="p", url=f"{self.base}/v1", catalog="models", health=None, wake=False, window="")
        d.update(kw)
        return config.Provider(**d)

    def test_served_models_catalog(self):
        self.assertEqual(llm.served_models(self.provider()), {"a", "b"})

    def test_served_models_health(self):
        p = self.provider(catalog=None, health=f"{self.base}/healthz")
        self.assertEqual(llm.served_models(p), {"c"})
        with self.assertRaises(llm.LLMError) as cm:
            llm.served_models(self.provider(catalog=None, health=f"{self.base}/healthz-nokey"))
        self.assertIn("token", str(cm.exception))

    def test_served_models_conn_error(self):
        with self.assertRaises(llm.LLMError) as cm:
            llm.served_models(self.provider(url="http://127.0.0.1:1/v1"))
        self.assertEqual(cm.exception.kind, "conn")

    def test_chat_ok(self):
        content, usage = llm.chat(f"{self.base}/v1", "m", [{"role": "user", "content": "x"}], 64, 5,
                                  temperature=0.1)
        self.assertEqual(content, "hi")
        self.assertEqual((usage["tokens_in"], usage["tokens_out"], usage["reasoning_chars"]), (7, 3, 2))
        self.assertEqual(usage["finish_reason"], "stop")
        self.assertEqual(Handler.calls[-1]["max_tokens"], 64)
        self.assertEqual(Handler.calls[-1]["temperature"], 0.1)

    def test_chat_errors_classified(self):
        for model, kind in (("rate", "rate"), ("boom", "http"), ("weird", "shape")):
            with self.assertRaises(llm.LLMError) as cm:
                llm.chat(f"{self.base}/v1", model, [], 8, 5)
            self.assertEqual(cm.exception.kind, kind, model)
        with self.assertRaises(llm.LLMError) as cm:
            llm.chat("http://127.0.0.1:1/v1", "m", [], 8, 5)
        self.assertEqual(cm.exception.kind, "conn")


if __name__ == "__main__":
    unittest.main()
