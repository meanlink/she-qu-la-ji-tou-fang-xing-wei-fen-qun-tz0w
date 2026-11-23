"""Exercises the offline backfill state machine against a real SQL engine.

The container ships MySQL; this suite uses SQLite with the same column shapes so the
production logic in src.offline_sync runs unmodified (placeholders are rewritten).
"""
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

import pytest
from fastapi import HTTPException

from src import offline_sync
from src.offline_sync import BackfillResolution, OfflineBatch, OfflineRecord

SCHEMA = '''
CREATE TABLE disposal_points (id INTEGER PRIMARY KEY, code TEXT, name TEXT, revision INTEGER,
    sample_from DATE, pending_location TEXT);
CREATE TABLE sampling_windows (id INTEGER PRIMARY KEY AUTOINCREMENT, point_id INTEGER, point_revision INTEGER,
    sample_day DATE, slot TEXT, total INTEGER, correct INTEGER, overflow INTEGER, task_id INTEGER, entered_by TEXT);
CREATE TABLE outreach_tasks (id INTEGER PRIMARY KEY, point_id INTEGER, point_revision INTEGER, focus_slot TEXT,
    delivered_day DATE, state TEXT);
CREATE TABLE retest_appointments (task_id INTEGER PRIMARY KEY, first_day DATE, second_day DATE);
CREATE TABLE offline_batches (id INTEGER PRIMARY KEY AUTOINCREMENT, client_batch_id TEXT UNIQUE,
    submitted_by TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE offline_backfills (id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, client_record_id TEXT UNIQUE,
    point_id INTEGER, point_revision INTEGER, sample_day DATE, slot TEXT, total INTEGER, correct INTEGER,
    overflow INTEGER, task_id INTEGER, state TEXT, reason TEXT DEFAULT '', window_id INTEGER, captured_at DATETIME,
    submitted_by TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, decided_by TEXT, decided_at TIMESTAMP,
    decision_note TEXT);
CREATE TABLE field_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT, event TEXT, object_id INTEGER,
    facts TEXT, happened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
'''


class Ledger:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, args=()):
        cursor = self.connection.execute(sql.replace('%s', '?').replace('NOW()', 'CURRENT_TIMESTAMP'), args)
        return cursor.lastrowid

    def rows(self, sql, args=()):
        cursor = self.connection.execute(sql.replace('%s', '?').replace('NOW()', 'CURRENT_TIMESTAMP'), args)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    def one(self, sql, args=()):
        return next(iter(self.rows(sql, args)), None)

    def audit(self, actor, event, object_id, facts):
        import json
        self.execute('INSERT INTO field_audit (actor,event,object_id,facts) VALUES (%s,%s,%s,%s)',
                     (actor, event, object_id, json.dumps(facts, ensure_ascii=False)))


class World:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:', detect_types=sqlite3.PARSE_DECLTYPES)
        self.connection.row_factory = None
        self.connection.executescript(SCHEMA)
        self.connection.executescript('''
            INSERT INTO disposal_points VALUES (1,'QW-01','银杏西门',1,NULL,NULL);
            INSERT INTO disposal_points VALUES (2,'QW-03','云岫车棚',1,NULL,NULL);
            INSERT INTO disposal_points VALUES (3,'QW-05','溪径花厅',2,'2026-11-12',NULL);
            INSERT INTO outreach_tasks VALUES (9,2,1,'morning','2026-11-12','retesting');
            INSERT INTO retest_appointments VALUES (9,'2026-11-13','2026-11-14');
        ''')

    @contextmanager
    def register(self, write=False):
        yield Ledger(self.connection)
        self.connection.commit()

    def count(self, table, where='1=1'):
        return self.connection.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}').fetchone()[0]


@pytest.fixture
def world(monkeypatch):
    instance = World()
    monkeypatch.setattr(offline_sync, 'register', instance.register)
    return instance


def record(**overrides):
    data = dict(client_record_id='rec-0001', point_id=1, point_revision=1, sample_day=date(2026, 11, 10),
                slot='morning', total=22, correct=12, overflow=1, task_id=None)
    data.update(overrides)
    return OfflineRecord(**data)


def sync(world, *records, batch='batch-0001'):
    return offline_sync.sync_backfill(OfflineBatch(client_batch_id=batch, records=list(records)), {'username': 'field'})


