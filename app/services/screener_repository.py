"""Read-only screening session, sharing one MySQL snapshot per run."""
from app.services.mysql_instrument_repository import MySQLInstrumentRepository, MySQLRepositoryError
from app.schemas.models import Candle


class ScreenerRepository(MySQLInstrumentRepository):
    def __enter__(self):
        self.connection = self.cursor = None
        try:
            self.connection = self._connect_database(self._connector())
            self.connection.start_transaction(isolation_level="REPEATABLE READ", consistent_snapshot=True, readonly=True)
            self.cursor = self.connection.cursor()
            return self
        except Exception as exc:
            if self.connection:
                self.connection.close()
            raise MySQLRepositoryError(f"選股資料庫連線失敗：{exc}") from exc

    def __exit__(self, *args):
        try:
            if self.cursor:
                self.cursor.close()
        finally:
            if self.connection:
                try:
                    self.connection.rollback()
                finally:
                    self.connection.close()

    def instruments(self, markets, category_nos, candle_table, symbols=None):
        table = "odd_lot_instruments" if candle_table == "odd_lot_candle" else "instruments"
        if symbols == []:
            return []
        filters = ["is_active=TRUE", "category_no NOT IN (94,98)", "market_no IN (" + ",".join(["%s"] * len(markets)) + ")"]
        params = list(markets)
        for column, values in (("category_no", category_nos), ("symbol", symbols)):
            if values:
                filters.append(column + " IN (" + ",".join(["%s"] * len(values)) + ")")
                params.extend(values)
        try:
            self.cursor.execute(f"SELECT market_no,symbol,name,category_no,category_name FROM `{table}` WHERE " + " AND ".join(filters) + " ORDER BY symbol,market_no", tuple(params))
            return [dict(zip(("market_no", "symbol", "name", "category_no", "category_name"), row)) for row in self.cursor.fetchall()]
        except Exception as exc:
            raise MySQLRepositoryError(f"讀取選股商品範圍失敗：{exc}") from exc

    def candles(self, symbol, interval, table, limit):
        table = self._validate_table_name(table)
        try:
            self.cursor.execute(f"SELECT candle_time,open_price,high_price,low_price,close_price,volume,fetched_at FROM `{table}` WHERE symbol=%s AND interval_code=%s ORDER BY candle_time DESC LIMIT %s", (symbol, interval, limit))
            rows = list(reversed(self.cursor.fetchall()))
        except Exception as exc:
            raise MySQLRepositoryError(f"讀取選股 K 線失敗：{exc}") from exc
        candles = [Candle(timestamp=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]), volume=int(r[5])) for r in rows]
        return candles, max((r[6] for r in rows if r[6] is not None), default=None)

    def confirmed_candles(self, symbol, interval, table, limit, policy):
        """Stream past partial snapshots; bounded memory, per-row timestamps.

        Legacy candles() stays unchanged. LIMIT cannot be applied before
        completion filtering, or recent partial snapshots could hide valid bars.
        """
        table=self._validate_table_name(table)
        cur=None
        try:
            cur=self.connection.cursor()
            cur.execute(f"SELECT candle_time,open_price,high_price,low_price,close_price,volume,fetched_at FROM `{table}` WHERE symbol=%s AND interval_code=%s ORDER BY candle_time DESC",(symbol,interval))
            selected=[]; raw_count=0; skipped=0; seen=set()
            while len(selected)<limit:
                rows=cur.fetchmany(256)
                if not rows: break
                raw_count+=len(rows)
                for row in rows:
                    from app.services.backfill_policy import period_start
                    label=period_start(interval,row[0].date())
                    if label not in seen and policy.complete(row[0],row[6]):
                        selected.append(row); seen.add(label)
                    else: skipped+=1
                    if len(selected)>=limit: break
            # mysql.connector must consume unread rows before the next query.
            while cur.fetchmany(256): pass
            selected.reverse()
            candles=[Candle(timestamp=r[0],open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=int(r[5])) for r in selected]
            written=selected[-1][6] if selected else None
            return candles,dict(raw_count=raw_count,excluded_unverified_bars=skipped,latest_written_at=written)
        except Exception as exc:
            raise MySQLRepositoryError(f"讀取完整期間選股資料失敗：{exc}") from exc
        finally:
            if cur is not None: cur.close()
