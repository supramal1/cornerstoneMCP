# Google OAuth Setup — Cornerstone MCP Connector

This document walks through enabling Google as the identity provider for
the Cornerstone MCP connector. Targets GCP project `cornerstone-489916`.

## 1. Create OAuth client in GCP

1. Open https://console.cloud.google.com/apis/credentials?project=cornerstone-489916
2. Click **Create Credentials → OAuth client ID**
3. Application type: **Web application**
4. Name: **Cornerstone MCP**
5. Authorised redirect URIs: `https://cornerstone-mcp-34862349933.europe-west2.run.app/oauth/callback`
6. Save. Copy the **Client ID** and **Client Secret**.

## 2. Configure consent screen

1. Go to **OAuth consent screen**
2. User type: **Internal** (restricts to Charlie Oscar Workspace domain)
3. App name: `Cornerstone`
4. Support email: Mal's email
5. Authorised domains: `charlieoscar.com`
6. Scopes: `openid`, `email`, `profile` — no additional scopes needed

## 3. Store client secret in Secret Manager

```bash
gcloud secrets create GOOGLE_CLIENT_SECRET --project=cornerstone-489916 --replication-policy=automatic
echo -n "<client-secret>" | gcloud secrets versions add GOOGLE_CLIENT_SECRET --data-file=- --project=cornerstone-489916
```

## 4. Set environment variables on Cloud Run

The Cloud Run service `cornerstone-mcp` needs:

| Variable | Source | Value |
|----------|--------|-------|
| `GOOGLE_CLIENT_ID` | env | `<client-id>` from Step 1 |
| `GOOGLE_CLIENT_SECRET` | secret | Secret Manager → `GOOGLE_CLIENT_SECRET` |
| `GOOGLE_REDIRECT_URI` | env | `https://cornerstone-mcp-34862349933.europe-west2.run.app/oauth/callback` |
| `GOOGLE_HOSTED_DOMAIN` | env | `charlieoscar.com` |
| `MEMORY_API_KEY` | secret | Secret Manager → existing `MEMORY_API_KEY` (required for resolve-email bridge) |
| `DEFAULT_GOOGLE_NAMESPACE` | env (optional) | `charlie-oscar` (default if unset) |
| `ALLOW_API_KEY_LOGIN` | env (optional) | unset in prod, `true` in dev |

Deploy command:

```bash
gcloud run services update cornerstone-mcp \
  --project=cornerstone-489916 \
  --region=europe-west2 \
  --update-env-vars=GOOGLE_CLIENT_ID=<client-id>,GOOGLE_REDIRECT_URI=https://cornerstone-mcp-34862349933.europe-west2.run.app/oauth/callback,GOOGLE_HOSTED_DOMAIN=charlieoscar.com \
  --update-secrets=GOOGLE_CLIENT_SECRET=GOOGLE_CLIENT_SECRET:latest,MEMORY_API_KEY=MEMORY_API_KEY:latest \
  --no-traffic \
  --tag=google-oauth-test
```

The `--no-traffic --tag=google-oauth-test` creates a test revision on a tagged URL
without replacing production. Verify on the tagged URL before promoting.

## 5. Verify the flow

1. Open `https://google-oauth-test---cornerstone-mcp-34862349933.europe-west2.run.app/oauth/authorize?client_id=...&...`
   (or easier: configure Claude Desktop to use this test revision's MCP URL)
2. Expect redirect to Google with `hd=charlieoscar.com`
3. Sign in with a `@charlieoscar.com` account
4. Expect success page saying "Signed in as <Your Name>"
5. Confirm the Claude Desktop MCP connector is authenticated
6. Call a tool — confirm it works

## 6. Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| "We couldn't verify your Google account" | `hd` claim missing or wrong — user is on a personal Google account, not Workspace |
| "Google sign-in is not configured" | `GOOGLE_CLIENT_ID` or `GOOGLE_CLIENT_SECRET` env var missing on Cloud Run |
| "Could not provision your Cornerstone account" | `MEMORY_API_KEY` missing on Cloud Run, or backend `/admin/auth/resolve-email` returning 500. Check Cloud Run logs. |
| Redirect URI mismatch error from Google | The redirect URI in GCP console does not exactly match `GOOGLE_REDIRECT_URI` env var. Must match scheme, host, path exactly — trailing slash matters. |
| User signs in but cannot access any workspace | Default namespace grant goes to `charlie-oscar` with `read` access. Grant more workspaces via the admin control panel. |
