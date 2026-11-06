# -*- coding: utf-8 -*-
from odoo import http
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class UserController(http.Controller, SalesRepUtils):

    @http.route('/api/mobile/user/location', type='http', auth='user', methods=['GET'], cors='*', csrf=False)
    def get_user_location(self, **kwargs):
        try:
             user = request.env['res.users'].browse(request.uid)
             
             # 1. Check Sales Rep default location
             sales_rep = self._get_sales_rep(request.uid)
             if sales_rep and sales_rep.default_location_id:
                 return request.make_response(json.dumps({
                     'success': True, 
                     'location_id': sales_rep.default_location_id.id,
                     'location_name': sales_rep.default_location_id.name
                 }), headers={'Content-Type': 'application/json'})

             # 2. Check User Warehouse property
             warehouse_id = user.property_warehouse_id
             
             # 3. Fallback to Company Warehouse
             if not warehouse_id:
                  warehouse_id = request.env['stock.warehouse'].search([('company_id', '=', user.company_id.id)], limit=1)
             
             if not warehouse_id:
                  return request.make_response(json.dumps({'success': False, 'message': 'No warehouse found'}), headers={'Content-Type': 'application/json'})

             location = warehouse_id.lot_stock_id
             if location:
                  return request.make_response(json.dumps({
                     'success': True, 
                     'location_id': location.id,
                     'location_name': location.name
                 }), headers={'Content-Type': 'application/json'})
             
             return request.make_response(json.dumps({'success': False, 'message': 'No stock location found'}), headers={'Content-Type': 'application/json'})

        except Exception as e:
            _logger.error(f"Error in get_user_location: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/user/company', type='http', auth='user', methods=['GET'], cors='*', csrf=False)
    def get_user_company(self, **kwargs):
        try:
             user = request.env['res.users'].browse(request.uid)
             return request.make_response(json.dumps({
                 'success': True, 
                 'company_id': user.company_id.id,
                 'company_name': user.company_id.name
             }), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_user_company: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
