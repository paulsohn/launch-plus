# Contributing to roscope

## Branching Strategy

All development happens through feature branches merged into `devel`.
Do **not** push directly to `devel`.

### Branch naming

| Type | Prefix | Example |
|------|--------|---------|
| Feature | `feat/` | `feat/python-launch-support` |
| Bug fix | `fix/` | `fix/missing-param-file` |
| CI / tooling | `ci/` | `ci/add-release-workflow` |
| Docs | `docs/` | `docs/update-readme` |
| Refactor | `refactor/` | `refactor/resolver-context` |

### Workflow

1. Create a branch from `devel`:
   ```bash
   git checkout devel
   git pull
   git checkout -b feat/my-feature
   ```

2. Make changes, commit, and push:
   ```bash
   git push -u origin feat/my-feature
   ```

3. Open a pull request targeting `devel`.

4. After review, merge via **squash merge** or **rebase merge** (no merge commits).

## Development

### Prerequisites

- Python 3.10+
- **pnpm v10** — to build the web visualizer ([install](https://pnpm.io/installation)); pnpm v11 is not yet supported
- (Optional) `pre-commit` — https://pre-commit.com

### Setup

```bash
git clone https://github.com/paulsohn/roscope.git
cd roscope
pip install -e .
pre-commit install   # optional but recommended
```

### Test and lint

```bash
python -m pytest tests/ -v
ruff check
ruff format --check
mypy roscope/resolver.py
```

### Integration test (Autoware)

The Autoware example includes a self-contained integration test. Requires a
sourced ROS 2 environment and `colcon`.

```bash
cd example/autoware
bash test-autoware.sh -c   # clean run
bash test-autoware.sh -d   # dirty (reuse fetched packages)
```

## Code style

- Linting and formatting are enforced via `ruff`.
- Type checking via `mypy` (gradual adoption — new code should have type hints).

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 license.
