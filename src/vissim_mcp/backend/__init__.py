"""COM backends (PLAN 4.1, 4.3): the interface in :mod:`.base`, the pywin32 implementation in :mod:`.real`.

Nothing in this package imports pywin32 at import time: :class:`~.real.RealComBackend` imports ``pythoncom``
and ``win32com`` inside its methods, which run on the COM worker thread only.
"""

from .base import ComBackend, type_info_members
from .real import RealComBackend

__all__ = ["ComBackend", "RealComBackend", "type_info_members"]
