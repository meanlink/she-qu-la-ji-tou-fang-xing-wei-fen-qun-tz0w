"""HTTP-level checks for the offline backfill endpoints using the real FastAPI app.

SQLite stands in for MySQL with the same table shapes; src.offline_sync is unmodified.
"""
import json
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import offline_sync, security
from src.main import app
from src.offline_sync import OfflineRecord

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
INSERT INTO disposal_points VALUES (1,'QW-01','银杏西门',1,NULL,NULL);
'''

STAFF = {'field': {'username': 'field', 'display_name': '林晓 · 巡查员', 'role': 'analyst'},
         'ops': {'username': 'ops', 'display_name': '梁蔚 · 社区运营', 'role': 'admin'},
         'desk': {'username': 'desk', 'display_name': '方雪 · 社区联络', 'role': 'viewer'}}


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
        self.execute('INSERT INTO field_audit (actor,event,object_id,facts) VALUES (%s,%s,%s,%s)',
                     (actor, event, object_id, json.dumps(facts, ensure_ascii=False)))


@pytest.fixture
def connection(monkeypatch):
    store = sqlite3.connect(':memory:', detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    store.executescript(SCHEMA)

    @contextmanager
    def register(write=False):
        yield Ledger(store)
        store.commit()

    monkeypatch.setattr(offline_sync, 'register', register)
    return store


@pytest.fixture
def http():
    app.dependency_overrides[offline_sync.field] = lambda: STAFF['field']
    app.dependency_overrides[offline_sync.operations] = lambda: STAFF['ops']
    app.dependency_overrides[security.signed_in] = lambda: STAFF['desk']
    yield TestClient(app)
    app.dependency_overrides.clear()


def payload(**overrides):
    record = dict(client_record_id='rec-0001', point_id=1, point_revision=1, sample_day='2026-11-10',
                  slot='morning', total=22, correct=12, overflow=1, task_id=None)
    record.update(overrides)
    return {'client_batch_id': 'batch-0001', 'records': [record]}


def occupy(connection, total=19):
    connection.execute('INSERT INTO sampling_windows '
                       '(point_id,point_revision,sample_day,slot,total,correct,overflow,task_id,entered_by) '
                       "VALUES (1,1,'2026-11-10','morning',?,'%s',2,NULL,'ops')" % 9, (total,))
    connection.commit()


def test_models_validate_counts_and_dates():
    with pytest.raises(ValidationError):
        OfflineRecord(**{**payload()['records'][0], 'correct': 99})
    with pytest.raises(ValidationError):
        OfflineRecord(**{**payload()['records'][0], 'sample_day': '2999-01-01'})


def test_sync_books_free_windows_through_http(connection, http):
    created = http.post('/offline-backfills', json=payload())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body['summary'] == {'booked': 1, 'pending': 0, 'returned': 0}
    assert body['records'][0]['window_id'] is not None
    assert connection.execute('SELECT COUNT(*) FROM sampling_windows').fetchone()[0] == 1


def test_retry_then_adjudication_flow(connection, http):
    occupy(connection)
    created = http.post('/offline-backfills', json=payload())
    assert created.json()['summary'] == {'booked': 0, 'pending': 1, 'returned': 0}
    record_id = created.json()['records'][0]['id']

    retried = http.post('/offline-backfills', json=payload())
    assert retried.json()['summary'] == {'booked': 0, 'pending': 1, 'returned': 0}

    board = http.get('/offline-backfills').json()
    held = board['records'][0]
    assert held['state'] == 'pending' and held['reason'] == 'window_occupied'
    assert held['apply_blocker'] == 'window_occupied'
    assert board['reasons']['window_occupied'] == '同一窗口已有计数'

    conflict = http.post(f'/offline-backfills/{record_id}/resolve', json={'decision': 'apply', 'note': '补入'})
    assert conflict.status_code == 409
    assert connection.execute('SELECT COUNT(*) FROM sampling_windows').fetchone()[0] == 1

    connection.execute('DELETE FROM sampling_windows')
    connection.commit()
    applied = http.post(f'/offline-backfills/{record_id}/resolve', json={'decision': 'apply', 'note': '窗口已空'})
    assert applied.status_code == 200, applied.text
    assert applied.json()['state'] == 'booked' and applied.json()['window_id'] is not None


def test_viewer_cannot_sync(connection, monkeypatch):
    app.dependency_overrides[offline_sync.field] = lambda: (_ for _ in ()).throw(
        HTTPException(403, '当前岗位无权执行此操作'))
    try:
        response = TestClient(app).post('/offline-backfills', json=payload())
        assert response.status_code == 403
        assert connection.execute('SELECT COUNT(*) FROM offline_backfills').fetchone()[0] == 0
    finally:
        app.dependency_overrides.clear()


def test_validation_error_shape(http):
    response = http.post('/offline-backfills', json=payload(slot='night'))
    assert response.status_code == 422
    assert 'records.0.slot' in response.json()['fields']
