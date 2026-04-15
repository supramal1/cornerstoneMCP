"""Shared UI components: banner, panels, status markers, progress bars.

Every interactive screen in the wizard pulls its visual primitives from here so
the look and feel stay consistent across modes.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

console = Console()

BANNER = r"""[bold cyan] ██████╗ ██████╗ ██████╗ ███╗   ██╗███████╗██████╗[/bold cyan]
[bold cyan]██╔════╝██╔═══██╗██╔══██╗████╗  ██║██╔════╝██╔══██╗[/bold cyan]
[bold cyan]██║     ██║   ██║██████╔╝██╔██╗ ██║█████╗  ██████╔╝[/bold cyan]
[bold cyan]██║     ██║   ██║██╔══██╗██║╚██╗██║██╔══╝  ██╔══██╗[/bold cyan]
[bold cyan]╚██████╗╚██████╔╝██║  ██║██║ ╚████║███████╗██║  ██║[/bold cyan]
[bold cyan] ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝[/bold cyan]
[dim]                              S T O N E[/dim]"""

TAGLINE = "[dim]memory • context • continuity[/dim]"


def banner() -> None:
    """Print the Cornerstone banner with tagline."""
    console.print()
    console.print(BANNER)
    console.print()
    console.print(f"  {TAGLINE}")
    console.print()


def ok(msg: str) -> None:
    console.print(f"  [green]✓[/green] {msg}")


def fail(msg: str) -> None:
    console.print(f"  [red]✗[/red] {msg}")


def warn(msg: str) -> None:
    console.print(f"  [yellow]![/yellow] {msg}")


def info(msg: str) -> None:
    console.print(f"  [blue]i[/blue] {msg}")


def skip(msg: str) -> None:
    console.print(f"  [dim]○ {msg}[/dim]")


def step(msg: str) -> None:
    console.print(f"  [cyan]→[/cyan] {msg}")


def hint(msg: str) -> None:
    console.print(f"    [dim]{msg}[/dim]")


def divider(title: str = "") -> None:
    if title:
        console.print(f"\n[bold cyan]─── {title} ───[/bold cyan]\n")
    else:
        console.print("[dim]─────────────────────────────────────────────────[/dim]")


def panel(body: str, title: str = "", style: str = "cyan") -> None:
    console.print(Panel(body, title=title or None, border_style=style, padding=(1, 2)))


def error_panel(title: str, body: str) -> None:
    console.print(Panel(f"[red]{body}[/red]", title=f"[red]{title}[/red]", border_style="red", padding=(1, 2)))


def success_panel(title: str, body: str) -> None:
    console.print(Panel(f"[green]{body}[/green]", title=f"[green]{title}[/green]", border_style="green", padding=(1, 2)))


def warn_panel(title: str, body: str) -> None:
    console.print(Panel(f"[yellow]{body}[/yellow]", title=f"[yellow]{title}[/yellow]", border_style="yellow", padding=(1, 2)))


@contextmanager
def progress(description: str = "Working...") -> Iterator[Progress]:
    """Rich Progress with spinner + elapsed + percentage. Use for >2s operations."""
    prog = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )
    with prog:
        yield prog


@contextmanager
def spinner(description: str) -> Iterator[None]:
    """Lightweight spinner for short operations without a known end."""
    with console.status(f"[cyan]{description}[/cyan]", spinner="dots"):
        yield


def table(*headers: str, title: str = "") -> Table:
    """Build a Rich table with default Cornerstone styling."""
    tbl = Table(
        title=title or None,
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        padding=(0, 1),
    )
    for h in headers:
        tbl.add_column(h)
    return tbl


def render(*renderables) -> None:
    for r in renderables:
        console.print(r)
