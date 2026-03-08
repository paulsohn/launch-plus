//! launch-plus-core: Core library for the launch-plus build system
//!
//! This crate provides the core functionality for launch-plus, a Bazel-like
//! build and run system for ROS 2 that enables lazy, on-demand package
//! fetching and building based on actual launch-time dependencies.

pub mod error;
pub mod fetcher;
pub mod indexer;

// Re-export common types
pub use error::{Error, Result};

// Re-export indexer types for lockfile access
pub use indexer::{
    compute_build_order, resolve_dependencies,
    Dependencies, DependencyGraph, DependencyMode, Lockfile, PackageLock, RepoLock,
};

// Re-export fetcher types
pub use fetcher::{fetch_packages, FetchOptions, FetchedPackage};

/// Library version
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
