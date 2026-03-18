# Contributing to launch-plus

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

- Rust toolchain (edition 2024, MSRV 1.85) — https://rustup.rs
- `python3` in `PATH`
- (Optional) `pre-commit` — https://pre-commit.com

### Setup

```bash
git clone https://github.com/paulsohn/launch-plus.git
cd launch-plus
pre-commit install   # optional but recommended
```

### Build and test

```bash
cargo build --workspace
cargo test --workspace
cargo clippy --workspace --all-targets -- -D warnings
cargo fmt --all -- --check
```

### Integration test (Autoware)

The Autoware example includes a self-contained integration test. Requires a
sourced ROS 2 environment, `cmake`, and `make`.

```bash
cd example/autoware
bash test-autoware.sh -c   # clean run
bash test-autoware.sh -d   # dirty (reuse fetched packages)
```

## Code style

- Workspace-level clippy lints are enforced (`clippy::all` + `clippy::pedantic` as warnings).
- `cargo fmt` with default settings.
- `unsafe` code is denied by default; use `#[allow(unsafe_code)]` with a `// SAFETY:` comment when necessary.

## License

By contributing, you agree that your contributions will be licensed under the Apache-2.0 license.
