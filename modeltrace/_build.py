"""Build the wheel bank from the repository's single authoritative bank."""

import hashlib
from pathlib import Path

from setuptools.command.build_py import build_py


class BuildPy(build_py):
    def get_source_files(self):
        return [*super().get_source_files(), "data/unified_bank.json"]

    def run(self):
        super().run()
        source = Path(__file__).resolve().parent.parent / "data" / "unified_bank.json"
        target = Path(self.build_lib) / "modeltrace" / "data" / "unified_bank.json"
        contents = source.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(contents).digest():
            raise RuntimeError("Packaged fingerprint bank checksum mismatch")
