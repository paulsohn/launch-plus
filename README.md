# launch-plus

Static analyser and lazy resolver for ROS 2 launch files.

Instead of cloning and building an entire workspace upfront, launch-plus
reads a `.repos` manifest, generates a lockfile, and resolves a launch
file on demand — fetching only the packages actually needed, then
producing a single flattened XML that shows every node, parameter, and
remap that would be active at runtime.

> **Status:** early development — API and CLI flags may change.

## Prerequisites

- Rust toolchain (`cargo`) — https://rustup.rs

## Build

```bash
git clone https://github.com/paulsohn/launch-plus.git
cd launch-plus
cargo build --bin launch-plus --release
```

## Development

```bash
cargo test --workspace
cargo clippy --all-targets
cargo fmt --check
```

## License

Apache-2.0 — see [LICENSE](LICENSE).
