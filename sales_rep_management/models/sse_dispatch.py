# -*- coding: utf-8 -*-
"""
Coalesced, targeted SSE dispatch.

Business writes no longer push a full re-sync to every connected device.
Instead, each transaction collects *who* is affected and *why*, and a single
post-commit callback pushes at most one event per affected representative
(or one broadcast for master-data changes such as products and pricelists).

Events are published through PostgreSQL NOTIFY (see controllers/sse.py), so
a change committed by any Odoo process (HTTP worker, cron worker, gevent
server, odoo shell) reaches the SSE streams held by any other process.

Usage from models:

    from .sse_dispatch import schedule_all, schedule_reps, reps_for_partners
    schedule_reps(self.env, reps_for_partners(self.env, self.partner_id.ids), 'partner_updated')
"""
import logging
from datetime import timedelta

from odoo import fields

_logger = logging.getLogger(__name__)

_DATA_KEY = 'sales_rep_management.sse'


def _bucket(env):
    data = env.cr.postcommit.data
    bucket = data.get(_DATA_KEY)
    if bucket is None:
        # {'all': {event_type: reasons}, 'reps': {rep_id: {event_type: reasons}}}
        bucket = {'all': {}, 'reps': {}, 'skip': set()}
        data[_DATA_KEY] = bucket
        dbname = env.cr.dbname
        env.cr.postcommit.add(lambda: _flush(dbname, bucket))
    skip_id = env.context.get('skip_notify_sales_rep_id')
    if skip_id:
        bucket['skip'].add(skip_id)
    return bucket


def _flush(dbname, bucket):
    """Turn the transaction bucket into events and publish them once."""
    try:
        from odoo.addons.sales_rep_management.controllers.sse import publish
    except ImportError:
        return
    events = []
    broadcast = bucket['all']
    for event_type, reasons in broadcast.items():
        events.append({'rep': None, 'type': event_type, 'reason': ','.join(sorted(reasons))})
    for rep_id, by_type in bucket['reps'].items():
        for event_type, reasons in by_type.items():
            # a sync broadcast already covers every rep's sync_required
            if event_type in broadcast:
                continue
            events.append({'rep': rep_id, 'type': event_type, 'reason': ','.join(sorted(reasons))})
    if events:
        publish(dbname, events, skip=bucket['skip'])


def schedule_users(env, user_ids, reason, event_type='sync_required'):
    """Notify Odoo users: their manager-app stream and, when the user is a
    representative, his representative stream too (chat messages...)."""
    user_ids = [u for u in (user_ids or []) if u]
    if not user_ids:
        return
    try:
        reps = _bucket(env)['reps']
        rep_of = {r['user_id'][0]: r['id'] for r in env['sales.representative'].sudo().search_read(
            [('user_id', 'in', user_ids)], ['user_id'])}
        for uid in user_ids:
            for key in filter(None, (f'u{uid}', rep_of.get(uid))):
                reps.setdefault(key, {}).setdefault(event_type, set()).add(reason)
    except Exception as e:
        _logger.warning("SSE: failed to schedule user notification (%s): %s", reason, e)


def schedule_all(env, reason, event_type='sync_required'):
    """Broadcast once per transaction (master data: products, pricelists...)."""
    try:
        _bucket(env)['all'].setdefault(event_type, set()).add(reason)
    except Exception as e:  # never break a business write because of SSE
        _logger.warning("SSE: failed to schedule broadcast (%s): %s", reason, e)


def schedule_reps(env, rep_ids, reason, event_type='sync_required'):
    """Notify only the given representatives, once per transaction."""
    rep_ids = [r for r in (rep_ids or []) if r]
    if not rep_ids:
        return
    try:
        reps = _bucket(env)['reps']
        for rep_id in rep_ids:
            reps.setdefault(rep_id, {}).setdefault(event_type, set()).add(reason)
    except Exception as e:
        _logger.warning("SSE: failed to schedule rep notification (%s): %s", reason, e)


# ---------------------------------------------------------------------------
# Affected-representative resolvers
# ---------------------------------------------------------------------------

def reps_for_partners(env, partner_ids, days=45):
    """Reps that serve these customers: on a recent/active route or creator."""
    partner_ids = [p for p in (partner_ids or []) if p]
    if not partner_ids:
        return []
    since = fields.Date.context_today(env.user) - timedelta(days=days)
    rc = env['sales.route.customer'].sudo().search_read(
        [('partner_id', 'in', partner_ids),
         ('route_id.state', '!=', 'cancelled'),
         '|', ('route_id.date', '>=', since), ('route_id.is_recurrent', '=', True)],
        ['sales_rep_id'])
    rep_ids = {r['sales_rep_id'][0] for r in rc if r['sales_rep_id']}
    creators = env['res.partner'].sudo().browse(partner_ids).mapped('created_sales_rep_id')
    rep_ids |= set(creators.ids)
    return list(rep_ids)


def reps_for_locations(env, location_ids):
    """Reps whose van/default locations contain (or are) these locations."""
    location_ids = [loc for loc in (location_ids or []) if loc]
    if not location_ids:
        return []
    return env['sales.representative'].sudo().search(
        [('default_location_ids', 'parent_of', location_ids)]).ids


def reps_for_journals(env, journal_ids):
    """Reps whose mobile payment methods post into these journals."""
    journal_ids = [j for j in (journal_ids or []) if j]
    if not journal_ids:
        return []
    return env['sales.representative'].sudo().search(
        [('payment_method_ids.journal_id', 'in', journal_ids)]).ids
