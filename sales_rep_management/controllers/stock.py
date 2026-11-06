# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class StockController(http.Controller, SalesRepUtils):

    @http.route('/api/mobile/stock/locations/internal', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_internal_locations(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            locations = request.env['stock.location'].search_read([('usage', '=', 'internal'), ('active', '=', True)], ['id', 'complete_name'])
            return request.make_response(json.dumps({'success': True, 'locations': locations}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_internal_locations: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/transfers', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def create_internal_transfer(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            source_loc_id = data.get('source_location_id')
            dest_loc_id = data.get('dest_location_id')
            lines = data.get('lines', [])
            status = data.get('status', 'draft') 

            picking_type = request.env['stock.picking.type'].search([('code', '=', 'internal')], limit=1)
            if not picking_type:
                return request.make_response(json.dumps({'success': False, 'message': 'Internal picking type not found'}), headers={'Content-Type': 'application/json'})

            picking_vals = {
                'picking_type_id': picking_type.id,
                'location_id': int(source_loc_id),
                'location_dest_id': int(dest_loc_id),
                'move_type': 'direct',
                'company_id': sales_rep.company_id.id,
                'user_id': user.id,
            }
            picking = request.env['stock.picking'].create(picking_vals)

            for line in lines:
                request.env['stock.move'].create({
                    'name': 'Internal Transfer Mobile',
                    'picking_id': picking.id,
                    'product_id': int(line['product_id']),
                    'product_uom_qty': float(line['quantity']),
                    'product_uom': line.get('uom_id') or request.env['product.product'].browse(int(line['product_id'])).uom_id.id,
                    'location_id': int(source_loc_id),
                    'location_dest_id': int(dest_loc_id),
                    'company_id': sales_rep.company_id.id,
                })

            if status == 'done':
                picking.action_confirm()
                picking.action_assign()
                picking.button_validate()

            return request.make_response(json.dumps({'success': True, 'picking_id': picking.id}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in create_internal_transfer: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/request/create', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def create_stock_request(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            lines = data.get('lines', [])
            local_id = data.get('local_id')

            _logger.info(f"Creating stock request for {sales_rep.name} (ID: {sales_rep.id}) with {len(lines)} lines. Local ID: {local_id}")

            if not sales_rep.default_location_id or not sales_rep.location_request_id:
                return request.make_response(json.dumps({'success': False, 'message': 'Sales representative locations not configured'}), headers={'Content-Type': 'application/json'})

            picking_type = request.env['stock.picking.type'].sudo().search([
                ('code', '=', 'internal'),
                ('company_id', '=', sales_rep.company_id.id)
            ], limit=1)

            if not picking_type:
                # Fallback to any internal picking type if company-specific one not found
                picking_type = request.env['stock.picking.type'].sudo().search([
                    ('code', '=', 'internal')
                ], limit=1)

            if not picking_type:
                return request.make_response(json.dumps({'success': False, 'message': 'Internal picking type not found'}), headers={'Content-Type': 'application/json'})

            allowed_sources = sales_rep.location_request_ids.ids or [sales_rep.location_request_id.id]
            source_loc = int(data.get('source_location_id') or sales_rep.location_request_id.id)
            if source_loc not in allowed_sources:
                source_loc = sales_rep.location_request_id.id
            # Loads always go to the rep's own transit/van location (never a client-chosen one).
            dest_loc = sales_rep._get_load_destination().id

            picking_vals = {
                'picking_type_id': picking_type.id,
                'location_id': source_loc,
                'location_dest_id': dest_loc,
                'origin': local_id or 'Mobile Request',
                'company_id': sales_rep.company_id.id,
                'user_id': user.id,
                'sales_rep_id': sales_rep.id,
            }
            picking = request.env['stock.picking'].sudo().create(picking_vals)

            move_vals_list = []
            for line in lines:
                product_id = line.get('product_id')
                if not product_id:
                    continue
                    
                product = request.env['product.product'].sudo().browse(product_id)
                if not product.exists():
                    _logger.warning(f"Product ID {product_id} not found for stock request.")
                    continue

                qty = float(line.get('qty', line.get('product_uom_qty', 1.0)))
                uom_id = line.get('uom_id') or line.get('product_uom_id') or product.uom_id.id
                if isinstance(uom_id, list):
                    uom_id = uom_id[0]

                move_vals_list.append((0, 0, {
                    'name': f"Mobile Request: {local_id or 'Direct'}",
                    'product_id': product.id,
                    'product_uom_qty': qty,
                    'requested_uom_qty': qty,
                    'product_uom': uom_id,
                    'location_id': source_loc,
                    'location_dest_id': dest_loc,
                    'company_id': sales_rep.company_id.id,
                    'picking_id': picking.id,
                }))

            if move_vals_list:
                picking.sudo().write({'move_ids': move_vals_list})

            # picking.action_confirm() 
            
            return request.make_response(json.dumps({
                'success': True, 
                'picking_id': picking.id, 
                'name': picking.name
            }), headers={'Content-Type': 'application/json'})
        except Exception as e:
            import traceback
            _logger.error(f"Error in create_stock_request: {str(e)}\n{traceback.format_exc()}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/pickings/<int:picking_id>/return', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def return_picking(self, picking_id, **kwargs):
         # This endpoint mimics wizard behavior for creating a return
         try:
             sales_rep, user = self._authenticate_request()
             if not sales_rep:
                 return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

             data = json.loads(request.httprequest.data)
             return_lines = data.get('lines', []) # [{'product_id': x, 'quantity': y}]
             return_location_id = data.get('return_location_id')  
             return_reason_id = data.get('return_reason_id')
             return_reason_note = data.get('return_reason') # String text
             
             picking = request.env['stock.picking'].browse(picking_id)
             if not picking.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Picking not found'}), headers={'Content-Type': 'application/json'})

             # Get return location - priority: from request, then from sales rep
             return_location = None
             if return_location_id:
                 return_location = request.env['stock.location'].sudo().browse(int(return_location_id))
                 if not return_location.exists():
                     return_location = None
             
             # Fallback: search for sales rep's return location, then default location
             if not return_location and sales_rep:
                 return_location = (
                     sales_rep.return_location_id or 
                     (sales_rep.return_location_ids and sales_rep.return_location_ids[0]) or 
                     sales_rep.default_location_id or 
                     (sales_rep.default_location_ids and sales_rep.default_location_ids[0]) or 
                     None
                 )
             
             # Create wizard
             wizard = request.env['stock.return.picking'].sudo().with_context(active_id=picking_id, active_model='stock.picking').create({})
             
             # Override the return location if configured
             if return_location:
                 wizard.location_id = return_location
             
             # Update wizard lines with requested quantities
             for line in wizard.product_return_moves:
                 req_line = next((l for l in return_lines if int(l['product_id']) == line.product_id.id), None)
                 if req_line:
                     line.quantity = float(req_line['quantity'])
                 else:
                     line.quantity = 0 

             # Create returns
             res = wizard.create_returns()
             
             return_picking_id = res.get('res_id')
             
             # Auto-validate the return picking
             if return_picking_id:
                 ret_picking = request.env['stock.picking'].sudo().browse(return_picking_id)
                 
                 # Set return reason and note
                 ret_vals = {}
                 if return_reason_id:
                     ret_vals['return_reason_id'] = int(return_reason_id)
                 if return_reason_note:
                     ret_vals['note'] = return_reason_note
                 
                 if return_location:
                     ret_vals['location_dest_id'] = return_location.id
                 
                 if ret_vals:
                     ret_picking.write(ret_vals)

                 # Update move lines destination
                 if return_location:
                    for move in ret_picking.move_ids:
                        move.write({'location_dest_id': return_location.id})
                 
                 ret_picking.action_confirm()
                 ret_picking.action_assign()
                 
                 # Set quantities done and ensure destination on move lines
                 for move in ret_picking.move_ids:
                     for line in move.move_line_ids:
                         vals = {'quantity': line.quantity}
                         if return_location:
                             vals['location_dest_id'] = return_location.id
                         line.write(vals)
                 
                 ret_picking.button_validate()

             return request.make_response(json.dumps({'success': True, 'return_picking_id': return_picking_id}), headers={'Content-Type': 'application/json'})
         except Exception as e:
             _logger.error(f"Error in return_picking: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/pickings/<string:picking_ids_str>/return_proposal', type='http', auth='user', methods=['GET'], cors='*', csrf=False)
    def get_return_proposal(self, picking_ids_str, **kwargs):
        try:
            # Parse picking IDs from comma-separated string
            picking_ids = [int(p_id) for p_id in picking_ids_str.split(',') if p_id.strip().isdigit()]
            
            if not picking_ids:
                return request.make_response(json.dumps({'success': False, 'message': 'No valid picking IDs provided'}), headers={'Content-Type': 'application/json'})

            # Get pickings ensuring they exist
            pickings = request.env['stock.picking'].browse(picking_ids)
            if not pickings.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Pickings not found'}), headers={'Content-Type': 'application/json'})

            # Get sales rep's return location or fallback to default
            sales_rep = request.env['sales.representative'].sudo().search([('user_id', '=', request.uid)], limit=1)
            return_location = sales_rep.return_location_id or sales_rep.default_location_id if sales_rep else None

            aggregated_lines = {}
            default_location_id = None
            default_location_name = None

            for picking in pickings:
                if picking.state != 'done':
                    continue
                    
                # Use default_get to let Odoo calculate the actual returnable quantities (original - already returned)
                # wizard_vals = request.env['stock.return.picking'].with_context(
                #     active_id=picking.id, active_ids=[picking.id], active_model='stock.picking'
                # ).default_get(['product_return_moves', 'location_id'])
                # wizard = request.env['stock.return.picking'].create(wizard_vals)
                
                wizard = request.env['stock.return.picking'].with_context(
                    active_id=picking.id,
                    active_ids=[picking.id],
                    active_model='stock.picking'
                ).new({})

                try:
                    return_moves = wizard.product_return_moves
                except KeyError as e:
                    if 'reserved_qty' in str(e) or 'reserved_uom_qty' in str(e):
                        # Catch Odoo 17 error and manually build the return values
                        _logger.warning("Caught reserved_qty error in get_return_proposal, falling back to manual generation")
                        
                        # Calculate already-returned quantity per product from previous done return pickings
                        already_returned = {}
                        existing_returns = request.env['stock.picking'].sudo().search([
                            ('origin', 'like', picking.name),
                            ('picking_type_code', '=', 'incoming'),
                            ('state', '=', 'done'),
                        ])
                        for ret_pick in existing_returns:
                            for ret_move in ret_pick.move_ids:
                                pid = ret_move.product_id.id
                                already_returned[pid] = already_returned.get(pid, 0.0) + ret_move.quantity

                        return_moves_data = []
                        for move in picking.move_ids:
                            if move.state == 'cancel':
                                continue
                            if move.scrapped:
                                continue
                            # Available to return = delivered qty - already returned qty
                            delivered_qty = sum(move.move_line_ids.mapped('quantity')) if move.move_line_ids else move.product_qty
                            returned_qty = already_returned.get(move.product_id.id, 0.0)
                            qty = max(0.0, delivered_qty - returned_qty)
                            if qty > 0:
                                return_moves_data.append({
                                    'product_id': move.product_id,
                                    'quantity': qty,
                                    'uom_id': move.product_uom
                                })
                        
                        class FakeMove:
                            def __init__(self, data):
                                self.product_id = data['product_id']
                                self.quantity = data['quantity']
                                self.uom_id = data['uom_id']
                        
                        return_moves = [FakeMove(d) for d in return_moves_data]
                    else:
                        raise e

                # Capture wizard location fallback from the first valid picking
                if not default_location_id:
                    try:
                        if wizard.location_id:
                            default_location_id = wizard.location_id.id
                            default_location_name = wizard.location_id.display_name
                    except Exception:
                        pass
                
                for line in return_moves:
                    if line.quantity <= 0:
                        continue
                        
                    product_id = line.product_id.id
                    # Aggregate quantities per product across pickings
                    if product_id in aggregated_lines:
                        aggregated_lines[product_id]['quantity'] += line.quantity
                        aggregated_lines[product_id]['maxQuantity'] += line.quantity
                    else:
                        aggregated_lines[product_id] = {
                            'product_id': product_id, 
                            'product_name': line.product_id.display_name,
                            'quantity': line.quantity,
                            'maxQuantity': line.quantity,
                            'uom_id': line.uom_id.id,
                            'uom_name': line.uom_id.name
                        }
            
            # Use sales rep return location if available, otherwise wizard default
            location_id = return_location.id if return_location else default_location_id
            location_name = return_location.display_name if return_location else default_location_name
                
            return request.make_response(json.dumps({
                'success': True,
                'lines': list(aggregated_lines.values()),
                'locationId': location_id, 
                'locationName': location_name
            }), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_return_proposal: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/order/<int:order_id>/pending_returns', type='http', auth='user', methods=['GET'], cors='*', csrf=False)
    def get_pending_returns(self, order_id, **kwargs):
        try:
            order = request.env['sale.order'].browse(order_id)
            if not order.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Order not found'}), headers={'Content-Type': 'application/json'})

            # Get the outgoing delivery picking names for this order to find their returns
            # We search by origin since we no longer write sale_id on return pickings
            outgoing_pickings = order.picking_ids.filtered(lambda p: p.picking_type_code == 'outgoing')
            outgoing_names = outgoing_pickings.mapped('name')
            
            _logger.info(f"Sync: get_pending_returns for Order {order.id}, outgoing pickings: {outgoing_names}")

            # Find return pickings whose origin matches "Return of <delivery_name>"
            pending_pickings = request.env['stock.picking'].sudo()
            if outgoing_names:
                pending_pickings = request.env['stock.picking'].sudo().search([
                    ('origin', 'in', [f'Return of {n}' for n in outgoing_names]),
                    ('picking_type_code', '=', 'incoming'),
                    ('state', 'not in', ('done', 'cancel'))
                ])
            
            _logger.info(f"Sync: get_pending_returns for Order {order.id} found {len(pending_pickings)} pending incoming pickings")
            for p in pending_pickings:
                _logger.info(f"Sync: Pending Picking: {p.name} (ID: {p.id}) - State: {p.state} - Origin: {p.origin}")
            
            return request.make_response(json.dumps({
                'success': True, 
                'has_pending_returns': bool(pending_pickings),
                'picking_ids': pending_pickings.ids
            }), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_pending_returns: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/pickings/validate_returns', type='http', auth='user', methods=['POST'], cors='*', csrf=False)
    def validate_returns(self, **kwargs):
        try:
            data = json.loads(request.httprequest.data)
            picking_ids = data.get('picking_ids', [])
            
            if not picking_ids:
                return request.make_response(json.dumps({'success': False, 'message': 'No pickings provided'}), headers={'Content-Type': 'application/json'})
                
            pickings = request.env['stock.picking'].browse(picking_ids)
            validated_ids = []
            for p in pickings:
                if p.state not in ('done', 'cancel'):
                    p.with_context(skip_backorder=True, skip_sms=True).button_validate()
                    validated_ids.append(p.id)
                    
            return request.make_response(json.dumps({'success': True, 'validated_ids': validated_ids}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in validate_returns: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/pickings/<int:picking_id>/state', type='http', auth='user', methods=['GET'], cors='*', csrf=False)
    def get_picking_state(self, picking_id, **kwargs):
        try:
             picking = request.env['stock.picking'].browse(picking_id)
             if not picking.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Picking not found'}), headers={'Content-Type': 'application/json'})
             
             return request.make_response(json.dumps({'success': True, 'state': picking.state}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_picking_state: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/quants', type='http', auth='user', methods=['POST'], cors='*', csrf=False)
    def get_quants(self, **kwargs):
        try:
            data = json.loads(request.httprequest.data)
            product_ids = data.get('product_ids', [])
            location_ids = data.get('location_ids', [])
            
            domain = []
            if product_ids:
                domain.append(('product_id', 'in', product_ids))
            if location_ids:
                domain.append(('location_id', 'in', location_ids))
                
            quants = request.env['stock.quant'].search_read(domain, ['id', 'product_id', 'location_id', 'quantity', 'reserved_quantity'])
            return request.make_response(json.dumps({'success': True, 'quants': quants}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_quants: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/quants/<int:quant_id>', type='http', auth='user', methods=['PUT'], cors='*', csrf=False)
    def update_stock_quant(self, quant_id, **kwargs):
        try:
            data = json.loads(request.httprequest.data)
            vals = {}
            if 'quantity' in data:
                vals['quantity'] = data['quantity'] # Careful: updating inventory quantity directly
            if 'reserved_quantity' in data:
                vals['reserved_quantity'] = data['reserved_quantity']

            quant = request.env['stock.quant'].browse(quant_id)
            if not quant.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Quant not found'}), headers={'Content-Type': 'application/json'})
            
            # Writing to stock.quant directly is sensitive. Ensure correctness.
            # Odoo generally manages this via moves. 
            # But if legacy app relied on this, we support it with caution.
            quant.sudo().write(vals) 
            
            return request.make_response(json.dumps({'success': True}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in update_stock_quant: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/pickings/<int:picking_id>', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_picking_details(self, picking_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            picking = request.env['stock.picking'].browse(picking_id)
            if not picking.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Picking not found'}), headers={'Content-Type': 'application/json'})
            
            lines = []
            for move in picking.move_ids:
                req_qty = getattr(move, 'requested_uom_qty', 0.0) or move.product_uom_qty
                sent_qty = move.quantity if move.state == 'done' else (move.quantity or 0.0)
                lines.append({
                    'product_id': move.product_id.id,
                    'product_name': move.product_id.display_name,
                    'product_code': move.product_id.default_code or '',
                    'requested_qty': req_qty,
                    'sent_qty': sent_qty,
                    'quantity': sent_qty if move.state == 'done' else move.product_uom_qty,
                    'product_uom_qty': req_qty,
                    'uom_name': move.product_uom.name if move.product_uom else '',
                    'state': move.state
                })
                
            return request.make_response(json.dumps({
                'success': True,
                'picking': {
                    'id': picking.id,
                    'name': picking.name,
                    'state': picking.state,
                    'location_name': picking.location_id.display_name or picking.location_id.name,
                    'location_dest_name': picking.location_dest_id.display_name or picking.location_dest_id.name,
                    'date': (picking.date_done or picking.scheduled_date).strftime('%Y-%m-%d %H:%M:%S') if (picking.date_done or picking.scheduled_date) else None,
                    'lines': lines
                }
            }), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_picking_details: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
    @http.route('/api/mobile/stock/inventory_adjustment/create', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def create_inventory_adjustment(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            lines = data.get('lines', [])
            local_id = data.get('local_id')

            _logger.info(f"Creating inventory adjustment for {sales_rep.name} (ID: {sales_rep.id}) with {len(lines)} lines. Local ID: {local_id}")

            adj_vals = {
                'sales_rep_id': sales_rep.id,
                'date': fields.Datetime.now(),
                'mobile_local_id': local_id,
            }
            if data.get('location_id'):
                adj_vals['location_id'] = int(data.get('location_id'))
            
            adj = request.env['sales.rep.inventory.adjustment'].with_user(user).create(adj_vals)
            
            for line in lines:
                product_id = line.get('product_id')
                if not product_id:
                    continue
                
                line_vals = {
                    'adjustment_id': adj.id,
                    'product_id': product_id,
                    'product_uom_id': line.get('product_uom_id'),
                    'counted_qty': float(line.get('counted_qty', 0.0)),
                }
                request.env['sales.rep.inventory.adjustment.line'].with_user(user).create(line_vals)

            return request.make_response(json.dumps({
                'success': True, 
                'adjustment_id': adj.id, 
                'name': adj.name
            }), headers={'Content-Type': 'application/json'})
        except Exception as e:
            import traceback
            _logger.error(f"Error in create_inventory_adjustment: {str(e)}\n{traceback.format_exc()}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/general_return/reasons', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_general_return_reasons(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            if 'customer.return.reason' not in request.env:
                return request.make_response(json.dumps({'success': False, 'message': 'Customer Return module not installed'}), headers={'Content-Type': 'application/json'})
            
            reasons = request.env['customer.return.reason'].sudo().search_read(
                [('active', '=', True)],
                ['id', 'name', 'sequence']
            )
            return request.make_response(json.dumps({'success': True, 'reasons': reasons}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_general_return_reasons: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/general_return/list', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def list_general_returns(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            if 'customer.return.request' not in request.env:
                return request.make_response(json.dumps({'success': False, 'message': 'Customer Return module not installed'}), headers={'Content-Type': 'application/json'})
            
            domain = [('requested_by', '=', user.id)]
            requests = request.env['customer.return.request'].sudo().search_read(
                domain,
                ['id', 'name', 'partner_id', 'state', 'return_date'],
                order='return_date desc, id desc'
            )
            
            # Format partner_id to get just the name if it's a tuple
            for req in requests:
                if req.get('partner_id') and isinstance(req['partner_id'], (list, tuple)):
                    req['partner_name'] = req['partner_id'][1]
                    req['partner_id'] = req['partner_id'][0]
                return_date = req.get('return_date')
                req['return_date'] = fields.Date.to_string(return_date) if return_date else None
                    
            return request.make_response(json.dumps({'success': True, 'requests': requests}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in list_general_returns: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/general_return/details/<int:request_id>', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_general_return_details(self, request_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            if 'customer.return.request' not in request.env:
                return request.make_response(json.dumps({'success': False, 'message': 'Customer Return module not installed'}), headers={'Content-Type': 'application/json'})
            
            req = request.env['customer.return.request'].sudo().browse(request_id)
            if not req.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Request not found'}), headers={'Content-Type': 'application/json'})
                
            lines = []
            for line in req.line_ids:
                lines.append({
                    'product_id': line.product_id.id,
                    'product_name': line.product_id.display_name,
                    'product_uom_id': line.product_uom_id.id,
                    'product_uom_name': line.product_uom_id.name,
                    'quantity': line.quantity,
                })
                
            attachments = []
            att_records = request.env['ir.attachment'].sudo().search([
                ('res_model', '=', 'customer.return.request'),
                ('res_id', '=', req.id)
            ])
            for att in att_records:
                attachments.append({
                    'id': att.id,
                    'name': att.name,
                    'mimetype': att.mimetype,
                    # We might not want to send the full base64 here if it's large, but for mobile preview it's often needed
                    'datas': att.datas.decode('utf-8') if isinstance(att.datas, bytes) else att.datas
                })
                
            return request.make_response(json.dumps({
                'success': True, 
                'request': {
                    'id': req.id,
                    'name': req.name,
                    'partner_id': req.partner_id.id,
                    'partner_name': req.partner_id.display_name,
                    'location_dest_id': req.location_dest_id.id,
                    'location_dest_name': req.location_dest_id.display_name,
                    'reason_id': req.reason_id.id,
                    'reason_name': req.reason_id.name,
                    'state': req.state,
                    'return_date': req.return_date.strftime('%Y-%m-%d') if req.return_date else None,
                    'lines': lines,
                    'attachments': attachments
                }
            }), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_general_return_details: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/stock/general_return/submit', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def submit_general_return(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)
            
            if 'customer.return.request' not in request.env:
                return request.make_response(json.dumps({'success': False, 'message': 'Customer Return module not installed'}), headers={'Content-Type': 'application/json'})
            
            data = json.loads(request.httprequest.data)
            partner_id = data.get('partner_id')
            location_dest_id = data.get('location_dest_id')
            reason_id = data.get('reason_id')
            lines = data.get('lines', [])
            attachments = data.get('attachments', [])
            
            if not partner_id or not location_dest_id or not reason_id or not lines:
                return request.make_response(json.dumps({'success': False, 'message': 'Missing required fields'}), headers={'Content-Type': 'application/json'})
            
            # Create the request
            req_vals = {
                'partner_id': partner_id,
                'location_dest_id': location_dest_id,
                'reason_id': reason_id,
                'company_id': sales_rep.company_id.id,
            }
            
            req_record = request.env['customer.return.request'].with_user(user).create(req_vals)
            
            # Create lines
            for line in lines:
                product_id = line.get('product_id')
                product = request.env['product.product'].sudo().browse(product_id)
                if not product.exists():
                    continue
                    
                uom_id = line.get('product_uom_id') or product.uom_id.id
                
                request.env['customer.return.request.line'].with_user(user).create({
                    'request_id': req_record.id,
                    'product_id': product_id,
                    'product_uom_id': uom_id,
                    'quantity': float(line.get('quantity', 1.0)),
                    # Trigger the onchange equivalent
                    'inventory_unit_cost': product.standard_price,
                    'credit_note_unit_price': product.lst_price,
                })
                
            # Handle attachments
            for att in attachments:
                if att.get('base64'):
                    request.env['ir.attachment'].sudo().create({
                        'name': att.get('name', 'Attachment'),
                        'type': 'binary',
                        'datas': att.get('base64'),
                        'res_model': 'customer.return.request',
                        'res_id': req_record.id,
                    })
            
            # Submit the request
            req_record.with_user(user).action_submit()
            
            return request.make_response(json.dumps({
                'success': True, 
                'request_id': req_record.id,
                'name': req_record.name,
                'state': req_record.state
            }), headers={'Content-Type': 'application/json'})
            
        except Exception as e:
            import traceback
            _logger.error(f"Error in submit_general_return: {str(e)}\n{traceback.format_exc()}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
