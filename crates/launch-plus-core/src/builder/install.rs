//! Install layout generation: DSV files, setup scripts, ament_index markers.
//!
//! Generates a colcon-compatible isolated install layout under `install/`.
//! The layout matches what `colcon build --install-base <dir>` produces so that
//! `source install/setup.bash` works identically.

use std::fs;
use std::path::Path;

/// Embedded copy of colcon's `_local_setup_util_sh.py`.
///
/// This script is written to the install root and invoked by `local_setup.bash`
/// (and `.sh`, `.zsh`) to topologically sort packages and generate the shell
/// commands that set up the environment.
const LOCAL_SETUP_UTIL_SH_PY: &str = include_str!("colcon_local_setup_util_sh.py");

// ---------------------------------------------------------------------------
// Root-level layout
// ---------------------------------------------------------------------------

/// Create the root-level install layout files.
///
/// These files live directly under `install_base/` and are responsible for
/// chaining to the parent prefix (e.g. `/opt/ros/humble`) and then sourcing
/// all per-package environment hooks in topological order.
///
/// # Arguments
/// * `install_base` - The install directory (e.g. `install/`)
/// * `parent_prefix` - The parent ROS prefix to chain to (e.g. `/opt/ros/humble`).
///   If `None`, no parent prefix is chained.
pub fn create_root_layout(install_base: &Path, parent_prefix: Option<&str>) -> crate::Result<()> {
    fs::create_dir_all(install_base)?;

    // .colcon_install_layout
    fs::write(install_base.join(".colcon_install_layout"), "isolated\n")?;

    // COLCON_IGNORE (empty marker)
    fs::write(install_base.join("COLCON_IGNORE"), "")?;

    // _local_setup_util_sh.py
    fs::write(
        install_base.join("_local_setup_util_sh.py"),
        LOCAL_SETUP_UTIL_SH_PY,
    )?;

    let parent = parent_prefix.unwrap_or("/opt/ros/humble");

    // setup.bash
    fs::write(install_base.join("setup.bash"), generate_setup_bash(parent))?;

    // setup.sh
    fs::write(
        install_base.join("setup.sh"),
        generate_setup_sh(parent, install_base),
    )?;

    // setup.zsh
    fs::write(install_base.join("setup.zsh"), generate_setup_zsh(parent))?;

    // local_setup.bash
    fs::write(
        install_base.join("local_setup.bash"),
        TEMPLATE_LOCAL_SETUP_BASH,
    )?;

    // local_setup.sh
    fs::write(
        install_base.join("local_setup.sh"),
        generate_local_setup_sh(install_base),
    )?;

    // local_setup.zsh
    fs::write(
        install_base.join("local_setup.zsh"),
        TEMPLATE_LOCAL_SETUP_ZSH,
    )?;

    Ok(())
}

// ---------------------------------------------------------------------------
// Per-package layout
// ---------------------------------------------------------------------------

