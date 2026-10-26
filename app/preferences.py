"""Small per-user preferences file stored next to the indexes."""
from __future__ import annotations
import json
import logging

from core.paths import IS_MACOS, data_dir

logger = logging.getLogger("acrobat_semantic_search.preferences")


class Preferences:
    def __init__(self, path=None):
        self.path = path or data_dir() / "preferences.json"
        self._values: dict = {}
        try:
            self._values = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable preferences at %s", self.path)

    @property
    def viewer_integration(self) -> bool:
        # macOS integrations need privacy permissions, so they are opt-in there.
        # Windows Acrobat/Reader automation needs no prompts and stays on by default.
        return bool(self._values.get("viewer_integration", not IS_MACOS))

    @viewer_integration.setter
    def viewer_integration(self, enabled: bool) -> None:
        self._values["viewer_integration"] = bool(enabled)
        self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._values, indent=2), encoding="utf-8")
        except OSError:
            logger.exception("Could not save preferences to %s", self.path)
