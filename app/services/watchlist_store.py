from __future__ import annotations

import json
import os
from pathlib import Path
from threading import RLock


class WatchlistStore:
    """Small local JSON store so a watchlist survives a Uvicorn restart."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = RLock()

    def load(self) -> dict[str, int]:
        with self._lock:
            if not self.path.is_file():
                return {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return {str(symbol).upper(): int(page) for symbol, page in data.items()}
            except (OSError, ValueError, TypeError):
                return {}

    def save(self, watchlist: dict[str, int]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(watchlist, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)
