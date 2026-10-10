"""Compatibility import for modeltrace.fingerprint."""

import sys
from modeltrace import fingerprint as _module

sys.modules[__name__] = _module
