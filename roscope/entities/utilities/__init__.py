# Copyright 2018 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Originally from:
# - https://github.com/ros2/launch/blob/rolling/launch/launch/utilities/__init__.py
# Modified for roscope project by Taeseung Sohn, 2026.

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
