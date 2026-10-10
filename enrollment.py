"""Compatibility import for modeltrace.enrollment."""

import sys
from modeltrace import enrollment as _module

sys.modules[__name__] = _module
