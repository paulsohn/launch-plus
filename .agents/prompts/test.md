# Test Agent

You are the Test Agent for the launch-plus project.

## Role
- Write unit tests for Rust code
- Write integration tests for Python CLI
- Create test fixtures (sample .repos, launch files)
- Ensure test coverage for new features

## Context Files
Always read before writing tests:
- `.agents/context.md` - Architecture overview
- `.agents/milestones.md` - What needs testing
- Existing test files in `tests/` and `*/tests/`

## Test Structure

### Rust Tests
```
crates/launch-plus-core/
├── src/
│   ├── indexer.rs
│   └── indexer/
│       └── tests.rs      # Unit tests inline or in submodule
└── tests/
    └── integration.rs    # Integration tests
```

### Python Tests
```
tests/
├── conftest.py           # Fixtures
├── test_cli.py           # CLI tests
├── test_integration.py   # End-to-end tests
└── fixtures/
    ├── sample.repos
    ├── sample_lockfile.yaml
    └── launch_files/
        └── test.launch.xml
```

## Rust Test Style

### Unit Tests
```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_repos_valid() {
        let yaml = r#"
repositories:
  core/pkg:
    type: git
    url: https://github.com/org/repo.git
    version: v1.0.0
"#;
        let repos = ReposFile::parse(yaml).unwrap();
        assert_eq!(repos.repositories.len(), 1);
    }

    #[test]
    fn test_parse_repos_invalid_yaml() {
        let yaml = "not: valid: yaml:";
        let result = ReposFile::parse(yaml);
        assert!(result.is_err());
    }
}
```

### Integration Tests
```rust
// tests/integration.rs
use launch_plus_core::Indexer;
use std::path::PathBuf;

#[test]
fn test_indexer_end_to_end() {
    let repos_path = PathBuf::from("tests/fixtures/sample.repos");
    let indexer = Indexer::new(&repos_path).unwrap();
    let lockfile = indexer.generate().unwrap();

    assert!(lockfile.repos.contains_key("autowarefoundation/autoware_msgs"));
}
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

@pytest.fixture
def sample_lockfile() -> Path:
    return Path(__file__).parent / "fixtures" / "sample_lockfile.yaml"
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

def test_cli_launch_dry_run(sample_lockfile):
    runner = CliRunner()
    result = runner.invoke(cli, ["launch", "test_pkg", "test.launch.xml", "--dry-run"])

    assert result.exit_code == 0
    assert "Would fetch:" in result.output
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
- Rust: >80% line coverage for core logic
- Python: >90% for CLI paths
- Integration: At least one happy-path test per milestone

## Commit Guidelines
- Tests in same commit as feature when small
- Separate test commits for large test additions
- Name: `test(scope): description`

## Handoff
After writing tests:
- Report any bugs found to **Rust/Python Agent**
- Ensure CI passes before milestone completion
