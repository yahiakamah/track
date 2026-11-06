# -*- coding: utf-8 -*-
"""Rep operations shared by the online endpoints and the offline queue.

The same function runs whether the app calls the endpoint directly (online)
or the operation was queued on the phone and arrives later as a pending
action inside /api/mobile/sync. Every function is safe to replay: the queue
may resend an action whose acknowledgement was lost, so a second run of an
operation that already happened returns the current state instead of
failing or doing the work twice.

Functions raise UserError / ValidationError for business refusals.
"""
from datetime import timedelta

from odoo import fields, _
from odoo.exceptions import UserError


def _strip_data_url(value):
    if value and isinstance(value, str) and value.startswith('data:'):
        return value.split(',', 1)[1]
    return value


# ---------------------------------------------------------------------- tasks
def task_payload(t):
    return {
        'id': t.id,
        'name': t.name,
        'task_type': t.task_type,
        'state': t.state,
        'priority': t.priority,
        'date_deadline': t.date_deadline,
        'is_overdue': t.is_overdue,
        'partner_id': t.partner_id.id,
        'partner_name': t.partner_id.name or '',
        'route_id': t.route_id.id,
        'description': t.description or '',
        'proof_type': t.proof_type,
        'result_note': t.result_note or '',
        'amount_due': t.amount_due,
        'currency_symbol': t.currency_id.symbol,
    }


def rep_tasks(env, rep):
    today = fields.Date.context_today(rep.user_id)
    Task = env['sales.rep.task'].sudo()
    open_tasks = Task.search([('sales_rep_id', '=', rep.id), ('state', 'in', ('new', 'in_progress')),
                              ('date_deadline', '<=', today + timedelta(days=7))])
    closed = Task.search([('sales_rep_id', '=', rep.id), ('state', 'in', ('done', 'failed')),
                          ('done_date', '>=', fields.Datetime.to_string(fields.Datetime.now() - timedelta(hours=24)))])
    return open_tasks | closed


def task_update(env, rep, task_id, data):
    task = env['sales.rep.task'].sudo().search([('id', '=', int(task_id or 0)), ('sales_rep_id', '=', rep.id)], limit=1)
    if not task:
        raise UserError(_('Task not found'))
    action = data.get('action')
    vals = {k: data[k] for k in ('result_note', 'latitude', 'longitude') if data.get(k) not in (None, '')}
    if data.get('photo'):
        vals['proof_image'] = _strip_data_url(data['photo'])
    if action == 'start':
        task.action_start()  # no-op unless the task is new
    elif action in ('done', 'fail'):
        final = 'done' if action == 'done' else 'failed'
        if task.state in ('done', 'failed'):
            # replay, or the office closed it meanwhile: keep the existing outcome
            return dict(task_payload(task), replayed=True)
        task.action_done(vals) if action == 'done' else task.action_fail(vals)
        assert task.state == final
    else:
        raise UserError(_('Unknown action'))
    return task_payload(task)


# ---------------------------------------------------------- department requests
def request_payload(r):
    return {
        'id': r.id,
        'local_id': r.mobile_local_id or '',
        'name': r.name,
        'type_id': r.type_id.id,
        'type_name': r.type_id.name,
        'type_code': r.type_code,
        'icon': r.type_id.icon or 'document-text-outline',
        'state': r.state,
        'description': r.description,
        'amount': r.amount,
        'currency_symbol': r.currency_id.symbol,
        'date_from': r.date_from,
        'date_to': r.date_to,
        'partner_name': r.partner_id.name or '',
        'vehicle_name': r.vehicle_id.display_name or '',
        'response': r.response or '',
        'create_date': r.create_date,
        'decided_date': r.decided_date,
    }


def _find_request(env, rep, req_id=None, local_id=None):
    Req = env['sales.rep.dept.request'].sudo()
    if req_id and str(req_id).isdigit():
        found = Req.search([('id', '=', int(req_id)), ('sales_rep_id', '=', rep.id)], limit=1)
        if found:
            return found
    # a request created offline is known to the phone by its local id only
    ref = local_id or req_id
    if ref:
        return Req.search([('mobile_local_id', '=', str(ref)), ('sales_rep_id', '=', rep.id)], limit=1)
    return Req


