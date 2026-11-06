# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class RouteController(http.Controller, SalesRepUtils):

    @http.route('/api/mobile/routes', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_routes(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            _logger.info(f"Get Routes: Fetching routes for Sales Rep ID {sales_rep.id} ({sales_rep.name}) - User ID {user.id}")
            domain = [('sales_rep_id', '=', sales_rep.id), ('state', '=', 'in_progress')]
            routes = request.env['sales.rep.route'].with_user(user).search_read(domain, ['id', 'name', 'date', 'state', 'company_id'])
            return request.make_response(json.dumps({'success': True, 'routes': routes}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_routes: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/routes/<int:route_id>/customers', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_route_customers(self, route_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            customers = request.env['sales.route.customer'].with_user(user).search_read(
                [('route_id', '=', route_id)],
                ['id', 'partner_id', 'sequence', 'visit_result', 'state', 'visit_id', 'route_id', 'visit_start_time', 'visit_end_time'],
                order='sequence asc'
            )
            
            # Enrich with partner location data (manual loop for nested field access if needed, or use read on partner)
            # search_read on many2one (partner_id) returns (id, name). We need lat/long too.
            # Best way: fetch partners separately
            partner_ids = [c['partner_id'][0] for c in customers if c['partner_id']]
            partners = request.env['res.partner'].search_read(
                [('id', 'in', partner_ids)],
                ['id', 'visit_latitude', 'visit_longitude', 'enable_location', 'location_radius', 'street', 'city', 'phone', 'mobile']
            )
            partner_map = {p['id']: p for p in partners}
            
            for c in customers:
                pid = c['partner_id'][0] if c['partner_id'] else None
                if pid and pid in partner_map:
                    p_data = partner_map[pid]
                    c['visit_latitude'] = p_data.get('visit_latitude')
                    c['visit_longitude'] = p_data.get('visit_longitude')
                    c['enable_location'] = p_data.get('enable_location')
                    c['location_radius'] = p_data.get('location_radius')
                    c['partner_street'] = p_data.get('street')
                    c['partner_city'] = p_data.get('city')
                    c['partner_phone'] = p_data.get('phone') or p_data.get('mobile')

            return request.make_response(json.dumps({'success': True, 'customers': customers}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_route_customers: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
            
    @http.route('/api/mobile/customers', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def create_customer(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            route_id = data.get('route_id')
            customer_data = data.get('customer')

            # Create Partner
            partner_vals = {
                'name': customer_data.get('name'),
                'street': customer_data.get('address'), # Frontend sends 'address'
                'phone': customer_data.get('phone'),
                'email': customer_data.get('email'),
                'customer_rank': 1,
                'vat': customer_data.get('taxId'),
                'comment': customer_data.get('internalNote'),
                'property_payment_term_id': int(customer_data.get('paymentTermId')) if customer_data.get('paymentTermId') else False,
                'property_product_pricelist': int(customer_data.get('priceListId')) if customer_data.get('priceListId') else False,
            }
            
            # Add custom location fields if model has them (assuming based on previous context)
            if customer_data.get('latitude'):
                partner_vals['visit_latitude'] = float(customer_data.get('latitude'))
            if customer_data.get('longitude'):
                partner_vals['visit_longitude'] = float(customer_data.get('longitude'))

            partner = request.env['res.partner'].create(partner_vals)

            # Create Route Customer
            if route_id:
                request.env['sales.route.customer'].create({
                    'route_id': route_id,
                    'partner_id': partner.id,
                    'state': 'planned'
                })

            return request.make_response(json.dumps({'success': True, 'partner_id': partner.id}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in create_customer: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/partners/<int:partner_id>', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_partner_details(self, partner_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            partner = request.env['res.partner'].with_user(user).search_read([('id', '=', partner_id)], fields=[], limit=1)
            if not partner:
                 return request.make_response(json.dumps({'success': False, 'message': 'Partner not found'}), headers={'Content-Type': 'application/json'})
            
            # Calculate total due
            domain = [
                ('partner_id', '=', partner_id),
                ('account_id.account_type', '=', 'asset_receivable'),
                ('reconciled', '=', False)
            ]
            receivable_lines = request.env['account.move.line'].search(domain)
            total_due = sum(receivable_lines.mapped('amount_residual'))
            
            partner[0]['total_due'] = total_due
            
            return request.make_response(json.dumps({'success': True, 'partner': partner[0]}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_partner_details: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/route_customers/<int:route_customer_id>', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_route_customer_details(self, route_customer_id, **kwargs):
        try:
             sales_rep, user = self._authenticate_request()
             if not sales_rep:
                 return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

             customer = request.env['sales.route.customer'].with_user(user).browse(route_customer_id)
             if not customer.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Route Customer not found'}), headers={'Content-Type': 'application/json'})
             
             data = customer.read(['id', 'partner_id', 'sequence', 'visit_result', 'state', 'visit_id', 'visit_start_time', 'visit_end_time', 'visit_notes'])[0]
             
             # Add detailed partner info if needed
             if customer.partner_id:
                  p = customer.partner_id
                  data['partner_data'] = {
                      'id': p.id,
                      'name': p.name,
                      'street': p.street,
                      'city': p.city,
                      'phone': p.phone or p.mobile,
                      'latitude': p.partner_latitude,
                      'longitude': p.partner_longitude
                  }

             return request.make_response(json.dumps({'success': True, 'customer': data}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_route_customer_details: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/route_customers/active', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_active_route_customers(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            # Find active routes for this user
            today = fields.Date.today()
            routes = request.env['sales.rep.route'].search([
                ('sales_rep_id', '=', sales_rep.id),
                ('state', '=', 'in_progress'),
                ('date', '=', today)
            ])
            
            if not routes:
                 return request.make_response(json.dumps({'success': True, 'customers': []}), headers={'Content-Type': 'application/json'})

            customers = request.env['sales.route.customer'].search_read(
                [('route_id', 'in', routes.ids)],
                ['id', 'partner_id', 'sequence', 'visit_result', 'state', 'visit_id', 'route_id', 'visit_start_time', 'visit_end_time'],
                order='sequence asc'
            )
            
            # Enrich with partner location data
            partner_ids = [c['partner_id'][0] for c in customers if c['partner_id']]
            partners = request.env['res.partner'].search_read(
                [('id', 'in', partner_ids)],
                ['id', 'visit_latitude', 'visit_longitude', 'enable_location', 'location_radius', 'street', 'city', 'phone', 'mobile']
            )
            partner_map = {p['id']: p for p in partners}
            
            for c in customers:
                pid = c['partner_id'][0] if c['partner_id'] else None
                if pid and pid in partner_map:
                    p_data = partner_map[pid]
                    c['visit_latitude'] = p_data.get('visit_latitude')
                    c['visit_longitude'] = p_data.get('visit_longitude')
                    c['enable_location'] = p_data.get('enable_location')
                    c['location_radius'] = p_data.get('location_radius')
                    c['partner_street'] = p_data.get('street')
                    c['partner_city'] = p_data.get('city')
                    c['partner_phone'] = p_data.get('phone') or p_data.get('mobile')

            return request.make_response(json.dumps({'success': True, 'customers': customers}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_active_route_customers: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
