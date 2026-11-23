from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, model_validator

from src.contracts import Form, Role, Slot, local_day
from src.fieldwork import insert_window, required
from src.security import allow, signed_in
from src.storage import register

router = APIRouter()
field = allow(Role.ANALYST, Role.ADMIN)
operations = allow(Role.ADMIN)

MAX_RECORDS = 100

REASONS = {
    'migration_pending': '点位迁移待核，地址尚未核实',
    'revision_changed': '点位地址版本已变更，原班次已失效',
    'sample_not_started': '新址采样尚未开始，原班次无效',
    'window_occupied': '同一窗口已有计数',
    'task_invalid': '复测任务与记录不匹配',
    'task_closed': '宣教任务已归档',
    'slot_rescheduled': '班次已改约，原日期不在约定班次内',
    'slot_reserved': '原窗口已为其他宣教任务预留',
}

WINDOW_SQL = '''SELECT id FROM sampling_windows WHERE point_id=%s AND point_revision=%s
    AND sample_day=%s AND slot=%s'''

POINT_SQL = 'SELECT id,code,name,revision,sample_from,pending_location FROM disposal_points WHERE id=%s'

TASK_SQL = '''SELECT id,point_id,point_revision,focus_slot,delivered_day,state
    FROM outreach_tasks WHERE id=%s'''

APPOINTMENT_SQL = 'SELECT first_day,second_day FROM retest_appointments WHERE task_id=%s'

RESERVATION_SQL = '''SELECT t.id FROM outreach_tasks t JOIN retest_appointments a ON a.task_id=t.id
    WHERE t.point_id=%s AND t.point_revision=%s AND t.focus_slot=%s AND t.state='retesting'
    AND %s IN (a.first_day,a.second_day) LIMIT 1'''

RECORD_SELECT = '''SELECT b.*,p.code,p.name,p.revision AS current_revision,p.pending_location,
    bw.client_batch_id AS batch_ref FROM offline_backfills b JOIN disposal_points p ON p.id=b.point_id
    JOIN offline_batches bw ON bw.id=b.batch_id'''


class OfflineRecord(Form):
    client_record_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9._:-]+$')
    point_id: int = Field(gt=0)
    point_revision: int = Field(ge=1)
    sample_day: date
    slot: Slot
    total: int = Field(ge=5, le=1000)
    correct: int = Field(ge=0)
    overflow: int = Field(ge=0)
    task_id: int | None = Field(default=None, gt=0)
    captured_at: datetime | None = None

    @model_validator(mode='after')
    def valid_counts(self):
        if self.correct > self.total or self.overflow > self.total:
            raise ValueError('正确数和满溢记录数不能超过观察总数')
        if self.sample_day > local_day():
            raise ValueError('采样日期不能在未来')
        return self


class OfflineBatch(Form):
    client_batch_id: str = Field(min_length=8, max_length=64, pattern=r'^[A-Za-z0-9._:-]+$')
    records: list[OfflineRecord] = Field(min_length=1, max_length=MAX_RECORDS)


class BackfillResolution(Form):
    decision: Literal['apply', 'return']
    note: str = Field(default='', max_length=240)

    @model_validator(mode='after')
    def return_needs_reason(self):
        if self.decision == 'return' and len(self.note.strip()) < 4:
            raise ValueError('退回补传记录须注明原因')
        return self


def hold_reason(ledger, point_id, point_revision, sample_day, slot, task_id):
    """Return a reason code when the record cannot enter its original window, else None.

    Only an empty, still-effective original window may take the record. A count already
    booked into the window is never displaced: the record stays for manual adjudication.
    """
    point = ledger.one(POINT_SQL, (point_id,))
    if point is None:
        return 'revision_changed'
    if point['pending_location']:
        return 'migration_pending'
    if point['revision'] != point_revision:
        return 'revision_changed'
    if point['sample_from'] and sample_day < point['sample_from']:
        return 'sample_not_started'
    if ledger.one(WINDOW_SQL, (point_id, point_revision, sample_day, slot)):
        return 'window_occupied'
    if task_id is not None:
        task = ledger.one(TASK_SQL, (task_id,))
        if (task is None or task['point_id'] != point_id or task['point_revision'] != point_revision
                or task['focus_slot'] != slot or task['delivered_day'] is None
                or sample_day <= task['delivered_day']):
            return 'task_invalid'
        if task['state'] != 'retesting':
            return 'task_closed'
        assignment = ledger.one(APPOINTMENT_SQL, (task_id,))
        if assignment and sample_day not in {assignment['first_day'], assignment['second_day']}:
            return 'slot_rescheduled'
    reserved = ledger.one(RESERVATION_SQL, (point_id, point_revision, slot, sample_day))
    if reserved and reserved['id'] != task_id:
        return 'slot_reserved'
    return None


def batch_for(ledger, client_batch_id, actor):
    batch = ledger.one('SELECT id,submitted_by FROM offline_batches WHERE client_batch_id=%s', (client_batch_id,))
    if batch:
        if batch['submitted_by'] != actor:
            raise HTTPException(409, '该补传批次属于其他巡查员')
        return batch['id']
    return ledger.execute('INSERT INTO offline_batches (client_batch_id,submitted_by) VALUES (%s,%s)',
                          (client_batch_id, actor))


def record_outcome(ledger, record_id):
    return ledger.one(RECORD_SELECT + ' WHERE b.id=%s', (record_id,))


def batch_outcomes(ledger, batch_id):
    return ledger.rows(RECORD_SELECT + ' WHERE b.batch_id=%s ORDER BY b.id', (batch_id,))


