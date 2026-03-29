"""LaunchConfiguration and DeferredDefault — Python-shim substitutions.

These are NOT actions.  They implement ``perform(context)`` for value
resolution, not ``execute()``.
"""

from __future__ import annotations


class _DeferredDefault:
    """Wraps an unresolved DeclareLaunchArgument default_value.

    Stored in ``_launch_configurations`` instead of a resolved string.
    Resolution is deferred until the value is actually read via
    ``_LaunchConfiguration.perform()``.  This avoids eagerly calling
    ``FindPackageShare.perform()`` for packages that may not be installed
    when the arg is never actually used (e.g. gated by a false condition).
    """

    def __init__(self, default_value):
        self.default_value = default_value

    def resolve(self, context):
        """Resolve the deferred substitutions to a string."""
        dv = self.default_value
        if hasattr(dv, "perform"):
            result = dv.perform(context)
            return str(result) if result is not None else str(dv)
        if isinstance(dv, list):
            parts = []
            for sub in dv:
                if hasattr(sub, "perform"):
                    result = sub.perform(context)
                    parts.append(str(result) if result is not None else str(sub))
                else:
                    parts.append(str(sub))
            return "".join(parts)
        return str(dv)


class _LaunchConfiguration:
    """Substitution that resolves to a launch configuration value at runtime."""

    def __init__(self, variable_name, default=None, **kwargs):
        self._name = variable_name
        self._default = default

    def perform(self, context):
        if context and hasattr(context, "_launch_configurations"):
            if self._name in context._launch_configurations:
                value = context._launch_configurations[self._name]
                # Resolve deferred defaults on first read.
                if isinstance(value, _DeferredDefault):
                    resolved = value.resolve(context)
                    context._launch_configurations[self._name] = resolved
                    return resolved
                return value
            if self._default is not None:
                return str(self._default)
        return None

    def __str__(self):
        return self._name
