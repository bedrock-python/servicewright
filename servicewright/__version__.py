"""The installed package version.

Read from the distribution metadata rather than hard-coded, so it always matches
the artifact that was actually installed; a source checkout that was never
installed falls back to the development placeholder.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("servicewright")
except PackageNotFoundError:  # pragma: no cover - source checkout without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