def book(ledger, record, actor):
    window_id = insert_window(
        ledger, point_id=record['point_id'], point_revision=record['point_revision'],
        sample_day=record['sample_day'], slot=record['slot'], total=record['total'],
        correct=record['correct'], overflow=record['overflow'], task_id=record['task_id'],
        entered_by=record['submitted_by'],
    )
    ledger.execute('UPDATE offline_backfills SET window_id=%s WHERE id=%s', (window_id, record['id']))
    ledger.audit(actor, 'offline_backfill_booked', record['id'], {
        'window_id': window_id, 'point_id': record['point_id'], 'point_revision': record['point_revision'],
        'task_id': record['task_id'], 'sample_day': str(record['sample_day']), 'slot': record['slot'],
        'batch_id': record['batch_id'],
    })
    return window_id


@router.post('/offline-backfills')
def sync_backfill(form: OfflineBatch, staff=Depends(field)):
    actor = staff['username']
    with register(write=True) as ledger:
        # Reject malformed references before writing, so a retry can be applied cleanly.
        for record in form.records:
            if ledger.one(POINT_SQL, (record.point_id,)) is None:
                raise HTTPException(409, f'点位 #{record.point_id} 不存在，整批补传未入账')
            if record.task_id is not None and ledger.one(TASK_SQL, (record.task_id,)) is None:
                raise HTTPException(409, f'宣教任务 #{record.task_id} 不存在，整批补传未入账')
        batch_id = batch_for(ledger, form.client_batch_id, actor)
        seen = set()
        for record in form.records:
            if record.client_record_id in seen:
                continue
            seen.add(record.client_record_id)
            existing = ledger.one('SELECT submitted_by FROM offline_backfills WHERE client_record_id=%s',
                                  (record.client_record_id,))
            if existing:
                # A retried or overlapping submission keeps the original outcome untouched.
                if existing['submitted_by'] != actor:
                    raise HTTPException(409, '补传记录编号已被其他巡查员使用')
                continue
            reason = hold_reason(ledger, record.point_id, record.point_revision, record.sample_day,
                                 record.slot, record.task_id)
            record_id = ledger.execute('''INSERT INTO offline_backfills
                (batch_id,client_record_id,point_id,point_revision,sample_day,slot,total,correct,overflow,
                 task_id,state,reason,captured_at,submitted_by)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                (batch_id, record.client_record_id, record.point_id, record.point_revision, record.sample_day,
                 record.slot, record.total, record.correct, record.overflow, record.task_id,
                 'pending' if reason else 'booked', reason or '',
                 record.captured_at.replace(tzinfo=None) if record.captured_at else None, actor))
            stored = ledger.one('SELECT * FROM offline_backfills WHERE id=%s', (record_id,))
            if reason:
                ledger.audit(actor, 'offline_backfill_held', record_id, {
                    'reason': reason, 'batch_id': batch_id, 'point_id': record.point_id,
                    'sample_day': str(record.sample_day), 'slot': record.slot,
                })
            else:
                book(ledger, stored, actor)
        records = batch_outcomes(ledger, batch_id)
        summary = {state: sum(1 for row in records if row['state'] == state)
                   for state in ('booked', 'pending', 'returned')}
        ledger.audit(actor, 'offline_batch_synced', batch_id, {
            'client_batch_id': form.client_batch_id, 'received': len(form.records), 'summary': summary,
        })
        return {'id': batch_id, 'client_batch_id': form.client_batch_id,
                'records': records, 'summary': summary}


@router.get('/offline-backfills')
def backfill_ledger(_staff=Depends(signed_in)):
    with register() as ledger:
        records = ledger.rows(RECORD_SELECT + " ORDER BY (b.state='pending') DESC,b.id DESC LIMIT 200")
        for record in records:
            if record['state'] == 'pending':
                record['apply_blocker'] = hold_reason(
                    ledger, record['point_id'], record['point_revision'], record['sample_day'],
                    record['slot'], record['task_id'])
        return {'records': records, 'reasons': REASONS}


@router.post('/offline-backfills/{record_id}/resolve')
def resolve_backfill(record_id: int, form: BackfillResolution, staff=Depends(operations)):
    actor = staff['username']
    with register(write=True) as ledger:
        record = required(ledger, 'SELECT * FROM offline_backfills WHERE id=%s', record_id, '补传记录')
        if record['state'] != 'pending':
            raise HTTPException(409, '仅待裁定的补传记录可处置')
        if form.decision == 'return':
            ledger.execute('''UPDATE offline_backfills SET state='returned',decided_by=%s,decided_at=NOW(),
                decision_note=%s WHERE id=%s''', (actor, form.note, record_id))
            ledger.audit(actor, 'offline_backfill_returned', record_id, {'note': form.note})
            return record_outcome(ledger, record_id)
        reason = hold_reason(ledger, record['point_id'], record['point_revision'], record['sample_day'],
                             record['slot'], record['task_id'])
        if reason:
            raise HTTPException(409, f'{REASONS.get(reason, reason)}，无法补入原班次')
        window_id = book(ledger, record, actor)
        ledger.execute('''UPDATE offline_backfills SET state='booked',decided_by=%s,decided_at=NOW(),
            decision_note=%s WHERE id=%s''', (actor, form.note, record_id))
        ledger.audit(actor, 'offline_backfill_applied', record_id, {'window_id': window_id, 'note': form.note})
        return record_outcome(ledger, record_id)
