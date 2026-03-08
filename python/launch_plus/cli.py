"""Standalone CLI for launch-plus.

This is a thin Python wrapper that delegates to the Rust CLI or provides
fallback functionality when the Rust extension is not available.
"""

import subprocess
import sys
from pathlib import Path

import click


@click.group()
@click.version_option()
@click.option("-v", "--verbose", is_flag=True, help="Verbose output")
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """Bazel-like build and run system for ROS 2."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose


@main.command()
@click.argument("files", nargs=-1, default=("manifest.repos",))
@click.option("--append", is_flag=True, help="Append to existing lockfile")
@click.pass_context
def index(ctx: click.Context, files: tuple[str, ...], append: bool) -> None:
    """Generate lockfile from .repos files."""
    click.echo(f"Indexing {files} (append={append})")
    click.echo("Not yet implemented")


@main.command()
@click.option("--branch", is_flag=True, help="Update from branch heads")
@click.option("--diff", is_flag=True, help="Show what would change")
@click.pass_context
def update(ctx: click.Context, branch: bool, diff: bool) -> None:
    """Update lockfile with latest SHAs."""
    click.echo(f"Updating (branch={branch}, diff={diff})")
    click.echo("Not yet implemented")


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.option(
    "--output-format",
    type=click.Choice(["xml", "yaml"]),
    default="yaml",
    help="Output format",
)
@click.pass_context
def resolve(ctx: click.Context, package: str, launcher: str, output_format: str) -> None:
    """Resolve and flatten launch file (no build)."""
    click.echo(f"Resolving {package}/{launcher} (format={output_format})")
    click.echo("Not yet implemented")


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.pass_context
def build(ctx: click.Context, package: str, launcher: str) -> None:
    """Fetch and build packages (no launch)."""
    click.echo(f"Building {package}/{launcher}")
    click.echo("Not yet implemented")


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.pass_context
def test(ctx: click.Context, package: str, launcher: str) -> None:
    """Fetch, build, and run tests."""
    click.echo(f"Testing {package}/{launcher}")
    click.echo("Not yet implemented")


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.pass_context
def run(ctx: click.Context, package: str, launcher: str) -> None:
    """Fetch, build, and launch (full execution)."""
    click.echo(f"Running {package}/{launcher}")
    click.echo("Not yet implemented")


@main.command()
@click.argument("package")
@click.argument("launcher")
@click.option("--strict", is_flag=True, help="Fail on warnings too")
@click.pass_context
def check(ctx: click.Context, package: str, launcher: str, strict: bool) -> None:
    """Validate launch file without running."""
    click.echo(f"Checking {package}/{launcher} (strict={strict})")
    click.echo("Not yet implemented")


@main.command("list")
@click.pass_context
def list_packages(ctx: click.Context) -> None:
    """List packages from lockfile."""
    click.echo("Listing packages")
    click.echo("Not yet implemented")


@main.command()
@click.pass_context
def clean(ctx: click.Context) -> None:
    """Clean fetched packages."""
    click.echo("Cleaning")
    click.echo("Not yet implemented")


if __name__ == "__main__":
    main()
