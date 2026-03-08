//! launch-plus CLI: Bazel-like build and run system for ROS 2

use anyhow::{Context, Result};
use clap::{Parser, Subcommand};
use launch_plus_core::indexer::{
    blobless_clone, discover_packages, generate_lockfile, parse_lockfile, parse_repos,
    resolve_version_local, serialize_lockfile, Lockfile,
};
use std::fs;
use std::path::Path;

/// Bazel-like build and run system for ROS 2
#[derive(Parser)]
#[command(name = "launch-plus")]
#[command(author, version, about, long_about = None)]
struct Cli {
    /// Verbose output
    #[arg(short, long, global = true)]
    verbose: bool,

    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Generate lockfile from .repos files
    #[command(override_usage = "launch-plus index [OPTIONS] [FILES]...")]
    Index {
        /// .repos files to process (default: manifest.repos)
        #[arg(default_value = "manifest.repos")]
        files: Vec<String>,

        /// Append to existing lockfile instead of overwriting
        #[arg(long)]
        append: bool,

        /// Output lockfile path (default: manifest.lock.repos)
        #[arg(short, long, alias = "lockfile")]
        output: Option<String>,

        /// Source directory for cloning repositories
        /// Repos are cloned to <src>/<workspace_path> and reused on subsequent runs
        #[arg(long, default_value = "src")]
        src: String,

        /// Disable recursive cloning of submodules
        /// By default, submodules are cloned recursively with --shallow-submodules
        #[arg(long)]
        no_recurse_submodules: bool,

        /// Verify lockfile consistency without cloning or making HTTP requests
        /// Exit codes: 0=consistent, 1=mismatch, 2=missing repo in lockfile
        #[arg(long)]
        verify: bool,
    },

