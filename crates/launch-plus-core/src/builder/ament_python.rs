//! ament_python build backend: Python package build and install.
//!
//! Handles two installation modes:
//! - **Regular install**: `python3 setup.py install --prefix=<install_dir>`
//! - **Symlink install**: `python3 setup.py develop --prefix=<install_dir>`
//!
//! Also copies `package.xml` into the install tree for ament indexing.

use std::fs;

use super::ament_cmake::{PackageBuildContext, run_command};

/// Build an ament_python package.
///
/// Steps:
/// 1. Install Python package via setup.py (or pip for pyproject.toml-only)
/// 2. Copy `package.xml` to `share/<pkg>/` in the install tree
pub fn build_ament_python(ctx: &PackageBuildContext) -> crate::Result<()> {
    fs::create_dir_all(&ctx.build_dir)?;
    fs::create_dir_all(&ctx.log_dir)?;

    // Determine build entry point.
    let has_setup_py = ctx.src_dir.join("setup.py").exists();
    let has_setup_cfg = ctx.src_dir.join("setup.cfg").exists();
    let has_pyproject = ctx.src_dir.join("pyproject.toml").exists();

    if has_setup_py {
        // Classic setuptools path.
        if ctx.options.symlink_install {
            setup_py_develop(ctx)?;
        } else {
            setup_py_install(ctx)?;
        }
    } else if has_setup_cfg || has_pyproject {
        // Modern Python packaging — use pip install.
        pip_install(ctx)?;
    } else {
        return Err(crate::Error::BuildFailed {
            package: ctx.pkg_name.clone(),
            detail: "no setup.py, setup.cfg, or pyproject.toml found".to_string(),
        });
    }

    // Copy package.xml to share/<pkg>/ so ament can index it.
    copy_package_xml(ctx)?;

    Ok(())
}

/// `python3 setup.py install --prefix=<install_dir> --record <build_dir>/install_manifest.txt`
fn setup_py_install(ctx: &PackageBuildContext) -> crate::Result<()> {
    let record_file = ctx.build_dir.join("install_manifest.txt");
    let args = vec![
        "setup.py".to_string(),
        "install".to_string(),
        format!("--prefix={}", ctx.install_dir.to_string_lossy()),
        format!("--record={}", record_file.to_string_lossy()),
    ];

    run_command(ctx, "python3", &args, Some(&ctx.src_dir), "install")
}

/// `python3 setup.py develop --prefix=<install_dir>` (symlink install)
fn setup_py_develop(ctx: &PackageBuildContext) -> crate::Result<()> {
    let args = vec![
        "setup.py".to_string(),
        "develop".to_string(),
        format!("--prefix={}", ctx.install_dir.to_string_lossy()),
    ];

    run_command(ctx, "python3", &args, Some(&ctx.src_dir), "develop")
}

/// `pip3 install --prefix=<install_dir> --no-deps --no-build-isolation .`
///
/// Used for modern Python packages that use pyproject.toml or setup.cfg
/// without setup.py.
fn pip_install(ctx: &PackageBuildContext) -> crate::Result<()> {
    let mut args = vec![
        "install".to_string(),
        format!("--prefix={}", ctx.install_dir.to_string_lossy()),
        "--no-deps".to_string(),
        "--no-build-isolation".to_string(),
    ];

    if ctx.options.symlink_install {
        args.push("--editable".to_string());
    }

    args.push(".".to_string());

    run_command(ctx, "pip3", &args, Some(&ctx.src_dir), "pip-install")
}

/// Copy `package.xml` from source to `<install_dir>/share/<pkg>/package.xml`.
///
/// This is needed for ament to discover the package after installation.
fn copy_package_xml(ctx: &PackageBuildContext) -> crate::Result<()> {
    let src_xml = ctx.src_dir.join("package.xml");
    if !src_xml.exists() {
        // Not all Python packages have package.xml (though ROS 2 ones should).
        return Ok(());
    }

    let dest_dir = ctx.install_dir.join("share").join(&ctx.pkg_name);
    fs::create_dir_all(&dest_dir)?;
    let dest_xml = dest_dir.join("package.xml");

    if ctx.options.symlink_install {
        // Create symlink instead of copy.
        #[cfg(unix)]
        {
            // Remove existing symlink/file if present.
            if dest_xml.exists() || dest_xml.is_symlink() {
                fs::remove_file(&dest_xml)?;
            }
            std::os::unix::fs::symlink(&src_xml, &dest_xml)?;
        }
        #[cfg(not(unix))]
        {
            fs::copy(&src_xml, &dest_xml)?;
        }
    } else {
        fs::copy(&src_xml, &dest_xml)?;
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;
    use std::path::Path;
    use std::sync::atomic::AtomicBool;

    use super::super::ament_cmake::BuildOptionsRef;

    static DUMMY_INTERRUPTED: AtomicBool = AtomicBool::new(false);

    fn make_ctx(tmp: &Path) -> PackageBuildContext {
        PackageBuildContext {
            pkg_name: "test_py_pkg".to_string(),
            src_dir: tmp.join("src"),
            build_dir: tmp.join("build"),
            install_dir: tmp.join("install"),
            log_dir: tmp.join("log"),
            env: HashMap::new(),
            options: BuildOptionsRef {
                parallel_workers: 4,
                cmake_args: vec![],
                make_args: vec![],
                symlink_install: false,
                test_mode: false,
                dry_run: true,
            },
            interrupted: &DUMMY_INTERRUPTED,
        }
    }

    #[test]
    fn test_no_build_entry_fails() {
        let tmp = tempfile::TempDir::new().unwrap();
        let mut ctx = make_ctx(tmp.path());
        ctx.options.dry_run = false; // Need to actually check for files
        fs::create_dir_all(&ctx.src_dir).unwrap();

        let result = build_ament_python(&ctx);
        assert!(result.is_err());
        let err = result.unwrap_err().to_string();
        assert!(err.contains("no setup.py"));
    }

    #[test]
    fn test_dry_run_with_setup_py() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = make_ctx(tmp.path());
        fs::create_dir_all(&ctx.src_dir).unwrap();
        fs::write(ctx.src_dir.join("setup.py"), "# stub").unwrap();

        let result = build_ament_python(&ctx);
        assert!(result.is_ok());
    }

    #[test]
    fn test_copy_package_xml() {
        let tmp = tempfile::TempDir::new().unwrap();
        let ctx = make_ctx(tmp.path());
        fs::create_dir_all(&ctx.src_dir).unwrap();
        fs::create_dir_all(&ctx.install_dir).unwrap();
        fs::write(
            ctx.src_dir.join("package.xml"),
            "<package><name>test_py_pkg</name></package>",
        )
        .unwrap();

        copy_package_xml(&ctx).unwrap();

        let dest = ctx
            .install_dir
            .join("share")
            .join("test_py_pkg")
            .join("package.xml");
        assert!(dest.exists());
    }

    #[cfg(unix)]
    #[test]
    fn test_symlink_package_xml() {
        let tmp = tempfile::TempDir::new().unwrap();
        let mut ctx = make_ctx(tmp.path());
        ctx.options.symlink_install = true;
        fs::create_dir_all(&ctx.src_dir).unwrap();
        fs::create_dir_all(&ctx.install_dir).unwrap();
        fs::write(
            ctx.src_dir.join("package.xml"),
            "<package><name>test_py_pkg</name></package>",
        )
        .unwrap();

        copy_package_xml(&ctx).unwrap();

        let dest = ctx
            .install_dir
            .join("share")
            .join("test_py_pkg")
            .join("package.xml");
        assert!(dest.is_symlink());
    }
}
