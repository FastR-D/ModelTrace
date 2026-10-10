# Tests

Install the project and its web dependencies in a virtual environment:

```sh
python -m pip install ".[web]" build
python -m unittest discover -s tests -v
node --test tests/test_api_analysis_retry.mjs
```

Tests use temporary directories, OS-assigned loopback ports and placeholder
tokens. They do not call a paid model API or write client configuration.
Candidate IDs come from the repository bank, so adding a model does not require
changing a fixed test count. Public-mode tests isolate deployment settings.

Verify a non-editable wheel installation away from the source tree:

```sh
python -m build
python tests/verify_installation.py --wheels dist/modeltrace-0.1.0-py3-none-any.whl
```

The verifier creates a temporary virtual environment and compares the installed
bank SHA256 with `data/unified_bank.json`. It can also accept a cc-switch-simple
wheel to test its installed `check` command. For an offline installation, pass
`--wheelhouse /path/to/wheels` containing all dependency wheels.

GitHub Actions runs the Python suite on Ubuntu and Windows with Python 3.10,
3.12 and 3.14. Local success does not imply those CI jobs have already passed.

For a manual local demo, run `python tests/run_demo.py`. It prints the assigned
UI and mock API addresses. Optional `--ui-port` and `--api-port` select ports.
