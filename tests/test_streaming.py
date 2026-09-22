"""로컬 HTTP 스텁으로 SSE 점진 수신/실패/일반 JSON 호환을 검증한다."""
import io
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from harness.llm import LLMError, OpenAICompatibleLLM


def frame(content):
    return ("data: " + json.dumps(content, ensure_ascii=False) + "\n\n").encode()


class StreamingTests(unittest.TestCase):
    def test_incremental_http_response(self):
        first_received, release = threading.Event(), threading.Event()
        payloads, events, results, errors = [], [], [], []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payloads.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()
                self.wfile.write(frame({"choices": [{"delta": {
                    "reasoning_content": "INTERNAL_NOT_DISPLAYED", "content": '{"done":true,'}}]}))
                self.wfile.flush()
                release.wait(3)
                self.wfile.write(frame({"choices": [{"delta": {"content": '"answer":"안녕"}'},
                                                        "finish_reason": "stop"}]}))
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = OpenAICompatibleLLM(f"http://127.0.0.1:{server.server_port}/v1", "", 5, 1, 0)
        def callback(kind, value):
            events.append((kind, value))
            first_received.set()
        def request():
            try:
                results.append(client.chat([], "test", stream=True, on_event=callback))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=request)
        worker.start()
        try:
            self.assertTrue(first_received.wait(2))
            self.assertEqual(results, [], "최종 응답 이전에 부분 수신 이벤트가 와야 함")
        finally:
            release.set()
            worker.join(4)
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(payloads[0]["stream"])
        self.assertEqual(json.loads(results[0])["answer"], "안녕")
        self.assertEqual(events[-1][1], len(results[0]))
        self.assertNotIn("INTERNAL_NOT_DISPLAYED", str(events) + str(results))

    def test_incomplete_stream_and_length_limit(self):
        for data in (
            frame({"choices": [{"delta": {"content": "partial"}}]}),
            frame({"choices": [{"delta": {}, "finish_reason": "length"}]}),
            b"data: [DONE]\n\n",
        ):
            with self.subTest(data=data), self.assertRaises(LLMError):
                OpenAICompatibleLLM._read_stream(io.BytesIO(data), None)

    def test_non_stream_server_works_and_retry_is_visible(self):
        class Response(io.BytesIO):
            headers = {"Content-Type": "application/json"}
        body = {"choices": [{"message": {"content": '{"done":true,"answer":"ok"}'}}]}
        response = Response(json.dumps(body).encode())
        events = []
        client = OpenAICompatibleLLM("http://localhost/v1", "", 5, 2, 0)
        with patch("harness.llm.urllib.request.urlopen", side_effect=[OSError("offline"), response]), \
             patch("harness.llm.time.sleep"):
            result = client.chat([], "test", stream=True, on_event=lambda *args: events.append(args))
        self.assertEqual(json.loads(result)["answer"], "ok")
        self.assertEqual(events[0][0], "retry")
        self.assertEqual(events[-1][0], "received")


if __name__ == "__main__":
    unittest.main()
