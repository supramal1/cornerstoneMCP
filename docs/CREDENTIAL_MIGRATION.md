# MCP Credential Migration Plan

## Current state
The MCP server uses `MEMORY_API_KEY` (superuser key) for all requests.
This means all Claude Desktop users share the same identity — every
request appears as "superuser" with full access to all namespaces.

## Target state
Each Claude Desktop user should have their own principal credential,
scoped to their granted workspaces only. This enables:
- Per-user audit trails (who read/wrote what)
- Namespace isolation enforced at the API layer
- Individual rate limiting and capability enforcement
- Credential rotation per user without affecting others

## Why not yet
Requires changes to how credentials are distributed to staff.
Currently credentials are generated per-user in the admin UI,
but the MCP server reads a single shared env var (`MEMORY_API_KEY`).
The MCP server would need to read the per-user credential from
Claude Desktop settings (the Bearer token in the Auth header)
rather than a shared env var.

This is a breaking change to the MCP auth flow and too risky to
ship before the Charlie Oscar go-live in June 2026.

## Migration steps (post-launch)
1. Generate a per-principal credential for each CO staff member (Sprint 11)
2. Update `CLAUDE_DESKTOP_SYSTEM_PROMPT.md` to use individual credentials
3. Update MCP server to pass the user credential from the incoming request
   (`Authorization: Bearer <credential>`) through to backend API calls
   instead of the shared `MEMORY_API_KEY`
4. Test namespace isolation end-to-end: user A cannot see user B's workspace
5. Rotate and retire `MEMORY_API_KEY` from MCP server env vars
6. Update `docs/OPERATIONAL_RUNBOOK.md` with per-user credential rotation procedure

## Interim mitigation
The workspace list endpoint (`GET /connection/workspaces`) now returns
empty for superuser — this prevents cross-workspace data leakage
even with the shared key (fixed in Sprint P, commit 25c172c).

Namespace resolution for superuser falls back to the `default` workspace
or requires explicit `namespace` parameter. Risk is accepted for launch.

## Related files
- `cornerstone-mcp/core.py` — MCP server entry, reads `MEMORY_API_KEY`
- `cornerstone/api/auth.py` — backend auth middleware (shared key vs principal)
- `cornerstone/api/routes/connection.py` — workspace list (superuser returns empty)
