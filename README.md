# Cornerstone MCP Server

MCP server providing 17+ tools for Claude Code, Claude Desktop, and any MCP-compatible client to interact with Cornerstone memory.

## Quick Start

1. Clone and set up:
   ```bash
   git clone https://github.com/supramal1/cornerstoneMCP.git
   cd cornerstoneMCP
   bash setup.sh
   ```

2. Edit `.env` with your API URL and key

3. Add to Claude Code settings

4. Start using it:
   - **"Remember that the project deadline is March 15"** → saves to memory
   - **"What do we know about the Google pitch?"** → recalls from memory
   - **"Forget the old project deadline"** → removes from memory

## Deploy
Deployed to Google Cloud Run (europe-west2). See cornerstone repo for deploy scripts.

## Auth
OAuth 2.1 with JWT principal-based access control. Falls back to legacy API key auth.

## Configuration
Required env vars: CORNERSTONE_URL, CORNERSTONE_API_KEY, OAUTH_JWT_SECRET, MCP_PUBLIC_URL

## Tools

### Simple (start here)

| Tool | Purpose | Example |
|------|---------|---------|
| remember | Save anything to memory | `remember("The budget is $50K")` |
| recall | Search memory | `recall("client's email?")` |
| forget | Remove from memory | `forget("old_deadline", confirm=True)` |

### Workspace

| Tool | Purpose |
|------|---------|
| list_workspaces | See available workspaces |
| switch_workspace | Change active workspace |
| get_current_workspace | See which workspace is active |
| set_default_workspace | Set default workspace |

### Advanced

| Tool | Purpose |
|------|---------|
| get_context | Full context retrieval with metadata |
| add_fact | Save a structured key-value fact |
| add_note | Save a freeform note |
| search | Search across all memory types |
| list_facts | List recent facts |
| get_recent_sessions | See recent conversations |
| list_threads | See conversation threads |
| report_context_feedback | Rate retrieval quality |

Most users only need `remember` and `recall`. The advanced tools give you more control when you need it.