/// Create the install layout metadata for a single package.
///
/// This writes the DSV manifest, hook scripts, environment scripts,
/// ament_index markers, and colcon-core package metadata needed for
/// `setup.bash` to correctly set up the environment for this package.
///
/// # Arguments
/// * `install_base` - The install root (e.g. `install/`)
/// * `pkg_name` - The package name
/// * `runtime_deps` - Runtime dependencies (exec_depend) of this package
///   that are also in the install set.  Used for the colcon-core metadata
///   that drives topological ordering in `_local_setup_util_sh.py`.
/// * `has_library` - Whether the package installs shared libraries into `lib/`.
///   If true, an `ld_library_path_lib` hook is generated.
pub fn create_package_install_metadata(
    install_base: &Path,
    pkg_name: &str,
    runtime_deps: &[String],
    has_library: bool,
) -> crate::Result<()> {
    let pkg_dir = install_base.join(pkg_name);

    // share/colcon-core/packages/<pkg>
    let colcon_core_dir = pkg_dir.join("share/colcon-core/packages");
    fs::create_dir_all(&colcon_core_dir)?;
    let deps_str = runtime_deps.join(":");
    fs::write(colcon_core_dir.join(pkg_name), &deps_str)?;

    // share/ament_index/resource_index/packages/<pkg>
    let ament_idx_dir = pkg_dir.join("share/ament_index/resource_index/packages");
    fs::create_dir_all(&ament_idx_dir)?;
    fs::write(ament_idx_dir.join(pkg_name), "")?;

    // share/<pkg>/hook/
    let hook_dir = pkg_dir.join("share").join(pkg_name).join("hook");
    fs::create_dir_all(&hook_dir)?;

    // cmake_prefix_path hook (always present)
    fs::write(
        hook_dir.join("cmake_prefix_path.dsv"),
        "prepend-non-duplicate;CMAKE_PREFIX_PATH;\n",
    )?;
    fs::write(
        hook_dir.join("cmake_prefix_path.sh"),
        "# generated from launch-plus builder\n\n\
         _colcon_prepend_unique_value CMAKE_PREFIX_PATH \"$COLCON_CURRENT_PREFIX\"\n",
    )?;

    // ld_library_path_lib hook (only if the package has libraries)
    if has_library {
        fs::write(
            hook_dir.join("ld_library_path_lib.dsv"),
            "prepend-non-duplicate;LD_LIBRARY_PATH;lib\n",
        )?;
        fs::write(
            hook_dir.join("ld_library_path_lib.sh"),
            "# generated from launch-plus builder\n\n\
             _colcon_prepend_unique_value LD_LIBRARY_PATH \"$COLCON_CURRENT_PREFIX/lib\"\n",
        )?;
    }

    // share/<pkg>/environment/
    let env_dir = pkg_dir.join("share").join(pkg_name).join("environment");
    fs::create_dir_all(&env_dir)?;

    // ament_prefix_path
    fs::write(
        env_dir.join("ament_prefix_path.dsv"),
        "prepend-non-duplicate;AMENT_PREFIX_PATH;\n",
    )?;
    fs::write(
        env_dir.join("ament_prefix_path.sh"),
        "# generated from launch-plus builder\n\n\
         ament_prepend_unique_value AMENT_PREFIX_PATH \"$AMENT_CURRENT_PREFIX\"\n",
    )?;

    // path
    fs::write(
        env_dir.join("path.dsv"),
        "prepend-non-duplicate-if-exists;PATH;bin\n",
    )?;
    fs::write(
        env_dir.join("path.sh"),
        "# generated from launch-plus builder\n\n\
         if [ -d \"$AMENT_CURRENT_PREFIX/bin\" ]; then\n  \
         ament_prepend_unique_value PATH \"$AMENT_CURRENT_PREFIX/bin\"\nfi\n",
    )?;

    // share/<pkg>/package.dsv
    let share_pkg_dir = pkg_dir.join("share").join(pkg_name);
    let mut dsv_lines = Vec::new();
    dsv_lines.push(format!(
        "source;share/{pkg_name}/hook/cmake_prefix_path.dsv"
    ));
    dsv_lines.push(format!("source;share/{pkg_name}/hook/cmake_prefix_path.sh"));
    if has_library {
        dsv_lines.push(format!(
            "source;share/{pkg_name}/hook/ld_library_path_lib.dsv"
        ));
        dsv_lines.push(format!(
            "source;share/{pkg_name}/hook/ld_library_path_lib.sh"
        ));
    }
    dsv_lines.push(format!("source;share/{pkg_name}/local_setup.bash"));
    dsv_lines.push(format!("source;share/{pkg_name}/local_setup.dsv"));
    dsv_lines.push(format!("source;share/{pkg_name}/local_setup.sh"));
    dsv_lines.push(format!("source;share/{pkg_name}/local_setup.zsh"));
    let dsv_content = dsv_lines.join("\n") + "\n";
    fs::write(share_pkg_dir.join("package.dsv"), &dsv_content)?;

    // share/<pkg>/local_setup.dsv
    let mut local_dsv = String::new();
    local_dsv.push_str("source;share/");
    local_dsv.push_str(pkg_name);
    local_dsv.push_str("/environment/ament_prefix_path.dsv\n");
    local_dsv.push_str("source;share/");
    local_dsv.push_str(pkg_name);
    local_dsv.push_str("/environment/ament_prefix_path.sh\n");
    local_dsv.push_str("source;share/");
    local_dsv.push_str(pkg_name);
    local_dsv.push_str("/environment/path.dsv\n");
    local_dsv.push_str("source;share/");
    local_dsv.push_str(pkg_name);
    local_dsv.push_str("/environment/path.sh\n");
    fs::write(share_pkg_dir.join("local_setup.dsv"), &local_dsv)?;

    // share/<pkg>/local_setup.bash
    fs::write(
        share_pkg_dir.join("local_setup.bash"),
        generate_pkg_local_setup_bash(pkg_name),
    )?;

    // share/<pkg>/local_setup.sh
    fs::write(
        share_pkg_dir.join("local_setup.sh"),
        generate_pkg_local_setup_sh(pkg_name),
    )?;

    // share/<pkg>/local_setup.zsh
    fs::write(
        share_pkg_dir.join("local_setup.zsh"),
        generate_pkg_local_setup_zsh(pkg_name),
    )?;

    Ok(())
}

