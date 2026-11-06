# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils
from ..models.sse_dispatch import schedule_reps

_logger = logging.getLogger(__name__)

class PaymentController(http.Controller, SalesRepUtils):

    # GET /api/mobile/payments/today and /api/mobile/payment_terms are served by
    # account.py and product.py (token authenticated); duplicates removed here.

    @http.route('/api/mobile/payments', type='http', auth='user', methods=['POST'], cors='*', csrf=False)
    def create_payment(self, **kwargs):
        try:
            data = json.loads(request.httprequest.data)
            partner_id = data.get('partnerId') or data.get('partner_id')
            journal_id = data.get('journalId') or data.get('journal_id')
            amount = data.get('amount')
            memo = data.get('memo')
            route_id = data.get('routeId') or data.get('route_id')
            visit_id = data.get('visitId') or data.get('visit_id')
            route_customer_id = data.get('routeCustomerId') or data.get('route_customer_id')

            if not all([partner_id, journal_id, amount]):
                 return request.make_response(json.dumps({'success': False, 'message': 'Missing required fields'}), headers={'Content-Type': 'application/json'})

            # 1. Create Payment
            payment_vals = {
                'payment_type': 'inbound',
                'partner_type': 'customer',
                'partner_id': partner_id,
                'amount': float(amount),
                'journal_id': int(journal_id),
                'date': fields.Date.today(),
                'memo': memo,
            }
            # Link additional fields if model supports it
            if route_id: 
                payment_vals['route_id'] = int(route_id)
            
            payment = request.env['account.payment'].create(payment_vals)
            payment.action_post()
            
            # 2. Create Collection Record (sales.rep.collection)
            # try:
            #     collection_vals = {
            #         'name': memo or f"Collection for Visit {visit_id}",
            #         'visit_id': visit_id,
            #         'collection_date': fields.Date.today(),
            #         'amount': float(amount),
            #         'payment_method': 'cash', # Default, or fetch journal type
            #         'state': 'confirmed'
            #     }
            #     request.env['sales.rep.collection'].create(collection_vals)
            # except Exception as ce:
            #     _logger.warning(f"Failed to create collection record: {ce}")

            # 3. Complete Visit
            if visit_id:
                visit = request.env['sales.rep.visit'].browse(visit_id)
                visit.write({
                    'state': 'completed',
                    'visit_result': 'successful',
                    'total_collected': float(amount),
                    'visit_time': fields.Datetime.now()
                })
            
            # 4. Update Route Customer
            if route_customer_id:
                 request.env['sales.route.customer'].browse(route_customer_id).write({
                    'state': 'visited',
                    'visit_end_time': fields.Datetime.now(),
                    'visit_result': 'successful'
                 })

            # 5. Notify SSE for real-time sync
            sales_rep = self._get_sales_rep(request.uid)
            if sales_rep:
                schedule_reps(request.env, [sales_rep.id], 'payment_created')

            return request.make_response(json.dumps({'success': True, 'paymentId': payment.id}, default=str), headers={'Content-Type': 'application/json'})

        except Exception as e:
             _logger.error(f"Error in create_payment: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
