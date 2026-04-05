"""EnvironmentVariable — Python shim substitution for deferred env var resolution."""

from __future__ import annotations

import logging
import os

from roscope.entities.helpers import _to_str
from roscope.entities.substitution import Substitution

logger = logging.getLogger("roscope")

_SENTINEL = object()


class DeferredEnvironmentVariable(Substitution):
    """Deferred substitution: reads context.environment at perform() time."""

    def __init__(self, name, **kw):
        self._name = name
        self._default = kw.get("default_value", _SENTINEL)

    def perform(self, context=None) -> str:
        name = _to_str(self._name, context) or ""
        if context is not None and hasattr(context, "environment"):
            env = context.environment
            if name in env:
                return str(env[name])
        if name in os.environ:
            return str(os.environ[name])
        if self._default is not _SENTINEL:
            return _to_str(self._default, context) or ""
        logger.error("EnvironmentVariable: '%s' is not set and no default", name)
        return ""

    def __str__(self):
        return str(self._name) if self._name is not None else ""
