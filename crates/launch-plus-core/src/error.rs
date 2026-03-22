//! Error types for launch-plus-core

use thiserror::Error;

/// Result type alias for launch-plus operations
pub type Result<T> = std::result::Result<T, Error>;

/// Errors that can occur in launch-plus operations
#[derive(Debug, Error)]
pub enum Error {
    /// Failed to parse YAML file
    #[error("failed to parse YAML: {0}")]
    YamlParse(#[from] serde_yaml::Error),

    /// Failed to parse XML file
    #[error("failed to parse XML: {0}")]
    XmlParse(String),

    /// Git operation failed
    #[error("git operation failed: {0}")]
    Git(String),

    /// IO error
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),

    /// Package not found in lockfile
    #[error("package not found: {0}")]
    PackageNotFound(String),

    /// Repository not found in lockfile
    #[error("repository not found: {0}")]
    RepoNotFound(String),

    /// Duplicate package name across repositories
    #[error("duplicate package '{package}' found in repos: {repo1} and {repo2}")]
    DuplicatePackage {
        package: String,
        repo1: String,
        repo2: String,
    },

    /// Invalid version specification
    #[error("invalid version: {0}")]
    InvalidVersion(String),

    /// Launch file parsing error
    #[error("launch file parse error: {0}")]
    LaunchParse(String),

    /// Circular dependency detected
    #[error("circular dependency detected: {0}")]
    CircularDependency(String),

    /// Python resolver failed
    #[error("Python resolver failed: {0}")]
    PythonResolver(String),

    /// Process execution failed
    #[error("process execution failed: {0}")]
    ProcessExecution(String),

    /// Build failed for a specific package
    #[error("build failed for package '{package}': {detail}")]
    BuildFailed { package: String, detail: String },

    /// Unsupported build type in package.xml
    #[error("unsupported build type '{build_type}' for package '{package}'")]
    UnsupportedBuildType { package: String, build_type: String },

    /// Build interrupted by signal (e.g. Ctrl+C)
    #[error("build interrupted by Ctrl+C")]
    BuildInterrupted,

    /// Invalid build flag (e.g. user manually specifying BUILD_TESTING)
    #[error("invalid build flag: {detail}")]
    InvalidBuildFlag { detail: String },
}
