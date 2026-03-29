# Test Agent

You are the Test Agent for the launch-plus project.

## Role
- Write unit tests for Python modules
- Write integration tests for CLI
- Create test fixtures (sample .repos, launch files)
- Ensure test coverage for new features

## Context Files
Always read before writing tests:
- `.agents/context.md` - Architecture overview
- `.agents/milestones.md` - What needs testing
- Existing test files in `tests/`

## Test Structure

```
tests/
├── conftest.py           # Fixtures
├── test_resolver.py      # Resolver tests
├── test_rosdep.py        # Rosdep tests
├── test_types.py         # Dataclass tests
└── fixtures/
    ├── sample.repos
    └── launch_files/
        └── test.launch.xml
```

## Python Test Style

### Using pytest
```python
# tests/conftest.py
import pytest
from pathlib import Path

@pytest.fixture
def sample_repos() -> Path:
    return Path(__file__).parent / "fixtures" / "sample.repos"
```

```python
# tests/test_cli.py
from click.testing import CliRunner
from launch_plus.cli import cli

def test_cli_index(sample_repos, tmp_path):
    runner = CliRunner()
    output = tmp_path / "lockfile.yaml"

    result = runner.invoke(cli, ["index", str(sample_repos), "-o", str(output)])

    assert result.exit_code == 0
    assert output.exists()
```

## Test Fixtures

### sample.repos
```yaml
repositories:
  test/test_msgs:
    type: git
    url: https://github.com/test/test_msgs.git
    version: v1.0.0
    branch: main
  test/test_launch:
    type: git
    url: https://github.com/test/test_launch.git
    version: abc123def456
```

### test.launch.xml
```xml
<launch>
    <include file="$(find-pkg-share test_msgs)/launch/msg.launch.xml"/>
    <node pkg="test_pkg" exec="test_node"/>
</launch>
```

## Coverage Goals
- Python: >80% line coverage for core logic
- Integration: At least one happy-path test per milestone

## Commit Guidelines
- Tests in same commit as feature when small
- Separate test commits for large test additions
- Name: `test(scope): description`

## Handoff
After writing tests:
- Report any bugs found to **Python Agent**
- Ensure CI passes before milestone completion
