#!/usr/bin/env python3
"""Generate TypeScript interfaces from the Python schema dataclasses.

Usage:
    python -m roscope.visualizer.generate_types > roscope_viz/src/types.generated.ts

Or via pnpm script (from roscope_viz/):
    pnpm run generate-types
"""

from __future__ import annotations

import dataclasses
import sys
import types
import typing
from typing import Literal, Union, get_type_hints

from roscope.visualizer import schema


def _ts_type(annotation: object, *, resolved: dict[str, object] | None = None) -> str:
    """Convert a Python type annotation to a TypeScript type string."""
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    # str, int, float, bool
    if annotation is str:
        return "string"
    if annotation is int or annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"

    # None
    if annotation is type(None):
        return "null"

    # Literal["a", "b"] -> "a" | "b"
    if origin is Literal:
        return " | ".join(f'"{a}"' for a in args)

    # list[X] -> X[]
    if origin is list:
        inner = _ts_type(args[0]) if args else "unknown"
        if "|" in inner:
            return f"({inner})[]"
        return f"{inner}[]"

    # dict[K, V] -> Record<K, V>
    if origin is dict:
        k = _ts_type(args[0]) if args else "string"
        v = _ts_type(args[1]) if len(args) > 1 else "unknown"
        return f"Record<{k}, {v}>"

    # X | None (Union with None) -> X | null
    if origin is Union or origin is types.UnionType:
        non_none = [a for a in args if a is not type(None)]
        has_none = len(non_none) < len(args)
        parts = [_ts_type(a) for a in non_none]
        if has_none:
            parts.append("null")
        return " | ".join(parts)

    # Dataclass reference -> interface name
    if isinstance(annotation, type) and dataclasses.is_dataclass(annotation):
        return annotation.__name__

    return "unknown"


def _field_name(python_name: str) -> str:
    """Convert Python field names to JS conventions.

    - Trailing underscore is stripped (``from_`` -> ``from``).
    - Snake case is converted to camelCase (``viz_id`` -> ``vizId``).
    """
    name = python_name.rstrip("_")
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _emit_interface(cls: type, *, indent: str = "  ") -> str:
    """Emit a TypeScript interface for a dataclass."""
    hints = get_type_hints(cls, include_extras=True)
    fields = dataclasses.fields(cls)

    lines = [f"export interface {cls.__name__} {{"]
    for f in fields:
        ts_name = _field_name(f.name)
        annotation = hints[f.name]

        # A field is optional (?) only when it has a Python default value.
        # Nullable (T | None) does not imply optional — those are separate concerns.
        has_default = (
            f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING  # type: ignore[misc]
        )

        ts = _ts_type(annotation)
        optional = "?" if has_default else ""
        lines.append(f"{indent}{ts_name}{optional}: {ts};")

    lines.append("}")
    return "\n".join(lines)


def generate() -> str:
    """Generate the full TypeScript file content."""
    # Collect all dataclasses in dependency order
    ordered: list[type] = [
        schema.ParamEntry,
        schema.RemapEntry,
        schema.ExtraArgEntry,
        schema.ArgEntry,
        schema.GraphNode,
        schema.GraphGroup,
        schema.GraphConnection,
        schema.GraphEdge,
        schema.GraphMetadata,
        schema.GraphData,
        schema.Snapshot,
    ]

    parts: list[str] = [
        "// AUTO-GENERATED — do not edit manually.",
        "// Source: roscope/visualizer/schema.py",
        "// Run: python -m roscope.visualizer.generate_types\n",
    ]

    for cls in ordered:
        parts.append(_emit_interface(cls))
        parts.append("")

    return "\n".join(parts)


if __name__ == "__main__":
    sys.stdout.write(generate())
