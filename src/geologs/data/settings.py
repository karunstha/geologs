"""Settings (app_settings), and small JSON values other parts keep beside them."""
from __future__ import annotations

import json

from .. import config
from ..domain.settings import DEFAULT_SETTINGS
from .repository import Repository

# How this person's latest import went, kept beside the settings so a restart can tell it was
# cut short. It describes this server, so a backup leaves it out.
IMPORT_STATUS_KEY = "last_import"


class SettingsRepository(Repository):
    def get(self):
        """Every setting: saved values over the defaults (GEOLOGS_TIMEZONE sets the default timezone)."""
        values = dict(DEFAULT_SETTINGS)
        if config.default_timezone():
            values["timezone"] = config.default_timezone()
        for row in self.rows("SELECT key,value FROM app_settings"):
            if row["key"] in values:
                try:
                    values[row["key"]] = json.loads(row["value"])
                except (TypeError, ValueError):
                    pass
        return values

    def save(self, values):
        for key, value in values.items():
            self.set_value(key, value)

    def value(self, key, default=None):
        """A stored JSON value, or `default` when it is missing or unreadable."""
        row = self.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        try:
            return json.loads(row["value"]) if row else default
        except ValueError:
            return default

    def set_value(self, key, value):
        self.execute("""INSERT INTO app_settings(key,value) VALUES (?,?)
            ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value""", (key, json.dumps(value)))
