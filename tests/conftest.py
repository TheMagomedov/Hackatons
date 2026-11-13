"""Canonical temporary paths keep FileStore's link checks meaningful on macOS."""

import tempfile
from pathlib import Path

# macOS exposes /var as a system symlink. Test-owned symlinks are still rejected;
# only the OS-selected temporary base is canonicalized before fixtures create it.
tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())
