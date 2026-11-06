# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
import random
from .utils import SalesRepUtils, parse_mobile_datetime

_logger = logging.getLogger(__name__)

class VisitController(http.Controller, SalesRepUtils):


    @http.route('/api/mobile/visits/today', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_visits_today(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            _logger.info(f"Visits Today: Request from {sales_rep.name} (user_id {user.id})")

            today = fields.Date.today()
            domain = [
                ('sales_rep_id', '=', sales_rep.id),
                ('planned_time', '>=', f"{today} 00:00:00"),
                ('planned_time', '<=', f"{today} 23:59:59")
            ]
            visits = request.env['sales.rep.visit'].with_user(user).search_read(domain, 
                ['id', 'name', 'planned_time', 'visit_time', 'state', 'visit_type', 'partner_id', 'route_id', 'sales_rep_id', 'duration', 'visit_result', 'notes'],
                order='planned_time desc')
            
            return request.make_response(json.dumps({'success': True, 'visits': visits}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_visits_today: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/visits/start', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def start_visit(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            route_customer_id = data.get('route_customer_id')
            latitude = data.get('latitude')
            longitude = data.get('longitude')
            
            if not route_customer_id:
                 return request.make_response(json.dumps({'success': False, 'message': 'Route Customer ID required'}), headers={'Content-Type': 'application/json'})

            customer = request.env['sales.route.customer'].with_user(user).browse(route_customer_id)
            if not customer.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Route Customer not found'}), headers={'Content-Type': 'application/json'})
            
            rand_suffix = str(random.randint(0, 1000000))
            
            visit_vals = {
                'name': f'VISIT-{rand_suffix}',
                'route_id': customer.route_id.id,
                'partner_id': customer.partner_id.id,
                'sales_rep_id': sales_rep.id,
                'route_customer_id': customer.id,
                'planned_time': fields.Datetime.now(),
                'visit_type': 'sales',
                'state': 'in_progress',
                'visit_location_lat': str(latitude) if latitude else False,
                'visit_location_long': str(longitude) if longitude else False,
            }
            visit = request.env['sales.rep.visit'].with_user(user).create(visit_vals)
            
            customer.write({
                'state': 'in_progress',
                'visit_start_time': fields.Datetime.now(),
                'visit_id': visit.id,
                'visit_location_lat': str(latitude) if latitude else False,
                'visit_location_long': str(longitude) if longitude else False,
            })
            
            return request.make_response(json.dumps({'success': True, 'visitId': visit.id}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in start_visit: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/visits/end', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def end_visit(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            route_customer_id = data.get('route_customer_id')
            visit_result = data.get('visit_result')
            visit_notes = data.get('visit_notes')
            visit_end_time = data.get('visit_end_time')
            follow_up_date = data.get('follow_up_date')
            
            customer = request.env['sales.route.customer'].with_user(user).browse(route_customer_id)
            if not customer.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Customer not found'}), headers={'Content-Type': 'application/json'})

            visit_id = customer.visit_id.id
            if not visit_id:
                 # Try to find recent visit? Or error.
                 return request.make_response(json.dumps({'success': False, 'message': 'No active visit found'}), headers={'Content-Type': 'application/json'})

            # Use Wizard Logic
            wizard_vals = {
                'visit_id': visit_id,
                'route_customer_id': route_customer_id,
                'visit_result': visit_result,
                'visit_notes': visit_notes,
                'follow_up_required': bool(follow_up_date),
                'next_visit_date': follow_up_date or False,
            }
            wizard = request.env['visit.result.wizard'].with_user(user).create(wizard_vals)
            wizard.action_confirm_visit()

            # Update Customer
            customer.write({
                'state': 'visited',
                'visit_end_time': parse_mobile_datetime(visit_end_time, user) or fields.Datetime.now(),
                'visit_id': visit_id,
            })
            
            return request.make_response(json.dumps({'success': True}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in end_visit: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
