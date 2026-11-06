# -*- coding: utf-8 -*-
from odoo import http, fields, _
from odoo.exceptions import UserError
from odoo.http import request
import json
import logging
import base64
import traceback
from datetime import datetime, timedelta
from psycopg2 import IntegrityError, OperationalError
from .utils import SalesRepUtils, parse_mobile_datetime
from .sync_actions import SyncActionsMixin
from .sync_orders import SyncOrdersMixin
from .sync_download import SyncDownloadMixin

_logger = logging.getLogger(__name__)

SYNC_LOCK_NAMESPACE = 731004  # advisory lock key space of the mobile sync

class SyncController(SyncActionsMixin, SyncOrdersMixin, SyncDownloadMixin, http.Controller, SalesRepUtils):
    """Mobile sync entry point + uploads. Actions, order lifecycle and
    downloads live in the sync_* mixins next to this file."""

    def _make_cors_response(self, data, status=200):
        return request.make_response(json.dumps(data, default=str), headers={
            'Content-Type': 'application/json',
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type, Authorization, access_token, Access-Token, X-Requested-With',
        }, status=status)

    @http.route('/api/mobile/sync', type='http', auth='public', methods=['POST', 'OPTIONS'], csrf=False)
    def sync_data(self, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return request.make_response('', headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type, Authorization, access_token, Access-Token, X-Requested-With',
                'Access-Control-Max-Age': '86400',
            })
        try:
            # 0. Auth Check
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                message, code = self.auth_error_message()
                return self._make_cors_response({'success': False, 'message': message, 'code': code}, status=401)

            # Use a context to skip self-notifications during processing
            context = dict(request.env.context or {}, skip_notify_sales_rep_id=sales_rep.id)
            request.env = request.env(context=context)

            # 0b. One sync at a time per representative. A retry sent while the
            # previous request is still running (slow network, double tap,
            # background + foreground sync) waits here, then finds everything
            # the first one committed and acknowledges it instead of creating
            # duplicates. Held until this transaction ends.
            try:
                with request.env.cr.savepoint():
                    request.env.cr.execute("SET LOCAL lock_timeout = '45s'")
                    request.env.cr.execute("SELECT pg_advisory_xact_lock(%s, %s)", (SYNC_LOCK_NAMESPACE, sales_rep.id))
                    request.env.cr.execute("SET LOCAL lock_timeout TO DEFAULT")
            except OperationalError:
                return self._make_cors_response({'success': False, 'busy': True,
                                                 'message': 'A previous sync of this device is still running. Retrying shortly.'}, status=409)

            # 1. Parse Input
            data = json.loads(request.httprequest.data)
            last_sync_date = data.get('last_sync_date')
            upload_data = data.get('upload_data', {})
            
            _logger.info(f"Sync: Request from {sales_rep.name} (ID: {sales_rep.id}). Last Sync: {last_sync_date}")
            
            response_data = {
                'success': True,
                'server_time': fields.Datetime.now(),
                'upload_ack': {},
                'downloads': {}
            }

            # 2. Pre-process: Create customers BEFORE uploading visits and orders
            # This ensures offline-created partners and route customers exist in Odoo before visits/orders referencing them are created.
            pending_actions = upload_data.get('pending_actions', []) if upload_data else []
            partner_id_map = {}  # Maps partner_local_id -> odoo partner_id
            route_customer_id_map = {}  # Maps temp_route_customer_id -> odoo route_customer_id
            
            # Extract and process create_customer actions first (Step 1: Created Customers)
            create_customer_actions = [a for a in pending_actions if a.get('action_type') == 'create_customer']
            remaining_actions = [a for a in pending_actions if a.get('action_type') != 'create_customer']
            
            pre_action_ack = {}
            if create_customer_actions:
                _logger.info(f"Sync (Step 1): Pre-processing {len(create_customer_actions)} create_customer actions before visit and order uploads")
                pre_action_ack = self._process_pending_actions(create_customer_actions, user, sales_rep, {})
                
                # Build partner_local_id -> odoo_id map and temp_route_customer_id -> odoo_id map
                for action_data in create_customer_actions:
                    local_id = action_data.get('local_id', '')
                    ack_entry = pre_action_ack.get(local_id, {})
                    if ack_entry.get('success'):
                        payload = action_data.get('payload', {})
                        customer_local_id = payload.get('customer_data', {}).get('local_id')
                        temp_rc_id = payload.get('temp_route_customer_id')

                        if customer_local_id and ack_entry.get('result', {}).get('partner_odoo_id'):
                            partner_id_map[customer_local_id] = ack_entry['result']['partner_odoo_id']
                            _logger.info(f"Sync: Mapped partner_local_id {customer_local_id} -> Odoo ID {ack_entry['result']['partner_odoo_id']}")

                        if temp_rc_id and ack_entry.get('result', {}).get('route_customer_odoo_id'):
                            route_customer_id_map[str(temp_rc_id)] = ack_entry['result']['route_customer_odoo_id']
                            try:
                                route_customer_id_map[int(temp_rc_id)] = ack_entry['result']['route_customer_odoo_id']
                            except Exception:
                                pass
                            _logger.info(f"Sync: Mapped temp_route_customer_id {temp_rc_id} -> Odoo RouteCustomer ID {ack_entry['result']['route_customer_odoo_id']}")

            # 3. Process Uploads (Upstream) - Sequence: Visits -> Sales Orders -> Payments (Collections) -> Adjustments / Requests
            if upload_data:
                response_data['upload_ack'] = self._process_uploads(sales_rep, upload_data, user, partner_id_map, route_customer_id_map)

            # 4. Process Remaining Pending Actions (everything except create_customer)
            if remaining_actions:
                remaining_ack = self._process_pending_actions(remaining_actions, user, sales_rep, response_data.get('upload_ack'), partner_id_map, route_customer_id_map)
                # Merge all action acks
                pre_action_ack.update(remaining_ack)
            
            if pre_action_ack:
                response_data['action_ack'] = pre_action_ack
                # IMPORTANT: Invalidate cache after actions that modify records to ensure 
                # downloads phase (like _process_downloads) fetches freshly recomputed fields.
                request.env.invalidate_all()

            # 5. Other devices are notified by the models' post-commit SSE
            #    dispatch (targeted; this rep is skipped via the context).

            # 6. Process Downloads (Downstream) - As specific user
            response_data['downloads'] = self._process_downloads(
                sales_rep, last_sync_date, user, capabilities=data.get('capabilities') or [])

            return self._make_cors_response(response_data)

        except Exception as e:
            error_msg = str(e)
            stack_trace = traceback.format_exc()
            _logger.error(f"Error in sync_data: {error_msg}\n{stack_trace}")
            # The stack trace stays in the server log; never send it to the client.
            return self._make_cors_response({
                'success': False,
                'error': error_msg,
            }, status=500)

    @http.route('/api/mobile/report/sales', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def get_sales_report(self, **kw):
        """
        Get sales report data for the mobile application.
        """
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            if not sales_rep.access_sales_report:
                return request.make_response(json.dumps({'success': False, 'message': 'Access Denied: Sales Report'}), headers={'Content-Type': 'application/json'}, status=403)

            data = json.loads(request.httprequest.data)
            params = data.get('params', {})
            date_from = params.get('date_from')
            date_to = params.get('date_to')
            
            if not date_from or not date_to:
                return request.make_response(json.dumps({'success': False, 'message': 'Missing date parameters'}), headers={'Content-Type': 'application/json'}, status=400)

            date_from_dt = fields.Datetime.to_datetime(date_from)
            date_to_dt = fields.Datetime.to_datetime(date_to).replace(hour=23, minute=59, second=59)

            domain = [
                ('date_order', '>=', date_from_dt),
                ('date_order', '<=', date_to_dt),
                ('state', 'in', ['sale', 'done']),
                ('sales_rep_id', '=', sales_rep.id)
            ]
            
            orders = request.env['sale.order'].with_user(user).search(domain, order='date_order desc')
            
            report_data = []
            total_before_all = 0.0
            total_discount_all = 0.0
            total_after_all = 0.0

            for order in orders:
                before = order.amount_total + order.discount_total
                report_data.append({
                    'id': order.id,
                    'name': order.name,
                    'date': order.date_order.strftime('%Y-%m-%d'),
                    'customer': order.partner_id.name,
                    'amount_before': before,
                    'discount': order.discount_total,
                    'amount_after': order.amount_total,
                    'currency_symbol': order.currency_id.symbol,
                })
                total_before_all += before
                total_discount_all += order.discount_total
                total_after_all += order.amount_total

            response_data = {
                'success': True,
                'orders': report_data,
                'summary': {
                    'total_before': total_before_all,
                    'total_discount': total_discount_all,
                    'total_after': total_after_all,
                    'currency_symbol': orders[0].currency_id.symbol if orders else request.env.company.currency_id.symbol,
                }
            }
            return request.make_response(json.dumps(response_data, default=str), headers={'Content-Type': 'application/json'})

        except Exception as e:
            _logger.error(f"Sync: Error in get_sales_report: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'message': str(e)}), headers={'Content-Type': 'application/json'}, status=500)

    @http.route('/api/mobile/report/debt', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def get_debt_report(self, **kw):
        """
        Get customer debt report data for the mobile application.
        """
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            if not sales_rep.access_customer_debt_report:
                return request.make_response(json.dumps({'success': False, 'message': 'Access Denied: Debt Report'}), headers={'Content-Type': 'application/json'}, status=403)

            # Find partners for the rep via routes
            route_customers = request.env['sales.route.customer'].sudo().search([('sales_rep_id', '=', sales_rep.id)])
            partner_ids = route_customers.mapped('partner_id').ids
            
            domain = [
                ('id', 'in', partner_ids),
                ('company_id', 'in', [sales_rep.company_id.id, False])
            ]
            
            # Use search() first to get records, then we can access computed fields
            # We avoid search_read with 'order' on computed fields which causes 500 error
            partner_records = request.env['res.partner'].with_user(user).search(domain)
            
            report_data = []
            total_debt = 0.0
            
            due_map = request.env['res.partner']._sales_rep_due_map(partner_records.ids, sales_rep.company_id.id)
            for p in partner_records:
                due = due_map.get(p.id, 0.0)
                if due > 0:
                    report_data.append({
                        'id': p.id,
                        'customer': p.name,
                        'amount_due': due,
                        'currency_symbol': p.currency_id.symbol if p.currency_id else request.env.company.currency_id.symbol,
                    })
                    total_debt += due

            # Sort by debt amount descending
            report_data.sort(key=lambda x: x['amount_due'], reverse=True)

            response_data = {
                'success': True,
                'data': report_data,
                'summary': {
                    'total_debt': total_debt,
                    'currency_symbol': request.env.company.currency_id.symbol,
                }
            }
            return request.make_response(json.dumps(response_data, default=str), headers={'Content-Type': 'application/json'})

        except Exception as e:
            _logger.error(f"Sync: Error in get_debt_report: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'message': str(e)}), headers={'Content-Type': 'application/json'}, status=500)

    @http.route('/api/mobile/report/collection', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def get_collection_report(self, **kw):
        """
        Get collection report data for the mobile application from account.payment.
        """
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            if not sales_rep.access_collection_report:
                return request.make_response(json.dumps({'success': False, 'message': 'Access Denied: Collection Report'}), headers={'Content-Type': 'application/json'}, status=403)

            data = json.loads(request.httprequest.data)
            params = data.get('params', {})
            date_from = params.get('date_from')
            date_to = params.get('date_to')
            
            if not date_from or not date_to:
                return request.make_response(json.dumps({'success': False, 'message': 'Missing date parameters'}), headers={'Content-Type': 'application/json'}, status=400)

            # account.payment uses Date fields
            date_from_val = fields.Date.to_date(date_from)
            date_to_val = fields.Date.to_date(date_to)

            domain = [
                ('date', '>=', date_from_val),
                ('date', '<=', date_to_val),
                ('sales_rep_id', '=', sales_rep.id),
                ('state', 'in', ['paid', 'in_process', 'draft', 'partial']),
                ('payment_type', '=', 'inbound')
            ]
            
            payments = request.env['account.payment'].with_user(user).search(domain, order='date desc, id desc')
            
            report_data = []
            total_amount = 0.0

            for pay in payments:
                report_data.append({
                    'id': pay.id,
                    'date': pay.date.strftime('%Y-%m-%d'),
                    'sales_rep': pay.sales_rep_id.name if pay.sales_rep_id else '',
                    'customer': pay.partner_id.name,
                    'payment': pay.amount,
                    'payment_method': pay.journal_id.name,
                    'currency_symbol': pay.currency_id.symbol or request.env.company.currency_id.symbol,
                })
                total_amount += pay.amount

            response_data = {
                'success': True,
                'data': report_data,
                'summary': {
                    'total_payment': total_amount,
                    'currency_symbol': request.env.company.currency_id.symbol,
                }
            }
            return request.make_response(json.dumps(response_data, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Sync: Error in get_collection_report: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'message': str(e)}), headers={'Content-Type': 'application/json'}, status=500)

    @http.route('/api/mobile/report/journal_transaction', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def get_journal_transaction_report(self, **kw):
        """
        Get journal transaction report data (Cash Journals) for the mobile application.
        """
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            data = json.loads(request.httprequest.data)
            params = data.get('params', {})
            date_from = params.get('date_from')
            date_to = params.get('date_to')
            
            if not date_from or not date_to:
                return request.make_response(json.dumps({'success': False, 'message': 'Missing date parameters'}), headers={'Content-Type': 'application/json'}, status=400)

            date_from_val = fields.Date.to_date(date_from)
            date_to_val = fields.Date.to_date(date_to)

            # Get the journals assigned to the sales rep for cash payments
            rep_cash_journals = sales_rep.payment_method_ids.filtered(lambda m: m.payment_type == 'cash').mapped('journal_id')
            
            if not rep_cash_journals:
                return request.make_response(json.dumps({'success': False, 'message': 'No cash journals assigned to this representative'}), headers={'Content-Type': 'application/json'}, status=400)

            # Domain for Cash Journal Moves for the Journals assigned to the Sales Rep
            domain = [
                ('date', '>=', date_from_val),
                ('date', '<=', date_to_val),
                ('journal_id', 'in', rep_cash_journals.ids),
                ('state', '=', 'posted')
            ]
            
            moves = request.env['account.move'].with_user(user).search(domain, order='date desc, id desc')
            
            report_data = []
            total_inbound = 0.0
            total_outbound = 0.0

            for move in moves:
                amount = move.amount_total
                is_outbound = False
                if move.move_type == 'out_receipt' or move.move_type == 'in_invoice':
                    is_outbound = True
                elif move.move_type == 'entry':
                    # Search for linked payment to check type
                    payment = request.env['account.payment'].sudo().search([('move_id', '=', move.id)], limit=1)
                    if payment and payment.payment_type == 'outbound':
                        is_outbound = True
                
                if is_outbound:
                    total_outbound += amount
                else:
                    total_inbound += amount

                report_data.append({
                    'id': move.id,
                    'name': move.name,
                    'date': move.date.strftime('%Y-%m-%d'),
                    'ref': move.ref or '',
                    'partner': move.partner_id.name or '',
                    'amount': amount,
                    'type': 'outbound' if is_outbound else 'inbound',
                    'journal': move.journal_id.name,
                    'currency_symbol': move.currency_id.symbol or request.env.company.currency_id.symbol,
                })

            _logger.info(f"Sync: Journal Report for {sales_rep.name}: Found {len(report_data)} transactions.")
            
            response_data = {
                'success': True,
                'data': report_data,
                'summary': {
                    'total_inbound': total_inbound,
                    'total_outbound': total_outbound,
                    'net_balance': total_inbound - total_outbound,
                    'currency_symbol': request.env.company.currency_id.symbol,
                }
            }
            _logger.debug(f"Sync: Journal Report Response: {json.dumps(response_data, default=str)}")
            return request.make_response(json.dumps(response_data, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Sync: Error in get_journal_transaction_report: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'message': str(e)}), headers={'Content-Type': 'application/json'}, status=500)



    def _process_uploads(self, sales_rep, upload_data, user, partner_id_map=None, route_customer_id_map=None):
        ack = {
            'visits': {},
            'orders': {},
            'collections': {},
            'inventory_adjustments': {},
            'stock_requests': {}
        }
        
        # =========================================================================
        # STEP 2: Process Visits
        # =========================================================================
        visits = upload_data.get('visits', [])
        for visit_data in visits:
            try:
                with request.env.cr.savepoint():
                    local_id = visit_data.get('local_id')
                    _logger.info(f"Processing visit upload: local_id={local_id}, data={visit_data}")
                    start_time = parse_mobile_datetime(visit_data.get('start_time'), user)
                    end_time = parse_mobile_datetime(visit_data.get('end_time'), user)

                    # Idempotency: the same visit arrives again after a retry, or
                    # first as 'in_progress' and later as 'completed'.
                    existing_visit = local_id and request.env['sales.rep.visit'].sudo().search(
                        [('mobile_local_id', '=', local_id)], limit=1)
                    if existing_visit:
                        if visit_data.get('state', 'completed') == 'completed' and existing_visit.state != 'completed':
                            existing_visit.write({
                                'state': 'completed',
                                'visit_result': visit_data.get('visit_result') or existing_visit.visit_result,
                                'notes': visit_data.get('notes') or existing_visit.notes,
                                'visit_time': end_time or fields.Datetime.now(),
                            })
                            if existing_visit.route_customer_id:
                                existing_visit.route_customer_id.write({
                                    'state': 'visited', 'visit_result': existing_visit.visit_result,
                                    'visit_end_time': end_time or fields.Datetime.now()})
                        ack['visits'][local_id] = existing_visit.id
                        continue
                    
                    # 1. Resolve route_customer_id (using route_customer_id_map from Step 1 create_customer)
                    route_customer_id = visit_data.get('route_customer_id') or visit_data.get('local_route_customer_id')
                    if route_customer_id_map:
                        if route_customer_id in route_customer_id_map:
                            route_customer_id = route_customer_id_map[route_customer_id]
                        elif str(route_customer_id) in route_customer_id_map:
                            route_customer_id = route_customer_id_map[str(route_customer_id)]

                    route_customer = False
                    if route_customer_id:
                        try:
                            route_customer = request.env['sales.route.customer'].with_user(user).browse(int(route_customer_id))
                            if not route_customer.exists():
                                route_customer = False
                        except Exception:
                            route_customer = False

                    # 2. Fallback: resolve route_customer by partner_id / partner_local_id
                    partner_id = visit_data.get('partner_id')
                    partner_local_id = visit_data.get('partner_local_id')
                    if not partner_id and partner_local_id and partner_id_map:
                        partner_id = partner_id_map.get(partner_local_id)
                    if partner_id and partner_id_map:
                        if str(partner_id) in partner_id_map:
                            partner_id = partner_id_map[str(partner_id)]
                        elif partner_id in partner_id_map:
                            partner_id = partner_id_map[partner_id]
                    if not partner_id and partner_local_id:
                        p = request.env['res.partner'].sudo().search([('mobile_local_id', '=', partner_local_id)], limit=1)
                        if p:
                            partner_id = p.id

                    if not route_customer and partner_id:
                        route_id = visit_data.get('route_id') or self._get_current_route_id(sales_rep)
                        domain = [('partner_id', '=', int(partner_id))]
                        if route_id:
                            domain.append(('route_id', '=', int(route_id)))
                        rc = request.env['sales.route.customer'].sudo().search(domain, limit=1)
                        if rc:
                            route_customer = rc
                        elif route_id:
                            # Automatically link partner to route
                            route_customer = request.env['sales.route.customer'].sudo().create({
                                'route_id': int(route_id),
                                'partner_id': int(partner_id),
                                'sequence': 999,
                            })
                            _logger.info(f"Sync: Auto-created route_customer {route_customer.id} for partner {partner_id} and route {route_id}")

                    if not route_customer or not route_customer.exists():
                        _logger.warning(f"Sync Upload: Route customer {visit_data.get('route_customer_id')} (partner {partner_id}) not found. Skipping visit {local_id}.")
                        continue

                    # Use sequence for name if available
                    visit_name = request.env['ir.sequence'].with_user(user).next_by_code('sales.rep.visit')
                    if not visit_name:
                        visit_name = f"VISIT-{local_id[-6:]}" if local_id else 'New Visit'

                    # Correct mapping logic for mobile visits
                    mobile_state = visit_data.get('state', 'completed')

                    vals = {
                        'name': visit_name,
                        'sales_rep_id': sales_rep.id,
                        'route_id': route_customer.route_id.id,
                        'route_customer_id': route_customer.id,
                        'partner_id': route_customer.partner_id.id,
                        'visit_type': visit_data.get('visit_type', 'sales'),
                        'state': mobile_state,
                        'visit_result': visit_data.get('visit_result'),
                        'notes': visit_data.get('notes'),
                        'visit_location_lat': visit_data.get('latitude'),
                        'visit_location_long': visit_data.get('longitude'),
                        'planned_time': start_time or fields.Datetime.now(),
                        'visit_time': end_time if mobile_state == 'completed' else (start_time or fields.Datetime.now()),
                        'mobile_local_id': local_id,
                        'follow_up_required': bool(visit_data.get('follow_up_date')),
                        'next_visit_date': visit_data.get('follow_up_date') or False,
                    }
                
                    visit = request.env['sales.rep.visit'].with_user(user).create([vals])[0]
                    ack['visits'][local_id] = visit.id
                
                    # Update route customer state and timing
                    route_customer.write({
                        'state': 'in_progress' if mobile_state == 'in_progress' else 'visited',
                        'visit_id': visit.id,
                        'visit_result': visit.visit_result,
                        'visit_start_time': start_time,
                        'visit_end_time': end_time if mobile_state == 'completed' else False
                    })

                    # Link existing sale order created for this visit or route customer if any
                    linked_so = request.env['sale.order'].sudo().search([
                        ('visit_id', '=', visit.id)
                    ], limit=1)
                    if not linked_so and local_id:
                        linked_so = request.env['sale.order'].sudo().search([
                            ('mobile_local_id', '=', local_id)
                        ], limit=1)
                    if not linked_so:
                        linked_so = request.env['sale.order'].sudo().search([
                            ('partner_id', '=', route_customer.partner_id.id),
                            ('sales_rep_id', '=', sales_rep.id),
                            ('route_customer_id', 'in', [route_customer.id, False]),
                            '|', ('visit_id', '=', False), ('visit_id', '=', visit.id)
                        ], order='id desc', limit=1)

                    if linked_so:
                        if not visit.sale_order_id:
                            visit.sudo().write({'sale_order_id': linked_so.id})
                        if not linked_so.visit_id:
                            linked_so.sudo().write({'visit_id': visit.id})
                        if not linked_so.route_customer_id:
                            linked_so.sudo().write({'route_customer_id': route_customer.id})
                        _logger.info(f"Sync: Bidirectionally linked visit {visit.name} (ID: {visit.id}) with sale.order {linked_so.name} (ID: {linked_so.id})")
                
                    request.env.flush_all()
                    _logger.info(f"Sync: Successfully created visit {visit.name} (ID: {visit.id}) from local_id {local_id}")
            except Exception as e:
                _logger.error(f"Failed to sync visit {visit_data.get('local_id')}: {e}")

        # =========================================================================
        # STEP 3: Process Sales Orders (BEFORE Payments so payments can link to orders)
        # =========================================================================
        orders = upload_data.get('orders', [])
        for order_data in orders:
            try:
                with request.env.cr.savepoint():
                    local_id = order_data.get('local_id')
                    visit_local_id = order_data.get('visit_local_id')
                
                    # Resolve visit_id (Priority: 1. Odoo ID from payload, 2. Local ID from current batch ack['visits'], 3. Lookup by local ID in DB, 4. Fallback route customer visit)
                    visit_id = order_data.get('visit_id')
                    if visit_id and isinstance(visit_id, str):
                        if visit_id.isdigit():
                            visit_id = int(visit_id)
                        else:
                            if not visit_local_id:
                                visit_local_id = visit_id
                            visit_id = False

                    if not visit_id and visit_local_id:
                        visit_id = ack['visits'].get(visit_local_id)
                        if not visit_id:
                            visit_rec = request.env['sales.rep.visit'].sudo().search([('mobile_local_id', '=', visit_local_id)], limit=1)
                            if visit_rec:
                                visit_id = visit_rec.id

                    # Resolve route_customer_id
                    route_customer_id = order_data.get('route_customer_id') or order_data.get('local_route_customer_id')
                    if route_customer_id_map:
                        if route_customer_id in route_customer_id_map:
                            route_customer_id = route_customer_id_map[route_customer_id]
                        elif str(route_customer_id) in route_customer_id_map:
                            route_customer_id = route_customer_id_map[str(route_customer_id)]

                    if not visit_id and route_customer_id:
                        rc_id = int(route_customer_id) if isinstance(route_customer_id, int) or (isinstance(route_customer_id, str) and route_customer_id.isdigit()) else False
                        if rc_id:
                            rc = request.env['sales.route.customer'].sudo().browse(rc_id)
                            if rc.exists() and rc.visit_id:
                                visit_id = rc.visit_id.id

                    visit = request.env['sales.rep.visit'].sudo().browse(visit_id) if visit_id else False
                
                    # === RESOLVE partner_id for offline-created customers ===
                    partner_id = order_data.get('partner_id')
                    partner_local_id = order_data.get('partner_local_id')
                
                    if partner_id and partner_id_map:
                        if str(partner_id) in partner_id_map:
                            partner_id = partner_id_map[str(partner_id)]
                        elif partner_id in partner_id_map:
                            partner_id = partner_id_map[partner_id]

                    if partner_id:
                        try:
                            if isinstance(partner_id, str) and not partner_id.isdigit():
                                if not partner_local_id:
                                    partner_local_id = partner_id
                                partner_id = False
                            else:
                                partner_rec = request.env['res.partner'].sudo().browse(int(partner_id))
                                if not partner_rec.exists():
                                    partner_id = False
                        except Exception:
                            partner_id = False

                    if visit:
                        partner_id = partner_id or visit.partner_id.id
                        route_customer_id = route_customer_id or visit.route_customer_id.id
                
                    if not partner_id and partner_local_id:
                        # Strategy 1: Check partner_id_map (from pre-processed create_customer actions in this batch)
                        if partner_id_map and partner_local_id in partner_id_map:
                            partner_id = partner_id_map[partner_local_id]
                            _logger.info(f"Sync: Resolved partner for order {local_id}: {partner_local_id} -> {partner_id} (from batch map)")
                        else:
                            # Strategy 2: DB lookup by mobile_local_id (partner was created in a previous sync)
                            partner = request.env['res.partner'].sudo().search([('mobile_local_id', '=', partner_local_id)], limit=1)
                            if partner:
                                partner_id = partner.id
                                _logger.info(f"Sync: Resolved partner for order {local_id}: {partner_local_id} -> {partner_id} (from DB)")
                            else:
                                _logger.error(f"Sync: Cannot resolve partner for order {local_id}: partner_local_id={partner_local_id} not found")
                                continue
                
                    # If we still have no partner_id, try resolving via route_customer
                    if not partner_id and route_customer_id:
                        try:
                            rc = request.env['sales.route.customer'].sudo().browse(int(route_customer_id))
                            if rc.exists() and rc.partner_id:
                                partner_id = rc.partner_id.id
                                _logger.info(f"Sync: Resolved partner for order {local_id}: from route_customer {route_customer_id} -> {partner_id}")
                        except Exception:
                            pass
                
                    if not partner_id:
                        _logger.error(f"Sync: Order {local_id} has no resolvable partner_id. Skipping.")
                        continue

                    # Ensure route_customer_id is set if partner belongs to current route
                    if not route_customer_id and partner_id:
                        route_id = order_data.get('route_id') or (visit.route_id.id if visit else self._get_current_route_id(sales_rep))
                        domain = [('partner_id', '=', int(partner_id))]
                        if route_id:
                            domain.append(('route_id', '=', int(route_id)))
                        rc = request.env['sales.route.customer'].sudo().search(domain, limit=1)
                        if rc:
                            route_customer_id = rc.id
                        elif route_id:
                            rc = request.env['sales.route.customer'].sudo().create({
                                'route_id': int(route_id),
                                'partner_id': int(partner_id),
                                'sequence': 999,
                            })
                            route_customer_id = rc.id
                            _logger.info(f"Sync: Auto-created route_customer {route_customer_id} for order {local_id} (partner {partner_id}, route {route_id})")


                    sync_is_cash = order_data.get('is_cash')
                    if sync_is_cash is None:
                        sync_is_cash = order_data.get('cash_payment') or order_data.get('is_cash_order')
                
                    vals = {
                        'partner_id': partner_id,
                        'route_id': order_data.get('route_id') or (visit.route_id.id if visit else False),
                        'route_customer_id': int(route_customer_id) if route_customer_id else False,
                        'visit_id': visit_id or (visit.id if visit else False),
                        'date_order': parse_mobile_datetime(order_data.get('date'), user) or fields.Datetime.now(),
                        'state': 'draft', # Always draft initially
                        'sales_rep_id': sales_rep.id,
                        'created_sales_rep_id': sales_rep.id,
                        'user_id': sales_rep.user_id.id,
                        'team_id': sales_rep.user_id.sale_team_id.id if sales_rep.user_id.sale_team_id else False,
                        'mobile_local_id': local_id,
                        'discount_total': float(order_data.get('discount_total', 0) or 0) if sales_rep.access_discount else 0.0,
                    }
                    if sync_is_cash is not None:
                        vals['is_cash'] = sync_is_cash

                    # The app sends its van *location* id in `warehouse_id`; never trust
                    # it as a warehouse id. Use the warehouse that owns the rep's van.
                    van_wh = sales_rep.default_location_id.warehouse_id
                    if van_wh and van_wh.company_id == sales_rep.company_id:
                        vals['warehouse_id'] = van_wh.id
                
                    if 'warehouse_id' not in vals:
                        wh = request.env['stock.warehouse'].with_user(user).search([('company_id', '=', sales_rep.company_id.id)], limit=1)
                        if wh:
                            vals['warehouse_id'] = wh.id
                        else:
                            _logger.error(f"Sync: No warehouse found for company {sales_rep.company_id.name}. Order creation may fail.")
                
                    odoo_id = order_data.get('odoo_id')
                    order = False
                    if odoo_id:
                        order = request.env['sale.order'].with_user(user).browse(odoo_id)
                        if not order.exists():
                            order = False
                
                    if not order and local_id:
                        order = request.env['sale.order'].with_user(user).search([('mobile_local_id', '=', local_id)], limit=1)

                    is_new_order = not bool(order)
                    was_draft = is_new_order or (order.state in ('draft', 'sent'))

                    if order:
                        order.write(vals)
                        if visit and not visit.sale_order_id:
                            visit.sudo().write({'sale_order_id': order.id})
                        if order.state in ('draft', 'sent'):
                            order.order_line.unlink()
                            self._create_upload_order_lines(order, order_data, user, sales_rep, ack, local_id)
                        _logger.info(f"Sync: Updated existing Order {order.name} (ID: {order.id}) from local_id: {local_id} with visit_id {visit_id}")
                    else:
                        if 'is_cash' not in vals:
                            partner = request.env['res.partner'].sudo().browse(partner_id)
                            vals['is_cash'] = partner.is_cash if partner.exists() else True
                        order = request.env['sale.order'].with_user(user).create(vals)
                        if visit and not visit.sale_order_id:
                            visit.sudo().write({'sale_order_id': order.id})
                        self._create_upload_order_lines(order, order_data, user, sales_rep, ack, local_id)
                        _logger.info(f"Sync: Successfully created Order {order.name} (ID: {order.id}) from local_id: {local_id} with visit_id {visit_id}")

                    ack['orders'][local_id] = order.id

                    # Auto-Lifecycle: Confirm → Deliver → Invoice (Only for new orders or orders transitioning from draft)
                    if was_draft and order_data.get('state') in ('sale', 'done'):
                        self._upload_order_lifecycle(order, sales_rep, user, local_id, upload_data, ack,
                                                     quick=bool(order_data.get('quick_invoice')))

                    ack['orders'][local_id] = order.id

            except Exception as e:
                import traceback
                _logger.error(f"Failed to sync order {order_data.get('local_id')}: {e}\n{traceback.format_exc()}")

        # =========================================================================
        # STEP 4: Process Collections / Payments (AFTER Orders & Visits)
        # =========================================================================
        collections = upload_data.get('collections', [])
        for col_data in collections:
            try:
                with request.env.cr.savepoint():
                    local_id = col_data.get('local_id')
                    visit_local_id = col_data.get('visit_local_id')
                
                    # Deduplication check
                    existing_payment = request.env['account.payment'].sudo().search([('mobile_local_id', '=', local_id)], limit=1)
                    if existing_payment:
                        ack['collections'][local_id] = existing_payment.id
                        continue

                    # Resolve visit_id
                    visit_id = col_data.get('visit_id')
                    if visit_id and isinstance(visit_id, str):
                        if visit_id.isdigit():
                            visit_id = int(visit_id)
                        else:
                            if not visit_local_id:
                                visit_local_id = visit_id
                            visit_id = False

                    if not visit_id and visit_local_id:
                        visit_id = ack['visits'].get(visit_local_id)
                        if not visit_id:
                            visit = request.env['sales.rep.visit'].sudo().search([('mobile_local_id', '=', visit_local_id)], limit=1)
                            if visit:
                                visit_id = visit.id

                    # Resolve route_customer_id
                    route_customer_id = col_data.get('route_customer_id') or col_data.get('local_route_customer_id')
                    if route_customer_id_map:
                        if route_customer_id in route_customer_id_map:
                            route_customer_id = route_customer_id_map[route_customer_id]
                        elif str(route_customer_id) in route_customer_id_map:
                            route_customer_id = route_customer_id_map[str(route_customer_id)]

                    if not visit_id and route_customer_id:
                        rc_id = int(route_customer_id) if isinstance(route_customer_id, int) or (isinstance(route_customer_id, str) and route_customer_id.isdigit()) else False
                        if rc_id:
                            rc = request.env['sales.route.customer'].sudo().browse(rc_id)
                            if rc.exists() and rc.visit_id:
                                visit_id = rc.visit_id.id
                
                    # === RESOLVE partner_id for offline-created customers ===
                    partner_id = col_data.get('partner_id')
                    partner_local_id = col_data.get('partner_local_id')
                    route_id = col_data.get('route_id')

                    if partner_id:
                        try:
                            if isinstance(partner_id, str) and not partner_id.isdigit():
                                if not partner_local_id:
                                    partner_local_id = partner_id
                                partner_id = False
                            else:
                                partner_rec = request.env['res.partner'].sudo().browse(int(partner_id))
                                if not partner_rec.exists():
                                    partner_id = False
                        except Exception:
                            partner_id = False

                    if visit_id:
                        visit = request.env['sales.rep.visit'].sudo().browse(visit_id)
                        partner_id = partner_id or visit.partner_id.id
                        route_id = route_id or visit.route_id.id
                        route_customer_id = route_customer_id or visit.route_customer_id.id

                    if not partner_id and partner_local_id:
                        if partner_id_map and partner_local_id in partner_id_map:
                            partner_id = partner_id_map[partner_local_id]
                            _logger.info(f"Sync: Resolved partner for collection {local_id}: {partner_local_id} -> {partner_id} (from batch map)")
                        else:
                            partner = request.env['res.partner'].sudo().search([('mobile_local_id', '=', partner_local_id)], limit=1)
                            if partner:
                                partner_id = partner.id
                                _logger.info(f"Sync: Resolved partner for collection {local_id}: {partner_local_id} -> {partner_id} (from DB)")
                            else:
                                _logger.error(f"Sync: Cannot resolve partner for collection {local_id}: partner_local_id={partner_local_id} not found")
                                continue

                    if not partner_id:
                        _logger.warning(f"Sync Upload: Collection {local_id} has no resolvable partner_id. Skipping.")
                        continue

                    # Inject resolved partner_id into col_data for _action_create_payment
                    col_data['partner_id'] = partner_id
                    if visit_id:
                        col_data['visit_id'] = visit_id
                    if route_customer_id:
                        col_data['route_customer_id'] = route_customer_id

                    # Create payment using the consolidated action method
                    res = self._action_create_payment(col_data, user, sales_rep, local_id)
                    payment_id = res.get('payment_id')
                    
                    if not payment_id:
                         _logger.error(f"Sync Upload: Failed to create payment for collection {local_id}: {res.get('error')}")
                         continue
                
                    ack['collections'][local_id] = payment_id
                    request.env.flush_all()
            except Exception as e:
                _logger.error(f"Failed to sync collection action {col_data.get('local_id')}: {e}")

        # Process Inventory Adjustments (NEW)
        adjustments = upload_data.get('inventory_adjustments', [])
        ack['inventory_adjustments'] = {}
        if adjustments:
            _logger.info(f"Sync: Processing {len(adjustments)} inventory adjustments")
        for adj_data in adjustments:
            try:
                with request.env.cr.savepoint():
                    local_id = adj_data.get('local_id')
                    _logger.info(f"Sync: Processing inventory adjustment local_id={local_id}, data={adj_data}")
                    
                    # Deduplication check
                    existing = request.env['sales.rep.inventory.adjustment'].sudo().search([('mobile_local_id', '=', local_id)], limit=1)
                    if existing:
                        ack['inventory_adjustments'][local_id] = existing.id
                        _logger.info(f"Sync: Inventory Adjustment {local_id} already exists (ID: {existing.id}), skipping")
                        continue

                    # Parse date from mobile ISO format to Odoo Datetime
                    adj_date = fields.Datetime.now()
                    raw_date = adj_data.get('date')
                    if raw_date:
                        try:
                            adj_date = raw_date.replace('T', ' ').split('.')[0].replace('Z', '')
                        except Exception:
                            _logger.warning(f"Sync: Failed to parse date '{raw_date}', using now()")

                    adj_vals = {
                        'sales_rep_id': sales_rep.id,
                        'date': adj_date,
                        'mobile_local_id': local_id,
                    }
                    
                    adj = request.env['sales.rep.inventory.adjustment'].with_user(user).create(adj_vals)
                    
                    # Create Lines
                    for line_data in adj_data.get('lines', []):
                        product_id = line_data.get('product_id')
                        if not product_id:
                            _logger.warning(f"Sync: Skipping adjustment line without product_id")
                            continue
                        line_vals = {
                            'adjustment_id': adj.id,
                            'product_id': product_id,
                            'product_uom_id': line_data.get('product_uom_id'),
                            'counted_qty': line_data.get('counted_qty', 0.0),
                        }
                        request.env['sales.rep.inventory.adjustment.line'].with_user(user).create(line_vals)
                    
                    request.env.flush_all()
                    ack['inventory_adjustments'][local_id] = adj.id
                    _logger.info(f"Sync: Created Inventory Adjustment {adj.name} (ID: {adj.id}) from local_id: {local_id} with {len(adj_data.get('lines', []))} lines")
            except Exception as e:
                _logger.error(f"Failed to sync inventory adjustment {adj_data.get('local_id')}: {e}\n{traceback.format_exc()}")        # Process Stock Requests (NEW)
        requests_data = upload_data.get('stock_requests', [])
        ack['stock_requests'] = {}
        if requests_data:
            _logger.info(f"Sync: Processing {len(requests_data)} stock requests")
        for req_data in requests_data:
            try:
                with request.env.cr.savepoint():
                    local_id = req_data.get('local_id')
                    _logger.info(f"Sync: Processing stock request local_id={local_id}")
                    
                    # Deduplication check
                    existing = request.env['stock.picking'].sudo().search([('origin', '=', local_id)], limit=1)
                    if existing:
                        ack['stock_requests'][local_id] = existing.id
                        continue

                    # Find Internal Picking Type
                    picking_type = request.env['stock.picking.type'].sudo().search([
                        ('code', '=', 'internal'),
                        ('company_id', '=', sales_rep.company_id.id)
                    ], limit=1)

                    if picking_type:
                        p_source_loc = sales_rep.location_request_id.id
                        # Transit location when load acceptance is enabled, else the van.
                        p_dest_loc = sales_rep._get_load_destination().id
                        
                        picking_vals = {
                            'picking_type_id': picking_type.id,
                            'location_id': p_source_loc,
                            'location_dest_id': p_dest_loc,
                            'origin': local_id, # Store local_id for deduplication
                            'company_id': sales_rep.company_id.id,
                            'sales_rep_id': sales_rep.id,
                        }
                        picking = request.env['stock.picking'].sudo().create(picking_vals)

                        # Create Moves
                        for line_data in req_data.get('lines', []):
                            product_id = line_data.get('product_id')
                            if not product_id:
                                continue
                            product = request.env['product.product'].sudo().browse(int(product_id))
                            if not product.exists():
                                _logger.warning(f"Sync: Product {product_id} not found for stock request {local_id}")
                                continue
                                
                            request.env['stock.move'].sudo().create({
                                'name': f"Mobile Request: {local_id}",
                                'picking_id': picking.id,
                                'product_id': product.id,
                                'product_uom_qty': line_data.get('qty', 1.0),
                                'product_uom': line_data.get('uom_id') or product.uom_id.id,
                                'location_id': p_source_loc,
                                'location_dest_id': p_dest_loc,
                                'company_id': sales_rep.company_id.id,
                            })
                        
                        ack['stock_requests'][local_id] = picking.id
                        _logger.info(f"Sync: Created Picking {picking.name} (ID: {picking.id}) for local_id: {local_id}")
                    else:
                        _logger.warning(f"Sync: Internal picking type not found for company {sales_rep.company_id.name}. Skipping picking creation.")
            except Exception as e:
                _logger.error(f"Failed to sync stock request {req_data.get('local_id')}: {e}")

        return ack
