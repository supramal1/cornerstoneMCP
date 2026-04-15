"""Fresh install wizard — guided 6-step flow to stand up a new Cornerstone instance.

Steps
-----
1. Database     — create Supabase project / collect URL + service_role key
2. Migrations   — run schema/migrations/*.sql against the collected DATABASE_URL
3. Backend      — deploy cornerstone-api to Cloud Run
4. MCP          — deploy cornerstone-mcp to Cloud Run
5. Frontend     — deploy cornerstone-ui to Vercel
6. Auth         — configure Google OAuth client + write credentials

The wizard is intentionally a **guided checklist**, not a one-click deploy:
each step prints the exact command it will run, asks for confirmation, and
falls through to manual instructions where OAuth/billing/account creation
blocks full automation.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import questionary

from . import ui
from .config import WizardState, save_credentials, save_instance_url


@dataclass
class StepResult:
    name: str
    status: str  # "ok" | "skip" | "manual" | "fail"
    detail: str = ""


# ── Entry point ──────────────────────────────────────────────────────────


def run_fresh_install() -> None:
    ui.divider("Fresh Install")
    ui.info("This wizard will walk you through a brand-new Cornerstone deployment.")
    ui.hint("Steps 1–5 each have an automated path (if you have the right CLI installed)")
    ui.hint("and a manual fallback that prints exactly what to do.")
    if not questionary.confirm("Ready to begin?", default=True).unsafe_ask():
        ui.skip("Fresh install cancelled")
        return

    state = WizardState()
    results: list[StepResult] = []

    steps: list[tuple[str, Callable[[WizardState], StepResult]]] = [
        ("Database", _step_database),
        ("Migrations", _step_migrations),
        ("Backend (Cloud Run)", _step_backend),
        ("MCP server (Cloud Run)", _step_mcp),
        ("Frontend (Vercel)", _step_frontend),
        ("Auth & credentials", _step_auth),
    ]

    for idx, (name, fn) in enumerate(steps, start=1):
        ui.divider(f"Step {idx}/{len(steps)} — {name}")
        result = fn(state)
        results.append(result)
        if result.status == "fail":
            ui.fail(f"{name} failed: {result.detail}")
            if not questionary.confirm("Continue anyway?", default=False).unsafe_ask():
                break

    _render_summary(results)


# ── Step 1: Database ─────────────────────────────────────────────────────


def _step_database(state: WizardState) -> StepResult:
    ui.step("Cornerstone needs a Supabase project (free tier is fine).")
    ui.hint("Dashboard: https://app.supabase.com/projects")

    choice = questionary.select(
        "How do you want to provision the database?",
        choices=[
            "I have an existing Supabase project",
            "Open Supabase to create a new one",
            "Skip — I'll configure it later",
        ],
    ).unsafe_ask()

    if choice.startswith("Skip"):
        return StepResult("Database", "skip")

    if choice.startswith("Open"):
        ui.info("Visit https://app.supabase.com/projects, create a project,")
        ui.info("then come back here with the URL + service_role key.")
        questionary.text("Press enter when ready...", default="").unsafe_ask()

    supabase_url = questionary.text(
        "Supabase URL",
        instruction="https://xxx.supabase.co",
    ).unsafe_ask()
    if not supabase_url or "supabase.co" not in supabase_url:
        return StepResult("Database", "fail", "invalid Supabase URL")

    service_key = questionary.password(
        "Supabase service_role key",
        instruction="Dashboard → Settings → API → service_role (NOT the anon key)",
    ).unsafe_ask()
    if not service_key or len(service_key) < 40:
        return StepResult("Database", "fail", "service_role key looks invalid")

    db_url = questionary.text(
        "DATABASE_URL (for migrations)",
        instruction="Dashboard → Settings → Database → Connection string → URI",
    ).unsafe_ask()

    state.__dict__["supabase_url"] = supabase_url
    state.__dict__["supabase_key"] = service_key
    state.__dict__["database_url"] = db_url
    ui.ok("Database credentials captured")
    return StepResult("Database", "ok", supabase_url)


# ── Step 2: Migrations ───────────────────────────────────────────────────


def _step_migrations(state: WizardState) -> StepResult:
    db_url = state.__dict__.get("database_url")
    if not db_url:
        ui.skip("No DATABASE_URL captured — skipping migrations")
        return StepResult("Migrations", "skip")

    migrations_dir = _find_migrations_dir()
    if migrations_dir is None:
        ui.warn("schema/migrations/ not found in this checkout")
        ui.hint("Run migrations manually from the cornerstone backend repo:")
        ui.hint("  cd ../cornerstone && python schema/bootstrap_schema.py")
        return StepResult("Migrations", "manual", "schema/migrations not found")

    files = sorted(migrations_dir.glob("*.sql"))
    if not files:
        return StepResult("Migrations", "skip", "no .sql files")

    ui.info(f"Found {len(files)} migration files in {migrations_dir}")
    if not questionary.confirm(
        f"Apply all {len(files)} migrations to the database?", default=True
    ).unsafe_ask():
        return StepResult("Migrations", "skip")

    psql = shutil.which("psql")
    if psql is None:
        ui.warn("psql not on PATH — falling back to manual instructions")
        ui.hint(f"Install psql, then: cat {migrations_dir}/*.sql | psql '{db_url}'")
        return StepResult("Migrations", "manual", "psql missing")

    with ui.progress() as prog:
        task = prog.add_task("Applying migrations", total=len(files))
        for path in files:
            prog.update(task, description=f"[cyan]→[/cyan] {path.name}")
            try:
                subprocess.run(
                    [psql, db_url, "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                ui.fail(f"Migration failed on {path.name}")
                ui.hint((exc.stderr or exc.stdout or "")[-400:])
                return StepResult("Migrations", "fail", path.name)
            prog.advance(task)

    ui.ok(f"Applied {len(files)} migrations")
    return StepResult("Migrations", "ok", f"{len(files)} files")


# ── Step 3: Backend ──────────────────────────────────────────────────────


def _step_backend(state: WizardState) -> StepResult:
    return _cloud_run_step(
        state,
        service_name="cornerstone-api",
        source_repo_hint="../cornerstone",
        state_key="api_url",
        label="Backend",
        env_vars=[
            ("SUPABASE_URL", state.__dict__.get("supabase_url")),
            ("SUPABASE_KEY", state.__dict__.get("supabase_key")),
            ("ANSWER_ENGINE_MODEL", "claude-sonnet-4-20250514"),
        ],
    )


# ── Step 4: MCP server ───────────────────────────────────────────────────


def _step_mcp(state: WizardState) -> StepResult:
    return _cloud_run_step(
        state,
        service_name="cornerstone-mcp",
        source_repo_hint=".",
        state_key="mcp_url",
        label="MCP server",
        env_vars=[
            ("CORNERSTONE_URL", state.__dict__.get("api_url")),
            ("ALLOW_API_KEY_LOGIN", "true"),
        ],
    )


def _cloud_run_step(
    state: WizardState,
    *,
    service_name: str,
    source_repo_hint: str,
    state_key: str,
    label: str,
    env_vars: list[tuple[str, str | None]],
) -> StepResult:
    ui.step(f"Deploy {service_name} to Cloud Run.")
    ui.hint("Required: gcloud CLI, authenticated, with a target project set.")

    gcloud = shutil.which("gcloud")
    if gcloud is None:
        ui.warn("gcloud not on PATH")
        ui.hint("Install: https://cloud.google.com/sdk/docs/install")
        return StepResult(label, "manual", "gcloud missing")

    if not questionary.confirm(f"Deploy {service_name} now?", default=True).unsafe_ask():
        return StepResult(label, "skip")

    project = questionary.text("GCP project ID", default="").unsafe_ask()
    region = questionary.text("Region", default="europe-west2").unsafe_ask()
    if not project:
        return StepResult(label, "fail", "no project")

    env_flags = ",".join(f"{k}={v}" for k, v in env_vars if v)
    cmd = [
        gcloud,
        "run",
        "deploy",
        service_name,
        "--source",
        source_repo_hint,
        "--project",
        project,
        "--region",
        region,
        "--allow-unauthenticated",
        "--platform",
        "managed",
    ]
    if env_flags:
        cmd.extend(["--set-env-vars", env_flags])

    ui.info("Command:")
    ui.hint("  " + " ".join(cmd))
    if not questionary.confirm("Run it?", default=True).unsafe_ask():
        return StepResult(label, "manual", "user declined")

    with ui.spinner(f"Deploying {service_name} (this can take several minutes)..."):
        result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        ui.fail(f"gcloud deploy failed for {service_name}")
        ui.hint((result.stderr or result.stdout or "")[-400:])
        return StepResult(label, "fail", "gcloud non-zero")

    service_url = _extract_cloud_run_url(result.stdout + result.stderr)
    if service_url:
        state.__dict__[state_key] = service_url
        ui.ok(f"{label} deployed: {service_url}")
        return StepResult(label, "ok", service_url)

    ui.warn("Deployed, but couldn't parse the service URL from gcloud output")
    return StepResult(label, "manual", "url parse failed")


def _extract_cloud_run_url(text: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("https://") and "run.app" in line:
            return line.split()[0].rstrip(".,;")
    return None


# ── Step 5: Frontend ─────────────────────────────────────────────────────


def _step_frontend(state: WizardState) -> StepResult:
    ui.step("Deploy cornerstone-ui to Vercel.")
    ui.hint("Required: vercel CLI, logged in (`vercel login`).")

    vercel = shutil.which("vercel")
    if vercel is None:
        ui.warn("vercel not on PATH")
        ui.hint("Install: npm i -g vercel")
        ui.hint("Then:     cd ../cornerstone-ui && vercel --prod")
        return StepResult("Frontend", "manual", "vercel missing")

    if not questionary.confirm("Run `vercel --prod` now?", default=True).unsafe_ask():
        return StepResult("Frontend", "skip")

    project_dir = questionary.text(
        "Path to cornerstone-ui checkout",
        default="../cornerstone-ui",
    ).unsafe_ask()

    ui.info("Remember: set NEXT_PUBLIC_CORNERSTONE_API_BASE_URL, ANTHROPIC_API_KEY,")
    ui.info("and CLERK keys in the Vercel project settings before deploying.")

    with ui.spinner("Deploying to Vercel..."):
        result = subprocess.run(
            [vercel, "--prod", "--yes"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )

    if result.returncode != 0:
        ui.fail("vercel deploy failed")
        ui.hint((result.stderr or result.stdout or "")[-400:])
        return StepResult("Frontend", "fail", "vercel non-zero")

    ui.ok("Frontend deployed")
    return StepResult("Frontend", "ok")


# ── Step 6: Auth ─────────────────────────────────────────────────────────


def _step_auth(state: WizardState) -> StepResult:
    ui.step("Configure auth and save credentials for the CLI.")

    mcp_url = state.__dict__.get("mcp_url") or questionary.text(
        "MCP server URL",
        default=state.instance_url,
    ).unsafe_ask()

    ui.info("Google OAuth setup (one-time, per instance):")
    ui.hint("1. https://console.cloud.google.com/apis/credentials")
    ui.hint("2. Create OAuth 2.0 client (Web application)")
    ui.hint(f"3. Add redirect URI: {mcp_url}/auth/google/callback")
    ui.hint("4. Set GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET on the MCP Cloud Run service")

    admin_key = questionary.password(
        "Cornerstone admin API key (to store for CLI use)",
        instruction="leave blank to skip",
    ).unsafe_ask()
    if not admin_key:
        ui.skip("No CLI credentials saved — run the wizard's 'connect' mode later")
        return StepResult("Auth", "skip")

    email = questionary.text("Your email", default="admin@example.com").unsafe_ask()
    save_credentials(
        token=admin_key,
        email=email,
        instance_url=mcp_url,
        method="api_key",
    )
    save_instance_url(mcp_url)
    ui.ok(f"Credentials saved to ~/.cornerstone/credentials.json")
    return StepResult("Auth", "ok", mcp_url)


# ── Summary ──────────────────────────────────────────────────────────────


def _render_summary(results: list[StepResult]) -> None:
    ui.divider("Summary")
    tbl = ui.table("Step", "Status", "Detail")
    for r in results:
        colour = {
            "ok": "[green]✓ done[/green]",
            "skip": "[dim]○ skipped[/dim]",
            "manual": "[yellow]! manual[/yellow]",
            "fail": "[red]✗ failed[/red]",
        }.get(r.status, r.status)
        tbl.add_row(r.name, colour, r.detail)
    ui.render(tbl)
    ui.info("Next: run `python -m cli.main` again and pick 'Health check' to verify.")


def _find_migrations_dir() -> Path | None:
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent / "schema" / "migrations",
        here.parent.parent.parent / "cornerstone" / "schema" / "migrations",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None