// ---------------------------------------------------------------------------
// Template generators — root level
// ---------------------------------------------------------------------------

fn generate_setup_bash(parent_prefix: &str) -> String {
    format!(
        r##"# generated from launch-plus builder

# function to source another script with conditional trace output
_colcon_prefix_chain_bash_source_script() {{
  if [ -f "$1" ]; then
    if [ -n "$COLCON_TRACE" ]; then
      echo "# . \"$1\""
    fi
    . "$1"
  else
    echo "not found: \"$1\"" 1>&2
  fi
}}

# source chained prefixes
COLCON_CURRENT_PREFIX="{parent_prefix}"
_colcon_prefix_chain_bash_source_script "$COLCON_CURRENT_PREFIX/local_setup.bash"

# source this prefix
COLCON_CURRENT_PREFIX="$(builtin cd "`dirname "${{BASH_SOURCE[0]}}"`" > /dev/null && pwd)"
_colcon_prefix_chain_bash_source_script "$COLCON_CURRENT_PREFIX/local_setup.bash"

unset COLCON_CURRENT_PREFIX
unset _colcon_prefix_chain_bash_source_script
"##
    )
}

fn generate_setup_sh(parent_prefix: &str, install_base: &Path) -> String {
    let install_abs = install_base
        .canonicalize()
        .unwrap_or_else(|_| install_base.to_path_buf());
    format!(
        r##"# generated from launch-plus builder

_colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX={install_abs}
if [ ! -z "$COLCON_CURRENT_PREFIX" ]; then
  _colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX="$COLCON_CURRENT_PREFIX"
elif [ ! -d "$_colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX" ]; then
  echo "The build time path \"$_colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX\" doesn't exist. Either source a script for a different shell or set the environment variable \"COLCON_CURRENT_PREFIX\" explicitly." 1>&2
  unset _colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX
  return 1
fi

_colcon_prefix_chain_sh_source_script() {{
  if [ -f "$1" ]; then
    if [ -n "$COLCON_TRACE" ]; then
      echo "# . \"$1\""
    fi
    . "$1"
  else
    echo "not found: \"$1\"" 1>&2
  fi
}}

# source chained prefixes
COLCON_CURRENT_PREFIX="{parent_prefix}"
_colcon_prefix_chain_sh_source_script "$COLCON_CURRENT_PREFIX/local_setup.sh"

# source this prefix
COLCON_CURRENT_PREFIX="$_colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX"
_colcon_prefix_chain_sh_source_script "$COLCON_CURRENT_PREFIX/local_setup.sh"

unset _colcon_prefix_chain_sh_COLCON_CURRENT_PREFIX
unset _colcon_prefix_chain_sh_source_script
unset COLCON_CURRENT_PREFIX
"##,
        install_abs = install_abs.display(),
        parent_prefix = parent_prefix,
    )
}

fn generate_setup_zsh(parent_prefix: &str) -> String {
    format!(
        r##"# generated from launch-plus builder

_colcon_prefix_chain_zsh_source_script() {{
  if [ -f "$1" ]; then
    if [ -n "$COLCON_TRACE" ]; then
      echo "# . \"$1\""
    fi
    . "$1"
  else
    echo "not found: \"$1\"" 1>&2
  fi
}}

# source chained prefixes
COLCON_CURRENT_PREFIX="{parent_prefix}"
_colcon_prefix_chain_zsh_source_script "$COLCON_CURRENT_PREFIX/local_setup.zsh"

# source this prefix
COLCON_CURRENT_PREFIX="$(builtin cd -q "`dirname "${{(%):-%N}}"`" > /dev/null && pwd)"
_colcon_prefix_chain_zsh_source_script "$COLCON_CURRENT_PREFIX/local_setup.zsh"

unset COLCON_CURRENT_PREFIX
unset _colcon_prefix_chain_zsh_source_script
"##
    )
}

