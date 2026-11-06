# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)


class LocationController(http.Controller, SalesRepUtils):

    @http.route('/api/mobile/location/security_alert', type='http', auth='public', methods=['POST', 'OPTIONS'], cors='*', csrf=False)
    def report_security_alert(self, **kwargs):
        """Record that the mobile app detected a security alert."""
        if request.httprequest.method == 'OPTIONS':
            return request.make_response('', headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization, access_token, Access-Token, X-Requested-With',
                'Access-Control-Max-Age': '86400',
            })
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(
                    json.dumps({'success': False, 'message': 'Unauthorized'}),
                    headers={
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*',
                    },
                    status=401,
                )

            data = json.loads(request.httprequest.data or '{}')
            latitude = data.get('latitude')
            longitude = data.get('longitude')

            alert = request.env['sales.rep.security.alert'].sudo().create({
                'sales_rep_id': sales_rep.id,
                'alert_type': data.get('alert_type') or 'unknown',
                'latitude': float(latitude) if latitude is not None else 0.0,
                'longitude': float(longitude) if longitude is not None else 0.0,
                'accuracy': float(data.get('accuracy') or 0.0),
                'device_identifier': data.get('device_identifier') or False,
                'detected_at': fields.Datetime.now(),
            })

            _logger.warning(
                "Mock location detected for sales rep %s (ID %s) at %s, %s",
                sales_rep.name, sales_rep.id, latitude, longitude,
            )

            return request.make_response(
                json.dumps({'success': True, 'alert_id': alert.id}),
                headers={
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                },
            )
        except Exception as e:
            _logger.error("Error in report_mock_location: %s", e)
            return request.make_response(
                json.dumps({'success': False, 'error': str(e)}),
                headers={
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*',
                },
            )
