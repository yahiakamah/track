# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class SessionController(http.Controller, SalesRepUtils):

    def _cors_response(self):
        return request.make_response('', headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization, access_token, Access-Token, X-Requested-With',
            'Access-Control-Max-Age': '86400',
        })

    def _json_response(self, data, status=200):
        return request.make_response(
            json.dumps(data),
            headers={
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*',
            },
            status=status
        )

    @http.route('/api/mobile/session/login', type='http', auth='public', methods=['POST', 'OPTIONS'], cors='*', csrf=False)
    def mobile_session_login(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return self._cors_response()
            
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return self._json_response({'success': False, 'message': 'Unauthorized'}, status=401)

            data = json.loads(request.httprequest.data or '{}')
            latitude = data.get('latitude')
            longitude = data.get('longitude')

            log = request.env['sales.representative.session.log'].sudo().create({
                'sales_rep_id': sales_rep.id,
                'action_type': 'login',
                'timestamp': fields.Datetime.now(),
                'latitude': float(latitude) if latitude is not None else 0.0,
                'longitude': float(longitude) if longitude is not None else 0.0,
                'device_model': data.get('device_model') or 'Unknown',
                'mac_address': data.get('mac_address') or 'Unknown',
                'device_identifier': data.get('device_identifier') or False,
                'session_identifier': request.session.sid,
            })

            _logger.info("Mobile login logged for sales rep %s (Device: %s, MAC: %s)", sales_rep.name, log.device_model, log.mac_address)
            return self._json_response({'success': True, 'log_id': log.id})
            
        except Exception as e:
            _logger.error("Error in mobile_session_login: %s", e)
            return self._json_response({'success': False, 'error': str(e)}, status=500)

    @http.route('/api/mobile/session/logout', type='http', auth='public', methods=['POST', 'OPTIONS'], cors='*', csrf=False)
    def mobile_session_logout(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return self._cors_response()
            
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return self._json_response({'success': False, 'message': 'Unauthorized'}, status=401)

            data = json.loads(request.httprequest.data or '{}')
            latitude = data.get('latitude')
            longitude = data.get('longitude')
            is_forced = data.get('is_forced') or data.get('forced') or False
            action_type = 'forced_logout' if is_forced else 'logout'

            log = request.env['sales.representative.session.log'].sudo().create({
                'sales_rep_id': sales_rep.id,
                'action_type': action_type,
                'timestamp': fields.Datetime.now(),
                'latitude': float(latitude) if latitude is not None else 0.0,
                'longitude': float(longitude) if longitude is not None else 0.0,
                'device_model': data.get('device_model') or 'Unknown',
                'mac_address': data.get('mac_address') or 'Unknown',
                'device_identifier': data.get('device_identifier') or False,
                'session_identifier': request.session.sid,
            })

            _logger.info("Mobile logout logged for sales rep %s (Device: %s, MAC: %s)", sales_rep.name, log.device_model, log.mac_address)
            return self._json_response({'success': True, 'log_id': log.id})
            
        except Exception as e:
            _logger.error("Error in mobile_session_logout: %s", e)
            return self._json_response({'success': False, 'error': str(e)}, status=500)