fn generate_local_setup_sh(install_base: &Path) -> String {
    let install_abs = install_base
        .canonicalize()
        .unwrap_or_else(|_| install_base.to_path_buf());
    format!(
        r##"# generated from launch-plus builder

_colcon_prefix_sh_COLCON_CURRENT_PREFIX="{install_abs}"
if [ -z "$COLCON_CURRENT_PREFIX" ]; then
  if [ ! -d "$_colcon_prefix_sh_COLCON_CURRENT_PREFIX" ]; then
    echo "The build time path \"$_colcon_prefix_sh_COLCON_CURRENT_PREFIX\" doesn't exist. Either source a script for a different shell or set the environment variable \"COLCON_CURRENT_PREFIX\" explicitly." 1>&2
    unset _colcon_prefix_sh_COLCON_CURRENT_PREFIX
    return 1
  fi
else
  _colcon_prefix_sh_COLCON_CURRENT_PREFIX="$COLCON_CURRENT_PREFIX"
fi

_colcon_prefix_sh_prepend_unique_value() {{
  _listname="$1"
  _value="$2"
  eval _values=\"\$$_listname\"
  _colcon_prefix_sh_prepend_unique_value_IFS="$IFS"
  IFS=":"
  _all_values="$_value"
  _contained_value=""
  for _item in $_values; do
    if [ -z "$_item" ]; then continue; fi
    if [ "$_item" = "$_value" ]; then _contained_value=1; continue; fi
    _all_values="$_all_values:$_item"
  done
  unset _item
  unset _contained_value
  IFS="$_colcon_prefix_sh_prepend_unique_value_IFS"
  unset _colcon_prefix_sh_prepend_unique_value_IFS
  eval export $_listname=\"$_all_values\"
  unset _all_values _values _value _listname
}}

_colcon_prefix_sh_prepend_unique_value COLCON_PREFIX_PATH "$_colcon_prefix_sh_COLCON_CURRENT_PREFIX"
unset _colcon_prefix_sh_prepend_unique_value

if [ -n "$COLCON_PYTHON_EXECUTABLE" ]; then
  if [ ! -f "$COLCON_PYTHON_EXECUTABLE" ]; then
    echo "error: COLCON_PYTHON_EXECUTABLE '$COLCON_PYTHON_EXECUTABLE' doesn't exist"
    return 1
  fi
  _colcon_python_executable="$COLCON_PYTHON_EXECUTABLE"
else
  _colcon_python_executable="/usr/bin/python3"
  if [ ! -f "$_colcon_python_executable" ]; then
    if ! /usr/bin/env python3 --version > /dev/null 2> /dev/null; then
      echo "error: unable to find python3 executable"
      return 1
    fi
    _colcon_python_executable=`/usr/bin/env python3 -c "import sys; print(sys.executable)"`
  fi
fi

_colcon_prefix_sh_source_script() {{
  if [ -f "$1" ]; then
    if [ -n "$COLCON_TRACE" ]; then echo "# . \"$1\""; fi
    . "$1"
  else
    echo "not found: \"$1\"" 1>&2
  fi
}}

_colcon_ordered_commands="$($_colcon_python_executable "$_colcon_prefix_sh_COLCON_CURRENT_PREFIX/_local_setup_util_sh.py" sh)"
unset _colcon_python_executable
eval "${{_colcon_ordered_commands}}"
unset _colcon_ordered_commands
unset _colcon_prefix_sh_source_script
unset _colcon_prefix_sh_COLCON_CURRENT_PREFIX
"##,
        install_abs = install_abs.display()
    )
}

/// `local_setup.bash` — can determine its own path, so no hardcoded fallback needed.
const TEMPLATE_LOCAL_SETUP_BASH: &str = r##"# generated from launch-plus builder

if [ -z "$COLCON_CURRENT_PREFIX" ]; then
  _colcon_prefix_bash_COLCON_CURRENT_PREFIX="$(builtin cd "`dirname "${BASH_SOURCE[0]}"`" > /dev/null && pwd)"
else
  _colcon_prefix_bash_COLCON_CURRENT_PREFIX="$COLCON_CURRENT_PREFIX"
fi

_colcon_prefix_bash_prepend_unique_value() {
  _listname="$1"
  _value="$2"
  eval _values=\"\$$_listname\"
  _colcon_prefix_bash_prepend_unique_value_IFS="$IFS"
  IFS=":"
  _all_values="$_value"
  _contained_value=""
  for _item in $_values; do
    if [ -z "$_item" ]; then continue; fi
    if [ "$_item" = "$_value" ]; then _contained_value=1; continue; fi
    _all_values="$_all_values:$_item"
  done
  unset _item _contained_value
  IFS="$_colcon_prefix_bash_prepend_unique_value_IFS"
  unset _colcon_prefix_bash_prepend_unique_value_IFS
  eval export $_listname=\"$_all_values\"
  unset _all_values _values _value _listname
}

