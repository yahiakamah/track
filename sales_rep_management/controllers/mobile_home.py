# -*- coding: utf-8 -*-
"""Mobile endpoints: home grid configuration and department requests."""
import json
import logging

from odoo import fields, http, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from .utils import SalesRepUtils
from . import mobile_ops

_logger = logging.getLogger(__name__)


class MobileHomeController(http.Controller, SalesRepUtils):

    def _json(self, data, status=200):
        return request.make_response(json.dumps(data, default=str),
                                     headers={'Content-Type': 'application/json'}, status=status)

    def _body(self):
        try:
            return json.loads(request.httprequest.data or '{}')
        except ValueError:
            return {}

    @http.route('/api/mobile/home/config', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def home_config(self, **kw):
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        return self._json({'success': True, 'tiles': rep.sudo()._get_home_tiles()})

    # ------------------------------------------------------------------
    _request_payload = staticmethod(mobile_ops.request_payload)

    @http.route('/api/mobile/dept_requests', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def dept_requests(self, **kw):
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        env = request.env
        types = env['sales.rep.dept.request.type'].sudo().search([])
        reqs = env['sales.rep.dept.request'].sudo().search([('sales_rep_id', '=', rep.id)], limit=100)
        tasks = env['sales.rep.task'].sudo().search([('sales_rep_id', '=', rep.id), ('state', 'in', ('new', 'in_progress'))])
        return self._json({
            'success': True,
            'types': [{
                'id': t.id, 'name': t.name, 'code': t.code, 'category': t.category, 'icon': t.icon or 'document-text-outline',
                'needs_amount': t.needs_amount, 'needs_dates': t.needs_dates,
                'needs_vehicle': t.needs_vehicle, 'needs_customer': t.needs_customer,
            } for t in types],
            'requests': [self._request_payload(r) for r in reqs],
            # Inbox: what is waiting for the rep (requests needing info + open work orders)
            'inbox': {
                'requests': [self._request_payload(r) for r in reqs if r.state == 'info'],
                'open_tasks': len(tasks),
            },
        })

    @http.route('/api/mobile/dept_requests/create', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def dept_request_create(self, **kw):
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        try:
            with request.env.cr.savepoint():
                req = mobile_ops.dept_request_create(request.env, rep, self._body())
        except (UserError, ValidationError, ValueError) as e:
            return self._json({'success': False, 'message': str(e)})
        return self._json({'success': True, 'request': req})

    @http.route('/api/mobile/dept_requests/<int:req_id>/<string:op>', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def dept_request_op(self, req_id, op, **kw):
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        try:
            with request.env.cr.savepoint():
                req = mobile_ops.dept_request_op(request.env, rep, req_id, op, self._body())
        except (UserError, ValidationError) as e:
            return self._json({'success': False, 'message': str(e)})
        return self._json({'success': True, 'request': req})
