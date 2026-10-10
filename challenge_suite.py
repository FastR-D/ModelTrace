"""Compatibility import for modeltrace.challenge_suite."""

import sys
from modeltrace import challenge_suite as _module

sys.modules[__name__] = _module