_colcon_prefix_bash_prepend_unique_value COLCON_PREFIX_PATH "$_colcon_prefix_bash_COLCON_CURRENT_PREFIX"
unset _colcon_prefix_bash_prepend_unique_value

if [ -n "$COLCON_PYTHON_EXECUTABLE" ]; then
  if [ ! -f "$COLCON_PYTHON_EXECUTABLE" ]; then
    echo "error: COLCON_PYTHON_EXECUTABLE '$COLCON_PYTHON_EXECUTABLE' doesn't exist"
    return 1
  fi
  _colcon_python_executable="$COLCON_PYTHON_EXECUTABLE"
else
  _colcon_python_executable="/usr/bin/python3"
  if [ ! -f "$_colcon_python_executable" ]; then
    if ! /usr/bin/env python3 --version > /dev/null 2> /dev/null; then
      echo "error: unable to find python3 executable"
      return 1
    fi
    _colcon_python_executable=`/usr/bin/env python3 -c "import sys; print(sys.executable)"`
  fi
fi

_colcon_prefix_sh_source_script() {
  if [ -f "$1" ]; then
    if [ -n "$COLCON_TRACE" ]; then echo "# . \"$1\""; fi
    . "$1"
  else
    echo "not found: \"$1\"" 1>&2
  fi
}

_colcon_ordered_commands="$($_colcon_python_executable "$_colcon_prefix_bash_COLCON_CURRENT_PREFIX/_local_setup_util_sh.py" sh bash)"
unset _colcon_python_executable
eval "${_colcon_ordered_commands}"
unset _colcon_ordered_commands
unset _colcon_prefix_sh_source_script
unset _colcon_prefix_bash_COLCON_CURRENT_PREFIX
"##;

/// `local_setup.zsh` — can determine its own path via zsh-specific syntax.
const TEMPLATE_LOCAL_SETUP_ZSH: &str = r##"# generated from launch-plus builder

if [ -z "$COLCON_CURRENT_PREFIX" ]; then
  _colcon_prefix_zsh_COLCON_CURRENT_PREFIX="$(builtin cd -q "`dirname "${(%):-%N}"`" > /dev/null && pwd)"
else
  _colcon_prefix_zsh_COLCON_CURRENT_PREFIX="$COLCON_CURRENT_PREFIX"
fi

_colcon_prefix_zsh_convert_to_array() {
  local _listname=$1
  local _dollar="$"
  local _split="{="
  local _to_array="(\"$_dollar$_split$_listname}\")"
  eval $_listname=$_to_array
}

_colcon_prefix_zsh_prepend_unique_value() {
  _listname="$1"
  _value="$2"
  eval _values=\"\$$_listname\"
  _colcon_prefix_zsh_prepend_unique_value_IFS="$IFS"
  IFS=":"
  _all_values="$_value"
  _contained_value=""
  _colcon_prefix_zsh_convert_to_array _values
  for _item in $_values; do
    if [ -z "$_item" ]; then continue; fi
    if [ "$_item" = "$_value" ]; then _contained_value=1; continue; fi
    _all_values="$_all_values:$_item"
  done
  unset _item _contained_value
  IFS="$_colcon_prefix_zsh_prepend_unique_value_IFS"
  unset _colcon_prefix_zsh_prepend_unique_value_IFS
  eval export $_listname=\"$_all_values\"
  unset _all_values _values _value _listname
}

_colcon_prefix_zsh_prepend_unique_value COLCON_PREFIX_PATH "$_colcon_prefix_zsh_COLCON_CURRENT_PREFIX"
unset _colcon_prefix_zsh_prepend_unique_value
unset _colcon_prefix_zsh_convert_to_array

if [ -n "$COLCON_PYTHON_EXECUTABLE" ]; then
  if [ ! -f "$COLCON_PYTHON_EXECUTABLE" ]; then
    echo "error: COLCON_PYTHON_EXECUTABLE '$COLCON_PYTHON_EXECUTABLE' doesn't exist"
    return 1
  fi
  _colcon_python_executable="$COLCON_PYTHON_EXECUTABLE"
