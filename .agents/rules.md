# Coding Rules & Conventions

## Rust Rules

### Style
- Run `cargo fmt` before every commit
- Run `cargo clippy` and fix warnings
- Use `#[must_use]` for functions returning values that shouldn't be ignored

### Error Handling
- Use `thiserror` for library errors
- Use `anyhow` only in CLI/binary crates
- Provide context with `.context()` or custom error messages

### Dependencies
- Prefer well-maintained crates from crates.io
- Pin versions in Cargo.toml
- Document why each dependency is needed

### Key Crates
```toml
serde = "1"           # Serialization
serde_yaml = "0.9"    # YAML parsing
thiserror = "2"       # Error derive
quick-xml = "0.37"    # XML parsing
clap = "4"            # CLI parsing
tar = "0.4"           # Archive handling
```

## Python Rules

### Style
- Run `ruff check --fix` and `ruff format` before commit
- Type hints required for all public functions
- Use `pathlib.Path` not string paths

### Dependencies
- Minimize dependencies (Python is thin wrapper)
- Required: `click` for CLI
- Optional: `ros2cli` for verb integration

### Structure
- Keep all logic in Rust, Python is just glue
- Type stubs must match Rust implementation exactly

### None Handling
- **Never** use `str(x)` then check `== "None"` to detect Python `None`. This conflates the literal string `"None"` with the absence of a value.
- Check `x is None` **before** stringifying: `if x is None: handle_missing()`, then `name = str(x)`.
- For optional values from ROS 2 APIs, always guard with `is None` first.

## Git Rules

### Commit Messages
```
type(scope): short description

Longer description if needed.

Refs: #issue-number
```

### Types
- `feat` - New feature
- `fix` - Bug fix
- `test` - Adding tests
- `docs` - Documentation
- `refactor` - Code restructure (no behavior change)
- `ci` - CI/CD changes
- `chore` - Maintenance

### Scopes
- `indexer` - .repos parsing, lockfile generation
- `resolver` - Launch file dependency resolution
- `fetcher` - Git sparse-checkout operations
- `builder` - Colcon build orchestration
- `launcher` - Launch orchestration
- `cli` - CLI and ros2 verb
- `core` - Cross-cutting core functionality

### Examples
```
feat(indexer): add .repos YAML parser
feat(indexer): resolve tags to SHA via git ls-remote
fix(resolver): handle nested include paths correctly
test(fetcher): add sparse-checkout integration tests
docs: update architecture diagram in context.md
```

## Testing Rules

### Coverage
- Unit tests for all public functions
- Integration tests for each milestone
- At least one end-to-end test per major feature
- Every bug fix or code change addressing PR review comments must include corresponding unit tests

### Naming
- Rust: `test_<function>_<scenario>`
- Python: `test_<function>_<scenario>`

### Fixtures
- Store in `tests/fixtures/`
- Use real-world data where possible (Autoware .repos)
- Document fixture purpose in comments

## File Organization

```
launch-plus/
├── Cargo.toml                    # Workspace root
├── crates/
│   └── launch-plus-core/
│       ├── Cargo.toml
│       └── src/
│           ├── lib.rs            # Public API
│           ├── error.rs          # Error types
│           ├── types.rs          # Shared types
│           ├── indexer/          # Indexer module
│           │   ├── mod.rs
│           │   ├── repos.rs
│           │   ├── lockfile.rs
│           │   └── tests.rs
│           ├── resolver/
│           ├── fetcher/
│           ├── builder/
│           └── launcher/
├── python/
│   └── launch_plus/
│       ├── __init__.py
│       ├── _core.pyi
│       ├── cli.py
│       └── verb/
├── tests/                        # Python integration tests
│   ├── fixtures/
│   └── test_*.py
└── pyproject.toml
```
