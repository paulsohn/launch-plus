"""Package for utilities."""

from .namespace_utils import make_namespace_absolute, prefix_namespace
from .normalize_to_list_of_substitutions_impl import normalize_to_list_of_substitutions
from .perform_substitutions_impl import perform_substitutions

__all__ = [
    "make_namespace_absolute",
    "normalize_to_list_of_substitutions",
    "perform_substitutions",
    "prefix_namespace",
]
