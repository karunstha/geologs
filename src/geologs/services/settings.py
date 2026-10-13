"""Changing settings, and redoing what the change affects."""
from ..domain.settings import ANALYSIS_DEFAULTS, validate
from ..integrations.places import configured_places_providers, PLACES_PROVIDERS
from .stays import process_stays
from .use_case import use_case


def place_providers():
    """Each place provider's label, env variable, and whether it is configured."""
    configured = configured_places_providers()
    return {name: {**info, "configured": configured[name]} for name, info in PLACES_PROVIDERS.items()}


@use_case
def save_settings(db, raw):
    current = db.settings.get()
    values = validate(raw, current, place_providers())
    db.settings.save(values)
    if values["timezone"] != current["timezone"]:
        # Summaries are derived; days are rebuilt in the new zone when requested.
        db.summaries.keep_canonical(values["timezone"])
    if values["source_priority"] != current["source_priority"]:
        # Days combine sources at read time; drop what was combined in the old order.
        db.caches.clear_analysis()
    if any(values[key] != current[key] for key in ANALYSIS_DEFAULTS):
        # Stays and their activities are stored; recalculate them under the new rules.
        # Stored summaries go stale through their watermark and are rebuilt when requested.
        process_stays(db, rebuild=True)
    return values
