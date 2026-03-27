# Cornerstone MCP Server

Connect Claude Code, Codex, or any MCP-compatible tool to your Cornerstone memory instance.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/your-org/cornerstone-mcp.git
cd cornerstone-mcp

# 2. Setup
bash setup.sh

# 3. Configure
# Edit .env with your API URL and key:
#   CORNERSTONE_URL=https://your-instance.run.app
#   CORNERSTONE_API_KEY=cs_your_key_here
nano .env

# 4. Add to Claude Code settings (~/.claude/settings.json):
```

```json
{
  "mcpServers": {
    "cornerstone": {
      "command": "/path/to/cornerstone-mcp/.venv/bin/python",
      "args": ["/path/to/cornerstone-mcp/server.py", "--transport", "stdio"]
    }
  }
}
```

Start a conversation. Cornerstone will remember.

## Available Tools

| Tool | Purpose |
|------|---------|
| `get_context` | Search memory for relevant information |
| `add_fact` | Save a key piece of information |
| `add_note` | Save a freeform note |
| `search` | Search across all memory types |
| `list_facts` | List recent facts |
| `list_workspaces` | See available workspaces |
| `switch_workspace` | Switch to a different workspace |
| `get_current_workspace` | See which workspace is active |
| `set_default_workspace` | Set your default workspace |
| `get_recent_sessions` | See recent conversations |

## Configuration

| Variable | Required | Description |
|----------|----------|-------------|
| `CORNERSTONE_URL` | Yes | Your Cornerstone instance URL |
| `CORNERSTONE_API_KEY` | Yes | Your API key |
| `CORNERSTONE_NAMESPACE` | No | Default workspace |
| `CORNERSTONE_AGENT_ID` | No | Agent identifier (default: `openclaw`) |

## Troubleshooting

- **"Connection refused"** — Check your API URL is correct and the instance is running.
- **"401 Unauthorized"** — Check your API key is valid. Get a new one from your Cornerstone admin.
- **"No workspaces available"** — Your API key may not have any workspace grants. Contact your admin.

## Requirements

- Python 3.11+
- `mcp`, `httpx`, `python-dotenv`