else
  _colcon_python_executable="/usr/bin/python3"
  if [ ! -f "$_colcon_python_executable" ]; then
    if ! /usr/bin/env python3 --version > /dev/null 2> /dev/null; then
      echo "error: unable to find python3 executable"
      return 1
    fi
    _colcon_python_executable=`/usr/bin/env python3 -c "import sys; print(sys.executable)"`
  fi
fi

_colcon_prefix_sh_source_script() {
  if [ -f "$1" ]; then
    if [ -n "$COLCON_TRACE" ]; then echo "# . \"$1\""; fi
    . "$1"
  else
    echo "not found: \"$1\"" 1>&2
  fi
}

_colcon_ordered_commands="$($_colcon_python_executable "$_colcon_prefix_zsh_COLCON_CURRENT_PREFIX/_local_setup_util_sh.py" sh zsh)"
unset _colcon_python_executable
eval "${_colcon_ordered_commands}"
unset _colcon_ordered_commands
unset _colcon_prefix_sh_source_script
unset _colcon_prefix_zsh_COLCON_CURRENT_PREFIX
"##;

// ---------------------------------------------------------------------------
// Template generators — per-package level
// ---------------------------------------------------------------------------

fn generate_pkg_local_setup_bash(_pkg_name: &str) -> String {
    r##"# generated from launch-plus builder

_this_path=$(builtin cd "`dirname "${BASH_SOURCE[0]}"`" && pwd)
AMENT_CURRENT_PREFIX=$(builtin cd "`dirname "${{BASH_SOURCE[0]}}"`/../.." && pwd)
_package_local_setup_AMENT_CURRENT_PREFIX=$AMENT_CURRENT_PREFIX

if [ -n "$AMENT_TRACE_SETUP_FILES" ]; then
  echo "# . \"$_this_path/local_setup.sh\""
fi
. "$_this_path/local_setup.sh"
unset _this_path

if [ -z "$AMENT_RETURN_ENVIRONMENT_HOOKS" ]; then
  unset AMENT_ENVIRONMENT_HOOKS
fi

AMENT_CURRENT_PREFIX=$_package_local_setup_AMENT_CURRENT_PREFIX

if [ -z "$AMENT_RETURN_ENVIRONMENT_HOOKS" ]; then
  _package_local_setup_IFS=$IFS
  IFS=":"
  for _hook in $AMENT_ENVIRONMENT_HOOKS; do
    AMENT_CURRENT_PREFIX=$_package_local_setup_AMENT_CURRENT_PREFIX
    IFS=$_package_local_setup_IFS
    . "$_hook"
  done
  unset _hook
  IFS=$_package_local_setup_IFS
  unset _package_local_setup_IFS
  unset AMENT_ENVIRONMENT_HOOKS
fi

unset _package_local_setup_AMENT_CURRENT_PREFIX
unset AMENT_CURRENT_PREFIX
"##
    .to_string()
}

