# Coding Rules & Conventions

## Python Rules

### Style
- Run `ruff check --fix` and `ruff format` before commit
- Type hints required for all public functions
- Use `pathlib.Path` not string paths in new code

### Dependencies
- Minimize external dependencies
- Required: `click` for CLI, `lark` for grammar parsing, `pyyaml`
- Optional: `ros2cli` for verb integration

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
- Python: `test_<function>_<scenario>`

### Fixtures
- Store in `tests/fixtures/`
- Use real-world data where possible (Autoware .repos)
- Document fixture purpose in comments

## File Organization

```
roscope/
├── pyproject.toml
├── roscope/
│   ├── __init__.py
│   ├── __main__.py               # Entry point
│   ├── cli.py                    # Click CLI
│   ├── indexer.py                # .repos → lockfile
│   ├── fetcher.py                # git sparse-checkout
│   ├── resolver.py               # Launch file resolution + shims
│   ├── orchestrator.py           # Resolve + fetch coordination
│   ├── renderer.py               # Resolved IR → XML output
│   ├── builder.py                # colcon build orchestration
│   ├── rosdep.py                 # System dependency resolution
│   ├── locator.py                # Package location
│   ├── types.py                  # Shared dataclasses
│   ├── exceptions.py             # Exception hierarchy
│   ├── entities/
│   │   ├── expose.py             # @expose_action / @expose_substitution
│   │   ├── substitution.py       # Substitution ABC
│   │   ├── substitutions/        # Typed substitution objects
│   │   └── actions/              # @expose_action handlers
│   ├── parsers/
│   │   ├── entity.py             # Entity ABC
│   │   ├── xml_parser.py         # XML → Entity tree
│   │   ├── yaml_parser.py        # YAML → Entity tree
│   │   ├── parse_substitution.py # Lark grammar → Substitution objects
│   │   └── grammar.lark          # Substitution grammar
│   └── verb/                     # ros2 CLI verb integration
├── tests/
│   ├── conftest.py
│   ├── test_resolver.py
│   └── ...
└── .agents/                      # AI agent instructions
```
