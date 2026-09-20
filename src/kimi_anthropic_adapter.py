import json
import logging
import os
import uuid

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


app = FastAPI()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("kimi-adapter")
UPSTREAM = os.environ.get("KIMI_UPSTREAM", "http://127.0.0.1:4000")
UPSTREAM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
INCOMPATIBLE_TOOLS = {
    "Artifact", "AskUserQuestion", "EndConversation", "EnterPlanMode",
    "ExitPlanMode", "SendFeedback", "TaskOutput",
}


def normalize_content(content):
    result = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            result.append({"type": "text", "text": block["text"]})
        elif kind == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.get("id", "tool_" + uuid.uuid4().hex),
                "name": block.get("name", "unknown"),
                "input": block.get("input", {}),
            })
    return result


def is_incompatible_tool(tool):
    name = tool.get("name", "")
    return name in INCOMPATIBLE_TOOLS or name.startswith("Artifact")


def anthro_response(upstream):
    content = normalize_content(upstream.get("content"))
    stop_reason = upstream.get("stop_reason") or "end_turn"
    usage = upstream.get("usage") or {}
    return {
        "id": upstream.get("id", "msg_" + uuid.uuid4().hex),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": upstream.get("model", "claude-sonnet-5"),
        "stop_reason": stop_reason,
        "stop_sequence": upstream.get("stop_sequence"),
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    }


async def call_upstream(payload):
    payload = dict(payload)
    tools = payload.get("tools")
    if tools:
        payload["tools"] = [
            tool for tool in tools
            if not is_incompatible_tool(tool)
        ]
    payload["stream"] = False
    headers = {
        "x-api-key": UPSTREAM_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=300) as client:
        response = await client.post(UPSTREAM + "/v1/messages", headers=headers, json=payload)
    if response.status_code >= 400:
        return None, JSONResponse(status_code=response.status_code, content=response.json())
    return response.json(), None


async def sse_events(message):
    yield "event: message_start\ndata: " + json.dumps({
        "type": "message_start",
        "message": {
            "id": message["id"], "type": "message", "role": "assistant",
            "content": [], "model": message["model"], "stop_reason": None,
            "stop_sequence": None, "usage": {"input_tokens": message["usage"]["input_tokens"], "output_tokens": 0},
        },
    }) + "\n\n"
    for index, block in enumerate(message["content"]):
        yield "event: content_block_start\ndata: " + json.dumps({
            "type": "content_block_start", "index": index, "content_block": block,
        }) + "\n\n"
        if block["type"] == "text":
            yield "event: content_block_delta\ndata: " + json.dumps({
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": block["text"]},
            }) + "\n\n"
        elif block["type"] == "tool_use":
            yield "event: content_block_delta\ndata: " + json.dumps({
                "type": "content_block_delta", "index": index,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
            }) + "\n\n"
        yield "event: content_block_stop\ndata: " + json.dumps({
            "type": "content_block_stop", "index": index,
        }) + "\n\n"
    yield "event: message_delta\ndata: " + json.dumps({
        "type": "message_delta",
        "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
        "usage": message["usage"],
    }) + "\n\n"
    yield "event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"


@app.post("/v1/messages")
async def messages(request: Request):
    payload = await request.json()
    original_tools = payload.get("tools") or []
    filtered_tools = [
        tool for tool in original_tools
        if not is_incompatible_tool(tool)
    ]
    logger.info(
        "request model=%s stream=%s max_tokens=%s messages=%s tools=%s system=%s top_keys=%s",
        payload.get("model"), payload.get("stream"), payload.get("max_tokens"),
        len(payload.get("messages") or []), len(payload.get("tools") or []),
        bool(payload.get("system")), sorted(payload.keys()),
    )
    logger.info(
        "tools=%s",
        [tool.get("name", tool.get("type", "?")) for tool in filtered_tools],
    )
    if len(filtered_tools) != len(original_tools):
        logger.info("filtered_incompatible_tools=%s", [
            tool.get("name") for tool in original_tools
            if is_incompatible_tool(tool)
        ])
    upstream, error = await call_upstream(payload)
    if error:
        return error
    message = anthro_response(upstream)
    if payload.get("stream"):
        return StreamingResponse(sse_events(message), media_type="text/event-stream")
    return JSONResponse(message)


@app.get("/health")
async def health():
    return {"status": "healthy"}
