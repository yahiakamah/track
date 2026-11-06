# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class SalesController(http.Controller, SalesRepUtils):
    
    @http.route('/api/mobile/return_reasons', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_return_reasons(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            model_name = 'customer.return.reason' if 'customer.return.reason' in request.env else 'sales.rep.return.reason'
            reasons = request.env[model_name].with_user(user).search_read(
                [('active', '=', True)], 
                ['id', 'name', 'sequence'],
                order='sequence, name'
            )
            return request.make_response(json.dumps({'success': True, 'reasons': reasons}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_return_reasons: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})


    @http.route('/api/mobile/orders/today', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_sales_orders_today(self, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            today = fields.Date.today()
            domain = [
                ('user_id', '=', user.id),
                ('date_order', '>=', f"{today} 00:00:00"),
                ('date_order', '<=', f"{today} 23:59:59"),
                ('state', 'in', ['sale', 'done'])
            ]
            orders = request.env['sale.order'].search_read(domain, 
                ['id', 'name', 'date_order', 'amount_total', 'state', 'partner_id', 'user_id', 'order_line', 'invoice_status'],
                order='date_order desc')
            
            return request.make_response(json.dumps({'success': True, 'orders': orders}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_sales_orders_today: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def create_sales_order(self, **kwargs):
        try:
            # Manual Bearer token auth (same as sync.py)
            auth_header = request.httprequest.headers.get('Authorization')
            if not auth_header or not auth_header.startswith('Bearer '):
                return request.make_response(json.dumps({'success': False, 'message': 'Missing Token'}), headers={'Content-Type': 'application/json'}, status=401)
            
            token = auth_header.split(' ')[1]
            sales_rep = self._authenticate_token(token)
            
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Invalid Token'}), headers={'Content-Type': 'application/json'}, status=401)

            user = sales_rep.user_id
            if not user:
                return request.make_response(json.dumps({'success': False, 'message': 'Sales Rep has no linked User'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            
            partner_id = data.get('partner_id')
            order_lines_data = data.get('order_lines', [])
            route_id = data.get('route_id')
            route_customer_id = data.get('route_customer_id')
            visit_id = data.get('visit_id')
            company_id = data.get('company_id')
            pricelist_id = data.get('pricelist_id')
            
            if not partner_id:
                return request.make_response(json.dumps({'success': False, 'message': 'Customer (partner_id) is required'}), headers={'Content-Type': 'application/json'})

            vals = {
                'partner_id': partner_id,
                'state': 'draft',
            }
            # The company always comes from the authenticated representative.
            vals['company_id'] = sales_rep.company_id.id
            if pricelist_id: vals['pricelist_id'] = pricelist_id
            
            # Set Sales Team, User and Employee from Sales Rep Config
            rep = sales_rep
            if rep:
                vals['sales_rep_id'] = rep.id
                vals['created_sales_rep_id'] = rep.id
                if rep.user_id:
                    vals['user_id'] = rep.user_id.id
                if rep.crm_team_id:
                    vals['team_id'] = rep.crm_team_id.id
                if rep.employee_id:
                    vals['employee_id'] = rep.employee_id.id
            else:
                vals['user_id'] = user.id
            
            if route_id: vals['route_id'] = route_id 
            if route_customer_id: 
                vals['route_customer_id'] = route_customer_id
                if not visit_id:
                     route_customer = request.env['sales.route.customer'].with_user(user).browse(route_customer_id)
                     if route_customer.exists() and route_customer.visit_id:
                         visit_id = route_customer.visit_id.id

            if visit_id: vals['visit_id'] = visit_id

            order_line = []
            for line in order_lines_data:
                product_id = line.get('product_id')
                uom_id = line.get('product_uom_id')
                
                line_vals = {
                    'product_id': product_id,
                    'product_uom_qty': line.get('quantity'),
                }
                # Price comes from the pricelist; a client price is a discount
                # and is accepted only with the discount right.
                if sales_rep.access_discount and line.get('price_unit') is not None:
                    line_vals['price_unit'] = line.get('price_unit')
                
                if uom_id:
                    # Validate UoM Category
                    product = request.env['product.product'].with_user(user).browse(int(product_id))
                    uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
                    if product.exists() and uom.exists():
                        if product.uom_id.category_id != uom.category_id:
                            return request.make_response(json.dumps({
                                'success': False, 
                                'message': f"Unit of Measure {uom.name} is not compatible with product {product.name}"
                            }), headers={'Content-Type': 'application/json'})
                        line_vals['product_uom'] = uom.id
                
                order_line.append((0, 0, line_vals))
            
            vals['order_line'] = order_line
            
            order = request.env['sale.order'].with_user(user).create(vals)
            
            return request.make_response(json.dumps({'success': True, 'orderId': order.id, 'name': order.name}, default=str), headers={'Content-Type': 'application/json'})

        except Exception as e:
            _logger.error(f"Error in create_sales_order: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders/<int:order_id>', type='http', auth='public', methods=['PUT'], cors='*', csrf=False)
    def update_sales_order(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            order_lines_data = data.get('order_lines', [])
            
            order = request.env['sale.order'].with_user(user).browse(order_id)
            if not order.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Order not found'}), headers={'Content-Type': 'application/json'})

            if order.state not in ['draft', 'sent']:
                return request.make_response(json.dumps({'success': False, 'message': 'Only draft orders can be updated'}), headers={'Content-Type': 'application/json'})

            # 1. Map existing lines by ID and Product
            existing_lines = {line.id: line for line in order.order_line}
            
            # 2. Process incoming lines
            processed_line_ids = []
            
            for line_data in order_lines_data:
                product_id = int(line_data.get('product_id'))
                qty = float(line_data.get('quantity', 0))
                price_unit = float(line_data.get('price_unit', 0))
                
                line_id = line_data.get('id') # backend ID if known
                uom_id = line_data.get('uom_id') or line_data.get('product_uom_id')
                
                vals = {
                    'product_uom_qty': qty,
                    'price_unit': price_unit,
                }
                if uom_id:
                    # Validate UoM Category
                    product = request.env['product.product'].with_user(user).browse(product_id)
                    uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
                    if product.exists() and uom.exists():
                        if product.uom_id.category_id != uom.category_id:
                            return request.make_response(json.dumps({
                                'success': False, 
                                'message': f"Unit of Measure {uom.name} is not compatible with product {product.name}"
                            }), headers={'Content-Type': 'application/json'})
                        vals['product_uom'] = uom.id
                
                if line_id and int(line_id) in existing_lines:
                    # Update existing
                    existing_lines[int(line_id)].write(vals)
                    processed_line_ids.append(int(line_id))
                else:
                    # Create new
                    vals['order_id'] = order.id
                    vals['product_id'] = product_id
                    new_line = request.env['sale.order.line'].with_user(user).create(vals)
                    processed_line_ids.append(new_line.id)

            for old_line_id, old_line in existing_lines.items():
                if old_line_id not in processed_line_ids:
                    old_line.unlink()

            return request.make_response(json.dumps({'success': True, 'message': 'Order updated'}), headers={'Content-Type': 'application/json'})

        except Exception as e:
            _logger.error(f"Error in update_sales_order: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders/<int:order_id>/confirm', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def confirm_sales_order(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            order = request.env['sale.order'].with_user(user).browse(order_id)
            if not order.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Order not found'}), headers={'Content-Type': 'application/json'})
            
            # Confirm the order
            order.action_confirm()
            
            # Check if auto delivery is enabled for the sales rep
            if sales_rep and sales_rep.auto_delivery:
                _logger.info(f"Auto delivery enabled for sales rep {sales_rep.name}. Processing delivery and invoice...")
                
                # Automatically process delivery
                delivery_result = self._process_delivery_internal(order)
                if not delivery_result.get('success'):
                    return request.make_response(json.dumps({
                        'success': False, 
                        'message': 'Order confirmed but delivery processing failed',
                        'error': delivery_result.get('error')
                    }), headers={'Content-Type': 'application/json'})
                
                # Automatically create and post invoice
                invoice_result = self._create_invoice_internal(order)
                if not invoice_result.get('success'):
                    return request.make_response(json.dumps({
                        'success': False,
                        'message': 'Order confirmed and delivery processed, but invoice creation failed', 
                        'error': invoice_result.get('error')
                    }), headers={'Content-Type': 'application/json'})
                    
                return request.make_response(json.dumps({
                    'success': True,
                    'message': 'Order confirmed, delivery processed, and invoice created',
                    'invoice_id': invoice_result.get('invoice_id'),
                    'invoice_name': invoice_result.get('name')
                }, default=str), headers={'Content-Type': 'application/json'})
            
            return request.make_response(json.dumps({'success': True}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in confirm_sales_order: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    def _process_delivery_internal(self, order, delivered_quantities=None):
        """Internal method to process delivery for an order"""
        try:
            pickings = order.picking_ids
            if not pickings:
                return {'success': False, 'message': 'No pickings found'}

            for picking in pickings:
                picking = picking.sudo() # Elevate privileges for stock validation
                _logger.info(f"Auto-delivery: Processing {picking.name} ({picking.state})")
                if picking.state == 'done':
                    continue
                
                if picking.state == 'draft':
                    picking.action_confirm()
                
                if picking.state in ('confirmed', 'waiting'):
                    picking.action_assign()

                # Count moves per product_id to detect duplicate-product situations
                # (e.g., paid line + free reward line for same product)
                product_move_count = {}
                for move in picking.move_ids:
                    pid = str(move.product_id.id)
                    product_move_count[pid] = product_move_count.get(pid, 0) + 1

                for move in picking.move_ids:
                    qty = 0.0
                    p_id = str(move.product_id.id)
                    if delivered_quantities:
                        if product_move_count.get(p_id, 0) > 1:
                            # Multiple moves for same product (e.g., paid + reward line)
                            # delivered_quantities can't distinguish them, deliver full demand
                            qty = move.product_uom_qty
                            _logger.info(f"Move {move.id} ({move.product_id.name}): Product has {product_move_count[p_id]} moves — delivering full demand {qty}")
                        else:
                            qty_raw = float(delivered_quantities.get(p_id, 0.0))
                            # Convert from SO line UoM to Move UoM if they differ
                            if move.sale_line_id and move.sale_line_id.product_uom != move.product_uom:
                                qty = move.sale_line_id.product_uom._compute_quantity(qty_raw, move.product_uom)
                                _logger.info(f"Move {move.id} ({move.product_id.name}): Converted {qty_raw} {move.sale_line_id.product_uom.name} -> {qty} {move.product_uom.name}")
                            else:
                                qty = qty_raw
                    else:
                        qty = move.product_uom_qty

                    _logger.info(f"Move {move.id} ({move.product_id.name}): Setting quantity={qty}")
                    
                    move.sudo().write({
                        'quantity': qty, 
                        'picked': True if qty > 0 else False
                    })
                
                # Force-set quantities on move lines to match demand
                # This prevents partial delivery when stock isn't fully available
                for move in picking.move_ids:
                    target_qty = 0.0
                    p_id = str(move.product_id.id)
                    if delivered_quantities:
                        if product_move_count.get(p_id, 0) > 1:
                            target_qty = move.product_uom_qty
                        else:
                            qty_raw = float(delivered_quantities.get(p_id, 0.0))
                            if move.sale_line_id and move.sale_line_id.product_uom != move.product_uom:
                                target_qty = move.sale_line_id.product_uom._compute_quantity(qty_raw, move.product_uom)
                            else:
                                target_qty = qty_raw
                    else:
                        target_qty = move.product_uom_qty
                    
                    if target_qty > 0:
                        if move.move_line_ids:
                            total_ml_qty = sum(move.move_line_ids.mapped('quantity'))
                            if total_ml_qty < target_qty:
                                first_ml = move.move_line_ids[0]
                                first_ml.sudo().write({
                                    'quantity': target_qty,
                                    'product_uom_id': move.product_uom.id
                                })
                                if len(move.move_line_ids) > 1:
                                    for extra_ml in move.move_line_ids[1:]:
                                        extra_ml.sudo().write({'quantity': 0})
                        else:
                            # No move lines created (no stock available) - create one
                            request.env['stock.move.line'].sudo().create({
                                'move_id': move.id,
                                'picking_id': picking.id,
                                'product_id': move.product_id.id,
                                'product_uom_id': move.product_uom.id,
                                'location_id': move.location_id.id,
                                'location_dest_id': move.location_dest_id.id,
                                'quantity': target_qty,
                            })
                    
                request.env.flush_all()

                # Validation loop
                res = picking.button_validate()
                _logger.info(f"Initial validate result: {res}")
                
                attempts = 0
                last_marker = None
                
                while isinstance(res, dict) and attempts < 15:
                    attempts += 1
                    res_model = res.get('res_model')
                    res_id = res.get('res_id')
                    ctx = res.get('context', {})
                    
                    if not res_id and res_model:
                        _logger.info(f"Creating wizard {res_model} using default_get and context")
                        wizard_obj = request.env[res_model].sudo().with_context(ctx)
                        default_fields = ['pick_ids', 'backorder_confirmation_line_ids', 'product_return_moves']
                        available_fields = [f for f in default_fields if f in wizard_obj._fields]
                        
                        vals = wizard_obj.default_get(available_fields)
                        if 'pick_ids' in available_fields and not vals.get('pick_ids'):
                            vals['pick_ids'] = [(4, picking.id)]
                        
                        wizard = wizard_obj.create(vals)
                        res_id = wizard.id
                    
                    if not res_id:
                        _logger.warning("No res_id could be determined. Breaking.")
                        break

                    current_marker = f"{res_model}_{res_id}"
                    if current_marker == last_marker:
                        _logger.warning(f"Stuck on {current_marker}. Breaking.")
                        break
                    last_marker = current_marker
                    
                    wizard = request.env[res_model].sudo().with_context(ctx).browse(res_id)
                    _logger.info(f"Step {attempts}: Executing wizard {res_model} (ID: {res_id})")
                    
                    if res_model == 'stock.backorder.confirmation':
                        # No backorder by default for auto-delivery
                        _logger.info("Wizard Action: No Backorder (auto-delivery)")
                        if hasattr(wizard, 'action_no_backorder'):
                            res = wizard.action_no_backorder()
                        else:
                            res = wizard.process_cancel_backorder()
                    
                    elif res_model == 'stock.immediate.transfer':
                        res = wizard.process()
                    
                    elif res_model == 'confirm.stock.sms': 
                        res = wizard.send_sms()

                    elif res_model == 'stock.warn.insufficient.qty':
                        res = wizard.action_done()
                    
                    else:
                        _logger.warning(f"Unhandled wizard model: {res_model}. Attempting direct call to 'process' or 'action_confirm'")
                        if hasattr(wizard, 'process'):
                            res = wizard.process()
                        elif hasattr(wizard, 'action_confirm'):
                            res = wizard.action_confirm()
                        else:
                            break
                        
                    picking.invalidate_recordset(['state'])
                    if picking.state == 'done':
                        _logger.info(f"Picking {picking.name} is now DONE.")
                        res = True
                        break
                    
                    if not (isinstance(res, dict) and res.get('res_model')):
                        _logger.info(f"Wizard {res_model} finished. Re-checking state with button_validate...")
                        res = picking.with_context(ctx).button_validate()

                picking.invalidate_recordset(['state'])
                _logger.info(f"Final state check for {picking.name}: {picking.state}")
                if picking.state != 'done':
                    error_msg = f"Validation incomplete for {picking.name}. Final State: {picking.state}"
                    if attempts >= 15:
                        error_msg += " (Max attempts reached)"
                    _logger.warning(error_msg)
                    return {
                        'success': False, 
                        'error': error_msg,
                        'debug_info': f"Steps: {attempts}, Last Marker: {last_marker}"
                    }

            return {'success': True, 'message': 'Delivery processed successfully'}

        except Exception as e:
            _logger.error(f"Error in _process_delivery_internal: {str(e)}")
            return {'success': False, 'error': str(e)}

    def _create_invoice_internal(self, order):
        """Internal method to create invoice for an order"""
        try:
            # Recompute to ensure status is fresh
            order._compute_get_invoiced()
            if order.invoice_status == 'invoiced':
                valid_invoices = order.invoice_ids.filtered(lambda inv: inv.state != 'cancel')
                if valid_invoices:
                    invoice = valid_invoices[0]
                    return {
                        'success': True,
                        'invoice_id': invoice.id,
                        'name': invoice.name,
                        'residual': invoice.amount_residual,
                        'amount_total': invoice.amount_total,
                        'skipped': True
                    }

            invoices = order._create_invoices()
            if not invoices:
                return {'success': False, 'message': 'Invoice not created (nothing to invoice?)'}

            invoice = invoices[0]
            invoice.action_post()
            
            return {
                'success': True,
                'invoice_id': invoice.id,
                'name': invoice.name,
                'residual': invoice.amount_residual,
                'amount_total': invoice.amount_total
            }
        except Exception as e:
            error_str = str(e).lower()
            if "no items are available to invoice" in error_str or "nothing to invoice" in error_str:
                order._compute_get_invoiced()
                if order.invoice_ids.filtered(lambda inv: inv.state != 'cancel'):
                    invoice = order.invoice_ids.filtered(lambda inv: inv.state != 'cancel')[0]
                    return {
                        'success': True,
                        'invoice_id': invoice.id,
                        'name': invoice.name,
                        'residual': invoice.amount_residual,
                        'amount_total': invoice.amount_total,
                        'skipped': True
                    }
                return {'success': True, 'message': 'Nothing to invoice', 'skipped': True}
            _logger.error(f"Error in _create_invoice_internal: {str(e)}")
            return {'success': False, 'error': str(e)}

    @http.route('/api/mobile/orders/<int:order_id>/process_delivery', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def process_delivery(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            order = request.env['sale.order'].with_user(user).browse(order_id)
            if not order.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Order not found'}), headers={'Content-Type': 'application/json'})

            # Parse optional delivered quantities: { str(product_id): qty }
            data = json.loads(request.httprequest.data) if request.httprequest.data else {}
            delivered_quantities = data.get('delivered_quantities', {})
            _logger.info(f"Processing delivery for order {order.name} (ID: {order_id}). Payload: {delivered_quantities}")
            
            result = self._process_delivery_internal(order, delivered_quantities)
            
            if result.get('success'):
                # AUTOMATIC INVOICING: If delivery was successful, trigger invoice creation
                try:
                    _logger.info(f"API: Auto-triggering invoice for {order.name} after successful delivery.")
                    invoice_result = self._create_invoice_internal(order)
                    if invoice_result.get('success'):
                        result['invoice_id'] = invoice_result.get('invoice_id')
                        result['invoice_name'] = invoice_result.get('name')
                        result['message'] = (result.get('message', '') + ' Invoice created automatically.').strip()
                except Exception as inv_err:
                    _logger.warning(f"API: Auto-invoicing failed for {order.name} after delivery: {inv_err}")

            return request.make_response(json.dumps(result), headers={'Content-Type': 'application/json'})

        except Exception as e:
            _logger.error(f"Error in process_delivery: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders/<int:order_id>', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_order_details(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            _logger.info(f"get_order_details: Order {order_id}. Authenticated as {user.name} (ID: {user.id}). Request UID: {request.env.uid}")

            # 1. First, search for the order with user context to ensure they have access to it.
            order_data = request.env['sale.order'].with_user(user).search_read([('id', '=', order_id)], 
                ['name', 'date_order', 'amount_total', 'amount_tax', 'amount_untaxed', 'state', 'is_cash', 'partner_id', 'user_id', 'order_line', 'picking_ids', 'invoice_ids', 'invoice_status', 'delivery_status'], 
                limit=1)
            
            if not order_data:
                return request.make_response(json.dumps({'success': False, 'message': 'Order not found or Access Denied'}), headers={'Content-Type': 'application/json'})
            
            # 2. Once order visibility is confirmed via with_user(user), we can use sudo() for the 
            # subsidiary records (lines, invoices) to prevent strict Public User ACL blocks 
            # that often occur in auth='public' routes despite with_user application.
            # Security is maintained because we only fetch IDs linked to the confirmed order.
            
            lines = request.env['sale.order.line'].sudo().search_read(
                [('id', 'in', order_data[0]['order_line'])],
                ['product_id', 'name', 'product_uom_qty', 'qty_delivered', 'qty_invoiced', 'price_unit', 'price_subtotal', 'discount', 'product_uom', 'is_reward_line']
            )

            # Calculate qty_returning for each line by looking at pending incoming pickings
            pending_picking_ids = request.env['stock.picking'].sudo().search([
                '|', ('sale_id', '=', order_id), ('origin', '=', order_data[0]['name']),
                ('picking_type_code', '=', 'incoming'),
                ('state', 'not in', ('done', 'cancel'))
            ]).ids
            
            qty_returning_map = {}
            if pending_picking_ids:
                moves = request.env['stock.move'].sudo().search([
                    ('picking_id', 'in', pending_picking_ids),
                    ('state', 'not in', ('cancel', 'done'))
                ])
                for move in moves:
                    pid = move.product_id.id
                    qty_returning_map[pid] = qty_returning_map.get(pid, 0.0) + move.product_uom_qty
            
            for line in lines:
                pid = line['product_id'][0] if isinstance(line['product_id'], (list, tuple)) else line['product_id']
                line['qty_returning'] = qty_returning_map.get(pid, 0.0)

            # Fetch invoices detail (both INV and credit notes/RTN)
            invoices = []
            total_paid = 0.0
            total_credited = 0.0
            if order_data[0]['invoice_ids']:
                inv_ids = order_data[0]['invoice_ids']
                # Fetch the direct invoices + any credit notes that reverse them
                all_inv_ids = list(inv_ids)
                credit_notes = request.env['account.move'].sudo().search_read(
                    [('reversed_entry_id', 'in', inv_ids), ('move_type', '=', 'out_refund')],
                    ['id']
                )
                all_inv_ids += [cn['id'] for cn in credit_notes]
                invoices = request.env['account.move'].sudo().search_read(
                    [('id', 'in', all_inv_ids)],
                    ['name', 'state', 'amount_total', 'amount_residual', 'invoice_date',
                     'payment_state', 'move_type', 'reversed_entry_id']
                )
                for inv in invoices:
                    if inv.get('state') == 'posted':
                        if inv.get('move_type') == 'out_invoice':
                            total_paid += (inv.get('amount_total', 0) - inv.get('amount_residual', 0))
                        elif inv.get('move_type') == 'out_refund':
                            total_credited += inv.get('amount_total', 0)
                    # Flatten reversed_entry_id many2one
                    if isinstance(inv.get('reversed_entry_id'), (list, tuple)):
                        inv['reversed_entry_id'] = inv['reversed_entry_id'][0]

            # Calculate amount_residual like sync.py does
            total_residual = sum(inv.get('amount_residual', 0) for inv in invoices if inv.get('state') == 'posted' and inv.get('move_type') == 'out_invoice')
            order_data[0]['amount_residual'] = total_residual
            
            # Determine correct payment state exactly like sync.py
            if invoices:
                 total_amount_inv = sum(inv.get('amount_total', 0) for inv in invoices if inv.get('state') == 'posted' and inv.get('move_type') == 'out_invoice')
                 if total_residual <= 0:
                      order_data[0]['payment_state'] = 'paid'
                 elif total_residual < total_amount_inv:
                      order_data[0]['payment_state'] = 'partial'
                 else:
                      order_data[0]['payment_state'] = 'not_paid'
            else:
                 order_data[0]['payment_state'] = 'not_paid'
                 order_data[0]['amount_residual'] = order_data[0].get('amount_total', 0)


            net_amount = order_data[0].get('amount_total', 0) - total_credited

            # Calculate Points to be Earned
            points_to_earn = 0.0
            try:
                programs = request.env['loyalty.program'].sudo().search([
                    ('active', '=', True), 
                    ('program_type', 'in', ['buy_x_get_y', 'promotion'])
                ])
                # Simplified check for points without re-implementing full Odoo loyalty engine
                # We can just check basic rules if needed or leave 0 if too complex for now.
                # Re-using logic from before if possible.
                # For brevity/stability, I will check just point rewards.
                for program in programs:
                    for rule in program.rule_ids:
                        # Basic match: if we have products from rule
                        # This is complex to do perfectly. I will attempt a best-effort.
                        match = False
                        if not rule.product_ids and not rule.product_category_id:
                             match = True # Global rule
                        else:
                             # Check lines
                             product_ids_in_order = [l['product_id'][0] if isinstance(l['product_id'], (list, tuple)) else l['product_id'] for l in lines]
                             if rule.product_ids:
                                 if any(pid in rule.product_ids.ids for pid in product_ids_in_order):
                                     match = True
                        
                        if match and rule.reward_point_amount > 0:
                             if rule.reward_point_mode == 'order':
                                 points_to_earn += rule.reward_point_amount
                             # Other modes require quantity calc
                             elif rule.reward_point_mode == 'money':
                                 points_to_earn += (order_data[0]['amount_total'] * rule.reward_point_amount)

            except Exception as e:
                _logger.error(f"Error calculating points: {e}")

            # Fetch attachments
            attachments_data = []
            try:
                attachments_records = request.env['ir.attachment'].sudo().search([
                    ('res_model', '=', 'sale.order'),
                    ('res_id', '=', order_id)
                ])
                for att in attachments_records:
                    attachments_data.append({
                        'id': att.id,
                        'name': att.name,
                        'file_size': f"{(att.file_size / 1024):.1f} KB" if att.file_size else "Unknown size",
                        'url': f"/web/content/{att.id}",
                    })
            except Exception as att_err:
                _logger.error(f"Error fetching attachments for order {order_id}: {att_err}")

            return request.make_response(json.dumps({
                'success': True,
                'order': order_data[0],
                'lines': lines,
                'allInvoices': invoices,
                'total_paid': total_paid,
                'total_credited': total_credited,
                'net_amount': net_amount,
                'points_to_earn': points_to_earn,
                'attachments': attachments_data
            }, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_order_details: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders/<int:order_id>/lines', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def add_order_line(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            product_id = data.get('product_id')
            qty = data.get('quantity', 1)
            price_unit = data.get('price_unit')
            uom_id = data.get('product_uom_id')

            vals = {
                'order_id': order_id,
                'product_id': product_id,
                'product_uom_qty': qty,
            }
            if price_unit: vals['price_unit'] = price_unit
            
            if uom_id:
                # Validate UoM Category
                product = request.env['product.product'].with_user(user).browse(int(product_id))
                uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
                if product.exists() and uom.exists():
                    if product.uom_id.category_id != uom.category_id:
                        return request.make_response(json.dumps({
                            'success': False, 
                            'message': f"Unit of Measure {uom.name} is not compatible with product {product.name}"
                        }), headers={'Content-Type': 'application/json'})
                    vals['product_uom'] = uom.id

            line = request.env['sale.order.line'].with_user(user).create(vals)
            return request.make_response(json.dumps({'success': True, 'line_id': line.id}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in add_order_line: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders/lines/<int:line_id>', type='http', auth='public', methods=['PUT'], cors='*', csrf=False)
    def update_order_line(self, line_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            qty = data.get('quantity')
            uom_id = data.get('uom_id') or data.get('product_uom_id')
            
            line = request.env['sale.order.line'].with_user(user).browse(line_id)
            if not line.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Line not found'}), headers={'Content-Type': 'application/json'})

            vals = {}
            if qty is not None:
                vals['product_uom_qty'] = qty
            
            if uom_id:
                # Validate UoM Category
                uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
                if line.product_id.uom_id.category_id != uom.category_id:
                    return request.make_response(json.dumps({
                        'success': False, 
                        'message': f"Unit of Measure {uom.name} is not compatible with product {line.product_id.name}"
                    }), headers={'Content-Type': 'application/json'})
                vals['product_uom'] = uom.id

            if vals:
                line.write(vals)
            
            return request.make_response(json.dumps({'success': True}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in update_order_line: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders/lines/<int:line_id>', type='http', auth='public', methods=['DELETE'], cors='*', csrf=False)
    def remove_order_line(self, line_id, **kwargs):
        try:
             sales_rep, user = self._authenticate_request()
             if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

             line = request.env['sale.order.line'].with_user(user).browse(line_id)
             if line.exists():
                 line.unlink()
             return request.make_response(json.dumps({'success': True}), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in remove_order_line: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
             
    @http.route('/api/mobile/orders/<int:order_id>/apply_promotion', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def apply_promotion(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            order = request.env['sale.order'].sudo().browse(order_id)
            if not order.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Order not found'}), headers={'Content-Type': 'application/json'})

            data = json.loads(request.httprequest.data) if request.httprequest.data else {}
            promotion_id = data.get('promotion_id')
            coupon_code = data.get('coupon_code', '')
            if isinstance(coupon_code, str):
                coupon_code = coupon_code.strip()
            
            _logger.info(f"Applying promotion/coupon to order {order.name} (ID: {order.id}): promotion_id={promotion_id}, coupon_code='{coupon_code}'")
            
            # TRACK INITIAL STATE
            initial_total = order.amount_total
            initial_line_count = len(order.order_line)
            
            # 1. Try to apply code if provided
            code_result = None
            if coupon_code:
                # DEEP DIAGNOSTIC
                matching_card = request.env['loyalty.card'].sudo().search([('code', '=', coupon_code)])
                _logger.info(f"DEBUG: Manually searched loyalty.card for '{coupon_code}': {matching_card}")
                
                if matching_card:
                    _logger.info(f"DEBUG: Card {matching_card.id} found. Program: {matching_card.program_id.name} (ID: {matching_card.program_id.id}, Active: {matching_card.program_id.active})")
                    _logger.info(f"DEBUG: Card Partner: {matching_card.partner_id.name if matching_card.partner_id else 'None'}")
                
                applicable_programs = order._get_applicable_programs() if hasattr(order, '_get_applicable_programs') else []
                _logger.info(f"DEBUG: Applicable programs for order {order.name}: {applicable_programs.ids if hasattr(applicable_programs, 'ids') else 'Method not found'}")

                if hasattr(order, '_try_apply_code'):
                    _logger.info(f"Calling _try_apply_code with '{coupon_code}'")
                    code_result = order._try_apply_code(coupon_code)
                    _logger.info(f"Odoo _try_apply_code result: {code_result}")
                    
                    if isinstance(code_result, dict):
                        if 'error' in code_result:
                             # If not found, we still might want to trigger update in case it was a semi-valid code?
                             # But usually better to report the error.
                             _logger.warning(f"Coupon code {coupon_code} rejected: {code_result['error']}")
                             return request.make_response(json.dumps({
                                 'success': False, 
                                 'message': code_result['error'],
                                 'code_invalid': True
                             }), headers={'Content-Type': 'application/json'})
                else:
                    _logger.warning("Order model has no _try_apply_code method")

            # 2. Trigger general update
            if hasattr(order, '_update_programs_and_rewards'):
                _logger.info(f"Triggering _update_programs_and_rewards for {order.name}")
                order._update_programs_and_rewards()
                
                # 3. AUTO-CLAIM REWARDS
                if hasattr(order, '_get_claimable_rewards') and hasattr(order, '_apply_program_reward'):
                    claimable_rewards = order._get_claimable_rewards()
                    _logger.info(f"DEBUG: Claimable rewards for order {order.name}: {claimable_rewards}")
                    
                    # LOG EACH CLAIMABLE REWARD DETAIL
                    for coupon, reward_list in claimable_rewards.items():
                        for rwd in reward_list:
                            _logger.info(f"DEBUG: CLAIMABLE: Program={rwd.program_id.name} (ID: {rwd.program_id.id}), Reward={rwd.description} (ID: {rwd.id})")

                    specific_match_found = False
                    applied_rewards_count = 0
                    applied_msg = []
                    
                    for coupon, rewards in claimable_rewards.items():
                        for reward in rewards:
                            # Check if this reward's program matches the one the user strictly requested
                            is_requested_promo = False
                            if promotion_id:
                                try:
                                    if reward.program_id.id == int(promotion_id):
                                        is_requested_promo = True
                                        specific_match_found = True
                                        _logger.info(f"DEBUG: Found requested Program ID {promotion_id}")
                                except (ValueError, TypeError):
                                    pass

                            should_apply = False
                            # Case A: User picked this specific promo
                            if is_requested_promo:
                                should_apply = True
                            
                            # Case B: User typed a code that matches this specific card/coupon
                            elif coupon_code and hasattr(coupon, 'code') and coupon.code == coupon_code:
                                should_apply = True
                                _logger.info(f"DEBUG: Match found for Coupon Code {coupon_code}")
                            
                            # Case C: Universal fallback (No ID and No Code from user)
                            # Only if there is exactly one claimable reward, to avoid surprises.
                            elif not promotion_id and not coupon_code:
                                if len(claimable_rewards) == 1 and len(rewards) == 1:
                                    should_apply = True
                                    _logger.info("DEBUG: Applying single available reward as fallback")
                            
                            if should_apply:
                                # Check if already applied (Simple check: is there a line with this program?)
                                # Odoo loyalty module usually handles its own lines, but we can be safe.
                                already_applied = any(l.reward_id.id == reward.id for l in order.order_line if l.reward_id)
                                if not already_applied:
                                    _logger.info(f"DEBUG: Auto-applying reward {reward.description}")
                                    order._apply_program_reward(reward, coupon)
                                    applied_rewards_count += 1
                                    applied_msg.append(reward.description)
                                else:
                                    # USER REQUEST: If user explicitly asked for this promo and it's already there, that's fine.
                                    if is_requested_promo:
                                         return request.make_response(json.dumps({
                                             'success': True, 
                                             'message': 'This promotion is already active on this order'
                                         }), headers={'Content-Type': 'application/json'})

                    if applied_rewards_count > 0:
                        order._update_programs_and_rewards()
                
                # FALLBACK DUPLICATE CHECK
                if promotion_id and not specific_match_found:
                     # Check if it exists in the order logic
                     is_active_on_order = any(l.reward_id.program_id.id == int(promotion_id) for l in order.order_line if l.reward_id)
                     if is_active_on_order:
                         return request.make_response(json.dumps({
                             'success': True, 
                             'message': 'This promotion is already active on this order'
                         }), headers={'Content-Type': 'application/json'})

                # FINAL EVALUATION: Did the order change at all?
                final_total = order.amount_total
                final_line_count = len(order.order_line)
                order_changed = (abs(final_total - initial_total) > 0.001) or (final_line_count != initial_line_count)

                if order_changed:
                    msg = "Order updated with available rewards."
                    if applied_msg:
                        msg = f"Applied: {', '.join(applied_msg)}"
                    elif promotion_id and not specific_match_found:
                        msg = "Requested promotion wasn't eligible, but other automatic rewards were applied."
                    
                    return request.make_response(json.dumps({'success': True, 'message': msg}), headers={'Content-Type': 'application/json'})
                
                if promotion_id and not specific_match_found:
                     # DEEP DIAGNOSTIC: Why wasn't it eligible?
                     fail_msg = "This promotion is not yet eligible. Check minimum quantities or product requirements."
                     try:
                         prog = request.env['loyalty.program'].sudo().browse(int(promotion_id))
                         if prog.exists():
                             _logger.info(f"DIAGNOSING: why {prog.name} is not eligible for {order.name}")
                             # Check rules
                             for rule in prog.rule_ids:
                                 # 1. Product Check
                                 if rule.product_ids or rule.product_category_id:
                                     eligible_lines = order.order_line.filtered(lambda l: not l.reward_id)
                                     if rule.product_ids:
                                         eligible_lines = eligible_lines.filtered(lambda l: l.product_id in rule.product_ids)
                                     if rule.product_category_id:
                                         eligible_lines = eligible_lines.filtered(lambda l: l.product_id.categ_id == rule.product_category_id)
                                     
                                     if not eligible_lines:
                                         lines_names = ', '.join(rule.product_ids.mapped('name')) if rule.product_ids else rule.product_category_id.name
                                         fail_msg = f"Promotion applies only to: {lines_names}. None found in order."
                                         break
                                     
                                     # 2. Qty Check
                                     if rule.minimum_qty > 0:
                                         order_qty = sum(eligible_lines.mapped('product_uom_qty'))
                                         if order_qty < rule.minimum_qty:
                                              fail_msg = f"Minimum {int(rule.minimum_qty)} units of {prog.name} qualifying items required. You have {int(order_qty)}."
                                              break
                                     
                                     # 3. Amount Check
                                     if rule.minimum_amount > 0:
                                         order_amt = sum(eligible_lines.mapped('price_subtotal'))
                                         if order_amt < rule.minimum_amount:
                                              fail_msg = f"Minimum spend of {rule.minimum_amount} for qualifying items required. You have {order_amt}."
                                              break
                             
                             # Check Points
                             for reward in prog.reward_ids:
                                 if reward.required_points > 0:
                                     cards = request.env['loyalty.card'].sudo().search([
                                         ('partner_id', '=', order.partner_id.id), 
                                         ('program_id', '=', prog.id)
                                     ])
                                     points = sum(cards.mapped('points'))
                                     if points < reward.required_points:
                                         fail_msg = f"Insufficient Credits. Required: {reward.required_points}, Available: {points}."
                                         break

                     except Exception as diagnose_err:
                         _logger.error(f"Error diagnosing promotion failure: {str(diagnose_err)}")
                         
                     return request.make_response(json.dumps({
                         'success': False, 
                         'message': fail_msg
                     }), headers={'Content-Type': 'application/json'})
                
                # If we are here and promotion_id was provided but not found/added, 
                # and no specific error was found by diagnosis, it might be a silent Odoo rejection.
                msg = 'Promotions checked. No new rewards were eligible at this time.'
                if promotion_id:
                    msg = 'This promotion is not eligible for the current order items.'

                return request.make_response(json.dumps({
                    'success': True, 
                    'message': msg
                }), headers={'Content-Type': 'application/json'})
            else:
                 return request.make_response(json.dumps({'success': False, 'message': 'Promotion system not available on this order'}), headers={'Content-Type': 'application/json'})

        except Exception as e:
            _logger.error(f"Error in apply_promotion: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
            
    @http.route('/api/mobile/orders/<int:order_id>/create_invoice', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def create_invoice(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            order = request.env['sale.order'].with_user(user).browse(order_id)
            if not order.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Order not found'}), headers={'Content-Type': 'application/json'})

            # Create invoice via native method
            # This returns dict or id or action.
            # In Odoo 17, _create_invoices returns invoices recordset.
            invoices = order._create_invoices()
            
            if not invoices:
                 return request.make_response(json.dumps({'success': False, 'message': 'Invoice not created (nothing to invoice?)'}), headers={'Content-Type': 'application/json'})

            invoice = invoices[0]
            invoice.action_post()
            
            journals = request.env['account.journal'].search_read([('type', 'in', ['bank', 'cash'])], ['id', 'name', 'type', 'code'])
            
            return request.make_response(json.dumps({
                'success': True, 
                'invoiceId': invoice.id,
                'name': invoice.name,
                'residual': invoice.amount_residual,
                'amount_total': invoice.amount_total,
                'journals': journals
            }, default=str), headers={'Content-Type': 'application/json'})
            
        except Exception as e:
             _logger.error(f"Error in create_invoice: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/customers/<int:partner_id>/orders', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_customer_orders(self, partner_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            orders = request.env['sale.order'].with_user(user).search_read(
                [('partner_id', '=', partner_id)],
                ['id', 'name', 'date_order', 'amount_total', 'state', 'is_cash', 'partner_id', 'user_id', 'invoice_status', 'delivery_status'],
                order='date_order desc'
            )
            return request.make_response(json.dumps({'success': True, 'orders': orders}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_customer_orders: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/orders/<int:order_id>/cancel', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def cancel_order(self, order_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            order = request.env['sale.order'].with_user(user).browse(order_id)
            if not order.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Order not found'}), headers={'Content-Type': 'application/json'})
            
            order.action_cancel()
            return request.make_response(json.dumps({'success': True}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in cancel_order: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
