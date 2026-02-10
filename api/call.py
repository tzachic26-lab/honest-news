import json
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable

from servers.HonestNewsMCPServer import headline_details, latest_headlines, summarize_news_topic


TOOL_MAP: dict[str, Callable[..., Any]] = {
    "latest_headlines": latest_headlines,
    "summarize_news_topic": summarize_news_topic,
    "headline_details": headline_details,
}


class handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"error": "Invalid JSON body"})
            return

        name = payload.get("name")
        args = payload.get("arguments") or {}
        if not name:
            self._send(400, {"error": "Missing tool name"})
            return
        tool = TOOL_MAP.get(str(name))
        if not tool:
            self._send(400, {"error": f"Unknown tool: {name}"})
            return
        if not isinstance(args, dict):
            self._send(400, {"error": "Tool arguments must be an object"})
            return

        try:
            result = tool(**args)
        except Exception as exc:
            self._send(500, {"error": str(exc)})
            return

        self._send(200, {"structuredContent": result})