def dept_request_create(env, rep, data):
    Req = env['sales.rep.dept.request'].sudo()
    local_id = data.get('local_id') or data.get('request_local_id')
    if local_id:
        existing = _find_request(env, rep, local_id=local_id)
        if existing:
            return request_payload(existing)
    rtype = env['sales.rep.dept.request.type'].sudo().browse(int(data.get('type_id') or 0)).exists()
    if not rtype:
        raise UserError(_('Choose a request type'))
    vals = {
        'type_id': rtype.id,
        'sales_rep_id': rep.id,
        'description': (data.get('description') or '').strip(),
        'amount': float(data.get('amount') or 0.0),
        'date_from': data.get('date_from') or False,
        'date_to': data.get('date_to') or False,
        'mobile_local_id': local_id or False,
    }
    if not vals['description']:
        raise UserError(_('Describe your request'))
    if data.get('partner_id'):
        vals['partner_id'] = int(data['partner_id'])
    if data.get('attachment'):
        vals['attachment'] = _strip_data_url(data['attachment'])
    req = Req.with_user(rep.user_id).sudo().create(vals)
    return request_payload(req)


def dept_request_op(env, rep, req_id, op, data, local_id=None):
    req = _find_request(env, rep, req_id, local_id)
    if not req:
        raise UserError(_('Request not found'))
    if op == 'reply':
        if req.state != 'info':
            # already answered (replay) or decided meanwhile
            return dict(request_payload(req), replayed=True)
        req.rep_reply((data.get('text') or '').strip() or '-', _strip_data_url(data.get('attachment')))
    elif op == 'cancel':
        req.action_cancel()  # no-op unless submitted / waiting for info
    else:
        raise UserError(_('Unknown operation'))
    return request_payload(req)


# ---------------------------------------------------------------- van loads
def find_pending_load(env, rep, picking_id, include_done=False):
    Picking = env['stock.picking'].sudo()
    states = ('pending', 'accepted', 'disputed') if include_done else ('pending',)
    domain = [('sales_rep_id', '=', rep.id), ('load_acceptance_state', 'in', states)]
    picking = Picking.search([('id', '=', picking_id)] + domain, limit=1)
    if not picking:  # the app may send the warehouse transfer it knows about
        picking = Picking.search([('load_source_picking_id', '=', picking_id)] + domain, limit=1)
    return picking


def load_accept(env, rep, picking_id, data, replay_safe=False):
    picking = find_pending_load(env, rep, int(picking_id or 0), include_done=replay_safe)
    if not picking:
        raise UserError(_('No pending load found'))
    if picking.load_acceptance_state != 'pending':
        return {'state': picking.load_acceptance_state, 'replayed': True,
                'variance_pickings': picking.load_variance_picking_ids.mapped('name')}
    picking.with_user(rep.user_id).sudo().action_rep_accept_load(data.get('lines') or [], data.get('note'))
    return {'state': picking.load_acceptance_state,
            'variance_pickings': picking.load_variance_picking_ids.mapped('name')}


# ---------------------------------------------------------------- working day
def workday_op(env, rep, op, data, user=None):
    """Start / end of the rep's working day (online or queued, replay-safe)."""
    from .utils import parse_mobile_datetime
    Workday = env['sales.rep.workday'].sudo()
    when = parse_mobile_datetime(data.get('time'), user or rep.user_id) or fields.Datetime.now()
    day = fields.Date.context_today(rep.user_id, when) if rep.user_id else when.date()
    if op == 'status':
        wd = Workday.search([('sales_rep_id', '=', rep.id), ('date', '=', day)], limit=1)
        return wd.payload() if wd else {'date': day, 'started': False, 'ended': False, 'start_time': False,
                                         'end_time': False, 'duration_hours': 0.0}
    wd = Workday.get_or_create(rep, day)
    lat, lng = float(data.get('latitude') or 0.0), float(data.get('longitude') or 0.0)
    if op == 'start':
        wd.action_start(when, lat, lng)
    elif op == 'end':
        wd.action_end(when, lat, lng)
    else:
        raise UserError(_('Unknown operation'))
    return wd.payload()
