"""Durable notification inbox; desktop dispatch is claimed at most once."""
import json
from datetime import datetime, timezone
from uuid import uuid4


def initialize_notifications(db):
    db.execute("""CREATE TABLE IF NOT EXISTS notifications (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
        event_id TEXT UNIQUE, payload TEXT NOT NULL, created_epoch REAL NOT NULL,
        read_at TEXT, desktop_status TEXT NOT NULL, claim_token TEXT,
        desktop_error TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS notifications_unread ON notifications(read_at, sequence)")
    # Existing V3.3 history is imported once, without desktop replay.
    for row in db.execute("SELECT data FROM alert_events WHERE json_extract(data, '$.id') NOT IN (SELECT event_id FROM notifications WHERE event_id IS NOT NULL)").fetchall():
        insert_event_notification(db, json.loads(row[0]), historical=True)


def insert_event_notification(db, event, historical=False):
    reasons = event["result"].get("reasons", [])
    payload = {"id": str(uuid4()), "event_id": event["id"], "alert_id": event["alert_id"],
               "symbol": event["symbol"], "title": f"{event['symbol']} · {event['name']}",
               "message": "；".join(reasons) or "提醒條件由不符合轉為符合。",
               "created_at": event["triggered_at"], "kind": "alert", "result": event["result"]}
    stamp = datetime.fromisoformat(event["triggered_at"]).timestamp()
    db.execute("INSERT OR IGNORE INTO notifications(id,event_id,payload,created_epoch,desktop_status) VALUES (?,?,?,?,?)",
               (payload["id"], event["id"], json.dumps(payload, ensure_ascii=False), stamp, "skipped_history" if historical else "pending"))


class NotificationService:
    def __init__(self, alerts):
        self.alerts = alerts

    def _decode(self, row):
        payload = json.loads(row[2])
        return {**payload, "sequence": row[0], "read": row[3] is not None, "read_at": row[3], "desktop_status": row[4], "desktop_error": row[5]}

    def list(self, unread_only=False, before=None, limit=50):
        with self.alerts.connection() as db:
            rows = db.execute("SELECT sequence,id,payload,read_at,desktop_status,desktop_error FROM notifications WHERE (?=0 OR read_at IS NULL) AND (? IS NULL OR sequence < ?) ORDER BY sequence DESC LIMIT ?",
                              (unread_only, before, before, limit + 1)).fetchall()
            count = db.execute("SELECT COUNT(*) FROM notifications WHERE read_at IS NULL").fetchone()[0]
            return {"items": [self._decode(r) for r in rows[:limit]], "unread_count": count,
                    "next_before": rows[limit - 1][0] if len(rows) > limit else None}

    def mark(self, notification_id, read):
        with self.alerts.connection() as db:
            stamp = datetime.fromtimestamp(self.alerts.clock(), timezone.utc).isoformat() if read else None
            if not db.execute("UPDATE notifications SET read_at=? WHERE id=?", (stamp, notification_id)).rowcount:
                raise KeyError(notification_id)
        return {"id": notification_id, "read": read}

    def mark_all(self, through_sequence):
        with self.alerts.connection() as db:
            stamp = datetime.fromtimestamp(self.alerts.clock(), timezone.utc).isoformat()
            count = db.execute("UPDATE notifications SET read_at=? WHERE read_at IS NULL AND sequence<=?", (stamp, through_sequence)).rowcount
        return {"updated_count": count}

    def create_test(self):
        now = self.alerts.clock()
        payload = {"id": str(uuid4()), "event_id": None, "alert_id": None, "symbol": "", "kind": "test", "title": "股票分析 · 測試通知",
                   "message": "通知系統測試成功。這不是股票條件觸發。", "created_at": datetime.fromtimestamp(now, timezone.utc).isoformat(), "result": {}}
        with self.alerts.connection() as db:
            db.execute("INSERT INTO notifications(id,payload,created_epoch,desktop_status) VALUES (?,?,?, 'pending')", (payload["id"], json.dumps(payload, ensure_ascii=False), now))
        return payload

    def claim(self):
        with self.alerts.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            cutoff = self.alerts.clock() - 300
            db.execute("UPDATE notifications SET desktop_status='expired' WHERE desktop_status='pending' AND created_epoch<?", (cutoff,))
            row = db.execute("SELECT id,payload FROM notifications WHERE desktop_status='pending' AND read_at IS NULL ORDER BY sequence LIMIT 1").fetchone()
            if row is None:
                return {"notification": None}
            token = str(uuid4())
            db.execute("UPDATE notifications SET desktop_status='claimed',claim_token=? WHERE id=?", (token, row[0]))
            return {"notification": json.loads(row[1]), "claim_token": token}

    def acknowledge(self, notification_id, token, status, error=None):
        with self.alerts.connection() as db:
            changed = db.execute("UPDATE notifications SET desktop_status=?,desktop_error=? WHERE id=? AND claim_token=? AND desktop_status='claimed'",
                                 (status, error, notification_id, token)).rowcount
        return {"accepted": bool(changed)}
