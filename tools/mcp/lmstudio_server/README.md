# LM Studio MCP Server

A minimal MCP server that proxies tool calls to LM Studio's local OpenAI-compatible API.

## Prereqs
- LM Studio installed and **Local Server** started (default: http://localhost:1234)
- Python env with dependencies in `requirements.txt`

## Install deps

```powershell
pip install -r tools/mcp/lmstudio_server/requirements.txt
```

## Run (stdio)

```powershell
python tools/mcp/lmstudio_server/server.py
```

## Env vars
- `LMSTUDIO_API_BASE` (default: `http://localhost:1234/v1`)
- `LMSTUDIO_API_KEY` (default: `lmstudio`)
- `LMSTUDIO_MODEL` (optional, required if `model` not passed in requests)

## Tools
- `lmstudio_list_models`
- `lmstudio_chat`
- `lmstudio_health`
