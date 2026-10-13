"""What every repository shares: the database it reads and writes."""

WRITES = ("INSERT", "UPDATE", "DELETE")


class Repository:
    def __init__(self, db):
        self.db = db

    def execute(self, query, args=()):
        if query.lstrip()[:6].upper() in WRITES:
            self.db.pending_writes = True
        return self.db.conn.execute(query, args)

    def rows(self, query, args=()):
        return self.db.rows(query, args)

    def one(self, query, args=()):
        """The first row as a dict, or None: never a driver's row object."""
        row = self.db.conn.execute(query, args).fetchone()
        return dict(row) if row else None
