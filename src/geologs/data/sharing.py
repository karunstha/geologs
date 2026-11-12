"""Live location sharing: the shares you make (location_shares), and the one position each may show
(live_positions). Row-level security lets a viewer read the shares made to them and, while a share
lasts, the sharer's position at its precision; nothing else of the sharer's."""
from __future__ import annotations

from .repository import Repository


class SharingRepository(Repository):
    # Shares you make.

    def given(self):
        """Your shares, newest first, live or ended."""
        return self.rows("""SELECT id,viewer_id,precision,expires_at,created_at,
            (expires_at IS NULL OR expires_at>now()) AS active FROM location_shares
            WHERE user_id=geologs_user() ORDER BY id DESC""")

    def save(self, viewer_id, precision, expires_at, now):
        """Share with a viewer; sharing with them again replaces the share (its precision and end)."""
        return self.execute("""INSERT INTO location_shares(viewer_id,precision,expires_at,created_at) VALUES (?,?,?,?)
            ON CONFLICT(user_id,viewer_id) DO UPDATE SET precision=excluded.precision,expires_at=excluded.expires_at,
            created_at=excluded.created_at RETURNING id""", (viewer_id, precision, expires_at, now)).fetchone()["id"]

    def stop(self, share_id):
        """Stop one of your shares; False when there is no such share of yours."""
        return bool(self.execute("DELETE FROM location_shares WHERE id=? AND user_id=geologs_user()",
                                 (int(share_id),)).rowcount)

    def forget_ended(self):
        """Ended shares of yours go: an ended share shows nothing, and the list stays short."""
        self.execute("DELETE FROM location_shares WHERE user_id=geologs_user() AND expires_at<=now()")

    def precisions_shared(self):
        """The precisions your live shares show your position at."""
        return {row["precision"] for row in self.rows("""SELECT DISTINCT precision FROM location_shares
            WHERE user_id=geologs_user() AND (expires_at IS NULL OR expires_at>now())""")}

    # Your position as shares show it.

    def set_position(self, precision, lat, lon, place_name, moving, fix_at, now):
        self.execute("""INSERT INTO live_positions(precision,lat,lon,place_name,moving,fix_at,updated_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,precision) DO UPDATE SET lat=excluded.lat,lon=excluded.lon,
            place_name=excluded.place_name,moving=excluded.moving,fix_at=excluded.fix_at,updated_at=excluded.updated_at""",
            (precision, lat, lon, place_name, moving, fix_at, now))

    def keep_positions(self, precisions):
        """Remove your shown positions at any other precision (none when nothing is shared)."""
        self.execute("DELETE FROM live_positions WHERE user_id=geologs_user() AND NOT (precision = ANY(?))",
                     (list(precisions),))

    # Shares made to you.

    def received(self):
        """Live shares made to you, each with the sharer's position at its precision (None until the
        sharer's server has worked it out)."""
        return self.rows("""SELECT s.id,s.user_id AS sharer_id,s.precision,s.expires_at,s.created_at,
                p.lat,p.lon,p.place_name,p.moving,p.fix_at,p.updated_at
            FROM location_shares s LEFT JOIN live_positions p ON p.user_id=s.user_id AND p.precision=s.precision
            WHERE s.viewer_id=geologs_user() AND (s.expires_at IS NULL OR s.expires_at>now())
            ORDER BY s.created_at DESC""")

    def leave(self, share_id):
        """Stop seeing a share made to you; False when there is no such share."""
        return bool(self.execute("DELETE FROM location_shares WHERE id=? AND viewer_id=geologs_user()",
                                 (int(share_id),)).rowcount)
