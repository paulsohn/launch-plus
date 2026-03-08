use clap::Parser;

/// launch-plus: Bazel-like build and run system for ROS 2.
///
/// Resolves launch file dependencies on demand, fetching and building
/// only the packages that are actually needed.
#[derive(Parser)]
#[command(name = "launch-plus", version, about)]
struct Cli {
    /// Enable verbose logging
    #[arg(short, long)]
    verbose: bool,
}

fn main() -> anyhow::Result<()> {
    let cli = Cli::parse();

    let filter = if cli.verbose { "debug" } else { "info" };
    tracing_subscriber::fmt()
        .with_env_filter(filter)
        .with_writer(std::io::stderr)
        .init();

    eprintln!("launch-plus v{}", launch_plus_core::VERSION);
    Ok(())
}
