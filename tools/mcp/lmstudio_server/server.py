"""
LM Studio MCP server (stdio)

Exposes tools to call LM Studio's local OpenAI-compatible API.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field
from openai import OpenAI


DEFAULT_BASE_URL = "http://localhost:1234/v1"
DEFAULT_API_KEY = "lmstudio"


class ChatMessage(BaseModel):
    role: str = Field(..., description="Message role: system|user|assistant|tool")
    content: str = Field(..., description="Message content")


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = Field(
        default=None, description="Model ID (if omitted, uses LMSTUDIO_MODEL env var)"
    )
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    presence_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: Optional[float] = Field(default=None, ge=-2.0, le=2.0)
    stop: Optional[List[str]] = None
    timeout_s: Optional[float] = Field(default=60.0, ge=1.0)


class ModelsResponse(BaseModel):
    models: List[str]


mcp = FastMCP("lmstudio_mcp")


def _client(timeout_s: float | None = None) -> OpenAI:
    base_url = os.getenv("LMSTUDIO_API_BASE", DEFAULT_BASE_URL)
    api_key = os.getenv("LMSTUDIO_API_KEY", DEFAULT_API_KEY)
    timeout = timeout_s if timeout_s is not None else 60.0
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout)


def _resolve_model(model: Optional[str]) -> str:
    resolved = model or os.getenv("LMSTUDIO_MODEL")
    if not resolved:
        raise ValueError(
            "Model is required. Set LMSTUDIO_MODEL env var or pass model in the request."
        )
    return resolved


@mcp.tool(name="lmstudio_list_models")
def list_models() -> ModelsResponse:
    """List available models from LM Studio local server."""
    client = _client()
    response = client.models.list()
    model_ids = [item.id for item in getattr(response, "data", [])]
    return ModelsResponse(models=model_ids)


@mcp.tool(name="lmstudio_chat")
def chat(request: ChatRequest) -> Dict[str, Any]:
    """Send a chat completion request to LM Studio local server."""
    model = _resolve_model(request.model)
    client = _client(timeout_s=request.timeout_s)

    response = client.chat.completions.create(
        model=model,
        messages=[m.model_dump() for m in request.messages],
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens,
        presence_penalty=request.presence_penalty,
        frequency_penalty=request.frequency_penalty,
        stop=request.stop,
        stream=False,
    )

    message = response.choices[0].message
    content = message.content

    usage = None
    if getattr(response, "usage", None) is not None:
        usage = {
            "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
            "completion_tokens": getattr(response.usage, "completion_tokens", 0),
            "total_tokens": getattr(response.usage, "total_tokens", 0),
        }

    return {
        "content": content,
        "model": model,
        "usage": usage,
        "raw": response.model_dump(),
    }


@mcp.tool(name="lmstudio_health")
def health() -> Dict[str, Any]:
    """Simple health check for LM Studio server."""
    client = _client(timeout_s=10.0)
    response = client.models.list()
    return {
        "ok": True,
        "models_count": len(getattr(response, "data", []) or []),
        "base_url": os.getenv("LMSTUDIO_API_BASE", DEFAULT_BASE_URL),
    }


if __name__ == "__main__":
    mcp.run()
