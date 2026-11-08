"""Reset is backed up, rollback-capable, lease-guarded, and never touches prices."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts.reset_day_integrity import TABLES, backup_target, require_idle, reset_ledger, verify_backup


class Cursor:
    def __init__(self, connection):
        self.c = connection
        self.description = [('first',), ('second',)]
        self.rows = []
        self.rowcount = 0

    def execute(self, sql):
        self.c.sql.append(sql)
        if 'information_schema.tables' in sql:
            self.rows = [(t, 'InnoDB') for t in TABLES]
        elif 'information_schema.triggers' in sql:
            self.rows = [('unexpected',)] if self.c.trigger else []
        elif 'information_schema.referential_constraints' in sql:
            self.rows = []
        else:
            table = next(t for t in TABLES if '`'+t+'`' in sql)
            if sql.startswith('SELECT COUNT'):
                self.rows = [(len(self.c.working[table]),)]
            elif sql.startswith('SELECT *'):
                self.rows = list(self.c.working[table])
            elif sql.startswith('DELETE'):
                if table == self.c.fail_delete:
                    raise OSError('injected write failure')
                self.rowcount = len(self.c.working[table])
                self.c.working[table] = []
                self.rows = []
            else:
                raise AssertionError(sql)

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def fetchone(self):
        return self.fetchmany(1)[0]

    def fetchmany(self, count):
        rows, self.rows = self.rows[:count], self.rows[count:]
        return rows

    def close(self):
        pass


class Connection:
    def __init__(self):
        self.tables = {TABLES[0]: [('00401A', 'unresolved')],
                       TABLES[1]: [('00401A', '2021-09-16'), ('00401A', '2026-07-10')]}
        self.working = deepcopy(self.tables)
        self.candles = [('00401A', '2026-04-10', 10)]
        self.sql = []
        self.committed = False
        self.rolled_back = False
        self.fail_delete = None
        self.trigger = False

    def cursor(self):
        return Cursor(self)

    def start_transaction(self, **kwargs):
        pass

    def commit(self):
        self.tables = deepcopy(self.working)
        self.committed = True

    def rollback(self):
        self.working = deepcopy(self.tables)
        self.rolled_back = True


class ResetTests(unittest.TestCase):
    def test_idle_guard_never_unpauses_or_interrupts_work(self):
        for state, enabled in (({'state':'running','pause_requested':True}, False),
                               ({'day_integrity_active':True}, False),
                               ({'day_integrity_maintenance':{'active':True}}, False),
                               ({'preparation_pending':True}, False), ({}, True)):
            with self.subTest(state=state), self.assertRaises(RuntimeError):
                require_idle(state, enabled)
        require_idle({'pause_requested':True}, True)
        require_idle({'pause_requested':False}, False)

    def test_backup_path_cannot_overwrite_or_escape_output(self):
        with self.assertRaises(ValueError):
            backup_target(Path('/unrelated/report.jsonl.gz'))
        with self.assertRaises(ValueError):
            backup_target(Path('output/backup.json'))

    def test_success_backs_up_all_rows_before_deleting_exactly_two_tables(self):
        c = Connection()
        with TemporaryDirectory() as folder:
            backup = Path(folder)/'backup.jsonl.gz'
            result = reset_ledger(c, backup, {'plans':[{'market_no':0}], 'pause_requested':False}, progress=lambda *a,**k:None)
            saved = verify_backup(backup)
        self.assertEqual(saved['counts'], {TABLES[0]:1, TABLES[1]:2})
        self.assertEqual(result['deleted'], {TABLES[0]:1, TABLES[1]:2})
        self.assertTrue(c.committed)
        self.assertTrue(all(not rows for rows in c.tables.values()))
        self.assertEqual(c.candles, [('00401A', '2026-04-10', 10)])
        self.assertEqual([s for s in c.sql if s.startswith('DELETE')],
                         ['DELETE FROM `candle_day_gaps`', 'DELETE FROM `candle_day_integrity`'])

    def test_backup_failure_never_deletes(self):
        c = Connection()
        with TemporaryDirectory() as folder, patch('scripts.reset_day_integrity.verify_backup', side_effect=ValueError('corrupt')):
            with self.assertRaises(ValueError):
                reset_ledger(c, Path(folder)/'backup.jsonl.gz', {}, progress=lambda *a,**k:None)
        self.assertTrue(c.rolled_back)
        self.assertFalse(any(s.startswith('DELETE') for s in c.sql))

    def test_second_delete_failure_rolls_back_both_tables_and_retains_backup(self):
        c = Connection(); before = deepcopy(c.tables); c.fail_delete = TABLES[0]
        with TemporaryDirectory() as folder:
            backup = Path(folder)/'backup.jsonl.gz'
            with self.assertRaises(OSError):
                reset_ledger(c, backup, {}, progress=lambda *a,**k:None)
            self.assertEqual(verify_backup(backup)['counts'], {TABLES[0]:1, TABLES[1]:2})
        self.assertEqual(c.working, before)
        self.assertFalse(c.committed)

    def test_unexpected_trigger_prevents_backup_and_deletion(self):
        c = Connection(); c.trigger = True
        with TemporaryDirectory() as folder:
            backup = Path(folder)/'backup.jsonl.gz'
            with self.assertRaises(RuntimeError):
                reset_ledger(c, backup, {}, progress=lambda *a,**k:None)
            self.assertFalse(backup.exists())
        self.assertFalse(any(s.startswith('DELETE') for s in c.sql))