fn generate_pkg_local_setup_sh(pkg_name: &str) -> String {
    format!(
        r##"# generated from launch-plus builder

: ${{AMENT_CURRENT_PREFIX:="$COLCON_CURRENT_PREFIX/{pkg_name}"}}
if [ ! -d "$AMENT_CURRENT_PREFIX" ]; then
  if [ -z "$COLCON_CURRENT_PREFIX" ]; then
    echo "The compile time prefix path '$AMENT_CURRENT_PREFIX' doesn't " \
      "exist. Consider sourcing a different extension than '.sh'." 1>&2
  else
    AMENT_CURRENT_PREFIX="$COLCON_CURRENT_PREFIX"
  fi
fi

ament_append_value() {{
  _listname="$1"
  _value="$2"
  eval _values=\"\$$_listname\"
  if [ -z "$_values" ]; then
    eval export $_listname=\"$_value\"
  else
    _ament_append_value_IFS=$IFS
    unset IFS
    eval export $_listname=\"\$$_listname:$_value\"
    IFS=$_ament_append_value_IFS
    unset _ament_append_value_IFS
  fi
  unset _values _value _listname
}}

ament_append_unique_value() {{
  _listname=$1
  _value=$2
  eval _values=\$$_listname
  _duplicate=
  _ament_append_unique_value_IFS=$IFS
  IFS=":"
  if [ "$AMENT_SHELL" = "zsh" ]; then
    ament_zsh_to_array _values
  fi
  for _item in $_values; do
    if [ -z "$_item" ]; then continue; fi
    if [ $_item = $_value ]; then _duplicate=1; fi
  done
  unset _item
  if [ -z "$_duplicate" ]; then
    if [ -z "$_values" ]; then
      eval $_listname=\"$_value\"
    else
      unset IFS
      eval $_listname=\"\$$_listname:$_value\"
    fi
  fi
  IFS=$_ament_append_unique_value_IFS
  unset _ament_append_unique_value_IFS _duplicate _values _value _listname
}}

ament_prepend_unique_value() {{
  _listname="$1"
  _value="$2"
  eval _values=\"\$$_listname\"
  _duplicate=
  _ament_prepend_unique_value_IFS=$IFS
  IFS=":"
  if [ "$AMENT_SHELL" = "zsh" ]; then
    ament_zsh_to_array _values
  fi
  for _item in $_values; do
    if [ -z "$_item" ]; then continue; fi
    if [ "$_item" = "$_value" ]; then _duplicate=1; fi
  done
  unset _item
  if [ -z "$_duplicate" ]; then
    if [ -z "$_values" ]; then
      eval export $_listname=\"$_value\"
    else
      unset IFS
      eval export $_listname=\"$_value:\$$_listname\"
    fi
  fi
  IFS=$_ament_prepend_unique_value_IFS
  unset _ament_prepend_unique_value_IFS _duplicate _values _value _listname
}}

if [ -z "$AMENT_RETURN_ENVIRONMENT_HOOKS" ]; then
  unset AMENT_ENVIRONMENT_HOOKS
fi

ament_append_value AMENT_ENVIRONMENT_HOOKS "$AMENT_CURRENT_PREFIX/share/{pkg_name}/environment/ament_prefix_path.sh"
ament_append_value AMENT_ENVIRONMENT_HOOKS "$AMENT_CURRENT_PREFIX/share/{pkg_name}/environment/path.sh"

if [ -z "$AMENT_RETURN_ENVIRONMENT_HOOKS" ]; then
  _package_local_setup_IFS=$IFS
  IFS=":"
  if [ "$AMENT_SHELL" = "zsh" ]; then
    ament_zsh_to_array AMENT_ENVIRONMENT_HOOKS
  fi
  for _hook in $AMENT_ENVIRONMENT_HOOKS; do
    if [ -f "$_hook" ]; then
      IFS=$_package_local_setup_IFS
      if [ -n "$AMENT_TRACE_SETUP_FILES" ]; then
        echo "# . \"$_hook\""
      fi
      . "$_hook"
    fi
  done
  unset _hook
  IFS=$_package_local_setup_IFS
  unset _package_local_setup_IFS
  unset AMENT_ENVIRONMENT_HOOKS
fi

unset AMENT_CURRENT_PREFIX
"##
    )
}

