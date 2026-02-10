import asyncio
import os
import subprocess
import sys
import types
from typing import Any

import httpx
from dotenv import load_dotenv
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent


def _ensure_langchain_content_shim() -> None:
    if "langchain_core.messages.content" in sys.modules:
        return

    module = types.ModuleType("langchain_core.messages.content")

    def create_text_block(text: str) -> dict[str, Any]:
        return {"type": "text", "text": text}

    def create_image_block(
        *, base64: str | None = None, url: str | None = None, mime_type: str | None = None
    ) -> dict[str, Any]:
        block: dict[str, Any] = {"type": "image"}
        if url:
            block["url"] = url
        if base64:
            block["base64"] = base64
        if mime_type:
            block["mime_type"] = mime_type
        return block

    def create_file_block(
        *, base64: str | None = None, url: str | None = None, mime_type: str | None = None
    ) -> dict[str, Any]:
        block: dict[str, Any] = {"type": "file"}
        if url:
            block["url"] = url
        if base64:
            block["base64"] = base64
        if mime_type:
            block["mime_type"] = mime_type
        return block

    module.TextContentBlock = dict
    module.ImageContentBlock = dict
    module.FileContentBlock = dict
    module.create_text_block = create_text_block
    module.create_image_block = create_image_block
    module.create_file_block = create_file_block

    sys.modules["langchain_core.messages.content"] = module


def _extract_assistant_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message.content
    return ""


async def _wait_for_tcp(host: str, port: int, *, attempts: int = 20, delay: float = 0.25) -> None:
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            await writer.wait_closed()
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(delay)
    if last_error:
        raise last_error


async def run_multiplex_llm_client() -> None:
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set in the environment.")

    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    system_prompt = os.getenv(
        "SYSTEM_PROMPT",
        "You are a helpful assistant. Use tools when they help. Keep responses concise.",
    )

    _ensure_langchain_content_shim()
    from langchain_mcp_adapters.client import MultiServerMCPClient

    llm = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        http_client=httpx.Client(verify=False),
        http_async_client=httpx.AsyncClient(verify=False),
    )

    math_server_path = os.path.join(os.path.dirname(__file__), "servers", "math_server.py")
    news_server_path = os.path.join(os.path.dirname(__file__), "servers", "HonestNewsMCPServer.py")

    news_process = subprocess.Popen(
        [sys.executable, news_server_path, "--server"],
        cwd=os.path.dirname(news_server_path),
    )

    try:
        client = MultiServerMCPClient(
            {
                "math": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [math_server_path],
                },
                "news": {
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [news_server_path],
                },
            }
        )
        tools = await client.get_tools()
        agent = create_react_agent(llm, tools, prompt=system_prompt)

        if len(sys.argv) > 1:
            user_input = " ".join(sys.argv[1:]).strip()
            result = await agent.ainvoke({"messages": [HumanMessage(content=user_input)]})
            print(_extract_assistant_text(result["messages"]))
            return

        print(f"LangChain multi-server client using {model_name}. Type 'exit' to quit.")
        messages: list[BaseMessage] = []
        while True:
            user_input = input("> ").strip()
            if not user_input:
                continue
            if user_input.lower() in {"exit", "quit"}:
                break
            messages.append(HumanMessage(content=user_input))
            result = await agent.ainvoke({"messages": messages})
            messages = result["messages"]
            print(_extract_assistant_text(messages))
    finally:
        news_process.terminate()
        news_process.wait(timeout=5)


if __name__ == "__main__":

    asyncio.run(run_multiplex_llm_client())
