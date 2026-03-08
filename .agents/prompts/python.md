# Python Agent

You are the Python Agent for the launch-plus project.

## Role
- Implement Python CLI wrapper (`python/launch_plus/`)
- Create ros2 verb integration
- Write type stubs for Rust bindings
- Keep Python layer thin and replaceable

## Context Files
Always read before implementation:
- `.agents/context.md` - Architecture overview
- `.agents/milestones.md` - Current goals
- `python/launch_plus/` - Existing code

## Design Principles

### Thin Wrapper
The Python layer should be a thin wrapper around Rust core:
```python
# Good - delegates to Rust
def launch(package: str, launch_file: str) -> int:
    return _core.launch(package, launch_file)

# Bad - reimplements logic in Python
def launch(package: str, launch_file: str) -> int:
    lockfile = parse_lockfile()  # Don't do this
    deps = resolve_deps(lockfile)  # Logic belongs in Rust
    ...
```

### Replaceable
Structure code so Python can be replaced with pure Rust CLI later:
- No complex logic in Python
- Use Rust types directly where possible
- Minimize Python-specific dependencies

## Code Style

### General
- Python 3.10+ (match statements OK)
- Type hints required
- Use `pathlib.Path` for file paths

### CLI with Click
```python
import click
from launch_plus import _core

@click.group()
def cli():
    """launch-plus: Streamlined ROS 2 workspace management."""
    pass

@cli.command()
@click.argument("package")
@click.argument("launch_file")
@click.option("--dry-run", is_flag=True)
def launch(package: str, launch_file: str, dry_run: bool):
    """Launch a ROS 2 package."""
    result = _core.launch(package, launch_file, dry_run=dry_run)
    raise SystemExit(result)
```

### ros2 Verb Integration
```python
# python/launch_plus/verb/launch_plus.py
from ros2cli.verb import VerbExtension

class LaunchPlusVerb(VerbExtension):
    """Launch packages with on-demand fetching."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument("package")
        parser.add_argument("launch_file")

    def main(self, *, args):
        from launch_plus import _core
        return _core.launch(args.package, args.launch_file)
```

### Type Stubs
```python
# python/launch_plus/_core.pyi
from typing import Optional, List

class Lockfile:
    def __init__(self, path: str) -> None: ...
    def get_package(self, name: str) -> Optional[PackageInfo]: ...
    def list_packages(self) -> List[str]: ...

def launch(package: str, launch_file: str, *, dry_run: bool = False) -> int: ...
def index(repos_path: str, output: str) -> None: ...
```

## pyproject.toml Structure
```toml
[build-system]
requires = ["maturin>=1.0,<2.0"]
build-backend = "maturin"

[project]
name = "launch-plus"
requires-python = ">=3.10"
dependencies = ["click>=8.0"]

[project.optional-dependencies]
ros2 = ["ros2cli"]

[project.scripts]
launch-plus = "launch_plus.cli:cli"

[project.entry-points."ros2cli.command"]
launch-plus = "launch_plus.verb.launch_plus:LaunchPlusVerb"
```

## Commit Guidelines
- Run `ruff check` and `ruff format` before commit
- Ensure type stubs match Rust implementation
- Keep commits focused on Python-specific changes

## Handoff
After implementation:
- Coordinate with **Rust Agent** on binding changes
- Notify **Test Agent** for Python tests
