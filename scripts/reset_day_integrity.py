"""Back up and reset ONLY daily integrity assessments/gaps; never candles/jobs.

Default is SELECT-only preview. Apply requires the cross-process worker lease,
an idle audit, and either an existing user pause or integrity already disabled.
No login, pause request, restart, table DROP/TRUNCATE or automatic re-audit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TABLES = ('candle_day_integrity', 'candle_day_gaps')
PRIMARY_ORDER = {'candle_day_integrity': 'candle_table,market_no,symbol',
                 'candle_day_gaps': 'candle_table,market_no,symbol,candle_date'}


def require_idle(state, integrity_enabled):
    maintenance = state.get('day_integrity_maintenance') or {}
    if (state.get('day_integrity_active') or maintenance.get('active')
            or state.get('preparation_pending') or state.get('state') == 'running'):
        raise RuntimeError('工作尚未安全停止；不清空驗收清單，也不主動中斷工作')
    if not state.get('pause_requested') and integrity_enabled:
        raise RuntimeError('請先安全暫停工作或停用完整性驗收，避免清空後立即被舊程序重建')


def backup_target(path):
    path = path.resolve()
    if (not path.is_relative_to((ROOT / 'output').resolve())
            or path.suffixes[-2:] != ['.jsonl', '.gz'] or path.exists()):
        raise ValueError('備份必須是 output/ 內尚不存在的 .jsonl.gz 檔案')
    return path


def table_counts(cursor):
    cursor.execute("SELECT TABLE_NAME,ENGINE FROM information_schema.tables "
                   "WHERE table_schema=DATABASE() AND table_name IN ('candle_day_integrity','candle_day_gaps')")
    engines = dict(cursor.fetchall())
    if set(engines) != set(TABLES) or any(engine != 'InnoDB' for engine in engines.values()):
        raise RuntimeError('兩張驗收表必須已存在且為 InnoDB；不建立／修改資料表')
    result = {}
    for table in TABLES:
        cursor.execute(f'SELECT COUNT(*) FROM `{table}`')
        result[table] = int(cursor.fetchone()[0])
    return result


def verify_backup(path):
    """Stream and verify EVERY backed-up row before allowing a DELETE."""
    digest, counts, current, footer = hashlib.sha256(), {}, None, None
    with gzip.open(path, 'rb') as stream:
        for line in stream:
            if footer is not None:
                raise ValueError('備份結束標記後仍有資料')
            row = json.loads(line)
            if isinstance(row, dict) and row.get('type') == 'complete':
                footer = row
                continue
            digest.update(line)
            if isinstance(row, dict) and row.get('type') == 'table':
                current = row['name']
                if current not in TABLES or current in counts or not row.get('columns'):
                    raise ValueError('備份資料表不符／重複')
                counts[current] = 0
            elif isinstance(row, list):
                if current is None:
                    raise ValueError('備份資料缺少資料表標頭')
                counts[current] += 1
    if (footer is None or set(counts) != set(TABLES) or footer['counts'] != counts
            or footer['sha256'] != digest.hexdigest()):
        raise ValueError('備份未完成、筆數不符或雜湊驗證失敗；不刪除清單')
    return footer


def reset_ledger(connection, backup, control_before, progress=print):
    """Caller owns the worker lease. One rollback-capable transaction, two tables."""
    cursor = connection.cursor()
    committing = False
    try:
        connection.start_transaction(isolation_level='REPEATABLE READ')
        before = table_counts(cursor)
        cursor.execute("SELECT TRIGGER_NAME FROM information_schema.triggers WHERE trigger_schema=DATABASE() "
                       "AND event_object_table IN ('candle_day_integrity','candle_day_gaps')")
        if cursor.fetchall():
            raise RuntimeError('驗收表有額外觸發器，無法保證只影響清單；停止')
        cursor.execute("SELECT TABLE_NAME FROM information_schema.referential_constraints "
                       "WHERE constraint_schema=DATABASE() "
                       "AND referenced_table_name IN ('candle_day_integrity','candle_day_gaps')")
        if cursor.fetchall():
            raise RuntimeError('另有資料表參照驗收表；不執行可能連帶影響資料的清空')
        backup.parent.mkdir(parents=True, exist_ok=True)
        digest, exported = hashlib.sha256(), {}
        with backup.open('xb') as raw:
            with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1) as stream:
                def emit(value, *, hashed=True):
                    line = (json.dumps(value, ensure_ascii=False, default=str, separators=(',', ':'))+'\n').encode('utf-8')
                    stream.write(line)
                    if hashed:
                        digest.update(line)
                emit(dict(type='header', version=1, created_at=datetime.now(timezone.utc),
                          scope=list(TABLES), control_before=control_before,
                          note='Full pre-reset rows. All datetime/decimal values serialized as strings. No candles/jobs included.'))
                for table in TABLES:
                    # Row/next-key locks also protect against out-of-band writes
                    # while backing up. Runtime workers are excluded by the lease.
                    cursor.execute(f'SELECT * FROM `{table}` ORDER BY {PRIMARY_ORDER[table]} FOR UPDATE')
                    emit(dict(type='table', name=table, columns=[d[0] for d in cursor.description]))
                    count = 0
                    while block := cursor.fetchmany(2000):
                        for row in block:
                            emit(list(row))
                        count += len(block)
                        if count % 100000 == 0:
                            progress(f'備份 {table}：{count} 筆', flush=True)
                    exported[table] = count
                    if count != before[table]:
                        raise RuntimeError('備份期間清單筆數變動；停止，不刪除')
                    progress(f'備份完成 {table}：{count} 筆', flush=True)
                emit(dict(type='complete', counts=exported, sha256=digest.hexdigest()), hashed=False)
            raw.flush()
            os.fsync(raw.fileno())
        verified = verify_backup(backup)
        if verified['counts'] != before:
            raise RuntimeError('完整備份與資料庫筆數不符')
        progress('備份逐列及 SHA-256 驗證成功；開始清空兩張驗收表', flush=True)
        deleted = {}
        for table in reversed(TABLES):
            cursor.execute(f'DELETE FROM `{table}`')
            deleted[table] = cursor.rowcount
            if deleted[table] != before[table]:
                raise RuntimeError('刪除筆數與備份不符；回滾')
        for table in TABLES:
            cursor.execute(f'SELECT COUNT(*) FROM `{table}`')
            if cursor.fetchone()[0] != 0:
                raise RuntimeError('清空後筆數非零；回滾')
        committing = True
        connection.commit()
        return dict(status='committed', deleted=deleted, backup=str(backup),
                    backup_sha256=verified['sha256'], candles_jobs_checkpoints='untouched')
    except BaseException as error:
        try:
            connection.rollback()
        except Exception:
            pass
        if committing:
            raise RuntimeError(f'提交結果未確認；備份保留在 {backup}，請先查詢清單筆數，不要盲目重跑') from error
        raise
    finally:
        cursor.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='明確清空兩張驗收清單；預設只預覽')
    parser.add_argument('--backup', type=Path, help='apply 必填：output/ 內新的 .jsonl.gz 完整備份')
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    from app.services.backfill_control import BackfillControl
    from app.services.day_candle_integrity import integrity_enabled, POLICY_VERSION
    from app.services.mysql_instrument_repository import MySQLSettings
    import mysql.connector
    settings = MySQLSettings.from_env()
    control = BackfillControl()
    def connect():
        return mysql.connector.connect(host=settings.host, port=settings.port, user=settings.user,
            password=settings.password, database=settings.database, connection_timeout=8, autocommit=False)
    if not args.apply:
        connection = connect()
        cursor = connection.cursor()
        try:
            connection.start_transaction(readonly=True, isolation_level='REPEATABLE READ')
            print(json.dumps(dict(mode='preview', tables=table_counts(cursor),
                worker_active=control.worker_active(), integrity_enabled=integrity_enabled()), ensure_ascii=False))
        finally:
            connection.rollback()
            cursor.close()
            connection.close()
        return 0
    if args.backup is None:
        parser.error('--apply 必須指定 --backup；不允許無備份清空')
    backup = backup_target(args.backup)
    # Do not pause or stop a busy API. The caller may safely retry after its
    # existing user-authorized work has reached a safe boundary.
    with control.worker():
        state = control.snapshot()
        require_idle(state, integrity_enabled())
        connection = connect()
        try:
            result = reset_ledger(connection, backup, state)
        finally:
            connection.close()
        print(json.dumps(result, ensure_ascii=False), flush=True)
        try:
            control.update(day_integrity=None, day_integrity_checked=0, day_gap_date=None,
                day_integrity_maintenance=None, audit_pending_symbols=0, audit_pending_dates=0,
                day_integrity_reset=dict(at=datetime.now(timezone.utc).isoformat(), backup=str(backup),
                    deleted=result['deleted'], status='not_checked', policy_version=POLICY_VERSION))
        except Exception as error:
            print(f'兩張 MySQL 清單已清空且有完整備份，但本機驗收統計未能清除：{error}', file=sys.stderr)
            return 2
    print('舊驗收與缺口已清空；尚未重新驗收。行情、一般工作與斷點未修改，未登入／啟動回補。', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'驗收清單作業停止：{error}', file=sys.stderr)
        raise SystemExit(1)