def test_free_window_books_immediately(world):
    result = sync(world, record())
    assert result['summary'] == {'booked': 1, 'pending': 0, 'returned': 0}
    assert world.count('sampling_windows') == 1
    row = world.connection.execute('SELECT * FROM sampling_windows').fetchone()
    assert row[1:9] == (1, 1, date(2026, 11, 10), 'morning', 22, 12, 1, None)
    stored = result['records'][0]
    assert stored['state'] == 'booked' and stored['window_id'] is not None
    assert stored['batch_ref'] == 'batch-0001'


def test_occupied_window_is_held_and_original_counts_survive(world):
    world.connection.execute("INSERT INTO sampling_windows (point_id,point_revision,sample_day,slot,total,correct,overflow,task_id,entered_by) "
                             "VALUES (1,1,'2026-11-10','morning',19,9,2,NULL,'ops')")
    world.connection.commit()
    result = sync(world, record())
    held = result['records'][0]
    assert held['state'] == 'pending' and held['reason'] == 'window_occupied'
    assert held['window_id'] is None
    assert world.count('sampling_windows') == 1
    assert world.connection.execute('SELECT total FROM sampling_windows').fetchone()[0] == 19
    assert world.count('offline_backfills') == 1


def test_retry_is_idempotent(world):
    first = sync(world, record(), record(client_record_id='rec-0002', slot='evening', total=38, correct=22, overflow=3))
    again = sync(world, record(), record(client_record_id='rec-0002', slot='evening', total=38, correct=22, overflow=3))
    assert first['summary'] == {'booked': 2, 'pending': 0, 'returned': 0}
    assert again['summary'] == {'booked': 2, 'pending': 0, 'returned': 0}
    assert world.count('sampling_windows') == 2
    assert world.count('offline_backfills') == 2
    assert world.count('offline_batches') == 1


def test_partial_batch_retry_reconciles_new_records_only(world):
    sync(world, record())
    result = sync(world, record(), record(client_record_id='rec-0002', slot='evening', total=38, correct=22, overflow=3))
    assert result['summary']['booked'] == 2
    assert world.count('sampling_windows') == 2


def test_hold_reasons_do_not_book(world):
    world.connection.execute("UPDATE disposal_points SET pending_location='临时围挡' WHERE id=2")
    world.connection.commit()
    _, _, mixed = None, None, sync(
        world,
        record(point_id=2, client_record_id='rec-pending'),
        record(point_id=1, point_revision=2, client_record_id='rec-revision'),
        record(point_id=3, point_revision=2, sample_day=date(2026, 11, 11), client_record_id='rec-early-1'),
        record(point_id=1, client_record_id='rec-okay-1', sample_day=date(2026, 11, 11)),
    )
    reasons = {row['client_record_id']: row['reason'] for row in mixed['records']}
    assert reasons['rec-pending'] == 'migration_pending'
    assert reasons['rec-revision'] == 'revision_changed'
    assert reasons['rec-early-1'] == 'sample_not_started'
    assert reasons['rec-okay-1'] == ''
    assert mixed['summary'] == {'booked': 1, 'pending': 3, 'returned': 0}
    assert world.count('sampling_windows') == 1


def test_retest_record_must_match_booked_slot(world):
    booked = sync(world, record(point_id=2, sample_day=date(2026, 11, 13), total=9, correct=8, overflow=0, task_id=9))
    assert booked['records'][0]['state'] == 'booked'
    rescheduled = sync(world, record(point_id=2, sample_day=date(2026, 11, 15), total=9, correct=8, overflow=0,
                                     task_id=9, client_record_id='rec-0002'), batch='batch-0002')
    assert rescheduled['records'][0]['reason'] == 'slot_rescheduled'
    assert world.count('sampling_windows') == 1


def test_closed_task_is_held(world):
    world.connection.execute("UPDATE outreach_tasks SET state='improved' WHERE id=9")
    world.connection.commit()
    result = sync(world, record(point_id=2, sample_day=date(2026, 11, 13), total=9, correct=8, overflow=0, task_id=9))
    assert result['records'][0]['reason'] == 'task_closed'


def test_task_without_delivery_is_invalid_not_crashing(world):
    world.connection.execute('UPDATE outreach_tasks SET delivered_day=NULL WHERE id=9')
    world.connection.commit()
    result = sync(world, record(point_id=2, sample_day=date(2026, 11, 13), total=9, correct=8, overflow=0, task_id=9))
    assert result['records'][0]['reason'] == 'task_invalid'


