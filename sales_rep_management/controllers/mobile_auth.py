# -*- coding: utf-8 -*-
from odoo import http, _
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class MobileAuthController(http.Controller, SalesRepUtils):

    @http.route('/api/mobile/get_sales_rep_info', type='json', auth='public', methods=['POST'], csrf=False)
    def get_sales_rep_info(self, **kw):
        """
        Get Sales Representative info by email.
        Requires authenticated token.
        """
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return {'success': False, 'message': 'Unauthorized', 'data': None}
            email = kw.get('email')
            if not email:
                return {
                    'success': False,
                    'message': 'Email is required',
                    'data': None
                }
            
            _logger.info(f"Mobile Auth: Searching for Sales Rep with email '{email}'")
            
            # Search for Sales Rep by related employee's work email
            # We search case-insensitive
            sales_rep = request.env['sales.representative'].search([
                ('email', '=ilike', email)
            ], limit=1)

            if not sales_rep:
                _logger.warning(f"Mobile Auth: No Sales Rep found for email '{email}'")
                return {
                    'success': False,
                    'message': f'Sales Representative not found for email: {email}',
                    'data': None
                }

            if not sales_rep.active:
                 return {
                    'success': False,
                    'message': 'Sales Representative account is inactive',
                    'data': None
                }

            return {
                'success': True,
                'message': 'Sales Rep found',
                'data': {
                    'sales_rep_id': sales_rep.id,
                    'name': sales_rep.name,
                    'code': sales_rep.code,
                    'user_id': sales_rep.user_id.id,
                    'partner_id': sales_rep.user_id.partner_id.id,
                    'company_id': sales_rep.user_id.company_id.id,
                    'company_name': sales_rep.user_id.company_id.name,
                    # Contact
                    'email': sales_rep.email or '',
                    'phone': sales_rep.phone or '',
                    # Role
                    'is_supervisor': sales_rep.is_supervisor,
                    'is_manager': sales_rep.is_manager,
                    'supervisor_name': sales_rep.supervisor_id.name if sales_rep.supervisor_id else False,
                    # Stats
                    'total_routes': sales_rep.total_routes,
                    'total_visits': sales_rep.total_visits,
                    'total_collections': sales_rep.total_collections,
                    'monthly_target': sales_rep.monthly_target,
                    # Driver Info
                    'is_driver': sales_rep.is_driver,
                    'license_number': sales_rep.license_number or '',
                    'license_expiry_date': sales_rep.license_expiry_date,
                    # Config (Read-only)
                    'invoice_journal_id': sales_rep.invoice_journal_id.id,
                    'invoice_journal_name': sales_rep.invoice_journal_id.name if sales_rep.invoice_journal_id else False,
                    
                    # 'stock_location_id': sales_rep.stock_location_id.id,
                    # 'stock_location_name': sales_rep.stock_location_id.name if sales_rep.stock_location_id else False,
                    
                    'default_location_id': sales_rep.default_location_id.id,
                    'default_location_name': sales_rep.default_location_id.name if sales_rep.default_location_id else False,
                    'default_location_ids': [{'id': loc.id, 'name': loc.display_name} for loc in sales_rep.default_location_ids],
                    
                    'fiscal_position_id': sales_rep.fiscal_position_id.id,
                    'fiscal_position_name': sales_rep.fiscal_position_id.name if sales_rep.fiscal_position_id else False,

                    'return_location_id': sales_rep.return_location_id.id if sales_rep.return_location_id else False,
                    'return_location_name': sales_rep.return_location_id.name if sales_rep.return_location_id else False,
                    'return_location_ids': [{'id': loc.id, 'name': loc.display_name} for loc in sales_rep.return_location_ids],

                    'location_general_return_id': sales_rep.location_general_return.id if hasattr(sales_rep, 'location_general_return') and sales_rep.location_general_return else False,
                    'location_general_return_name': sales_rep.location_general_return.display_name if hasattr(sales_rep, 'location_general_return') and sales_rep.location_general_return else False,
                    'location_general_return_ids': [{'id': loc.id, 'name': loc.display_name} for loc in sales_rep.location_general_return_ids] if hasattr(sales_rep, 'location_general_return_ids') else [],
                    'access_general_return': sales_rep.access_general_return if hasattr(sales_rep, 'access_general_return') else False,

                    'picking_type_id': sales_rep.picking_type_id.id,
                    'picking_type_name': sales_rep.picking_type_id.name if sales_rep.picking_type_id else False,

                    'default_pricelist_id': sales_rep.available_pricelist_ids[0].id if sales_rep.available_pricelist_ids else False,
                    'default_pricelist_name': sales_rep.available_pricelist_ids[0].name if sales_rep.available_pricelist_ids else False,
                    'payment_term_id': sales_rep.available_payment_term_ids[0].id if sales_rep.available_payment_term_ids else False,
                    'payment_term_name': sales_rep.available_payment_term_ids[0].name if sales_rep.available_payment_term_ids else False,
                    
                    # 'auto_confirm_order': sales_rep.auto_confirm_order,
                    # 'auto_create_invoice': sales_rep.auto_create_invoice,
                    # 'auto_register_payment': sales_rep.auto_register_payment,
                    # 'allow_partial_payment': sales_rep.allow_partial_payment,

                    # Detailed Lists
                    'payment_methods': [{
                        'id': pm.id,
                        'name': pm.name,
                        'type': pm.payment_type,
                        'journal_id': pm.journal_id.id,
                    } for pm in sales_rep.payment_method_ids] if sales_rep.payment_method_ids else [],
                    
                    'available_pricelists': [{
                        'id': pl.id,
                        'name': pl.name,
                        'currency_id': pl.currency_id.id
                    } for pl in sales_rep.available_pricelist_ids] if sales_rep.available_pricelist_ids else [],

                    'available_payment_terms': [{
                        'id': pt.id,
                        'name': pt.name,
                    } for pt in sales_rep.available_payment_term_ids] if sales_rep.available_payment_term_ids else [],
                }
            }

        except Exception as e:
            _logger.error(f"Error in get_sales_rep_info: {str(e)}")
            return {
                'success': False,
                'message': str(e),
                'data': None
            }
