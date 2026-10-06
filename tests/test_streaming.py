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
        self.assertEqual(json.loads(results[0].content)["answer"], "안녕")
        self.assertEqual(events[-1][1], len(results[0].content))
        self.assertNotIn("INTERNAL_NOT_DISPLAYED", str(events) + str(results))

    def test_incomplete_stream_and_length_limit(self):
        for data in (
            frame({"choices": [{"delta": {"content": "partial"}}]}),
            frame({"choices": [{"delta": {}, "finish_reason": "length"}]}),
            b"data: [DONE]\n\n",
        ):
            with self.subTest(data=data), self.assertRaises(LLMError):
                OpenAICompatibleLLM._read_stream(io.BytesIO(data), None)

    def test_finish_reason_ends_stream_without_done_marker(self):
        """finish_reason이 오면 [DONE] 없이도 끝나야 한다.

        이전에는 finished=True만 찍고 루프를 계속해, 서버가 연결을 열어둔 채
        (HTTP keep-alive) 아무것도 안 보내면 readline()에서 영영 막혔다.
        """
        payload = (frame({"choices": [{"delta": {"content": '{"done":true,'}}]})
                   + frame({"choices": [{"delta": {"content": '"answer":"ok"}'},
                                         "finish_reason": "stop"}]}))
        result = []

        def read():
            result.append(OpenAICompatibleLLM._read_stream(io.BytesIO(payload), None))

        worker = threading.Thread(target=read, daemon=True)
        worker.start()
        worker.join(3)
        self.assertFalse(worker.is_alive(),
                         "finish_reason 이후에도 스트림 루프가 종료되지 않음")
        self.assertEqual(json.loads(result[0].content)["answer"], "ok")

    def test_keep_alive_server_does_not_hang(self):
        """[DONE]도 finish_reason도 없이 연결만 열어두는 서버는 수신 상한에 걸려야 한다."""
        class KeepAliveSocket(io.BytesIO):
            def __init__(self):
                super().__init__(b"")
                self.reads = 0

            def readline(self, limit=-1):
                self.reads += 1
                return b": keep-alive\n\n"

        socket = KeepAliveSocket()
        with self.assertRaises(LLMError):
            OpenAICompatibleLLM._read_stream(socket, None)
        # 4MB 수신 상한(llm.py)으로 끊겨야 한다. 상한이 없으면 영영 못 끝난다.
        self.assertLessEqual(socket.reads * 13, 4_000_001)

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
        self.assertEqual(json.loads(result.content)["answer"], "ok")
        self.assertEqual(events[0][0], "retry")
        self.assertEqual(events[-1][0], "received")


if __name__ == "__main__":
    unittest.main()
