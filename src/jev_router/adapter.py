"""Compatibility import for the experimental Codex integration.

Implementation lives under jev_router.integrations.codex, not in the service.
"""

import sys

from .integrations.codex import adapter as _implementation

sys.modules[__name__] = _implementation
