"""The compact timeline (timeline_fixes): movement samples plus each stay's arrival and departure."""
from __future__ import annotations

from .repository import Repository


class TimelineRepository(Repository):
    def delete_sources(self, sources):
        """Forget sources' timelines (they are rebuilt from samples and stays)."""
        self.execute(f"DELETE FROM timeline_fixes WHERE source IN ({','.join('?' * len(sources))})", list(sources))

    def sync(self, source, since_id=0, rebuild=False, open_stop=None):
        """Bring a source's timeline up to date with its samples and stays.

        Samples while travelling remain available. A stationary stay keeps only
        its arrival and latest/departure samples; raw samples are untouched.
        `open_stop` is a still-open stop that is not yet a stay, compacted the
        same way while it lasts.
        """
        if rebuild:
            self.execute("DELETE FROM timeline_fixes WHERE source=?", (source,))
        self.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
            SELECT id,source,'movement',NULL FROM point_observations
            WHERE source=? AND id>? ON CONFLICT(observation_id) DO NOTHING""", (source, since_id))
        self.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
            SELECT id FROM (SELECT id,ROW_NUMBER() OVER (
                PARTITION BY source,timestamp ORDER BY COALESCE(accuracy_m,1000000000),id DESC) AS rank
                FROM point_observations WHERE source=? AND timestamp IN (
                    SELECT timestamp FROM point_observations WHERE source=? AND id>?)) ranked WHERE rank>1)""",
            (source, source, since_id))
        # Closing a stay changes no observation ID, so it would not be selected
        # by the incremental end_observation_id query below. Promote its former
        # `latest` edge when the first point outside the stop arrives.
        self.execute("""UPDATE timeline_fixes SET role='departure' WHERE source=? AND role='latest'
            AND stay_id IN (SELECT id FROM observed_stays_v2 WHERE source=? AND ongoing=0)""",
            (source, source))
        # Area stays are movement; only stationary runs are compacted to their edges.
        stay_query = "SELECT * FROM observed_stays_v2 WHERE source=? AND kind='stationary'"
        stay_args = [source]
        if not rebuild:
            stay_query += " AND end_observation_id>?"
            stay_args.append(since_id)
        stay_query += " ORDER BY started_at,id"
        for stay in self.rows(stay_query, stay_args):
            self.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
                SELECT id FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
                AND id<>? AND id<>?)""",
                (source, stay["started_at"], stay["ended_at"], stay["start_observation_id"], stay["end_observation_id"]))
            self.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role,stay_id=excluded.stay_id""",
                (stay["start_observation_id"], source, "arrival", stay["id"]))
            self.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role,stay_id=excluded.stay_id""",
                (stay["end_observation_id"], source, "latest" if stay["ongoing"] else "departure", stay["id"]))
        if open_stop:
            self.execute("""DELETE FROM timeline_fixes WHERE observation_id IN (
                SELECT id FROM point_observations WHERE source=? AND timestamp>=? AND timestamp<=?
                AND id<>? AND id<>?)""",
                (source, open_stop["start"], open_stop["end"], open_stop["start_id"], open_stop["end_id"]))
            self.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,NULL) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""",
                (open_stop["start_id"], source, "arrival"))
            self.execute("""INSERT INTO timeline_fixes(observation_id,source,role,stay_id)
                VALUES (?,?,?,NULL) ON CONFLICT(observation_id) DO UPDATE SET role=excluded.role""",
                (open_stop["end_id"], source, "latest"))
