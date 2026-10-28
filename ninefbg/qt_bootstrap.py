"""Prepare the optional Windows ML library before Qt loads its native DLLs."""
from __future__ import annotations

import ctypes
import importlib.util
from pathlib import Path
import sys


# Keep the native handle alive; importing the full torch package remains lazy.
_c10_library = None


def prepare_optional_torch():
    """Avoid the Windows Qt-before-torch initialization failure, without setup.

    Only load c10 from the torch package selected by this Python environment.
    A missing or broken optional ML runtime must not prevent optical-only use.
    The actual model command still reports runtime failures to the operator.
    Upstream: https://github.com/pytorch/pytorch/issues/166628
    """
    global _c10_library
    if (sys.platform != "win32" or _c10_library is not None or "torch" in sys.modules or
            any(name in sys.modules for name in ("PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets"))):
        return
    try:
        spec = importlib.util.find_spec("torch")
        if spec is None or not spec.origin:
            return
        library = Path(spec.origin).resolve().parent / "lib" / "c10.dll"
        if library.is_file():
            _c10_library = ctypes.CDLL(str(library))
    except (ImportError, OSError, ValueError):
        # Preserve the optional dependency boundary and original load exception.
        return