    /// Update lockfile with latest SHAs by re-resolving refs
    #[command(override_usage = "launch-plus update [OPTIONS] [REPOS]...")]
    Update {
        /// Specific workspace paths to update (default: all repos with a ref)
        repos: Vec<String>,

        /// Input lockfile path (default: manifest.lock.repos)
        #[arg(short, long, alias = "lockfile", default_value = "manifest.lock.repos")]
        input: String,

        /// Output lockfile path (default: same as input)
        #[arg(short, long, alias = "output-lockfile")]
        output: Option<String>,

        /// Source directory for local clones (default: src)
        #[arg(long, default_value = "src")]
        src: String,

        /// Show what would change without updating
        #[arg(long)]
        diff: bool,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let verbose = cli.verbose;

    // Initialize tracing
    let subscriber = tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(if verbose { "debug" } else { "info" })
        .finish();
    tracing::subscriber::set_global_default(subscriber)?;

    match cli.command {
        Commands::Index {
            files,
            append,
            output,
            src,
            no_recurse_submodules,
            verify,
        } => {
            if verify {
                let exit_code = cmd_verify(&files, output.as_deref())?;
                std::process::exit(exit_code);
            } else {
                cmd_index(&files, append, output.as_deref(), &src, !no_recurse_submodules)?;
            }
        }
        Commands::Update {
            repos,
            input,
            output,
            src,
            diff,
        } => {
            cmd_update(&repos, &input, output.as_deref(), &src, diff)?;
        }
    }

    Ok(())
}

/// Execute the index command: generate lockfile from .repos files
fn cmd_index(
    files: &[String],
    append: bool,
    output: Option<&str>,
    src_dir: &str,
    recurse_submodules: bool,
) -> Result<()> {
    let output_path = output.unwrap_or("manifest.lock.repos");
    let src_path = Path::new(src_dir);

    tracing::info!("Using source directory: {}", src_path.display());

    // Start with existing lockfile if appending
    let mut lockfile = if append && Path::new(output_path).exists() {
        let content = fs::read_to_string(output_path)
            .with_context(|| format!("failed to read existing lockfile: {output_path}"))?;
        parse_lockfile(&content).with_context(|| "failed to parse existing lockfile")?
    } else {
        Lockfile::new()
    };

    // Process each .repos file
    for file in files {
        tracing::info!("Processing {}", file);

        let content = fs::read_to_string(file)
            .with_context(|| format!("failed to read .repos file: {file}"))?;

        let repos = parse_repos(&content).with_context(|| format!("failed to parse {file}"))?;

        tracing::info!(
            "Found {} repositories in {}",
            repos.repositories.len(),
            file
        );

        // Atomic update: remove old entries for repos we're about to process
        for workspace_path in repos.repositories.keys() {
            if lockfile.repositories.contains_key(workspace_path) {
                tracing::debug!(
                    "Removing old entry for {} before re-indexing",
                    workspace_path
                );
                lockfile.remove_repo(workspace_path);
            }
        }

        // Generate lockfile entries for this .repos file
        let file_lockfile = generate_lockfile(&repos, Some(src_path), recurse_submodules)
            .with_context(|| format!("failed to index {file}"))?;

        // Merge into main lockfile
        for (key, repo) in file_lockfile.repositories {
            lockfile.repositories.insert(key, repo);
        }

        for (name, pkg) in file_lockfile.packages {
            if let Some(existing) = lockfile.packages.get(&name) {
                if !repos.repositories.contains_key(&existing.repo) {
                    tracing::warn!(
                        "Package {} already exists in {}, skipping from {}",
                        name,
                        existing.repo,
                        pkg.repo
                    );
                    continue;
                }
            }
            lockfile.packages.insert(name, pkg);
        }
    }

    // Write lockfile
    let yaml = serialize_lockfile(&lockfile).with_context(|| "failed to serialize lockfile")?;
    fs::write(output_path, &yaml)
        .with_context(|| format!("failed to write lockfile: {output_path}"))?;

    tracing::info!(
        "Wrote lockfile with {} repositories and {} packages to {}",
        lockfile.repositories.len(),
        lockfile.packages.len(),
        output_path
    );

    println!(
        "Generated {} with {} repositories and {} packages",
        output_path,
        lockfile.repositories.len(),
        lockfile.packages.len()
    );

    Ok(())
}

/// Verify lockfile consistency with .repos files (no HTTP requests, no cloning)
fn cmd_verify(files: &[String], output: Option<&str>) -> Result<i32> {
    let lockfile_path = output.unwrap_or("manifest.lock.repos");

    if !Path::new(lockfile_path).exists() {
        println!("Error: Lockfile not found: {}", lockfile_path);
        println!("Run 'launch-plus index' to generate it.");
        return Ok(2);
    }

    let lockfile_content = fs::read_to_string(lockfile_path)
        .with_context(|| format!("failed to read lockfile: {lockfile_path}"))?;
    let lockfile = parse_lockfile(&lockfile_content)
        .with_context(|| format!("failed to parse lockfile: {lockfile_path}"))?;

    println!("Verifying against {}...", lockfile_path);

    let mut all_ok = true;
    let mut missing = false;
    let mut total_repos = 0;

    for file in files {
        let content = fs::read_to_string(file)
            .with_context(|| format!("failed to read .repos file: {file}"))?;
        let repos = parse_repos(&content).with_context(|| format!("failed to parse {file}"))?;

        for (workspace_path, entry) in &repos.repositories {
            total_repos += 1;

            let Some(lock_entry) = lockfile.repositories.get(workspace_path) else {
                println!("  {}: MISSING from lockfile", workspace_path);
                missing = true;
                continue;
            };

            let is_sha = entry.version.len() == 40
                && entry.version.chars().all(|c| c.is_ascii_hexdigit());

            let (expected, actual, label) = if is_sha {
                (&entry.version, &lock_entry.version, "sha")
            } else {
                let actual = lock_entry.version_ref.as_ref().unwrap_or(&lock_entry.version);
                (&entry.version, actual, "ref")
            };

            if expected == actual {
                println!("  {}: OK ({}={})", workspace_path, label, expected);
            } else {
                println!("  {}: MISMATCH", workspace_path);
                println!("    .repos {}: {}", label, expected);
                println!("    lockfile {}: {}", label, actual);
                all_ok = false;
            }
        }
    }

    println!();

    if missing {
        println!(
            "Error: {} has repositories not in lockfile.",
            files.join(", ")
        );
        println!("Run 'launch-plus index' to regenerate.");
        return Ok(2);
    }

    if !all_ok {
        println!("Error: Lockfile inconsistent with .repos files.");
        println!("Run 'launch-plus index' to regenerate.");
        return Ok(1);
    }

    println!("All {} repositories consistent.", total_repos);
    Ok(0)
}

/// Execute the update command: update lockfile with latest SHAs
fn cmd_update(
    filter: &[String],
    input: &str,
    output: Option<&str>,
    src_dir: &str,
    diff_only: bool,
) -> Result<()> {
    let output_path = output.unwrap_or(input);
    let src_path = Path::new(src_dir);

    let content =
        fs::read_to_string(input).with_context(|| format!("failed to read lockfile: {input}"))?;
    let mut lockfile =
        parse_lockfile(&content).with_context(|| format!("failed to parse lockfile: {input}"))?;

    tracing::info!(
        "Loaded lockfile with {} repositories",
        lockfile.repositories.len()
    );

    if !filter.is_empty() {
        tracing::info!("Updating only: {:?}", filter);
    }

    let mut updates: Vec<(String, String, String)> = Vec::new();
    let mut errors: Vec<(String, String)> = Vec::new();

    for (workspace_path, repo) in &lockfile.repositories {
        if !filter.is_empty() && !filter.iter().any(|f| f == workspace_path) {
            continue;
        }

        let Some(ref version_ref) = repo.version_ref else {
            tracing::debug!(
                "Skipping {} (pinned to SHA, no ref to update)",
                workspace_path,
            );
            continue;
        };

        tracing::debug!(
            "Checking {} (ref={}, current={})",
            workspace_path,
            version_ref,
            &repo.version[..8.min(repo.version.len())]
        );

        let repo_dir = src_path.join(workspace_path);
        let resolve_result = if !repo_dir.join(".git").exists() {
            tracing::debug!("No local clone for {}, blobless-cloning first", workspace_path);
            blobless_clone(&repo.url, &repo_dir)
                .and_then(|()| resolve_version_local(&repo_dir, version_ref))
        } else {
            resolve_version_local(&repo_dir, version_ref)
        };

        match resolve_result {
            Ok(new_sha) => {
                if new_sha != repo.version {
                    updates.push((workspace_path.clone(), repo.version.clone(), new_sha));
                }
            }
            Err(e) => {
                errors.push((workspace_path.clone(), e.to_string()));
            }
        }
    }

    for (workspace_path, error) in &errors {
        tracing::warn!("Failed to resolve {}: {}", workspace_path, error);
    }

    if updates.is_empty() {
        println!("All repositories are up to date.");
        return Ok(());
    }

    println!("Found {} updates:", updates.len());
    for (workspace_path, old_sha, new_sha) in &updates {
        println!(
            "  {} {} -> {}",
            workspace_path,
            &old_sha[..8.min(old_sha.len())],
            &new_sha[..8.min(new_sha.len())]
        );
    }

    if diff_only {
        println!("\nRun without --diff to apply updates.");
        return Ok(());
    }

    println!("\nApplying updates...");
    for (workspace_path, _old_sha, new_sha) in &updates {
        let repo = lockfile.repositories.get_mut(workspace_path).unwrap();
        let old_packages = repo.packages.clone();

        repo.version = new_sha.clone();

        let repo_dir = src_path.join(workspace_path);
        if repo_dir.exists() {
            tracing::debug!("Re-scanning packages in {}", workspace_path);
            match discover_packages(&repo.url, new_sha, Some(&repo_dir), true) {
                Ok(packages) => {
                    for pkg_name in &old_packages {
                        lockfile.packages.remove(pkg_name);
                    }

                    let new_package_names: Vec<String> =
                        packages.iter().map(|p| p.name.clone()).collect();
                    repo.packages = new_package_names;

                    for pkg in packages {
                        lockfile.packages.insert(
                            pkg.name,
                            launch_plus_core::indexer::PackageLock {
                                repo: workspace_path.clone(),
                                path: pkg.path,
                                dependencies: pkg.dependencies,
                            },
                        );
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        "Failed to re-scan packages in {}: {}. Keeping old package list.",
                        workspace_path,
                        e
                    );
                }
            }
        }
    }

    let yaml = serialize_lockfile(&lockfile).with_context(|| "failed to serialize lockfile")?;
    fs::write(output_path, &yaml)
        .with_context(|| format!("failed to write lockfile: {output_path}"))?;

    println!(
        "Updated {} with {} repositories and {} packages",
        output_path,
        lockfile.repositories.len(),
        lockfile.packages.len()
    );

    Ok(())
}