fn generate_pkg_local_setup_zsh(_pkg_name: &str) -> String {
    r##"# generated from launch-plus builder

AMENT_SHELL=zsh

_this_path=$(builtin cd -q "`dirname "${(%):-%N}"`" > /dev/null && pwd)
AMENT_CURRENT_PREFIX=$(builtin cd -q "`dirname "${(%):-%N}"`/../.." > /dev/null && pwd)
_package_local_setup_AMENT_CURRENT_PREFIX=$AMENT_CURRENT_PREFIX

ament_zsh_to_array() {
  local _listname=$1
  local _dollar="$"
  local _split="{="
  local _to_array="(\"$_dollar$_split$_listname}\")"
  eval $_listname=$_to_array
}

if [ -n "$AMENT_TRACE_SETUP_FILES" ]; then
  echo "# . \"$_this_path/local_setup.sh\""
fi
. "$_this_path/local_setup.sh"
unset _this_path

if [ -z "$AMENT_RETURN_ENVIRONMENT_HOOKS" ]; then
  unset AMENT_ENVIRONMENT_HOOKS
fi

AMENT_CURRENT_PREFIX=$_package_local_setup_AMENT_CURRENT_PREFIX

if [ -z "$AMENT_RETURN_ENVIRONMENT_HOOKS" ]; then
  _package_local_setup_IFS=$IFS
  IFS=":"
  for _hook in $AMENT_ENVIRONMENT_HOOKS; do
    AMENT_CURRENT_PREFIX=$_package_local_setup_AMENT_CURRENT_PREFIX
    IFS=$_package_local_setup_IFS
    . "$_hook"
  done
  unset _hook
  IFS=$_package_local_setup_IFS
  unset _package_local_setup_IFS
  unset AMENT_ENVIRONMENT_HOOKS
fi

unset _package_local_setup_AMENT_CURRENT_PREFIX
unset AMENT_CURRENT_PREFIX
"##
    .to_string()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    #[test]
    fn test_create_root_layout() {
        let tmp = TempDir::new().unwrap();
        let install = tmp.path().join("install");
        create_root_layout(&install, Some("/opt/ros/humble")).unwrap();

        // Check layout marker
        let layout = fs::read_to_string(install.join(".colcon_install_layout")).unwrap();
        assert_eq!(layout.trim(), "isolated");

        // Check COLCON_IGNORE exists
        assert!(install.join("COLCON_IGNORE").exists());

        // Check all setup scripts exist
        assert!(install.join("setup.bash").exists());
        assert!(install.join("setup.sh").exists());
        assert!(install.join("setup.zsh").exists());
        assert!(install.join("local_setup.bash").exists());
        assert!(install.join("local_setup.sh").exists());
        assert!(install.join("local_setup.zsh").exists());
        assert!(install.join("_local_setup_util_sh.py").exists());

        // Check parent prefix is referenced in setup.bash
        let setup_bash = fs::read_to_string(install.join("setup.bash")).unwrap();
        assert!(setup_bash.contains("/opt/ros/humble"));
    }

    #[test]
    fn test_create_package_install_metadata() {
        let tmp = TempDir::new().unwrap();
        let install = tmp.path().join("install");
        fs::create_dir_all(&install).unwrap();

        let deps = vec!["rclcpp".to_string(), "std_msgs".to_string()];
        create_package_install_metadata(&install, "my_pkg", &deps, true).unwrap();

        let pkg_dir = install.join("my_pkg");

        // colcon-core deps
        let deps_file =
            fs::read_to_string(pkg_dir.join("share/colcon-core/packages/my_pkg")).unwrap();
        assert_eq!(deps_file, "rclcpp:std_msgs");

        // ament_index marker
        assert!(
            pkg_dir
                .join("share/ament_index/resource_index/packages/my_pkg")
                .exists()
        );

        // cmake_prefix_path hook
        let cmake_dsv =
            fs::read_to_string(pkg_dir.join("share/my_pkg/hook/cmake_prefix_path.dsv")).unwrap();
        assert!(cmake_dsv.contains("prepend-non-duplicate;CMAKE_PREFIX_PATH;"));

        // ld_library_path hook (has_library=true)
        assert!(
            pkg_dir
                .join("share/my_pkg/hook/ld_library_path_lib.dsv")
                .exists()
        );

        // environment hooks
        assert!(
            pkg_dir
                .join("share/my_pkg/environment/ament_prefix_path.dsv")
                .exists()
        );
        assert!(pkg_dir.join("share/my_pkg/environment/path.dsv").exists());

        // package.dsv
        let pkg_dsv = fs::read_to_string(pkg_dir.join("share/my_pkg/package.dsv")).unwrap();
        assert!(pkg_dsv.contains("source;share/my_pkg/hook/cmake_prefix_path.dsv"));
        assert!(pkg_dsv.contains("source;share/my_pkg/hook/ld_library_path_lib.dsv"));
        assert!(pkg_dsv.contains("source;share/my_pkg/local_setup.bash"));

        // local_setup scripts
        assert!(pkg_dir.join("share/my_pkg/local_setup.bash").exists());
        assert!(pkg_dir.join("share/my_pkg/local_setup.sh").exists());
        assert!(pkg_dir.join("share/my_pkg/local_setup.zsh").exists());
        assert!(pkg_dir.join("share/my_pkg/local_setup.dsv").exists());

        // local_setup.sh references this package
        let local_sh = fs::read_to_string(pkg_dir.join("share/my_pkg/local_setup.sh")).unwrap();
        assert!(local_sh.contains("my_pkg/environment/ament_prefix_path.sh"));
    }

    #[test]
    fn test_no_ld_library_path_when_no_library() {
        let tmp = TempDir::new().unwrap();
        let install = tmp.path().join("install");
        fs::create_dir_all(&install).unwrap();

        create_package_install_metadata(&install, "pure_py_pkg", &[], false).unwrap();

        let pkg_dir = install.join("pure_py_pkg");

        // ld_library_path hook should NOT exist
        assert!(
            !pkg_dir
                .join("share/pure_py_pkg/hook/ld_library_path_lib.dsv")
                .exists()
        );

        // package.dsv should not reference it
        let pkg_dsv = fs::read_to_string(pkg_dir.join("share/pure_py_pkg/package.dsv")).unwrap();
        assert!(!pkg_dsv.contains("ld_library_path"));
    }
}
