"""Compatibility import for modeltrace.bank_builder."""

import sys
from modeltrace import bank_builder as _module

if __name__ == "__main__":
    _module.main()
else:
    sys.modules[__name__] = _module
