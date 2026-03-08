//! Python bindings for launch-plus-core

use pyo3::prelude::*;

/// A simple hello function for testing the Python bindings
#[pyfunction]
pub fn hello() -> String {
    format!("Hello from launch-plus-core v{}", crate::VERSION)
}
