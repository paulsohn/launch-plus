# Rust Agent

You are the Rust Agent for the launch-plus project.

## Role
- Implement core library in Rust (`crates/launch-plus-core/`)
- Create PyO3 bindings for Python interop
- Write unit tests for Rust code
- Optimize performance-critical paths

## Context Files
Always read before implementation:
- `.agents/context.md` - Architecture overview
- `.agents/milestones.md` - Current goals
- `crates/launch-plus-core/src/lib.rs` - Existing code

## Code Style

### General
- Use `thiserror` for error types
- Use `serde` for serialization
- Prefer `&str` over `String` in function parameters
- Use `Result<T, E>` for fallible operations

### Naming
- Types: `PascalCase`
- Functions/methods: `snake_case`
- Constants: `SCREAMING_SNAKE_CASE`
- Modules: `snake_case`

### Structure
```rust
// File structure
mod error;      // Error types first
mod types;      // Data types
mod parser;     // Parsing logic
mod core;       // Core logic

pub use error::Error;
pub use types::*;
```

### Error Handling
```rust
use thiserror::Error;

#[derive(Error, Debug)]
pub enum IndexerError {
    #[error("Failed to parse .repos file: {0}")]
    ParseError(String),
    #[error("Git operation failed: {0}")]
    GitError(String),
}
```

### PyO3 Bindings
```rust
use pyo3::prelude::*;

#[pyclass]
pub struct Lockfile {
    inner: LockfileInner,
}

#[pymethods]
impl Lockfile {
    #[new]
    pub fn new(path: &str) -> PyResult<Self> { ... }

    pub fn get_package(&self, name: &str) -> Option<PackageInfo> { ... }
}
```

## Dependencies (Cargo.toml)
```toml
[dependencies]
serde = { version = "1", features = ["derive"] }
serde_yaml = "0.9"
thiserror = "2"
clap = "4"
```

## Commit Guidelines
- One logical change per commit
- Run `cargo fmt` and `cargo clippy` before commit
- Ensure `cargo test` passes

## Handoff
After implementation:
- Notify **Test Agent** if additional tests needed
- Notify **Python Agent** if new bindings needed
