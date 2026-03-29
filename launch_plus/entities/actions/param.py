"""Parameter-related tracked Python-shim actions."""

from __future__ import annotations

import launch_plus.resolver as _R
from launch_plus.entities.actions.base import _TrackedAction
from launch_plus.entities.state import _PackageNotFetchedError, _StubLaunchContext


class _TrackedParameterFile(_TrackedAction):
    """Tracks ParameterFile references so they can be reported as param_file dependencies."""

    def __init__(self, param_file=None, *args, allow_substs=False, **kwargs):
        if param_file is None and args:
            param_file = args[0]
        self._param_file = None
        self._raw_param_file = param_file
        if param_file is not None:
            if isinstance(param_file, str):
                path = param_file
            elif hasattr(param_file, "perform"):
                try:
                    result = param_file.perform(_StubLaunchContext())
                    path = str(result) if result is not None else None
                except Exception:
                    path = None
            else:
                path = str(param_file) if param_file is not None else None
            if path:
                self._param_file = path
                _R._track_param_file(path)


class _SetLaunchConfiguration(_TrackedAction):
    """Implements SetLaunchConfiguration: updates launch_configurations at walk time."""

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value

    def execute(self, context) -> list | None:
        name = self._name
        value = self._value
        if name and context is not None:
            if hasattr(value, "perform"):
                try:
                    value = value.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    pass
            resolved_value = str(value) if value is not None else ""
            context._launch_configurations[str(name)] = resolved_value
            _R._state.tracked["set_launch_configurations"][str(name)] = resolved_value
        return None


class _TrackedSetParameter(_TrackedAction):
    """Mirrors launch_ros SetParameter: accumulates (name, value) into context['global_params']."""

    def __init__(self, name=None, value=None, **kwargs):
        self._name = name
        self._value = value

    def execute(self, context) -> list | None:
        name = self._name
        value = self._value
        if hasattr(name, "perform") and context is not None:
            try:
                name = name.perform(context)
            except _PackageNotFetchedError:
                raise
            except Exception:
                name = str(name)
        else:
            name = str(name) if name is not None else ""
        if name and context is not None:
            if hasattr(value, "perform"):
                try:
                    value = value.perform(context)
                except _PackageNotFetchedError:
                    raise
                except Exception:
                    pass
            if isinstance(value, str):
                try:
                    value = int(value)
                except (ValueError, TypeError):
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        pass
            gp_list = context._launch_configurations.setdefault("global_params", [])
            gp_list.append((name, value))
            _R._state.tracked["global_params"].append([name, value])
            _R._state.global_params.append((name, value))
        return None
