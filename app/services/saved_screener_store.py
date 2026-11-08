import json
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from app.schemas.screener import ScreenerRequest


class StrategyConflict(Exception):
    pass


class InvalidStoredStrategy(Exception):
    pass


class SavedScreenerStore:
    """Store reusable configuration in the existing local SQLite database."""
    def __init__(self, alerts):
        self.database = alerts
        with alerts.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS saved_screeners (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                configuration TEXT NOT NULL, revision INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")

    def _now(self):
        return datetime.fromtimestamp(self.database.clock(), timezone.utc).isoformat()

    @staticmethod
    def _decode(row):
        if row is None:
            raise KeyError("strategy")
        try:
            configuration = ScreenerRequest.model_validate(json.loads(row[3])).model_dump(mode="json")
        except (ValidationError, ValueError, TypeError) as exc:
            raise InvalidStoredStrategy("已儲存策略格式無效，請檢查資料或重新建立策略") from exc
        return {"id": row[0], "name": row[1], "description": row[2], "configuration": configuration,
                "revision": row[4], "created_at": row[5], "updated_at": row[6]}

    def get(self, strategy_id):
        with self.database.connection() as db:
            return self._decode(db.execute("SELECT * FROM saved_screeners WHERE id=?", (strategy_id,)).fetchone())

    def list(self, limit=50, offset=0):
        with self.database.connection() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT COUNT(*) FROM saved_screeners").fetchone()[0]
            rows = db.execute("SELECT * FROM saved_screeners ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return {"items": [self._decode(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def create(self, payload):
        strategy_id, now = str(uuid4()), self._now()
        configuration = json.dumps(payload.configuration.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
        row = (strategy_id, payload.name, payload.description, configuration, 1, now, now)
        with self.database.connection() as db:
            db.execute("INSERT INTO saved_screeners VALUES (?,?,?,?,?,?,?)", row)
        return self._decode(row)

    def update(self, strategy_id, payload):
        with self.database.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM saved_screeners WHERE id=?", (strategy_id,)).fetchone()
            if row is None:
                raise KeyError(strategy_id)
            if row[4] != payload.expected_revision:
                raise StrategyConflict("策略已被修改，請重新讀取最新 revision 後再更新")
            configuration = json.dumps(payload.configuration.model_dump(mode="json"), ensure_ascii=False, allow_nan=False)
            db.execute("UPDATE saved_screeners SET name=?,description=?,configuration=?,revision=revision+1,updated_at=? WHERE id=?",
                       (payload.name, payload.description, configuration, self._now(), strategy_id))
            return self._decode(db.execute("SELECT * FROM saved_screeners WHERE id=?", (strategy_id,)).fetchone())

    def delete(self, strategy_id):
        with self.database.connection() as db:
            if not db.execute("DELETE FROM saved_screeners WHERE id=?", (strategy_id,)).rowcount:
                raise KeyError(strategy_id)
