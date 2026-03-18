//! Greedy parallel build scheduler with configurable worker pool.
//!
//! Dispatches package builds as soon as all dependencies are satisfied,
//! using `std::thread::scope` with N worker threads.