def test_window_reserved_for_another_task_is_held(world):
    world.connection.execute('INSERT INTO outreach_tasks VALUES (10,2,1,"morning","2026-11-11","retesting")')
    world.connection.execute("INSERT INTO retest_appointments VALUES (10,'2026-11-13','2026-11-16')")
    world.connection.commit()
    # A baseline record colliding with another task's reserved retest slot is held, never booked.
    result = sync(world, record(point_id=2, sample_day=date(2026, 11, 13), total=9, correct=8, overflow=0))
    assert result['records'][0]['reason'] == 'slot_reserved'
    assert world.count('sampling_windows') == 0


def test_ledger_exposes_reasons_and_apply_blocker(world):
    world.connection.execute("INSERT INTO sampling_windows (point_id,point_revision,sample_day,slot,total,correct,overflow,task_id,entered_by) "
                             "VALUES (1,1,'2026-11-10','morning',19,9,2,NULL,'ops')")
    world.connection.commit()
    sync(world, record())
    board = offline_sync.backfill_ledger({'username': 'desk'})
    row = board['records'][0]
    assert row['state'] == 'pending' and row['apply_blocker'] == 'window_occupied'
    assert board['reasons']['window_occupied'] == '同一窗口已有计数'


def test_operator_can_book_held_record_into_free_window(world):
    world.connection.execute("INSERT INTO sampling_windows (point_id,point_revision,sample_day,slot,total,correct,overflow,task_id,entered_by) "
                             "VALUES (1,1,'2026-11-10','morning',19,9,2,NULL,'ops')")
    world.connection.commit()
    held = sync(world, record())['records'][0]
    with pytest.raises(HTTPException) as error:
        offline_sync.resolve_backfill(held['id'], BackfillResolution(decision='apply', note='补入原班次'), {'username': 'ops'})
    assert error.value.status_code == 409
    world.connection.execute("DELETE FROM sampling_windows WHERE total=19")
    world.connection.commit()
    applied = offline_sync.resolve_backfill(held['id'], BackfillResolution(decision='apply', note='窗口已空'), {'username': 'ops'})
    assert applied['state'] == 'booked' and applied['window_id'] is not None
    assert applied['decided_by'] == 'ops'
    assert world.count('sampling_windows') == 1


def test_operator_return_requires_reason_and_keeps_counts(world):
    world.connection.execute("INSERT INTO sampling_windows (point_id,point_revision,sample_day,slot,total,correct,overflow,task_id,entered_by) "
                             "VALUES (1,1,'2026-11-10','morning',19,9,2,NULL,'ops')")
    world.connection.commit()
    held = sync(world, record())['records'][0]
    with pytest.raises(Exception):
        BackfillResolution(decision='return', note='')
    returned = offline_sync.resolve_backfill(held['id'], BackfillResolution(decision='return', note='重复计数，退回'), {'username': 'ops'})
    assert returned['state'] == 'returned' and returned['window_id'] is None
    assert world.connection.execute('SELECT total FROM sampling_windows').fetchone()[0] == 19
    assert world.count('sampling_windows') == 1


def test_resolved_record_cannot_be_redecided(world):
    world.connection.execute("INSERT INTO sampling_windows (point_id,point_revision,sample_day,slot,total,correct,overflow,task_id,entered_by) "
                             "VALUES (1,1,'2026-11-10','morning',19,9,2,NULL,'ops')")
    world.connection.commit()
    held = sync(world, record())['records'][0]
    offline_sync.resolve_backfill(held['id'], BackfillResolution(decision='return', note='退回说明'), {'username': 'ops'})
    with pytest.raises(HTTPException) as error:
        offline_sync.resolve_backfill(held['id'], BackfillResolution(decision='apply'), {'username': 'ops'})
    assert error.value.status_code == 409


def test_other_operator_cannot_hijack_batch(world):
    sync(world, record())
    with pytest.raises(HTTPException) as error:
        offline_sync.sync_backfill(OfflineBatch(client_batch_id='batch-0001', records=[record(client_record_id='rec-0002')]),
                                   {'username': 'someone-else'})
    assert error.value.status_code == 409


def test_capture_time_is_preserved(world):
    sync(world, record(captured_at=datetime(2026, 11, 10, 7, 32, 5)))
    assert world.connection.execute('SELECT captured_at FROM offline_backfills').fetchone()[0].startswith('2026-11-10 07:32:05')


def test_missing_point_rejects_whole_batch_before_writing(world):
    with pytest.raises(HTTPException) as error:
        sync(world, record(), record(point_id=99, client_record_id='rec-0002'))
    assert error.value.status_code == 409
    assert world.count('offline_backfills') == 0
    assert world.count('offline_batches') == 0
