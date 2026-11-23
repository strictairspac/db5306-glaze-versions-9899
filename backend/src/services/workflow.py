import hashlib
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError

from src.config.settings import cache, engine
from src.constants.workflow import Status
from src.models.tables import (audit, cases, correction_lines, correction_orders,
                               correction_state, evidence, runs)
from src.services import corrections, domain
from src.utils.encoding import canonical
from src.utils.time import utc_time


def log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action, reference=str(reference), created_at=utc_time()))


def recent(table):
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(table).order_by(table.c.id.desc()).limit(100)).mappings()]


def found(conn, table, identity):
    row = conn.execute(select(table).where(table.c.id == identity)).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def found_locked(conn, table, identity):
    """Locking read: always sees the latest committed row, not the snapshot."""

    row = conn.execute(select(table).where(table.c.id == identity)
                       .with_for_update()).mappings().first()
    if row is None:
        raise HTTPException(404, '找不到该记录')
    return dict(row)


def make_case(title, payload, actor):
    title = title.strip()
    if not title:
        raise HTTPException(422, '档案名称不能为空')
    try:
        if len(canonical(payload)) > 512 * 1024:
            raise ValueError('业务输入超过 512 KiB')
        if callable(getattr(domain, 'validate', None)):
            domain.validate(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    with engine.begin() as conn:
        case_id = conn.execute(cases.insert().values(slug=uuid.uuid4().hex, title=title, payload=payload,
                               author=actor, created_at=utc_time())).inserted_primary_key[0]
        conn.execute(correction_state.insert().values(case_id=case_id, current_batch=0,
                                                      updated_at=utc_time()))
        log(conn, actor, 'create_case', case_id)
        return found(conn, cases, case_id)


def enqueue(case_id, actor):
    with engine.begin() as conn:
        found(conn, cases, case_id)
        run_id = conn.execute(runs.insert().values(case_id=case_id, status=Status.QUEUED,
                              owner=actor, created_at=utc_time())).inserted_primary_key[0]
        log(conn, actor, 'enqueue', run_id)
        result = found(conn, runs, run_id)
    try:
        cache.lpush('worker:wake', 'queued')
        cache.ltrim('worker:wake', 0, 99)
    except Exception:
        # The durable database queue is polled even if Redis is temporarily unavailable.
        pass
    return result


def claim():
    ready = or_(runs.c.status == Status.QUEUED,
                and_(runs.c.status == Status.RUNNING, runs.c.started_at < utc_time(-600)))
    with engine.begin() as conn:
        run = conn.execute(select(runs.c.id, runs.c.case_id).where(ready).order_by(runs.c.id)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if not run:
            return None
        token = str(uuid.uuid4())
        conn.execute(runs.update().where(runs.c.id == run.id).values(
            started_at=utc_time(), status=Status.RUNNING, attempt_token=token))
        case = found(conn, cases, run.case_id)
        return run.id, token, case['payload'], run.case_id


def read_corrections(conn, case_id, locking=False):
    """Read one coherent batch of effective corrections for a single case.

    The batch counter moves forward only inside the transaction that inserts
    its lines, so a stamped batch always resolves to a complete correction set.

    ``locking`` turns both statements into current reads.  Callers that already
    hold the case state lock use it so a REPEATABLE READ snapshot taken before
    the lock cannot hide a correction that committed just before it.

    Returns ``(batch, lines)`` where ``lines`` is the full history up to the
    current batch.  The list is never cut down to a stale batch: only the
    effective replay is narrowed by ``domain.solve`` before a report is written.
    """

    state = select(correction_state.c.current_batch).where(correction_state.c.case_id == case_id)
    batch = conn.execute(state.with_for_update() if locking else state).scalar_one_or_none() or 0
    if not batch:
        return 0, []
    lines = select(correction_lines).where(
        correction_lines.c.case_id == case_id).order_by(
        correction_lines.c.batch_seq, correction_lines.c.order_id, correction_lines.c.id)
    rows = conn.execute(lines.with_for_update() if locking else lines).mappings()
    return batch, [line_from_row(row) for row in rows]


def line_from_row(row):
    raw = row['supersedes'] or ''
    return corrections.CorrectionLine(
        id=row['id'], order_id=row['order_id'], batch_seq=row['batch_seq'],
        formula_id=row['formula_id'], effective_from=row['effective_from'],
        effective_to=row['effective_to'], recipe=row['recipe'],
        supersedes=tuple(int(value) for value in raw.split(',') if value))


def lock_state(conn, case_id):
    """Serialize correction commits against evidence stamping for one case."""

    statement = select(correction_state.c.current_batch).where(
        correction_state.c.case_id == case_id).with_for_update()
    batch = conn.execute(statement).scalar_one_or_none()
    if batch is None:
        try:
            with conn.begin_nested():
                conn.execute(correction_state.insert().values(
                    case_id=case_id, current_batch=0, updated_at=utc_time()))
        except IntegrityError:
            pass
        batch = conn.execute(statement).scalar_one_or_none() or 0
    return batch


def finish(run_id, token, payload=None, case_id=None, error=None):
    """Write the evidence for a run.

    Evidence is stamped with the correction batch read under the case lock, and
    the report is computed from exactly that batch.  A run that finishes after
    a correction therefore stores the new batch instead of a stale preview.
    ``payload`` is accepted for API compatibility; the stored case payload is
    re-read under the lock so a concurrent ledger edit cannot slip in.
    """
    if error is not None:
        mark_failed(run_id, token, error)
        return
    try:
        with engine.begin() as conn:
            # Lock the state row before the first consistent read so a
            # correction that committed after claim() is still observed here.
            batch = lock_state(conn, case_id)
            count = conn.execute(runs.update().where(runs.c.id == run_id,
                            runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                            .values(status=Status.COMPLETE, finished_at=utc_time(),
                                    error=None)).rowcount
            if not count:
                return
            case = found_locked(conn, cases, case_id)
            _, lines = read_corrections(conn, case_id, locking=True)
            result = domain.solve(case['payload'], engine, (batch, lines))
            digest = hashlib.sha256(canonical(result)).hexdigest()
            conn.execute(evidence.insert().values(run_id=run_id, report=result, digest=digest,
                                                  correction_batch=batch))
            log(conn, 'worker', Status.COMPLETE.value, run_id)
    except (ValueError, TypeError, KeyError, RuntimeError) as exc:
        mark_failed(run_id, token, str(exc)[:500])


def mark_failed(run_id, token, error):
    with engine.begin() as conn:
        count = conn.execute(runs.update().where(runs.c.id == run_id,
                        runs.c.attempt_token == token, runs.c.status == Status.RUNNING)
                        .values(status=Status.FAILED, finished_at=utc_time(),
                                error=error)).rowcount
        if count:
            log(conn, 'worker', Status.FAILED.value, run_id)


def correct(case_id, payload, actor):
    """Commit one correction order atomically and idempotently.

    Retrying the same ``request_id`` returns the stored order without applying
    it twice or advancing the batch.  A new order advances the batch once, no
    matter how many formulas it touches.
    """

    if not isinstance(payload, dict):
        raise HTTPException(422, '更正单必须是对象')
    raw_request = payload.get('request_id')
    with engine.begin() as conn:
        try:
            # The state row is the per-case serialization point.  Take it before
            # any plain read: under REPEATABLE READ the first consistent read
            # fixes the snapshot, so locking first guarantees the reads below
            # include every correction that already won the lock.
            current = lock_state(conn, case_id)
            case = found(conn, cases, case_id)
            request_id = raw_request.strip() if isinstance(raw_request, str) else ''
            if request_id:
                existing = conn.execute(select(correction_orders).where(
                    correction_orders.c.request_id == request_id)).mappings().first()
                if existing is not None:
                    if existing['case_id'] != case_id:
                        raise HTTPException(409, '该提交单号已用于另一档案')
                    _ensure_same_order(conn, dict(existing), payload)
                    return _order_view(conn, dict(existing))
            request_id, note, drafts = corrections.parse_submission(
                payload, domain.parse_ledger(case['payload']))
            _, lines = read_corrections(conn, case_id, locking=True)
            corrections.validate_against_existing(lines, drafts)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        next_batch = current + 1
        try:
            with conn.begin_nested():
                order_id = conn.execute(correction_orders.insert().values(
                    request_id=request_id, case_id=case_id, batch_seq=next_batch,
                    note=note, author=actor, created_at=utc_time())).inserted_primary_key[0]
        except IntegrityError:
            # A concurrent retry of the same order got there first.
            existing = conn.execute(select(correction_orders).where(
                correction_orders.c.request_id == request_id)).mappings().first()
            if existing is None or existing['case_id'] != case_id:
                raise HTTPException(409, '提交单号已被另一张更正单使用，请重新取号') from None
            return _order_view(conn, dict(existing))
        for draft in drafts:
            conn.execute(correction_lines.insert().values(
                order_id=order_id, case_id=case_id, batch_seq=next_batch,
                formula_id=draft.formula_id, effective_from=draft.effective_from,
                effective_to=draft.effective_to, recipe=draft.recipe,
                supersedes=','.join(map(str, draft.supersedes))))
        conn.execute(correction_state.update().where(
            correction_state.c.case_id == case_id).values(
            current_batch=next_batch, updated_at=utc_time()))
        _refresh_unsigned(conn, case_id, case['payload'], next_batch)
        log(conn, actor, 'correct', order_id)
        return _order_view(conn, found(conn, correction_orders, order_id))


def _ensure_same_order(conn, order, payload):
    """Reject reusing a submission id for a different correction set.

    A retry is meant to be the same request after a broken response; reusing the
    id with changed lines would silently return the old order, so it is treated
    as a conflict instead.
    """

    stored = _order_view(conn, order)
    rows = payload.get('lines')
    if not isinstance(rows, list):
        return
    case = found(conn, cases, order['case_id'])
    try:
        _, note, drafts = corrections.parse_submission(
            {**payload, 'request_id': order['request_id']}, domain.parse_ledger(case['payload']))
    except ValueError as exc:
        raise HTTPException(409, f'提交单号已用于更正单 {order["id"]}：{exc}') from None
    incoming = sorted(
        (draft.formula_id, draft.effective_from, draft.effective_to, draft.recipe,
         draft.supersedes) for draft in drafts)
    current = sorted(
        (row['formula_id'], row['effective_from'], row['effective_to'], row['recipe'],
         tuple(sorted(row['supersedes']))) for row in stored['lines'])
    if incoming != current or note != (order['note'] or ''):
        raise HTTPException(409, '提交单号已用于另一份更正内容，请换号后重提')


def _refresh_unsigned(conn, case_id, payload, batch):
    """Bring computed-but-unsigned evidence onto the new correction batch.

    Signed evidence is left untouched: it must keep the recipe seen when it was
    signed.  Refreshing under the case lock means no run can finish halfway.
    """

    _, lines = read_corrections(conn, case_id, locking=True)
    result = domain.solve(payload, engine, (batch, lines))
    digest = hashlib.sha256(canonical(result)).hexdigest()
    rows = conn.execute(select(evidence.c.id).join(runs, runs.c.id == evidence.c.run_id).where(
        runs.c.case_id == case_id, evidence.c.reviewed_at.is_(None))).scalars().all()
    for identity in rows:
        conn.execute(evidence.update().where(evidence.c.id == identity).values(
            report=result, digest=digest, correction_batch=batch))


def _order_view(conn, order):
    rows = conn.execute(select(correction_lines).where(
        correction_lines.c.order_id == order['id']).order_by(correction_lines.c.id)).mappings()
    return {
        **order,
        'lines': [{
            'id': row['id'],
            'formula_id': row['formula_id'],
            'effective_from': row['effective_from'],
            'effective_to': row['effective_to'],
            'recipe': row['recipe'],
            'supersedes': [int(value) for value in (row['supersedes'] or '').split(',') if value],
            'batch_seq': row['batch_seq'],
        } for row in rows],
    }


def corrections_for(case_id):
    with engine.connect() as conn:
        found(conn, cases, case_id)
        batch, lines = read_corrections(conn, case_id)
        # Show exactly the batch that was read, even if a newer correction lands
        # between the two statements on a read-committed backend.
        segments = corrections.effective_segments(
            [line for line in lines if line.batch_seq <= batch])
        orders = conn.execute(select(correction_orders).where(
            correction_orders.c.case_id == case_id).order_by(correction_orders.c.id.desc())).mappings()
        return {
            'batch': batch,
            'orders': [dict(row) for row in orders],
            'coverage': corrections.coverage_rows(segments),
        }


def case_for_evidence(conn, identity):
    """Resolve the case of an evidence without fixing the transaction snapshot.

    ``run.case_id`` is immutable, so this read is safe to take before the case
    lock; making it the first statement keeps it out of a REPEATABLE READ
    snapshot that could miss a concurrent correction.
    """

    case_id = conn.execute(select(runs.c.case_id).where(runs.c.id == (
        select(evidence.c.run_id).where(evidence.c.id == identity).scalar_subquery()
    ))).scalar_one_or_none()
    if case_id is None:
        raise HTTPException(404, '找不到该记录')
    return case_id


def refresh_evidence(identity, actor):
    """Recompute an unsigned evidence from the current correction batch.

    Signed evidence is immutable: reopening it keeps the recipe that was seen
    when it was signed.  Only pre-computed, not-yet-signed evidence follows a
    later batch.
    """

    with engine.begin() as conn:
        case_id = case_for_evidence(conn, identity)
        # Take the case lock before the remaining reads so a correction that
        # commits during this transaction cannot be missed.
        batch = lock_state(conn, case_id)
        row = found_locked(conn, evidence, identity)
        if row['reviewed_at'] is not None:
            raise HTTPException(409, '已签证据不再重算，重开仍见签出时配方')
        case = found_locked(conn, cases, case_id)
        _, lines = read_corrections(conn, case_id, locking=True)
        result = domain.solve(case['payload'], engine, (batch, lines))
        digest = hashlib.sha256(canonical(result)).hexdigest()
        conn.execute(evidence.update().where(evidence.c.id == identity).values(
            report=result, digest=digest, correction_batch=batch))
        log(conn, actor, 'refresh_evidence', identity)
        return found(conn, evidence, identity)


def review(identity, note, actor):
    note = note.strip()
    if not note:
        raise HTTPException(422, '复核意见不能为空')
    with engine.begin() as conn:
        case_id = case_for_evidence(conn, identity)
        current = lock_state(conn, case_id)
        row = found_locked(conn, evidence, identity)
        if row['reviewed_at'] is not None:
            raise HTTPException(409, '该证据已经复核')
        if (row['correction_batch'] or 0) != current:
            raise HTTPException(409, '该证据基于旧更正批次，请按当前批次重算后再签署')
        conn.execute(evidence.update().where(evidence.c.id == identity).values(
            review_note=note, reviewer=actor, reviewed_at=utc_time()))
        log(conn, actor, 'review', identity)
        return found(conn, evidence, identity)
