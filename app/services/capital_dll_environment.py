"""Safe process environment setup for the Capital native DLL."""

from __future__ import annotations

import os
from pathlib import Path


QUOTE_CALLBACK_REGISTRATIONS = (
    ('RegisterEventOnReplyMessage', '_REPLY_CALLBACK_TYPE'),
    ('RegisterEventOnConnection', '_CONNECTION_CALLBACK_TYPE'),
    ('RegisterEventOnNotifyQuoteLONG', '_NOTIFY_QUOTE_LONG_CALLBACK_TYPE'),
    ('RegisterEventOnNotifyBest5LONG', '_NOTIFY_BEST5_LONG_CALLBACK_TYPE'),
    ('RegisterEventOnNotifyTicksLONG', '_NOTIFY_TICKS_LONG_CALLBACK_TYPE'),
)


def configure_quote_callback_abi(sk) -> None:
    """Use the vendor wrapper's own callback types, not guessed COM signatures.

    SKDLLPython 2.13.58 declares types but does not set argtypes/restype on
    these registration exports. All callback objects remain vendor-owned.
    This never registers an order/trade callback or loads another DLL.
    """
    for export, type_name in QUOTE_CALLBACK_REGISTRATIONS:
        native_function = getattr(sk._dll, export)
        native_function.argtypes = [getattr(sk, type_name)]
        native_function.restype = None


def ensure_dll_directory_on_path(directory: str | Path) -> None:
    """Put the DLL directory on ``PATH`` exactly once.

    Historical K-line requests run on many short-lived COM threads. Repeatedly
    prepending the same directory exceeds Windows' 32,767-character limit, so
    existing copies of this one directory are removed before it is inserted.
    """
    target = os.path.normcase(os.path.abspath(str(Path(directory))))
    remaining = [
        item for item in os.environ.get("PATH", "").split(os.pathsep)
        if item and os.path.normcase(os.path.abspath(item)) != target
    ]
    os.environ["PATH"] = os.pathsep.join([target, *remaining])
