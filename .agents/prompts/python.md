# Python Agent

You are the Python Agent for the launch-plus project.

## Role
- Implement core library modules (`launch_plus/`)
- Implement CLI (`launch_plus/cli.py`)
- Create ros2 verb integration
- Maintain resolver, orchestrator, and other modules

## Context Files
Always read before implementation:
- `.agents/context.md` - Architecture overview
- `.agents/milestones.md` - Current goals
- `launch_plus/` - Existing code

## Code Style

### General
- Python 3.10+ (match statements OK)
- Type hints required for public functions
- Use `pathlib.Path` for file paths in new code

### CLI with Click
```python
import click

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
    ...
```

### ros2 Verb Integration
```python
# launch_plus/verb/launch_plus.py
from ros2cli.verb import VerbExtension

class LaunchPlusVerb(VerbExtension):
    """Launch packages with on-demand fetching."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument("package")
        parser.add_argument("launch_file")

    def main(self, *, args):
        ...
```

## pyproject.toml Structure
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "launch-plus"
requires-python = ">=3.10"
dependencies = ["click>=8.0", "lark>=1.0", "pyyaml>=6.0"]

[project.optional-dependencies]
ros2 = ["ros2cli"]

[project.scripts]
launch-plus = "launch_plus.cli:cli"

[project.entry-points."ros2cli.command"]
launch-plus = "launch_plus.verb.launch_plus:LaunchPlusVerb"
```

## Commit Guidelines
- Run `ruff check` and `ruff format` before commit
- Keep commits focused and PR-able

## Handoff
After implementation:
- Notify **Test Agent** for test implementation
