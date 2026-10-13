"""Fuel prices (fuel_prices): your own, and the EIA's weekly series, each dated."""
from __future__ import annotations

from .repository import Repository


class FuelRepository(Repository):
    def save_price(self, series, effective_date, price_per_l, fetched_at):
        self.execute("""INSERT INTO fuel_prices(source,effective_date,price_per_l,fetched_at) VALUES (?,?,?,?)
            ON CONFLICT(source,effective_date) DO UPDATE SET price_per_l=excluded.price_per_l,fetched_at=excluded.fetched_at""",
            (series, effective_date, price_per_l, fetched_at))

    def table(self, series):
        """Every price of one series as (effective_date, price_per_l), oldest first."""
        if not series:
            return []
        return [(row["effective_date"], row["price_per_l"]) for row in self.rows(
            "SELECT effective_date,price_per_l FROM fuel_prices WHERE source=? ORDER BY effective_date", (series,))]

    def count(self, series):
        return self.execute("SELECT COUNT(*) AS n FROM fuel_prices WHERE source=?", (series,)).fetchone()["n"]

    def signature(self, series):
        """How many prices a series has and when it last changed."""
        row = self.execute("SELECT COUNT(*) AS n,COALESCE(MAX(fetched_at),'') AS at FROM fuel_prices WHERE source=?",
                           (series or "",)).fetchone()
        return row["n"], row["at"]
