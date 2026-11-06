# -*- coding: utf-8 -*-
"""Mobile endpoints for the van custody chain: load acceptance and day close.

Load acceptance can also arrive through the offline queue (pending action
'load_accept'); the day close stays online because the rep must see the
server's figures before signing."""
import json
import logging

from odoo import fields, http, _
from odoo.exceptions import UserError
from odoo.http import request

from .utils import SalesRepUtils
from . import mobile_ops

_logger = logging.getLogger(__name__)


class CustodyController(http.Controller, SalesRepUtils):

    def _json(self, data, status=200):
        return request.make_response(json.dumps(data, default=str),
                                     headers={'Content-Type': 'application/json'}, status=status)

    def _body(self):
        try:
            return json.loads(request.httprequest.data or '{}')
        except ValueError:
            return {}

    def _auth(self):
        sales_rep, _user = self._authenticate_request()
        return sales_rep

    @staticmethod
    def _load_payload(picking):
        return {
            'id': picking.id,
            'name': picking.name,
            'source_picking_id': picking.load_source_picking_id.id,
            'source_name': picking.load_source_picking_id.name,
            'state': picking.load_acceptance_state,
            'date': picking.scheduled_date,
            'lines': [{
                'product_id': m.product_id.id,
                'product_name': m.product_id.display_name,
                'product_code': m.product_id.default_code or '',
                'qty': m.product_uom_qty,
                'uom_name': m.product_uom.name,
            } for m in picking.move_ids if m.state != 'cancel'],
        }

    @staticmethod
    def _day_close_payload(close):
        return {
            'id': close.id,
            'name': close.name,
            'date': close.date,
            'state': close.state,
            'van_location': close.van_location_id.display_name,
            'currency_symbol': close.currency_id.symbol,
            'system_cash': close.system_cash,
            'counted_cash': close.counted_cash,
            'cash_difference': close.cash_difference,
            'collections_count': len(close.payment_ids),
            'lines': [{
                'product_id': l.product_id.id,
                'product_name': l.product_id.display_name,
                'product_code': l.product_id.default_code or '',
                'uom_name': l.product_uom_id.name,
                'system_qty': l.system_qty,
                'counted_qty': l.counted_qty,
                'counted_set': l.counted_set,
                'difference_qty': l.difference_qty,
            } for l in close.line_ids],
            'note': close.note or '',
        }

    # ------------------------------------------------------------------
    @http.route('/api/mobile/custody/status', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def custody_status(self, **kw):
        rep = self._auth()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        rep = rep.sudo()
        pending_loads = request.env['stock.picking'].sudo().search_count([
            ('sales_rep_id', '=', rep.id), ('load_acceptance_state', '=', 'pending'),
            ('state', 'not in', ('done', 'cancel'))])
        return self._json({
            'success': True,
            'require_day_close': rep.require_day_close,
            'pending_day_close_date': rep.pending_day_close_date,
            'pending_loads': pending_loads,
            'load_acceptance_enabled': bool(rep.transit_location_id),
            'workday': mobile_ops.workday_op(request.env, rep, 'status', {}),
            'open_tasks_today': request.env['sales.rep.task'].sudo().search_count([
                ('sales_rep_id', '=', rep.id), ('state', 'in', ('new', 'in_progress')),
                ('date_deadline', '<=', fields.Date.context_today(rep.user_id))]),
        })

    @http.route('/api/mobile/stock/loads/pending', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def pending_loads(self, **kw):
        rep = self._auth()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        pickings = request.env['stock.picking'].sudo().search([
            ('sales_rep_id', '=', rep.id), ('load_acceptance_state', '=', 'pending'),
            ('state', 'not in', ('done', 'cancel'))], order='scheduled_date')
        return self._json({'success': True, 'loads': [self._load_payload(p) for p in pickings]})

    @http.route('/api/mobile/stock/loads/<int:picking_id>/accept', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def accept_load(self, picking_id, **kw):
        rep = self._auth()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        try:
            with request.env.cr.savepoint():
                result = mobile_ops.load_accept(request.env, rep, picking_id, self._body())
        except UserError as e:
            return self._json({'success': False, 'message': str(e)})
        except Exception as e:
            _logger.exception("Load acceptance failed for %s", picking_id)
            return self._json({'success': False, 'message': str(e)})
        return self._json(dict(result, success=True))

    # ------------------------------------------------------------------
    @http.route('/api/mobile/day_close/current', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def day_close_current(self, **kw):
        rep = self._auth()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        rep = rep.sudo()
        day = kw.get('date') or rep.pending_day_close_date or fields.Date.context_today(rep.user_id)
        Close = request.env['sales.rep.day.close'].sudo()
        close = Close.search([('sales_rep_id', '=', rep.id), ('date', '=', day)], limit=1)
        try:
            if not close:
                close = Close.create({'sales_rep_id': rep.id, 'date': day})
            if close.state == 'draft':
                close.action_compute()
        except UserError as e:
            return self._json({'success': False, 'message': str(e)})
        return self._json({'success': True, 'day_close': self._day_close_payload(close)})

    @http.route('/api/mobile/day_close/submit', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def day_close_submit(self, **kw):
        rep = self._auth()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        data = self._body()
        close = request.env['sales.rep.day.close'].sudo().search([
            ('id', '=', int(data.get('day_close_id') or 0)), ('sales_rep_id', '=', rep.id)], limit=1)
        if not close:
            return self._json({'success': False, 'message': _('Day close not found')})
        try:
            with request.env.cr.savepoint():
                close.set_counts(data.get('counts') or [], data.get('counted_cash'), data.get('note'))
                close.action_submit()
        except UserError as e:
            return self._json({'success': False, 'message': str(e)})
        return self._json({'success': True, 'day_close': self._day_close_payload(close)})

    # ------------------------------------------------------------------
    @http.route('/api/mobile/workday/<string:op>', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def workday(self, op, **kw):
        rep, user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        if op not in ('start', 'end', 'status'):
            return self._json({'success': False, 'message': _('Unknown operation')})
        try:
            with request.env.cr.savepoint():
                day = mobile_ops.workday_op(request.env, rep.sudo(), op, self._body(), user)
        except UserError as e:
            return self._json({'success': False, 'message': str(e)})
        return self._json({'success': True, 'workday': day})
