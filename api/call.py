import json
import logging
import os
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    from servers.HonestNewsMCPServer import headline_details, latest_headlines, summarize_news_topic
    TAVILY_AVAILABLE = os.getenv("TAVILY_API_KEY") is not None
    logger.info(f"Tavily API key configured: {TAVILY_AVAILABLE}")
except ImportError as exc:
    logger.error(f"Failed to import from servers.HonestNewsMCPServer: {exc}")
    headline_details = None
    latest_headlines = None
    summarize_news_topic = None
    TAVILY_AVAILABLE = False

TOOL_MAP: dict[str, Callable[..., Any]] = {}
if headline_details:
    TOOL_MAP["headline_details"] = headline_details
if latest_headlines:
    TOOL_MAP["latest_headlines"] = latest_headlines
if summarize_news_topic:
    TOOL_MAP["summarize_news_topic"] = summarize_news_topic


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
        logger.info(f"Received request: name={name}, args={args}")
        
        if not name:
            self._send(400, {"error": "Missing tool name"})
            return
        
        tool = TOOL_MAP.get(str(name))
        if not tool:
            logger.error(f"Unknown tool: {name}")
            self._send(400, {"error": f"Unknown tool: {name}"})
            return
            
        if not isinstance(args, dict):
            self._send(400, {"error": "Tool arguments must be an object"})
            return

        try:
            logger.info(f"Calling tool: {name}")
            result = tool(**args)
            logger.info(f"Tool {name} succeeded")
        except Exception as exc:
            logger.error(f"Tool {name} failed: {exc}")
            self._send(500, {"error": str(exc)})
            return

        self._send(200, {"structuredContent": result})
