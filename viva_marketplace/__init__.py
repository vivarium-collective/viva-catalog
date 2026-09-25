"""Back-compat shim for the old ``viva_marketplace`` import package.

The package was renamed to :mod:`viva_catalog`. This shim re-exports the public
API so already-published consumers (e.g. ``viva-superpowers 0.23.0``'s guarded
``import viva_marketplace``) keep working. Import :mod:`viva_catalog` instead.
"""
from __future__ import annotations

import warnings

from viva_catalog import (  # noqa: F401
    INDEX_FILE,
    MODULES_FILE,
    load_ecosystem_index,
    load_modules,
)

warnings.warn(
    "viva_marketplace has been renamed to viva_catalog; "
    "import viva_catalog instead. This shim will be removed in a future release.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["load_modules", "load_ecosystem_index", "MODULES_FILE", "INDEX_FILE"]
