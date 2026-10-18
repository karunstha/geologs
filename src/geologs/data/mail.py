"""Messages waiting to be emailed (outbox): kept until sent, then without their text."""
from __future__ import annotations

from .repository import Repository


class OutboxRepository(Repository):
    def add(self, kind, to_address, subject, body, now):
        return self.execute("""INSERT INTO outbox(kind,to_address,subject,body,created_at,next_attempt_at)
            VALUES (?,?,?,?,?,?) RETURNING id""", (kind, to_address, subject, body, now, now)).fetchone()["id"]

    def take_due(self, now, limit=20):
        """Messages due to be sent, locked so another server process skips them."""
        return self.rows("""SELECT id,kind,to_address,subject,body,attempts FROM outbox
            WHERE sent_at IS NULL AND failed_at IS NULL AND next_attempt_at<=?
            ORDER BY next_attempt_at,id LIMIT ? FOR UPDATE SKIP LOCKED""", (now, limit))

    def sent(self, message_id, now):
        # The text held a link that signs someone in; it is not kept once delivered.
        self.execute("UPDATE outbox SET sent_at=?,body='',last_error=NULL WHERE id=?", (now, int(message_id)))

    def failed(self, message_id, error, next_attempt_at, give_up_at):
        """A failed try: try again at next_attempt_at, or give up (give_up_at) and drop the text."""
        if give_up_at:
            self.execute("UPDATE outbox SET attempts=attempts+1,failed_at=?,body='',last_error=? WHERE id=?",
                         (give_up_at, error[:300], int(message_id)))
        else:
            self.execute("UPDATE outbox SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE id=?",
                         (next_attempt_at, error[:300], int(message_id)))

    def recent(self, limit=100):
        """The latest messages, without their text, for the admin portal."""
        return self.rows("""SELECT id,kind,to_address,subject,created_at,attempts,sent_at,failed_at,last_error
            FROM outbox ORDER BY id DESC LIMIT ?""", (int(limit),))

    def forget_sent(self, before):
        self.execute("DELETE FROM outbox WHERE (sent_at IS NOT NULL OR failed_at IS NOT NULL) AND created_at<?", (before,))
