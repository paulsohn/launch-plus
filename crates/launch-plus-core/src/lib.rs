//! launch-plus-core: Core library for the launch-plus build system
//!
//! This crate provides the core functionality for launch-plus, a Bazel-like
//! build and run system for ROS 2 that enables lazy, on-demand package
//! fetching and building based on actual launch-time dependencies.
//!
//! # Modules
//!
//! - `indexer`: Parse .repos files and generate lockfiles
//! - `resolver`: Parse launch files and resolve dependencies
//! - `fetcher`: Partial git clone via sparse-checkout
//! - `builder`: Selective build orchestration (native cmake/setuptools backend)
//! - `executor`: Process spawning and lifecycle management

pub mod builder;
pub mod error;
pub mod fetcher;
pub mod indexer;
pub mod locator;
pub mod orchestrator;
pub mod parser;
pub mod resolver;
pub mod rosdep;

// Re-export common types
pub use error::{Error, Result};

// Re-export resolver types for convenience
pub use resolver::{
    Condition, ConditionKind, DependencyKind, EventHandlerKind, FileDependency, IncludeArgContext,
    LaunchElement, LaunchFile, LaunchInclude, ParsedLaunchFile, ResolveOptions,
    ResolvedEventAction, ResolvedLaunch, ResolvedNode, SemanticNode, SubstitutionContext,
    extract_file_dependency, parse_launch_xml, parse_launch_yaml, render_resolved_xml,
    resolve_launch, semantic_eq,
};

// Re-export locator types
pub use locator::{PackageLocator, locator_from_lockfile, locator_from_workspace};

// Re-export indexer types for lockfile access
pub use indexer::{
    Dependencies, DependencyGraph, DependencyMode, Lockfile, PackageLock, RepoLock,
    compute_build_order, read_package_build_type, resolve_dependencies,
};

// Re-export fetcher types
pub use fetcher::{FetchOptions, FetchedPackage, fetch_packages};

// Re-export builder types
pub use builder::{BuildOptions, BuildPlan, execute_build, plan_build_from_packages};

// Re-export rosdep helpers
pub use rosdep::{ensure_rosdep_updated, rosdep_install};

// Re-export orchestrator for resolve workflow
pub use orchestrator::{ResolveResult, ResolveWorkflowOptions, resolve_launch_recursive};

/// Library version
pub const VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(feature = "python")]
mod python;

#[cfg(feature = "python")]
use pyo3::prelude::*;

/// Python module initialization
#[cfg(feature = "python")]
#[pymodule]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", VERSION)?;
    m.add_function(wrap_pyfunction!(python::hello, m)?)?;
    Ok(())
}
