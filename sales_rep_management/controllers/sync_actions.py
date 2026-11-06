# -*- coding: utf-8 -*-
"""Pending-action handlers of the mobile sync (confirm, deliver, invoice,
pay, return, refund, customers, attachments...)."""
from odoo import http, fields, _
from odoo.exceptions import UserError
from odoo.http import request
import json
import logging
import base64
import traceback
from datetime import datetime, timedelta
from psycopg2 import IntegrityError

from . import mobile_ops

_logger = logging.getLogger(__name__)


class SyncActionsMixin:
    """Mixed into SyncController (controllers/sync.py); no routes here."""

    def _fix_loyalty_config(self, env):
        """
        Fixes loyalty rules that are incorrectly configured to give points-per-unit 
        equal to required-points, which results in 'Buy 1 Get 1' instead of 'Buy 3 Get 1'.
        """
        try:
            # Find rewards related to Large Cabinet (requested by user)
            rewards = env['loyalty.reward'].sudo().search([('reward_product_id.name', 'ilike', 'Large Cabinet')])
            for reward in rewards:
                for rule in reward.program_id.rule_ids:
                    # If rule gives 3 points per unit and reward costs 3 points, it's 1-for-1.
                    # Adjust to 1 point per unit so 3 units = 3 points = 1 reward.
                    if rule.reward_point_amount == 3.0 and reward.required_points == 3.0:
                        _logger.info(f"Sync: Fixing Loyalty Rule {rule.id} for {reward.program_id.name}")
                        rule.write({'reward_point_amount': 1.0})
        except Exception as e:
            _logger.error(f"Sync: Error fixing loyalty config: {e}")

    # ============================
    # PENDING ACTIONS PROCESSOR
    # ============================
    def _process_pending_actions(self, actions, user, sales_rep, upload_ack=None, partner_id_map=None, route_customer_id_map=None):
        """Process queued actions from the mobile app inside the authenticated sync context."""
        ack = {}
        for action_data in actions:
            local_id = action_data.get('local_id', '')
            action_type = action_data.get('action_type', '')
            payload = action_data.get('payload', {})

            # Resolve Partner ID
            partner_id = payload.get('partner_id')
            partner_local_id = payload.get('partner_local_id')
            if partner_id_map:
                if partner_local_id and partner_local_id in partner_id_map:
                    payload['partner_id'] = partner_id_map[partner_local_id]
                elif partner_id and str(partner_id) in partner_id_map:
                    payload['partner_id'] = partner_id_map[str(partner_id)]
                elif partner_id and partner_id in partner_id_map:
                    payload['partner_id'] = partner_id_map[partner_id]

            # Resolve Route Customer ID
            rc_id = payload.get('route_customer_id') or payload.get('local_route_customer_id')
            if route_customer_id_map and rc_id:
                if rc_id in route_customer_id_map:
                    payload['route_customer_id'] = route_customer_id_map[rc_id]
                elif str(rc_id) in route_customer_id_map:
                    payload['route_customer_id'] = route_customer_id_map[str(rc_id)]

            # Resolve IDs (Fix for offline created orders/visits/collections)
            # Strategy:
            # 1. Check if ID exists in payload and is a string (local ID).
            # 2. If not found in payload, check for 'local_id' or 'visit_local_id' keys.
            # 3. Resolve using upload_ack (current batch) or database (previous batches).

            def resolve_id(res_model, local_id_value, ack_category=None):
                if not local_id_value or not isinstance(local_id_value, str) or not local_id_value.startswith(('order_', 'visit_', 'action_', 'LO-', 'VISIT-')):
                     return local_id_value # Likely already an int or not a local ID

                # Try upload_ack first
                if upload_ack and ack_category and upload_ack.get(ack_category):
                    if local_id_value in upload_ack[ack_category]:
                        resolved = upload_ack[ack_category][local_id_value]
                        _logger.info(f"Sync: Resolved {res_model} {local_id_value} from ACK -> {resolved}")
                        return resolved

                # Fallback: Query database by mobile_local_id
                record = request.env[res_model].sudo().search([('mobile_local_id', '=', local_id_value)], limit=1)
                if record:
                    _logger.info(f"Sync: Resolved {res_model} {local_id_value} from DB -> {record.id}")
                    return record.id
                
                return local_id_value # Still a string, will be handled by execute

            # Resolve Order ID
            order_id = payload.get('order_id') or payload.get('local_order_id') or payload.get('local_id')
            was_new_order = False
            if order_id:
                old_order_id = order_id
                payload['order_id'] = resolve_id('sale.order', order_id, 'orders')
                # Check if this order was just created/updated in _process_uploads 
                # (to avoid duplicate lines if app sends both order sync + actions)
                if upload_ack and 'orders' in upload_ack:
                    if isinstance(old_order_id, str) and old_order_id in upload_ack['orders']:
                        was_new_order = True
                    elif isinstance(old_order_id, int) and old_order_id in upload_ack['orders'].values():
                        was_new_order = True

            # Resolve Visit ID
            visit_id = payload.get('visit_id') or payload.get('visit_local_id')
            if visit_id:
                payload['visit_id'] = resolve_id('sales.rep.visit', visit_id, 'visits')

            # Idempotency Guard: If we just synced this order's lines in _process_uploads,
            # we SHOULD skip add_order_line actions for it, as they are already included in the 'lines' payload of the order.
            if action_type in ('add_order_line', 'update_order_line') and was_new_order:
                _logger.info(f"Sync: Skipping {action_type} for order {payload['order_id']}, already synced in order upload.")
                ack[local_id] = {'status': 'skipped', 'reason': 'already_synced'}
                continue

            # Resolve Invoice ID
            invoice_id = payload.get('invoice_id')
            if invoice_id:
                payload['invoice_id'] = resolve_id('account.move', invoice_id, 'invoices')

            # Resolve Collection ID (for payment registration)
            # col_id = payload.get('collection_id')
            # if col_id:
            #     payload['collection_id'] = resolve_id('sales.rep.collection', col_id, 'collections')

            # Already applied in an earlier sync whose answer never reached the
            # phone: return the stored result, never run it twice.
            Ledger = request.env['sales.rep.sync.ledger']
            previous = Ledger.lookup(sales_rep.id, local_id)
            if previous is not None:
                ack[local_id] = {'success': True, 'result': previous, 'replayed': True}
                _logger.info(f"Sync: action {action_type} ({local_id}) already processed, returning stored result")
                continue

            try:
                _logger.info(f"Sync: Executing action {action_type} ({local_id})")
                # Use a savepoint for each action so a failure doesn't roll back the whole sync transaction
                with request.env.cr.savepoint():
                    result = self._execute_action(action_type, payload, user, sales_rep, action_id=local_id)
                    Ledger.record(sales_rep.id, local_id, action_type, result)
                    ack[local_id] = {'success': True, 'result': result}
                _logger.info(f"Sync Action OK: {action_type} ({local_id})")
            except Exception as e:
                ack[local_id] = {'success': False, 'error': str(e)}
                _logger.error(f"Sync Action FAIL: {action_type} ({local_id}): {e}", exc_info=True)
        return ack

    def _execute_action(self, action_type, payload, user, sales_rep, action_id=None):
        """Dispatch and execute a single pending action."""
        if action_type == 'confirm_order':
            return self._action_confirm_order(payload, user, sales_rep)
        elif action_type == 'cancel_order':
            return self._action_cancel_order(payload, user)
        elif action_type == 'process_delivery':
            return self._action_process_delivery(payload, user, sales_rep)
        elif action_type == 'create_invoice':
            return self._action_create_invoice(payload, user, sales_rep, action_id=action_id)
        elif action_type == 'register_payment':
            return self._action_register_payment(payload, user, sales_rep, action_id=action_id)
        elif action_type == 'create_payment':
            return self._action_create_payment(payload, user, sales_rep, action_id=action_id)
        elif action_type == 'return_picking':
            return self._action_return_picking(payload, user)
        elif action_type == 'refund_invoice':
            return self._action_refund_invoice(payload, user)
        elif action_type == 'apply_promotion':
            return self._action_apply_promotion(payload, user)
        elif action_type == 'receive_returns':
            return self._action_receive_returns(payload, user)
        elif action_type == 'add_order_line':
            return self._action_add_order_line(payload, user)
        elif action_type in ('update_order_line', 'remove_order_line'):
            return self._action_modify_order_line(action_type, payload, user)
        elif action_type == 'create_customer':
            return self._action_create_customer(payload, user, sales_rep)
        elif action_type == 'update_order':
            return self._action_update_order(payload, user)
        elif action_type == 'log_promotion':
            return {'skipped': True, 'reason': f'{action_type} not yet supported in sync'}
        elif action_type == 'apply_fixed_discount':
            return self._action_apply_fixed_discount(payload, user, sales_rep)
        elif action_type == 'upload_attachment':
            return self._action_upload_attachment(payload, user)
        elif action_type == 'delete_attachment':
            return self._action_delete_attachment(payload, user)
        elif action_type == 'reorder_route_customers':
            return self._action_reorder_route_customers(payload, user)
        # Field operations that the phone can queue while offline (same code
        # as the online endpoints, replay-safe).
        elif action_type == 'task_update':
            return mobile_ops.task_update(request.env, sales_rep, payload.get('task_id'), payload)
        elif action_type == 'dept_request_create':
            return mobile_ops.dept_request_create(request.env, sales_rep, payload)
        elif action_type in ('dept_request_reply', 'dept_request_cancel'):
            op = 'reply' if action_type == 'dept_request_reply' else 'cancel'
            return mobile_ops.dept_request_op(request.env, sales_rep, payload.get('request_id'), op, payload,
                                              local_id=payload.get('request_local_id'))
        elif action_type in ('workday_start', 'workday_end'):
            return mobile_ops.workday_op(request.env, sales_rep, action_type.split('_')[1], payload, user)
        elif action_type == 'load_accept':
            return mobile_ops.load_accept(request.env, sales_rep, payload.get('picking_id'), payload, replay_safe=True)
        else:
            raise ValueError(f"Unknown action type: {action_type}")

    def _get_order_from_payload(self, payload, user):
        """Helper to resolve sale.order from payload using odoo_id or local_id."""
        order_id = payload.get('order_id')
        local_id = payload.get('local_order_id')

        if order_id and isinstance(order_id, int):
            order = request.env['sale.order'].with_user(user).browse(order_id)
        elif local_id:
            order = request.env['sale.order'].with_user(user).search([('mobile_local_id', '=', local_id)], limit=1)
        else:
            raise ValueError("Missing 'order_id' or 'local_order_id' in payload")

        if not order.exists():
            raise ValueError(f"Order not found (ID: {order_id}, Local: {local_id})")
        return order

    # --- Individual action handlers ---

    def _action_confirm_order(self, payload, user, sales_rep):
        order = self._get_order_from_payload(payload, user)

        if order.state in ('sale', 'done'):
            _logger.info(f"Sync: Order {order.name} is already confirmed (state: {order.state}). Skipping action_confirm.")
        elif order.state == 'cancel':
            _logger.warning(f"Sync: Cannot confirm Order {order.name} because it is cancelled.")
            return {'order_id': order.id, 'state': order.state, 'skipped': True, 'reason': 'cancelled'}
        else:
            order.action_confirm()

        # Auto-delivery + invoice if sales rep has auto_delivery
        if sales_rep and sales_rep.auto_delivery:
            self._auto_deliver_and_invoice(order, user, sales_rep)
        return {'order_id': order.id, 'state': order.state}

    def _action_cancel_order(self, payload, user):
        order = request.env['sale.order'].with_user(user).browse(payload['order_id'])
        if not order.exists():
            raise ValueError('Order not found')
        order.action_cancel()
        return {'order_id': order.id, 'state': order.state}

    def _action_update_order(self, payload, user):
        order_id = payload.get('order_id')
        if not order_id or not isinstance(order_id, int):
            raise ValueError(f"Missing or invalid 'order_id' in payload: {order_id}")
        order = request.env['sale.order'].with_user(user).browse(order_id)
        if not order.exists():
            raise ValueError(f"Order {order_id} not found")
        
        # Prepare valid fields for write
        valid_fields = ['is_cash']
        update_vals = {k: v for k, v in payload.items() if k in valid_fields}
        
        if update_vals:
            order.write(update_vals)
            _logger.info(f"Sync: Updated Order {order.name} with {update_vals}")

            
        return {'order_id': order.id, 'success': True}
    
    def _action_apply_fixed_discount(self, payload, user, sales_rep):
        order = self._get_order_from_payload(payload, user)
        discount_amount = float(payload.get('discount_amount', 0))
        
        # Determine the discount product
        # 1. Check Sales Rep profile
        # 2. Fallback to a generic service product named 'Discount'
        discount_product = sales_rep.discount_product_id
        if not discount_product:
            discount_product = request.env['product.product'].sudo().search([
                ('name', '=', 'Discount'),
                ('type', '=', 'service')
            ], limit=1)
        
        if not discount_product:
             # Last fallback: create one if possible or raise error
             raise ValueError("No discount product configured for sales representative and no generic 'Discount' service found.")

        # Find existing discount line
        discount_line = order.order_line.filtered(lambda l: l.product_id == discount_product)
        
        if discount_amount > 0:
            if discount_line:
                discount_line.write({
                    'product_uom_qty': 1.0,
                    'price_unit': -discount_amount
                })
                _logger.info(f"Sync: Updated discount line on {order.name} to -{discount_amount}")
            else:
                request.env['sale.order.line'].with_user(user).create({
                    'order_id': order.id,
                    'product_id': discount_product.id,
                    'product_uom_qty': 1.0,
                    'price_unit': -discount_amount,
                    'sequence': 999, # Keep it at the end
                })
                _logger.info(f"Sync: Created new discount line on {order.name} for -{discount_amount}")
        else:
            if discount_line:
                discount_line.unlink()
                _logger.info(f"Sync: Removed discount line from {order.name}")
        
        # Update the header discount field as well (for compatibility/visibility)
        order.write({'discount_total': discount_amount})
        
        return {'order_id': order.id, 'success': True}

    def _action_process_delivery(self, payload, user, sales_rep=None):
        order_id = payload.get('order_id')
        if not order_id or not isinstance(order_id, (int, str)):
            raise ValueError(f"Missing or invalid 'order_id' in payload: {order_id}")
        order = request.env['sale.order'].with_user(user).browse(order_id)
        if not order.exists():
            raise ValueError('Order not found')
        
        delivered_quantities = payload.get('delivered_quantities')
        lines_data = payload.get('lines', [])  # [{odoo_line_id, product_id, quantity}, ...]
        create_backorder = payload.get('create_backorder', False)
        
        open_pickings = order.picking_ids.filtered(lambda p: p.picking_type_id.code == 'outgoing' and p.state not in ('done', 'cancel'))
        if not open_pickings:
            return {'skipped': True, 'reason': 'No open outgoing pickings found to process deliveries. It may have already been fully processed or returned.'}

        # Build a lookup from Odoo sale.order.line ID → data from mobile's lines array
        line_data_map = {}  # {sale_order_line_id: {'qty': quantity, 'uom_id': uom_id}}
        for ld in lines_data:
            lid = ld.get('line_id') or ld.get('odoo_line_id')
            if lid:
                line_data_map[int(lid)] = {
                    'qty': float(ld.get('quantity', 0)),
                    'uom_id': ld.get('uom_id')
                }

        has_explicit_quantities = bool(line_data_map or delivered_quantities)

        for picking in open_pickings:
            if picking.state == 'draft':
                picking.action_confirm()
            if picking.state in ('confirmed', 'waiting'):
                picking.action_assign()
            
            for move in picking.move_ids:
                if line_data_map and move.sale_line_id and move.sale_line_id.id in line_data_map:
                    # PRIMARY STRATEGY: Match via sale_line_id (precise, no ambiguity)
                    data = line_data_map[move.sale_line_id.id]
                    qty_raw = data['qty']
                    uom_id = data.get('uom_id')
                    if uom_id:
                        payload_uom = request.env['uom.uom'].sudo().browse(int(uom_id))
                        qty = payload_uom._compute_quantity(qty_raw, move.product_uom)
                        _logger.info(f"Sync: Move {move.id} using payload UoM {payload_uom.name}. Converted {qty_raw} -> {qty} {move.product_uom.name}")
                    elif move.sale_line_id.product_uom != move.product_uom:
                        qty = move.sale_line_id.product_uom._compute_quantity(qty_raw, move.product_uom)
                        _logger.info(f"Sync: Move {move.id} fallback to SO UoM. Converted {qty_raw} -> {qty} {move.product_uom.name}")
                    else:
                        qty = qty_raw
                    _logger.info(f"Sync: Move {move.id} → sale_line_id={move.sale_line_id.id}, delivering {qty}")
                elif delivered_quantities and str(move.product_id.id) in delivered_quantities:
                    # FALLBACK: Use product-keyed delivered_quantities (assumed in Ref UoM as per delivery.tsx)
                    pid = str(move.product_id.id)
                    qty_ref = float(delivered_quantities.get(pid, 0))
                    # delivered_quantities from delivery.tsx are in Reference UoM (factor=1)
                    # We need to convert from product.uom_id (Ref UoM) to move.product_uom
                    qty = move.product_id.uom_id._compute_quantity(qty_ref, move.product_uom)
                    _logger.info(f"Sync: Fallback delivery for product {pid}: {qty_ref} RefQty -> {qty} {move.product_uom.name}")
                elif has_explicit_quantities:
                    # If we have explicit quantities but this line/product was not included, it means 0 delivered
                    qty = 0.0
                    _logger.info(f"Sync: Move {move.id} (product {move.product_id.id}) not in payload. Setting quantity to 0.0")
                else:
                    # No specific quantities provided = deliver everything
                    qty = move.product_uom_qty
                
                move.sudo().write({'quantity': qty, 'picked': True if qty > 0 else False})

                # Force-set move line quantities
                if qty > 0:
                    if move.move_line_ids:
                        # Update first line, set others to 0
                        first_ml = move.move_line_ids[0]
                        first_ml.sudo().write({
                            'quantity': qty,
                            'product_uom_id': move.product_uom.id
                        })
                        if len(move.move_line_ids) > 1:
                            for extra_ml in move.move_line_ids[1:]:
                                extra_ml.sudo().write({'quantity': 0})
                    else:
                        # Create new line
                        request.env['stock.move.line'].sudo().create({
                            'move_id': move.id,
                            'picking_id': picking.id,
                            'product_id': move.product_id.id,
                            'product_uom_id': move.product_uom.id,
                            'location_id': move.location_id.id,
                            'location_dest_id': move.location_dest_id.id,
                            'quantity': qty,
                        })
            
            request.env.flush_all()
            # If create_backorder is False, we skip; if True, we allow Odoo to create it.
            # Odoo 17 might still return a wizard action if skip_backorder is not True.
            # Usually, to fully automate it, we want to avoid the wizard.
            context = {'skip_sms': True}
            # We must NOT set skip_backorder=True here without picking_ids_not_to_backorder.
            # Otherwise Odoo 17 suppresses the wizard and implicitly creates a backorder.
            self._assert_van_stock(picking)

            res = picking.with_context(**context).button_validate()
            
            # If Odoo returns a wizard (dict), automatically confirm or cancel the backorder.
            if isinstance(res, dict) and res.get('res_model') == 'stock.backorder.confirmation':
                wizard = request.env['stock.backorder.confirmation'].with_context(res['context']).create({'pick_ids': [picking.id]})
                if create_backorder:
                    wizard.process()
                else:
                    wizard.process_cancel_backorder()

        request.env.flush_all()
        try:
            # Force recompute of qty_delivered on the specific order lines
            # This is needed so create_invoice (if queued right after) sees the fresh quantities
            lines = order.sudo().order_line
            lines.invalidate_recordset(['qty_delivered'])
            lines._compute_qty_delivered()
            # Also recompute invoice_status on the order itself
            order.invalidate_recordset(['invoice_status'])
            order._compute_get_invoiced()
            request.env.flush_all()
        except Exception as recompute_err:
            _logger.warning(f"Sync: qty_delivered recompute warning: {recompute_err}")
            try:
                request.env['sale.order.line'].invalidate_model(['qty_delivered'])
            except Exception:
                pass
        # AUTOMATIC INVOICING: If delivery was successful, trigger invoice creation
        try:
            _logger.info(f"Sync: Auto-triggering invoice for {order.name} after successful delivery action.")
            # We don't pass action_id here to avoid blocking the real create_invoice action if it's also in the queue
            self._action_create_invoice({'order_id': order.id}, user, sales_rep)
        except Exception as inv_err:
            _logger.warning(f"Sync: Auto-invoicing failed for {order.name} after delivery: {inv_err}")

        return {'order_id': order.id}

    def _action_create_invoice(self, payload, user, sales_rep, action_id=None):
        order_id = payload.get('order_id')
        if not order_id or not isinstance(order_id, (int, str)):
            raise ValueError(f"Missing or invalid 'order_id' in payload: {order_id}")
        order = request.env['sale.order'].with_user(user).browse(order_id)
        if not order.exists():
            raise ValueError('Order not found')
        
        # Idempotency Check: Check if this specific action already created an invoice
        if action_id and order.invoice_ids:
            matched_invoice = order.invoice_ids.filtered(lambda inv: inv.mobile_local_id == action_id)
            if matched_invoice:
                latest_invoice = matched_invoice[0]
                _logger.info(f"Sync: _action_create_invoice skipped for {order.name}, already executed action {action_id}")
                return {'invoice_id': latest_invoice.id, 'name': latest_invoice.name, 'skipped': True, 'reason': 'Action already executed'}

        # Ensure Odoo's computed invoice_status is up to date in this transaction
        try:
            order.invalidate_recordset(['invoice_status'])
            order._compute_get_invoiced()
        except Exception:
            pass

        # If the order is already fully invoiced according to Odoo
        if order.invoice_status == 'invoiced':
             valid_invoices = order.invoice_ids.filtered(lambda inv: inv.state != 'cancel')
             if valid_invoices:
                 latest_invoice = valid_invoices[0]
                 # If we hit an auto-invoiced order that lacked our action ID but is fully invoiced
                 if action_id and not latest_invoice.mobile_local_id:
                     latest_invoice.sudo().write({'mobile_local_id': action_id})
                 _logger.info(f"Sync: _action_create_invoice skipped for {order.name}, order is fully invoiced")
                 return {'invoice_id': latest_invoice.id, 'name': latest_invoice.name, 'skipped': True, 'reason': 'Fully invoiced'}


        try:
            invoices = order._create_invoices()
            if invoices:
                if action_id:
                    invoices[0].sudo().write({'mobile_local_id': action_id})
                invoices[0].action_post()
                
                # Auto-pay for Cash Orders - ONLY if the order itself is an explicit cash order with no prior payments
                if order.is_cash:
                    if invoices[0].payment_state not in ('paid', 'in_payment') and invoices[0].amount_residual == invoices[0].amount_total:
                        _logger.info(f"Sync: Order {order.name} is CASH with no prior payments, triggering auto-payment for invoice {invoices[0].name}")
                        self._reconcile_cash_invoice(invoices[0], order, sales_rep)
                    else:
                        _logger.info(f"Sync: Order {order.name} is CASH but has partial payments or is already paid, skipping full auto-payment")
                else:
                    _logger.info(f"Sync: Order {order.name} is CREDIT, skipping auto-payment/reconciliation as requested.")

                try:
                    # Force recompute of qty_invoiced globally
                    request.env['sale.order.line'].invalidate_model(['qty_invoiced'])
                except Exception:
                    pass
                return {'invoice_id': invoices[0].id, 'name': invoices[0].name}
        except Exception as e:
         # 2nd Layer Idempotency: "No items to invoice" might mean it was just done.
         # Double check if an invoice exists now (race condition?)
         request.env.invalidate_all() # invalidate cache to see fresh DB state
         if action_id and order.invoice_ids:
             matched_invoice = order.invoice_ids.filtered(lambda inv: inv.mobile_local_id == action_id)
             if matched_invoice:
                 latest_invoice = matched_invoice[0]
                 return {'invoice_id': latest_invoice.id, 'name': latest_invoice.name, 'skipped': True, 'reason': 'Action already executed (race detected)'}

         if order.invoice_status == 'invoiced' and order.invoice_ids:
              valid_invoices = order.invoice_ids.filtered(lambda inv: inv.state != 'cancel')
              if valid_invoices:
                  latest_invoice = valid_invoices[0]
                  if action_id and not latest_invoice.mobile_local_id:
                      latest_invoice.sudo().write({'mobile_local_id': action_id})
                  return {'invoice_id': latest_invoice.id, 'name': latest_invoice.name, 'skipped': True, 'reason': 'Fully invoiced (race detected)'}

         # If it's literally "nothing to invoice" but not because it's fully invoiced (e.g. nothing delivered yet), just pass it back
         try:
             error_str = str(e).lower()
             if "no invoiceable line" in error_str or "nothing to invoice" in error_str or "no items are available to invoice" in error_str:
                 _logger.info(f"Sync: _action_create_invoice skipped for {order.name}, nothing to invoice (already done or policy restriction)")
                 return {'skipped': True, 'reason': 'Nothing to invoice'}
         except Exception:
             pass
         raise e
         
        return {'skipped': True, 'reason': 'Nothing to invoice'}

    def _action_register_payment(self, payload, user, sales_rep, action_id=None):
        check_ids = [i for i in [action_id, payload.get('local_id')] if i]
        if check_ids:
            existing_payment = request.env['account.payment'].sudo().search([('mobile_local_id', 'in', check_ids)], limit=1)
            if existing_payment:
                _logger.info(f"Sync: Register payment action {check_ids} already executed (found {existing_payment.name})")
                return {'payment_id': existing_payment.id, 'name': existing_payment.name, 'skipped': True, 'reason': 'Action already executed'}

        invoice_id = payload.get('invoice_id')
        if not invoice_id or not isinstance(invoice_id, int):
            raise ValueError(f"Missing or invalid 'invoice_id' in payload: {invoice_id}")
        invoice = request.env['account.move'].with_user(user).browse(invoice_id)
        if not invoice.exists():
            raise ValueError('Invoice not found')
        if invoice.payment_state == 'paid':
            return {'skipped': True, 'reason': 'Already paid'}
        amount = float(payload.get('amount', invoice.amount_residual))
        if amount > invoice.amount_residual:
            amount = invoice.amount_residual
        # Resolve Journal from Payment Method if provided (More reliable than frontend journal_id)
        journal_id = int(payload['journal_id'])
        if payload.get('payment_method_id'):
            pm = request.env['sales.rep.payment.method'].sudo().browse(payload['payment_method_id'])
            if pm.exists() and pm.journal_id:
                journal_id = pm.journal_id.id
                _logger.info(f"Sync: Resolved Journal {pm.journal_id.name} from Payment Method {pm.name}")

        j_rec = request.env['account.journal'].sudo().browse(journal_id)
        if j_rec.exists() and j_rec.default_account_id:
            for pml in j_rec.inbound_payment_method_line_ids:
                if not pml.payment_account_id:
                    try:
                        pml.write({'payment_account_id': j_rec.default_account_id.id})
                    except Exception:
                        pass

        wiz = request.env['account.payment.register'].sudo().with_context(
            active_model='account.move', active_ids=[invoice.id]
        ).create({
            'journal_id': journal_id,
            'amount': amount,
            'payment_date': fields.Date.today(),
        })
        payment = wiz._create_payments()

        target_local_id = payload.get('local_id') or action_id
        if target_local_id and payment:
            payment.write({
                'mobile_local_id': target_local_id,
                'sales_rep_id': sales_rep.id if sales_rep else False
            })
        elif payment and sales_rep:
            payment.write({'sales_rep_id': sales_rep.id})

        if payment and hasattr(payment, 'action_validate') and payment.state == 'in_process':
            try:
                payment.action_validate()
                _logger.info(f"Sync: Auto-validated payment {payment.name}")
            except Exception as ve:
                _logger.warning(f"Sync: Could not auto-validate payment {payment.name}: {ve}")

        # Force Reconciliation (Fix for 'Outstanding Credits' issue)
        if invoice.payment_state not in ('paid', 'in_payment'):
             # Find receivable lines of the invoice
             inv_receivable_lines = invoice.line_ids.filtered(lambda r: r.account_id.account_type == 'asset_receivable' and not r.reconciled)
             if inv_receivable_lines:
                 # Find matching payment line by account
                 matching_pay_lines = payment.line_ids.filtered(lambda l: l.account_id == inv_receivable_lines[0].account_id and not l.reconciled)
                 if matching_pay_lines:
                     try:
                        (inv_receivable_lines + matching_pay_lines).reconcile()
                        _logger.info(f"Sync: Forced reconciliation for Invoice {invoice.name} with Payment {payment.name}")
                     except Exception as e:
                        _logger.warning(f"Sync: Failed to force reconciliation: {e}")

        return {'payment_id': payment.id if payment else False}

    def _reconcile_payment(self, payment=None, order_id=None, invoice=None):
        """ Automatically reconcile a payment with open invoices, or an invoice with open payments """
        if not payment and not invoice:
            return

        if payment:
            if payment.state != 'posted':
                return
            _logger.info(f"Sync: Auto-reconcile Payment {payment.name} (Amount: {payment.amount})")
            
            # Find receivable lines of the payment
            pay_lines = payment.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
            if not pay_lines:
                _logger.info(f"Sync: No unreconciled receivable lines for payment {payment.name}")
                return
            # Separate the specific order invoices from other partner invoices
            order_invoices = request.env['account.move'].sudo()
            if order_id:
                try:
                    order = request.env['sale.order'].sudo().browse(int(order_id))
                    if order.exists():
                        # Detailed debug logs for order invoices
                        all_order_invs = order.invoice_ids
                        _logger.info(f"Sync: Order {order.name} has {len(all_order_invs)} total invoices: {all_order_invs.mapped('name')}")
                        order_invoices = all_order_invs.filtered(
                            lambda i: i.state == 'posted' and i.move_type == 'out_invoice' and i.payment_state in ('not_paid', 'partial')
                        )
                        _logger.info(f"Sync: Found {len(order_invoices)} open invoices specifically for order {order.name}")
                except Exception as e:
                    _logger.error(f"Sync: Error finding order invoices for reconciliation: {e}")

            # RESTRICTION: Only reconcile if we have an order_id and found invoices for it.
            # If no order_id is provided, it's a general payment - do not auto-reconcile.
            if not order_id:
                _logger.info(f"Sync: General payment {payment.name} - skipping auto-reconciliation as requested.")
                return

            if not order_invoices:
                _logger.info(f"Sync: No open invoices found for order {order_id} - payment {payment.name} remains outstanding.")
                return

            # Only target order-specific invoices
            for inv in order_invoices:
                if not pay_lines: break
                inv_receivable_lines = inv.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
                if inv_receivable_lines:
                    matched_pay_line = pay_lines.filtered(lambda l: l.account_id == inv_receivable_lines[0].account_id)
                    if matched_pay_line:
                        try:
                            (inv_receivable_lines + matched_pay_line).reconcile()
                            _logger.info(f"Sync: Successfully reconciled {payment.name} with {inv.name}")
                            # Refresh pay_lines after reconciliation
                            pay_lines = payment.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
                        except Exception as e:
                            _logger.error(f"Sync: Reconciliation error for {inv.name}: {e}")
            return

        elif invoice:
            if invoice.state != 'posted' or invoice.payment_state not in ('not_paid', 'partial'):
                return
            _logger.info(f"Sync: Auto-reconcile Invoice {invoice.name} against outstanding credits")
            
            # Find outstanding payments for this partner
            # We look for posted payments that are not fully reconciled
            domain = [
                ('partner_id', '=', invoice.partner_id.id),
                ('is_reconciled', '=', False),
                ('state', '=', 'posted'),
                ('payment_type', '=', 'inbound'),
            ]
            # Prioritize payments from the same order/visit if possible
            if order_id:
                # Odoo account.payment doesn't have a direct link to sale_order usually, 
                # but our custom ones have visit_id and mobile_local_id.
                # Here we just look at all outstanding for the partner to be safe.
                pass
                
            payments = request.env['account.payment'].sudo().search(domain, order='date asc, id asc')
            inv_receivable_lines = invoice.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
            
            for pay in payments:
                if not inv_receivable_lines: break
                pay_lines = pay.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
                if pay_lines:
                    matched_pay_line = pay_lines.filtered(lambda l: l.account_id == inv_receivable_lines[0].account_id)
                    if matched_pay_line:
                        try:
                            _logger.info(f"Sync: Linking outstanding payment {pay.name} to invoice {invoice.name}")
                            (inv_receivable_lines + matched_pay_line).reconcile()
                            inv_receivable_lines = invoice.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
                        except Exception as e:
                            _logger.error(f"Sync: Reconciliation error for {invoice.name}: {e}")

    def _reconcile_cash_invoice(self, invoice, order, sales_rep):
        """Automatically register payment for a cash invoice ONLY if strictly a cash order and invoice is fully unpaid"""
        try:
            # 0. Strictly ensure order is cash and invoice is completely unpaid
            if not (order and order.is_cash):
                _logger.info(f"Sync: Skipping _reconcile_cash_invoice: Order {order.name if order else 'N/A'} is not CASH")
                return False

            if invoice.payment_state in ('paid', 'in_payment'):
                _logger.info(f"Sync: Skipping _reconcile_cash_invoice: Invoice {invoice.name} is already {invoice.payment_state}")
                return False

            # If invoice was already partially paid (amount_residual < amount_total), DO NOT auto-pay remaining residual!
            if invoice.amount_residual < invoice.amount_total:
                _logger.info(f"Sync: Skipping _reconcile_cash_invoice: Invoice {invoice.name} has partial payments (Residual: {invoice.amount_residual} / {invoice.amount_total}), leaving residual open")
                return False

            # 1. Find a cash journal - Check Sales Rep's payment methods
            journal = False
            for pm in sales_rep.payment_method_ids:
                if pm.payment_type == 'cash' and pm.journal_id:
                    journal = pm.journal_id
                    break
            
            # Fallback to first available journal from payment methods if no explicit 'cash' type
            if not journal and sales_rep.payment_method_ids:
                journal = sales_rep.payment_method_ids[0].journal_id
            
            # Fallback to company default cash journal
            if not journal:
                journal = request.env['account.journal'].sudo().search([
                    ('type', '=', 'cash'), 
                    ('company_id', '=', sales_rep.company_id.id)
                ], limit=1)
            
            if not journal:
                _logger.warning(f"Sync: Could not find cash journal for auto-payment on invoice {invoice.name}")
                return False

            _logger.info(f"Sync: Auto-paying Cash Invoice {invoice.name} via Journal {journal.name}")

            # 2. Register Payment
            wiz = request.env['account.payment.register'].sudo().with_context(
                active_model='account.move', active_ids=[invoice.id]
            ).create({
                'journal_id': journal.id,
                'amount': invoice.amount_residual,
                'payment_date': fields.Date.today(),
            })
            payment = wiz._create_payments()
            if payment and sales_rep:
                payment.write({'sales_rep_id': sales_rep.id})

            # 3. Force Reconciliation
            inv_receivable_lines = invoice.line_ids.filtered(lambda r: r.account_id.account_type == 'asset_receivable' and not r.reconciled)
            if inv_receivable_lines and payment:
                pay_lines = payment.line_ids.filtered(lambda l: l.account_id == inv_receivable_lines[0].account_id and not l.reconciled)
                if pay_lines:
                    (inv_receivable_lines + pay_lines).reconcile()
                    _logger.info(f"Sync: Successfully auto-reconciled Cash Invoice {invoice.name}")
            return True
        except Exception as e:
            _logger.error(f"Sync: Failed to auto-reconcile cash invoice {invoice.name}: {e}")
            return False

    def _reconcile_refund_logic(self, refund, invoice, order):
        """Logic to reconcile refund with invoice based on order type and invoice status"""
        try:
            is_cash_order = order.partner_id.is_cash if (order and order.partner_id) else False
            # Ensure we have the latest payment state from DB
            invoice.invalidate_recordset(['payment_state'])
            invoice.sudo().read(['payment_state'])
            
            # Condition for auto-reconciliation: 
            # 1. It's a Cash Customer (Always reconcile)
            # 2. It's a Credit Customer AND the original invoice is UNPAID (reconcile to settle the invoice)
            should_reconcile = is_cash_order or (invoice.payment_state in ('not_paid', 'partial'))
            
            _logger.info(f"Sync: Refund Logic for {refund.name} (ID: {refund.id})")
            _logger.info(f"Sync:   - Order: {order.name if order else 'N/A'} (IsCash: {is_cash_order})")
            _logger.info(f"Sync:   - Invoice: {invoice.name} (PaymentState: {invoice.payment_state})")
            _logger.info(f"Sync:   - Decision: should_reconcile={should_reconcile}")
            
            if should_reconcile:
                refund_recv = refund.line_ids.sudo().filtered(
                    lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled
                )[:1]
                if refund_recv:
                    inv_recv = invoice.line_ids.sudo().filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
                    if inv_recv:
                        (inv_recv + refund_recv).sudo().reconcile()
                        _logger.info(f"Sync: Auto-reconciled refund {refund.name} with invoice {invoice.name}")
                    
                    # Try other open invoices if still remaining
                    if refund_recv.amount_residual != 0:
                        open_invoices = request.env['account.move'].sudo().search([
                            ('partner_id', '=', refund.partner_id.id),
                            ('state', '=', 'posted'),
                            ('move_type', '=', 'out_invoice'),
                            ('payment_state', 'in', ('not_paid', 'partial')),
                            ('id', '!=', invoice.id)
                        ], order='invoice_date asc, id asc')
                        for inv in open_invoices:
                            if refund_recv.reconciled or refund_recv.amount_residual == 0:
                                break
                            inv_recv_lines = inv.line_ids.filtered(lambda l: l.account_id.account_type == 'asset_receivable' and not l.reconciled)
                            if inv_recv_lines:
                                (inv_recv_lines + refund_recv).sudo().reconcile()
                                _logger.info(f"Sync: Reconciled remaining refund {refund.name} with extra invoice {inv.name}")
            else:
                # Credit Order + PAID/IN_PAYMENT Invoice -> Unreconcile (Refund becomes Not Paid)
                if not is_cash_order and invoice.payment_state in ('paid', 'in_payment'):
                    _logger.info(f"Sync: Force unreconciling refund {refund.name} to keep it Open/Not Paid for Credit Order")
                    # Target only the receivable lines to be precise
                    receivable_lines = refund.line_ids.sudo().filtered(lambda l: l.account_id.account_type == 'asset_receivable')
                    if receivable_lines:
                        receivable_lines.remove_move_reconcile()
                        _logger.info(f"Sync: remove_move_reconcile called on {len(receivable_lines)} lines")
        except Exception as e:
            _logger.warning(f"Sync: Refund reconciliation logic failed for {refund.name}: {e}")

    def _action_create_payment(self, payload, user, sales_rep, action_id=None):
        _logger.info(f"Sync: Starting _action_create_payment with payload: {payload}")
        partner_id = payload.get('partner_id')
        partner_local_id = payload.get('partner_local_id')
        journal_id = payload.get('journal_id')
        amount = payload.get('amount')
        visit_id = payload.get('visit_id')
        route_id = payload.get('route_id')
        route_customer_id = payload.get('route_customer_id')
        
        if (not partner_id and not partner_local_id) or amount is None:
            _logger.error(f"Sync: Missing required payment info: partner={partner_id}, partner_local_id={partner_local_id}, amount={amount}")
            return {'error': f"Missing required payment info: partner={partner_id}, amount={amount}"}

        # Ensure integer types and basic records
        partner = False
        if partner_id:
            try:
                partner_id = int(partner_id)
                partner = request.env['res.partner'].sudo().browse(partner_id)
                if not partner.exists():
                    _logger.warning(f"Sync: Partner {partner_id} not found, will try partner_local_id fallback")
                    partner = False
                    partner_id = False
            except (ValueError, TypeError) as e:
                _logger.warning(f"Sync: Invalid partner ID type: {partner_id}. Error: {e}. Will try partner_local_id fallback")
                partner = False
                partner_id = False

        # Fallback: resolve via partner_local_id (mobile_local_id on res.partner)
        if not partner and partner_local_id:
            partner = request.env['res.partner'].sudo().search([('mobile_local_id', '=', partner_local_id)], limit=1)
            if partner:
                partner_id = partner.id
                _logger.info(f"Sync: Resolved partner via partner_local_id {partner_local_id} -> {partner_id}")

        # Fallback 2: resolve via order_local_id / order_id
        order_local_id = payload.get('order_local_id') or payload.get('order_id')
        if not partner and order_local_id:
            order_rec = request.env['sale.order'].sudo().search([('mobile_local_id', '=', str(order_local_id))], limit=1)
            if not order_rec and str(order_local_id).isdigit():
                order_rec = request.env['sale.order'].sudo().browse(int(order_local_id))
            if order_rec and order_rec.exists() and order_rec.partner_id:
                partner = order_rec.partner_id
                partner_id = partner.id
                if not route_customer_id and order_rec.route_customer_id:
                    route_customer_id = order_rec.route_customer_id.id
                if not visit_id and order_rec.visit_id:
                    visit_id = order_rec.visit_id.id
                _logger.info(f"Sync: Resolved partner {partner.name} (ID: {partner_id}) via order {order_rec.name}")

        # Fallback 3: resolve via route_customer_id / local_route_customer_id
        # Never reuse partner_id as a route-customer id: ids of different models can collide.
        local_rc_id = payload.get('local_route_customer_id') or route_customer_id
        if not partner and local_rc_id:
            try:
                rc = request.env['sales.route.customer'].sudo().browse(int(local_rc_id))
                if rc.exists() and rc.partner_id:
                    partner = rc.partner_id
                    partner_id = partner.id
                    route_customer_id = rc.id
                    _logger.info(f"Sync: Resolved partner {partner.name} (ID: {partner_id}) via route_customer {rc.id}")
            except Exception:
                pass

        if not partner:
            _logger.error(f"Sync: Partner {partner_id} not found and no partner fallback available")
            return {'error': f"Partner {partner_id} not found"}

        # Fallback resolution for visit_id/route_id from route_customer_id
        if not visit_id and route_customer_id:
            try:
                customer = request.env['sales.route.customer'].sudo().browse(int(route_customer_id))
                if customer.exists() and customer.visit_id:
                    visit_id = customer.visit_id.id
                    if not route_id and customer.route_id:
                        route_id = customer.route_id.id
            except Exception: pass

        # Deduplication check
        local_id = payload.get('local_id') or action_id
        check_ids = [i for i in [local_id, action_id, payload.get('local_id')] if i]
        if check_ids:
            existing_payment = request.env['account.payment'].sudo().search([('mobile_local_id', 'in', check_ids)], limit=1)
            if existing_payment:
                _logger.info(f"Sync: Payment with local_id in {check_ids} already exists ({existing_payment.name}). Skipping.")
                return {'payment_id': existing_payment.id, 'name': existing_payment.name, 'skipped': True}

        # 1. Resolve Journal and Payment Method Line
        rep_payment_method = False
        payment_method_id = payload.get('payment_method_id')
        if payment_method_id:
            rep_payment_method = request.env['sales.rep.payment.method'].sudo().browse(int(payment_method_id))
        
        journal = False
        if rep_payment_method and rep_payment_method.journal_id:
            journal = rep_payment_method.journal_id
            _logger.info(f"Sync: Using journal {journal.name} from payment method {rep_payment_method.name}")
        
        if not journal and journal_id:
            journal = request.env['account.journal'].sudo().browse(int(journal_id))
            
        if not journal or not journal.exists():
            # Fallback to a default journal based on company
            journal_domain = [('type', 'in', ['bank', 'cash']), ('company_id', '=', partner.company_id.id)]
            journal = request.env['account.journal'].sudo().search(journal_domain, limit=1)

        if not journal:
            _logger.error(f"Sync: Could not resolve journal for payment. Payload: {payload}")
            return {'error': 'Could not resolve accounting journal'}

        # Resolve Payment Method Line (Odoo 17 REQUIREMENT)
        pm_domain = [('journal_id', '=', journal.id), ('payment_type', '=', 'inbound')]
        pm_lines = request.env['account.payment.method.line'].sudo().search(pm_domain)
        
        payment_method_line = False
        if rep_payment_method:
            target_code = 'manual' 
            if rep_payment_method.payment_type == 'cash':
                target_code = 'manual'
            elif rep_payment_method.payment_type == 'bank':
                target_code = 'manual' 
            
            payment_method_line = pm_lines.filtered(lambda l: l.code == target_code)[:1]
        
        if not payment_method_line:
            payment_method_line = pm_lines[:1]

        if not payment_method_line:
            _logger.error(f"Sync: No inbound payment method line found for journal {journal.name}")
            return {'error': f"No inbound payment method configured for journal {journal.name}"}

        _logger.info(f"Sync: Final Payment Resolution - Journal: {journal.name} (ID: {journal.id}), Method: {payment_method_line.name} (ID: {payment_method_line.id})")

        vals = {
            'partner_id': partner.id,
            'journal_id': journal.id,
            'payment_method_line_id': payment_method_line.id,
            'amount': float(amount),
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'date': payload.get('collection_date') or fields.Date.today(),
            'memo': payload.get('memo', payload.get('ref', '')),
            'visit_id': visit_id or False,
            'route_id': route_id or False,
            'route_customer_id': route_customer_id or False,
            'mobile_local_id': local_id or False,
            'sales_rep_id': sales_rep.id if sales_rep else False,
        }
        
        # 1. Official "Register Payment" Flow
        order_id = payload.get('order_id')
        invoice_id = payload.get('invoice_id')
        payment = False
        target_invoice = False

        if invoice_id:
            try:
                inv_rec = request.env['account.move'].sudo().browse(int(invoice_id))
                if inv_rec.exists() and inv_rec.state == 'posted' and inv_rec.payment_state in ('not_paid', 'partial'):
                    target_invoice = inv_rec
            except Exception:
                pass

        if not target_invoice and order_id:
            try:
                oid = int(order_id) if str(order_id).isdigit() else False
                if oid:
                    order = request.env['sale.order'].sudo().browse(oid)
                    if order.exists():
                        open_invoices = order.invoice_ids.filtered(lambda i: i.state == 'posted' and i.payment_state in ('not_paid', 'partial'))
                        if open_invoices:
                            target_invoice = open_invoices[0]
            except Exception:
                pass

        if journal and journal.default_account_id:
            for pml in journal.inbound_payment_method_line_ids:
                if not pml.payment_account_id:
                    try:
                        pml.sudo().write({'payment_account_id': journal.default_account_id.id})
                    except Exception:
                        pass

        if target_invoice:
            try:
                pay_amount = min(float(amount), target_invoice.amount_residual)
                _logger.info(f"Sync: Using Register Payment Wizard for invoice {target_invoice.name} (Amount: {pay_amount} / Residual: {target_invoice.amount_residual})")
                wiz_ctx = {'active_model': 'account.move', 'active_ids': [target_invoice.id]}
                wiz = request.env['account.payment.register'].sudo().with_user(user).with_context(wiz_ctx).create({
                    'journal_id': journal.id,
                    'amount': pay_amount,
                    'payment_date': vals['date'],
                    'communication': vals['memo'] or target_invoice.name,
                })
                payment = wiz._create_payments()
            except Exception as e:
                _logger.error(f"Sync: Failed official registration flow, fallback to standalone: {e}")

        # 2. Standalone Payment Flow (Fallback)
        if not payment:
            _logger.info(f"Sync: Creating standalone payment record")
            payment = request.env['account.payment'].sudo().with_user(user).create([vals])[0]
            payment.action_post()

        # Final decoration and reconciliation
        if payment:
            # Re-ensure local_id and other fields are set (especially if created via wizard)
            payment.sudo().write({
                'mobile_local_id': local_id or payment.mobile_local_id,
                'visit_id': visit_id or payment.visit_id.id or False,
                'route_id': route_id or payment.route_id.id or False,
                'route_customer_id': route_customer_id or payment.route_customer_id.id or False,
            })
            if hasattr(payment, 'action_validate') and payment.state == 'in_process':
                try:
                    payment.action_validate()
                    _logger.info(f"Sync: Auto-validated payment {payment.name}")
                except Exception as ve:
                    _logger.warning(f"Sync: Could not auto-validate payment {payment.name}: {ve}")
            if payload.get('receipt_image_base64'):
                _logger.info(f"Sync: Attaching receipt image to payment {payment.name}")
                payment.sudo().write({
                    'receipt_image': payload.get('receipt_image_base64'),
                    'receipt_filename': f"receipt_{local_id or payment.id}.jpg"
                })
            # 3. Create Custom Collection Record (for rep reports and balance calculation)
            # This links the accounting payment back to our rep management logic
            # collection = False
            # if not visit_id:
            #     # We need a visit for the collection model
            #     # This could happen for standalone payments not linked to routes
            #     # For now, we skip creation if no visit exists to avoid validation error, 
            #     # but in practice, mobile sync always provides visit_id or visit_local_id
            #     _logger.warning(f"Sync: No visit_id resolved for collection {local_id}. Cannot create sales.rep.collection record.")
            # else:
            #     try:
            #         # Deduplication for collection record
            #         collection = request.env['sales.rep.collection'].sudo().search([('mobile_local_id', '=', local_id)], limit=1)
            #         if not collection:
            #             col_vals = {
            #                 'visit_id': visit_id,
            #                 'route_id': route_id,
            #                 'route_customer_id': route_customer_id,
            #                 'partner_id': partner.id,
            #                 'amount': float(amount),
            #                 'payment_method': payload.get('payment_method', 'cash'),
            #                 'payment_method_id': rep_payment_method.id if rep_payment_method else False,
            #                 'payment_reference': payload.get('payment_reference'),
            #                 'collection_date': payload.get('collection_date') or fields.Datetime.now(),
            #                 'mobile_local_id': local_id,
            #                 'state': 'confirmed',  # Auto-confirm when synced
            #                 'payment_id': payment[0].id,
            #             }
            #             collection = request.env['sales.rep.collection'].sudo().create(col_vals)
            #             _logger.info(f"Sync: Created sales.rep.collection record {collection.name} for payment {payment[0].name}")
            #     except Exception as col_err:
            #         _logger.error(f"Sync: Failed to create sales.rep.collection: {col_err}")

            return {
                'payment_id': payment[0].id if payment else False, 
                'name': payment[0].name if payment else False,
                'collection_odoo_id': False
            }
            
        return {'error': 'Failed to create payment record'}



    def _adjust_credit_note_lines_for_return(self, credit_note, original_invoice, return_moves_for_order=None, return_lines=None, sale_lines_by_product=None):
        """
        Adjusts lines on a draft credit note created from original_invoice to reflect returned items,
        properly preserving and scaling line-level discounts and order-level promotion/discount lines.
        """
        if not credit_note or credit_note.state != 'draft':
            return

        # 1. Build map of sale order lines for traceability if not provided
        if sale_lines_by_product is None:
            sale_lines_by_product = {}
            for orig_line in original_invoice.invoice_line_ids:
                for sl in orig_line.sale_line_ids:
                    if sl.product_id.id not in sale_lines_by_product:
                        sale_lines_by_product[sl.product_id.id] = request.env['sale.order.line']
                    sale_lines_by_product[sl.product_id.id] |= sl

        # 2. Build returned quantities map by product_id
        ret_qty_by_product = {}
        if return_moves_for_order:
            for mv in return_moves_for_order:
                pid = mv.product_id.id
                ret_qty_by_product[pid] = ret_qty_by_product.get(pid, 0.0) + mv.quantity
        elif return_lines:
            for rl in return_lines:
                pid = int(rl.get('product_id', 0))
                if pid:
                    ret_qty_by_product[pid] = ret_qty_by_product.get(pid, 0.0) + float(rl.get('quantity', 0.0))

        # 3. Calculate original positive product amount (gross total of tangible products)
        orig_product_lines = original_invoice.invoice_line_ids.filtered(
            lambda l: l.display_type not in ('line_section', 'line_note') and (l.price_subtotal > 0 or l.price_unit > 0)
        )
        total_orig_product_amount = sum(l.price_subtotal for l in orig_product_lines)
        if total_orig_product_amount <= 0:
            total_orig_product_amount = sum(l.quantity * l.price_unit for l in orig_product_lines)

        # 4. Adjust physical product lines on credit note
        for cn_line in credit_note.invoice_line_ids:
            if cn_line.display_type in ('line_section', 'line_note'):
                continue

            pid = cn_line.product_id.id if cn_line.product_id else None
            # Set traceability
            if pid and pid in sale_lines_by_product:
                cn_line.sale_line_ids = [(6, 0, sale_lines_by_product[pid].ids)]

            # Check if this is a regular product line (positive price / subtotal)
            if cn_line.price_unit >= 0 and cn_line.price_subtotal >= 0:
                if pid and pid in ret_qty_by_product:
                    ret_qty = ret_qty_by_product[pid]
                    if ret_qty > 0:
                        take_qty = min(ret_qty, cn_line.quantity) if cn_line.quantity > 0 else ret_qty
                        cn_line.quantity = take_qty
                        ret_qty_by_product[pid] = max(0.0, ret_qty - take_qty)
                    else:
                        cn_line.quantity = 0.0
                else:
                    cn_line.quantity = 0.0

        # 5. Calculate returned product amount on credit note
        cn_returned_product_lines = credit_note.invoice_line_ids.filtered(
            lambda l: l.display_type not in ('line_section', 'line_note') and l.price_unit >= 0 and l.quantity > 0
        )
        returned_product_amount = sum(
            l.quantity * l.price_unit * (1.0 - (l.discount or 0.0) / 100.0)
            for l in cn_returned_product_lines
        )

        # 6. Calculate return ratio for scaling promotion discounts
        if total_orig_product_amount > 0:
            return_ratio = min(1.0, max(0.0, returned_product_amount / total_orig_product_amount))
        else:
            return_ratio = 1.0 if bool(cn_returned_product_lines) else 0.0

        # 7. Adjust discount / promotion lines (negative price or subtotal)
        for cn_line in credit_note.invoice_line_ids:
            if cn_line.display_type in ('line_section', 'line_note'):
                continue

            if cn_line.price_unit < 0 or cn_line.price_subtotal < 0:
                if return_ratio > 0:
                    # Find corresponding original invoice line to get initial base unit price
                    orig_disc = original_invoice.invoice_line_ids.filtered(
                        lambda l: l.product_id == cn_line.product_id and l.price_unit < 0
                    )[:1]
                    base_price = orig_disc.price_unit if orig_disc else cn_line.price_unit
                    cn_line.quantity = 1.0
                    cn_line.price_unit = round(base_price * return_ratio, 2)
                    _logger.info(f"Sync: Scaled promotion discount line '{cn_line.name}' to price_unit {cn_line.price_unit} (ratio: {return_ratio:.4f})")
                else:
                    cn_line.quantity = 0.0

        # 8. Unlink lines with zero quantity
        lines_to_remove = credit_note.invoice_line_ids.filtered(
            lambda l: l.display_type not in ('line_section', 'line_note') and l.quantity <= 0
        )
        if lines_to_remove:
            lines_to_remove.unlink()

    def _action_return_picking(self, payload, user):
        order_id_raw = payload.get('order_id')
        order_id = int(order_id_raw) if order_id_raw else None
        picking_ids = payload.get('picking_ids', [])
        # Backwards compatibility if frontend sends 'picking_id'
        if not picking_ids and payload.get('picking_id'):
            picking_ids = [payload.get('picking_id')]
            
        if not picking_ids:
            # Fallback to deriving pickings from order_id if present
            if order_id:
                # Need to use sudo to ensure we get pickings even if not explicitly assigned
                order = request.env['sale.order'].sudo().with_context(mail_notrack=True, mail_create_nolog=True).browse(order_id)
                if order.exists():
                    picking_ids = order.picking_ids.filtered(lambda p: p.state == 'done').ids

        if not picking_ids:
            raise ValueError('No pickings provided to return')

        # Retrieve all pickings, ensure they belong to the user/company, and sort them LIFO
        # Returning from the newest delivery first
        pickings = request.env['stock.picking'].with_user(user).with_context(mail_notrack=True, mail_create_nolog=True).search([('id', 'in', picking_ids)], order='id desc')
        if not pickings:
            raise ValueError('Pickings not found')
            
        requested_lines = payload.get('lines', [])
        _logger.info(f"Sync: Processing return for order {order_id}, pickings {picking_ids}, lines: {requested_lines}")
        if not requested_lines:
             _logger.warning("Sync: No return lines specified in payload")
             return {'result': False, 'message': 'No return lines specified'}
             
        # Create a mutable tracking dictionary of what remains to be returned
        pending_returns = { int(line['product_id']): float(line['quantity']) for line in requested_lines if float(line.get('quantity', 0)) > 0 }
        
        return_location_id = payload.get('location_id') or payload.get('return_location_id')
        
        # Fallback to current sales rep's return_location_id, return_location_ids, default_location_id, or default_location_ids
        sales_rep = request.env['sales.representative'].sudo().search([('user_id', '=', user.id)], limit=1)
        if not return_location_id and sales_rep:
            return_location_id = (
                sales_rep.return_location_id.id or 
                (sales_rep.return_location_ids and sales_rep.return_location_ids[0].id) or 
                sales_rep.default_location_id.id or 
                (sales_rep.default_location_ids and sales_rep.default_location_ids[0].id)
            )
            if return_location_id:
                _logger.info(f"Sync: Using rep fallback return/default location (ID: {return_location_id}) for rep {sales_rep.name}")
        
        created_return_pickings = []
        
        for picking in pickings:
            # Skip pickings that aren't done (cannot return from them)
            if picking.state != 'done':
                continue
                
            # Check if we still have things to return
            if sum(pending_returns.values()) <= 0:
                break
            
            # In Odoo 18, we must ensure picking_id is set so the compute _compute_moves_locations fires correctly.
            # Passing it in create() is safer than relying solely on context in some environments.
            ctx = {'active_id': picking.id, 'active_ids': [picking.id], 'active_model': 'stock.picking'}
            wizard = request.env['stock.return.picking'].with_user(user).with_context(ctx).with_context(mail_notrack=True, mail_create_nolog=True).create({
                'picking_id': picking.id
            })
            
            _logger.info(f"Sync: Created return wizard {wizard.id} for picking {picking.name}. Lines in wizard: {len(wizard.product_return_moves)}")
            
            # Track if this picking is actually contributing to the return
            contributed_qty = 0
            
            if wizard.product_return_moves:
                for ret_move in wizard.product_return_moves:
                    prod_id = ret_move.product_id.id
                    needed_qty = pending_returns.get(prod_id, 0)
                    
                    if needed_qty > 0:
                        # In Odoo 18, the wizard initializes all line quantities to 0 (user is expected to fill them in).
                        # We must read the MAX returnable qty from the original stock move, not the wizard line.
                        # The original move's delivered quantity is in ret_move.move_id.quantity (for done moves).
                        # We also need to subtract any previously returned quantities.
                        original_move = ret_move.move_id
                        max_returnable = original_move.quantity if original_move else 0
                        
                        # Subtract already-returned quantities from previous returns
                        if original_move:
                            for returned_move in original_move.returned_move_ids.filtered(lambda m: m.state != 'cancel'):
                                max_returnable -= returned_move.quantity
                        
                        _logger.info(f"Sync: Return line product {prod_id}: needed={needed_qty}, max_returnable={max_returnable}")
                        
                        if max_returnable > 0:
                            take_qty = min(needed_qty, max_returnable)
                            ret_move.write({'quantity': take_qty})
                            pending_returns[prod_id] -= take_qty
                            contributed_qty += take_qty
                            _logger.info(f"Sync: Set return qty to {take_qty} for product {prod_id}")
                        else:
                            ret_move.write({'quantity': 0})
                            _logger.info(f"Sync: No returnable qty left for product {prod_id}")
                    else:
                        ret_move.write({'quantity': 0})
                        
            # Only finalize this return if it actually returns something
            if contributed_qty > 0:
                if return_location_id and hasattr(wizard, 'location_id'):
                    wizard.location_id = return_location_id
                elif return_location_id:
                     _logger.info(f"Sync: Requested return location {return_location_id} ignored (wizard has no location_id field). Defaulting to picking source.")
                
                _logger.info(f"Sync: Finalizing return for picking {picking.name} with contributed qty {contributed_qty}")
                # Odoo 18 uses action_create_returns instead of create_returns
                result = wizard.sudo().with_context(mail_notrack=True, mail_create_nolog=True).action_create_returns()
                
                # Validate the generated return picking
                if result and result.get('res_id'):
                    return_picking = request.env['stock.picking'].with_user(user).with_context(mail_notrack=True, mail_create_nolog=True).browse(result['res_id'])
                    created_return_pickings.append(return_picking)
                    _logger.info(f"Sync: Created return picking {return_picking.name} (ID: {return_picking.id})")
                    
                    # Write return reason, note, and force return destination location
                    pick_vals = {}
                    reason_id = payload.get('return_reason_id')
                    reason_name = payload.get('return_reason')
                    resolved_reason = False
                    if reason_id:
                        resolved_reason = request.env['sales.rep.return.reason'].sudo().browse(int(reason_id))
                        if not resolved_reason.exists():
                            resolved_reason = False
                    if not resolved_reason and reason_name:
                        resolved_reason = request.env['sales.rep.return.reason'].sudo().search([('name', '=', reason_name)], limit=1)
                        if not resolved_reason:
                            resolved_reason = request.env['sales.rep.return.reason'].sudo().search([('name', 'ilike', reason_name)], limit=1)
                    if resolved_reason:
                        pick_vals['return_reason_id'] = resolved_reason.id
                    elif reason_id:
                        first_reason = request.env['sales.rep.return.reason'].sudo().search([], limit=1)
                        if first_reason:
                            pick_vals['return_reason_id'] = first_reason.id
                    if reason_name:
                        pick_vals['note'] = reason_name
                    
                    # Force the return destination to the sales rep's assigned location
                    if return_location_id:
                        pick_vals['location_dest_id'] = int(return_location_id)
                        _logger.info(f"Sync: Forcing return picking {return_picking.name} destination to location ID {return_location_id}")
                    
                    if pick_vals:
                        return_picking.write(pick_vals)

                    for move in return_picking.move_ids:
                        move_vals = {'quantity': move.product_uom_qty, 'picked': True}
                        # Also force destination on each stock move
                        if return_location_id:
                            move_vals['location_dest_id'] = int(return_location_id)
                        move.sudo().write(move_vals)

                        # Force-set move line quantities and UoM
                        if move.move_line_ids:
                            first_ml = move.move_line_ids[0]
                            ml_vals = {
                                'quantity': move.product_uom_qty,
                                'product_uom_id': move.product_uom.id
                            }
                            if return_location_id:
                                ml_vals['location_dest_id'] = int(return_location_id)
                            first_ml.sudo().write(ml_vals)
                            if len(move.move_line_ids) > 1:
                                for extra_ml in move.move_line_ids[1:]:
                                    extra_ml.sudo().write({'quantity': 0})
                        else:
                            ml_vals = {
                                'move_id': move.id,
                                'picking_id': return_picking.id,
                                'product_id': move.product_id.id,
                                'product_uom_id': move.product_uom.id,
                                'location_id': move.location_id.id,
                                'location_dest_id': int(return_location_id) if return_location_id else move.location_dest_id.id,
                                'quantity': move.product_uom_qty,
                            }
                            request.env['stock.move.line'].sudo().create(ml_vals)
                    
                    # Force destination on move lines as well (Odoo may reset these)
                    if return_location_id:
                        for move in return_picking.move_ids:
                            for move_line in move.move_line_ids:
                                move_line.sudo().write({'location_dest_id': int(return_location_id)})
                    
        request.env.flush_all()
        
        validated_pickings = []
        # Conditionally auto-receive based on sales rep settings
        if sales_rep and sales_rep.auto_receive:
            for rp in created_return_pickings:
                rp.with_context(skip_backorder=True, skip_sms=True).button_validate()
                validated_pickings.append({'picking_id': rp.id, 'name': rp.name})
                
            # Invalidate model so qty_delivered and invoice_status recompute properly
            try:
                lines_to_recompute = request.env['sale.order.line']
                for orig_picking in pickings:
                    if orig_picking.sale_id:
                        lines_to_recompute |= orig_picking.sale_id.order_line
                if lines_to_recompute:
                    lines_to_recompute.invalidate_recordset(['qty_delivered'])
                    lines_to_recompute._compute_qty_delivered()
                request.env.flush_all()
            except Exception as recomp_err:
                _logger.warning(f"Sync: qty_delivered recompute warning in return: {recomp_err}")
                try:
                    request.env['sale.order.line'].invalidate_model(['qty_delivered'])
                except Exception:
                    pass
                
            # --- Auto-Create Credit Note for Returned Products ---
            # We calculate the returned value and create a credit note via reversal wizard.
            # This works regardless of invoice_status (first or subsequent returns).
            sale_orders = pickings.mapped('sale_id')
            created_refunds = []
            
            for order in sale_orders:
                # Find the most recent posted original invoice for this order
                posted_invoices = order.invoice_ids.filtered(
                    lambda inv: inv.move_type == 'out_invoice' and inv.state == 'posted'
                ).sorted('invoice_date', reverse=True)
                
                if not posted_invoices:
                    _logger.info(f"Sync: No posted invoice found for order {order.name}, skipping auto-refund")
                    continue
                
                # Find the return moves that came from this order's deliveries
                return_moves_for_order = request.env['stock.move']
                for rp in created_return_pickings:
                    for mv in rp.move_ids:
                        # The origin_returned_move_id links to the original delivery move which links to the sale order
                        orig_move = mv.origin_returned_move_id
                        if orig_move and orig_move.picking_id and orig_move.picking_id.sale_id == order:
                            return_moves_for_order |= mv
                
                if not return_moves_for_order:
                    _logger.info(f"Sync: No return moves found for order {order.name}, skipping auto-refund")
                    continue
                
                # Calculate returned value per product
                returned_value = 0.0
                for mv in return_moves_for_order:
                    sale_line = mv.sale_line_id or order.order_line.filtered(
                        lambda l: l.product_id == mv.product_id
                    )[:1]
                    price_unit = sale_line.price_unit if sale_line else (mv.product_id.lst_price or 0.0)
                    tax_factor = 1.0
                    if sale_line and sale_line.tax_id:
                        taxes = sale_line.tax_id.compute_all(price_unit, order.currency_id, mv.quantity, mv.product_id, order.partner_id)
                        returned_value += taxes['total_included']
                    else:
                        returned_value += price_unit * mv.quantity
                
                if returned_value <= 0:
                    _logger.info(f"Sync: Returned value is {returned_value} for order {order.name}, skipping auto-refund")
                    continue
                
                try:
                    original_invoice = posted_invoices[0]
                    # Use account.move.reversal wizard to create a partial credit note
                    reversal_wizard = request.env['account.move.reversal'].with_user(user).create({
                        'move_ids': [(6, 0, [original_invoice.id])],
                        'reason': payload.get('return_reason') or 'Return of goods',
                        'journal_id': original_invoice.journal_id.id,
                        'date': fields.Date.today(),
                    })
                    reversal_result = reversal_wizard.refund_moves()
                    
                    # Get the created credit note
                    if reversal_result and reversal_result.get('res_id'):
                        credit_note = request.env['account.move'].with_user(user).browse(reversal_result['res_id'])
                    else:
                        # Fallback: find the draft credit note just created
                        credit_note = request.env['account.move'].sudo().search([
                            ('reversed_entry_id', '=', original_invoice.id),
                            ('state', '=', 'draft'),
                            ('move_type', '=', 'out_refund'),
                        ], order='id desc', limit=1)
                    
                    if credit_note:
                        self._adjust_credit_note_lines_for_return(
                            credit_note=credit_note,
                            original_invoice=original_invoice,
                            return_moves_for_order=return_moves_for_order,
                        )
                            
                        # Recompute taxes and totals (Odoo 17/18 way)
                        if credit_note.state == 'draft':
                            if any(l.quantity > 0 for l in credit_note.invoice_line_ids):
                                try:
                                    credit_note.with_context(mail_create_nosubscribe=True, mail_auto_subscribe_no_notify=True).action_post()
                                except Exception as e:
                                    _logger.warning(f"Sync: Non-blocking post failure for credit note {credit_note.name}: {e}")
                                
                                # Apply consistent reconciliation logic
                                self._reconcile_refund_logic(credit_note, original_invoice, order)
                                created_refunds.append({'invoice_id': credit_note.id, 'name': credit_note.name})
                                _logger.info(f"Sync: Auto-created credit note {credit_note.name} for return on order {order.name}")
                            else:
                                _logger.warning(f"Sync: Credit note for {order.name} would be empty, skipping post")
                                credit_note.unlink()
                    
                except Exception as e:
                    _logger.error(f"Sync: Failed to auto-create credit note for order {order.name}: {e}")
        else:
            for rp in created_return_pickings:
                validated_pickings.append({'picking_id': rp.id, 'name': rp.name})
                
        # Even if multiple were created, we return success if at least one was
        if validated_pickings:
             res_dict = {
                 'success': True, 
                 'returns_created': validated_pickings, 
                 'picking_id': validated_pickings[0]['picking_id'], 
                 'name': validated_pickings[0]['name']
             }
             if sales_rep and sales_rep.auto_receive and 'created_refunds' in locals() and created_refunds:
                 res_dict['refunds_created'] = created_refunds
             return res_dict
             
        # If we reached here, no returns were created (maybe everything was already returned)
        return {'result': False, 'message': 'No available quantities to return across provided pickings'}

    def _action_receive_returns(self, payload, user):
        picking_ids = payload.get('picking_ids', [])
        if not picking_ids:
            raise ValueError('No pickings provided to receive')

        pickings = request.env['stock.picking'].with_user(user).search([('id', 'in', picking_ids)])
        if not pickings:
            raise ValueError('Pickings not found')

        validated_pickings = []
        for picking in pickings:
            if picking.state not in ('done', 'cancel'):
                # Ensure quantities stay at max defined for return
                for move in picking.move_ids:
                    move.sudo().write({'quantity': move.product_uom_qty, 'picked': True})
                    # Force-set move lines
                    if move.move_line_ids:
                        first_ml = move.move_line_ids[0]
                        first_ml.sudo().write({
                            'quantity': move.product_uom_qty,
                            'product_uom_id': move.product_uom.id
                        })
                        if len(move.move_line_ids) > 1:
                            for extra_ml in move.move_line_ids[1:]:
                                extra_ml.sudo().write({'quantity': 0})
                    else:
                        request.env['stock.move.line'].sudo().create({
                            'move_id': move.id,
                            'picking_id': picking.id,
                            'product_id': move.product_id.id,
                            'product_uom_id': move.product_uom.id,
                            'location_id': move.location_id.id,
                            'location_dest_id': move.location_dest_id.id,
                            'quantity': move.product_uom_qty,
                        })
                
                picking.with_context(
                    skip_backorder=True, 
                    skip_sms=True,
                    mail_notrack=True,
                    mail_create_nolog=True,
                    mail_create_nosubscribe=True,
                    no_reset_password=True
                ).button_validate()
                validated_pickings.append(picking.id)

        request.env.flush_all()
        try:
            request.env['sale.order.line'].invalidate_model(['qty_delivered'])
        except Exception:
            pass
            
        # --- Auto-Create Refund Invoice (Credit Note) for Returned Products ---
        # Get the related Sales Order from the pickings
        sale_orders = pickings.mapped('sale_id')
        created_refunds = []
        
        for order in sale_orders:
            # Find the most recent posted original invoice for this order
            posted_invoices = order.invoice_ids.filtered(
                lambda inv: inv.move_type == 'out_invoice' and inv.state == 'posted'
            ).sorted('invoice_date', reverse=True)
            
            if not posted_invoices:
                _logger.info(f"Sync: No posted invoice found for order {order.name}, skipping auto-refund in receive")
                continue
            
            # Find the return moves that came from this order's deliveries
            return_moves_for_order = request.env['stock.move'].search([
                ('picking_id', 'in', pickings.ids),
                ('state', '=', 'done')
            ])
            # Filter moves linked to this order
            return_moves_for_order = return_moves_for_order.filtered(
                lambda mv: mv.origin_returned_move_id and mv.origin_returned_move_id.picking_id.sale_id == order
            )
            
            if not return_moves_for_order:
                _logger.info(f"Sync: No return moves found for order {order.name} in receive, skipping auto-refund")
                continue
            
            try:
                original_invoice = posted_invoices[0]
                # Use reversal wizard
                reversal_wizard = request.env['account.move.reversal'].with_user(user).create({
                    'move_ids': [(6, 0, [original_invoice.id])],
                    'reason': payload.get('return_reason') or 'Return of goods (Manual)',
                    'journal_id': original_invoice.journal_id.id,
                    'date': fields.Date.today(),
                })
                reversal_result = reversal_wizard.refund_moves()
                
                credit_note = None
                if reversal_result and isinstance(reversal_result, dict) and reversal_result.get('res_id'):
                    credit_note = request.env['account.move'].with_user(user).browse(reversal_result['res_id'])
                else:
                    credit_note = request.env['account.move'].sudo().search([
                        ('reversed_entry_id', '=', original_invoice.id),
                        ('move_type', '=', 'out_refund'),
                    ], order='id desc', limit=1)
                
                if credit_note:
                    self._adjust_credit_note_lines_for_return(
                        credit_note=credit_note,
                        original_invoice=original_invoice,
                        return_moves_for_order=return_moves_for_order,
                    )
                    
                    # Recompute and post
                    if credit_note.state == 'draft':
                        
                        # Prevent empty invoices
                        if any(l.quantity > 0 for l in credit_note.invoice_line_ids):
                            try:
                                credit_note.with_context(mail_create_nosubscribe=True, mail_auto_subscribe_no_notify=True).action_post()
                            except Exception as e:
                                _logger.warning(f"Sync: Non-blocking post failure for credit note {credit_note.name}: {e}")
                            
                            # Apply consistent reconciliation logic
                            self._reconcile_refund_logic(credit_note, original_invoice, order)
                            
                            created_refunds.append({'invoice_id': credit_note.id, 'name': credit_note.name})
                        else:
                            _logger.warning(f"Sync: Credit note for {order.name} would be empty, skipping post")
                            credit_note.unlink()
                    else:
                        created_refunds.append({'invoice_id': credit_note.id, 'name': credit_note.name})

            except Exception as e:
                _logger.error(f"Sync: Failed to auto-create refund invoice for order {order.name}: {e}")

        return {
            'success': True, 
            'validated': validated_pickings,
            'refunds_created': created_refunds
        }

    def _action_refund_invoice(self, payload, user):
        env = request.env
        invoice = env['account.move'].with_user(user).browse(payload['invoice_id'])

        if not invoice.exists():
            raise ValueError('Invoice not found')
            
        # 🛡️ Guard: Redundancy check. If it's already a refund, ignore
        if invoice.move_type == 'out_refund':
            _logger.info(f"Sync: Action refund_invoice called on an already existing refund {invoice.id}, skipping")
            return {'success': True, 'skipped': True, 'reason': 'already_refund'}
            
        # 🛡️ Guard: Only reverse posted moves
        if invoice.state != 'posted':
            _logger.warning(f"Sync: Action refund_invoice called on unposted move {invoice.id}, skipping")
            return {'success': True, 'skipped': True, 'reason': 'not_posted'}

        reason = payload.get('reason', 'Return')
        refund_type = payload.get('refund_type', 'credit')

        _logger.info(f"Creating refund for invoice {invoice.name} ({invoice.id})")

        # 1️⃣ Create reversal using Odoo wizard
        wizard = env['account.move.reversal'].sudo().with_context(
            active_model='account.move',
            active_ids=[invoice.id]
        ).create({
            'reason': reason,
            'journal_id': invoice.journal_id.id,
            'date': fields.Date.today(),
        })

        res = wizard.reverse_moves()

        # 2️⃣ Get created credit note
        refund = None
        if isinstance(res, dict) and res.get('res_id'):
            refund = env['account.move'].browse(res['res_id'])
        else:
            refund = env['account.move'].search([
                ('reversed_entry_id', '=', invoice.id),
                ('move_type', '=', 'out_refund')
            ], limit=1)

        if not refund:
            raise ValueError("Failed to create credit note")

        _logger.info(f"Credit note created: {refund.name} ({refund.id})")

        # =========================================================
        # 🔥 ROBUST quantity & promotion discount update
        # =========================================================

        return_lines = payload.get('return_lines', [])

        if refund.state != 'draft':
            _logger.warning("Refund already posted, cannot modify")
        else:
            if return_lines:
                self._adjust_credit_note_lines_for_return(
                    credit_note=refund,
                    original_invoice=invoice,
                    return_lines=return_lines,
                )
            else:
                # Full refund -> ensure sale lines linked for traceability
                product_to_sale_lines = {}
                for inv_line in invoice.invoice_line_ids:
                    for sl in inv_line.sale_line_ids:
                        if sl.product_id.id not in product_to_sale_lines:
                            product_to_sale_lines[sl.product_id.id] = env['sale.order.line']
                        product_to_sale_lines[sl.product_id.id] |= sl
                for inv_line in refund.invoice_line_ids:
                    if inv_line.product_id and inv_line.product_id.id in product_to_sale_lines:
                        inv_line.sale_line_ids = [(6, 0, product_to_sale_lines[inv_line.product_id.id].ids)]

        # =========================================================
        # ✅ Post the credit note (only if not empty)
        # =========================================================

        if refund.state == 'draft':
            if any(l.quantity > 0 for l in refund.invoice_line_ids):
                try:
                    refund.with_context(mail_create_nosubscribe=True, mail_auto_subscribe_no_notify=True).action_post()
                except Exception as e:
                    _logger.warning(f"Sync: Non-blocking post failure for refund {refund.id}: {e}")
                # Reconciliation handled below in CENTRALIZED RECONCILE block
            else:
                _logger.warning(f"Sync: Skipping post for empty refund {refund.id}")
                return {'success': True, 'message': 'Refund skipped (empty)', 'refund_id': refund.id}

        # =========================================================
        # CENTRALIZED RECONCILE
        # =========================================================

        if refund_type == 'credit':
            sale_order = invoice.line_ids.sale_line_ids.order_id[:1]
            self._reconcile_refund_logic(refund, invoice, sale_order)

        # =========================================================
        # 💰 DIRECT REFUND (Cash/Bank)
        # =========================================================

        if refund_type == 'refund' and payload.get('journal_id'):
            journal = env['account.journal'].sudo().browse(int(payload['journal_id']))

            if journal.exists() and journal.type in ['cash', 'bank']:
                payment_register = env['account.payment.register'].sudo().with_context(
                    active_model='account.move',
                    active_ids=[refund.id]
                ).create({
                    'journal_id': journal.id,
                    'amount': refund.amount_residual,
                    'payment_date': fields.Date.today(),
                })

                payment_register._create_payments()

        return {
            'refund_id': refund.id,
            'name': refund.name,
            'amount': refund.amount_residual
        }

    def _action_apply_promotion(self, payload, user):
        order = request.env['sale.order'].with_user(user).browse(payload['order_id'])
        if not order.exists():
            raise ValueError('Order not found')
        # Recompute coupons/promotions (requires loyalty module)
        try:
            order._update_programs_and_rewards()
            return {'order_id': order.id}
        except Exception:
            return {'skipped': True, 'reason': 'Promotion module not available'}

    def _action_add_order_line(self, payload, user):
        order = self._get_order_from_payload(payload, user)
        location_id = payload.get('location_id')
        
        # Check for existing line for this product on this order (Idempotency)
        search_domain = [
            ('order_id', '=', order.id),
            ('product_id', '=', int(payload['product_id']))
        ]
        if location_id:
            search_domain.append(('location_id', '=', int(location_id)))
        else:
            search_domain.append(('location_id', '=', False))

        existing_line = request.env['sale.order.line'].with_user(user).search(search_domain, limit=1)

        if existing_line:
            _logger.info(f"Sync: Updating existing line {existing_line.id} for product {payload['product_id']} on order {order.name}")
            update_vals = {
                'product_uom_qty': payload.get('quantity', 1.0),
                'price_unit': payload.get('price_unit') or existing_line.price_unit,
            }
            # Handle UoM
            uom_id = payload.get('product_uom_id')
            if uom_id:
                uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
                if uom.exists():
                    update_vals['product_uom'] = uom.id
            
            existing_line.write(update_vals)
            return {'line_id': existing_line.id, 'order_id': order.id, 'updated': True}

        vals = {
            'order_id': order.id,
            'product_id': int(payload['product_id']),
            'product_uom_qty': payload.get('quantity', 1.0),
            'price_unit': payload.get('price_unit'),
        }
        if location_id:
            vals['location_id'] = int(location_id)
        # Handle UoM
        uom_id = payload.get('product_uom_id')
        if uom_id:
            uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
            if uom.exists():
                vals['product_uom'] = uom.id
        if hasattr(request.env['sale.order.line'], 'is_reward_line'):
            vals['is_reward_line'] = payload.get('is_reward_line', False)
        # Optional: tax_id, discount, etc. if sent in payload
        
        line = request.env['sale.order.line'].with_user(user).create(vals)
        return {'line_id': line.id, 'order_id': order.id}

    def _action_modify_order_line(self, action_type, payload, user):
        line = request.env['sale.order.line'].with_user(user).browse(payload['line_id'])
        if not line.exists():
            return {'line_id': payload['line_id'], 'error': 'Line not found'}
        
        order = line.order_id
        if action_type == 'update_order_line':
            vals = {}
            if 'quantity' in payload and payload.get('quantity') is not None:
                vals['product_uom_qty'] = payload['quantity']
            
            uom_id = payload.get('product_uom_id') or payload.get('uom_id')
            if uom_id:
                uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
                if uom.exists():
                    vals['product_uom'] = uom.id
            
            if 'price_unit' in payload and payload.get('price_unit') is not None:
                vals['price_unit'] = payload['price_unit']

            if vals:
                line.write(vals)
                _logger.info(f"Sync: Updated line {line.id} with {vals}")
        else:  # remove_order_line
            line.unlink()
            _logger.info(f"Sync: Removed line {payload['line_id']}")

        # Re-apply promotions for draft orders
        # REMOVED: Recalculation is now only on confirmation.

        return {'line_id': payload['line_id']}

    # ------------------------------------------------------------------
    # Order line helpers
    # ------------------------------------------------------------------




    # --- Customer Creation ---
    def _action_create_customer(self, payload, user, sales_rep):
        """Create a new customer (res.partner) and link to route if needed."""
        try:
            customer_data = payload.get('customer_data', {})
            route_id = payload.get('route_id')

            _logger.info(f"Sync: Creating customer {customer_data.get('name')} with data: {customer_data}")
            _logger.info(f"Sync: Route ID: {route_id}")

            # 1. Prepare partner values
            vals = {
                'name': customer_data.get('name'),
                'email': customer_data.get('email'),
                'phone': customer_data.get('phone'),
                'mobile': customer_data.get('mobile'),
                'street': customer_data.get('street'),
                'city': customer_data.get('city'),
                'vat': customer_data.get('vat'),
                'comment': customer_data.get('comment'),
                'customer_rank': 1, # It's a customer
                'company_id': sales_rep.company_id.id if sales_rep.company_id else False,
                'user_id': sales_rep.user_id.id if sales_rep.user_id else False, # Assign to sales rep user
                'mobile_local_id': customer_data.get('local_id'), # Save original mobile local_id
                'visit_latitude': float(customer_data.get('latitude', 0) or 0),
                'visit_longitude': float(customer_data.get('longitude', 0) or 0),
                'is_cash': customer_data.get('is_cash', True),
            }

            # Handle relational fields (Pricelist, Payment Term) safely
            if customer_data.get('property_product_pricelist'):
                try:
                    pricelist_id = int(customer_data['property_product_pricelist'])
                    vals['property_product_pricelist'] = pricelist_id
                    
                    # Override is_cash based on pricelist property (as requested)
                    pricelist = request.env['product.pricelist'].sudo().browse(pricelist_id)
                    if pricelist.exists():
                        vals['is_cash'] = pricelist.is_sales_cash
                        # Inherit sales limit from pricelist
                        if 'sale_credit_limit' in request.env['res.partner']._fields:
                            vals['sale_credit_limit'] = pricelist.sales_limit
                        elif 'credit_limit' in request.env['res.partner']._fields:
                            vals['credit_limit'] = pricelist.sales_limit
                except (ValueError, TypeError):
                    _logger.warning(f"Sync: Invalid pricelist ID: {customer_data['property_product_pricelist']}")

            if customer_data.get('property_payment_term_id'):
                try:
                    vals['property_payment_term_id'] = int(customer_data['property_payment_term_id'])
                except (ValueError, TypeError):
                    _logger.warning(f"Sync: Invalid payment term ID: {customer_data['property_payment_term_id']}")

            Partner = request.env['res.partner'].sudo()

            # Check for existing partner with same mobile_local_id to avoid duplicates
            existing_partner = Partner.search([
                ('mobile_local_id', '=', customer_data.get('local_id')),
                ('mobile_local_id', '!=', False)
            ], limit=1)

            if existing_partner:
                _logger.info(f"Sync: Customer {customer_data.get('name')} already exists with local_id {customer_data.get('local_id')}. Skipping creation.")
                new_partner = existing_partner
            else:
                # Smart Duplicate Prevention: Check mobile, phone, or vat
                mobile = customer_data.get('mobile')
                phone = customer_data.get('phone')
                vat = customer_data.get('vat')
                
                or_conditions = []
                if mobile and len(str(mobile).strip()) > 4:
                    or_conditions.append(('mobile', '=', mobile.strip()))
                    or_conditions.append(('phone', '=', mobile.strip()))
                if phone and len(str(phone).strip()) > 4:
                    or_conditions.append(('mobile', '=', phone.strip()))
                    or_conditions.append(('phone', '=', phone.strip()))
                if vat and len(str(vat).strip()) > 2:
                    or_conditions.append(('vat', '=', vat.strip()))
                
                smart_partner = False
                if or_conditions:
                    search_domain = ['|'] * (len(or_conditions) - 1) + or_conditions
                    company_id = sales_rep.company_id.id if sales_rep.company_id else False
                    search_domain = ['&', ('company_id', 'in', [company_id, False])] + search_domain
                    smart_partner = Partner.search(search_domain, limit=1)
                
                if smart_partner:
                    _logger.info(f"Sync: Smart duplicate prevention matched existing partner: {smart_partner.name} (ID: {smart_partner.id})")
                    # Update mobile_local_id if not already set to ensure future sync works perfectly
                    if not smart_partner.mobile_local_id:
                        smart_partner.write({'mobile_local_id': customer_data.get('local_id')})
                    new_partner = smart_partner
                else:
                    try:
                        new_partner = Partner.create(vals)
                        # Automatically perform reverse geocoding if location provided
                        if new_partner.visit_latitude and new_partner.visit_longitude:
                            new_partner.action_reverse_geocode(background=True)
                    except IntegrityError:
                        # Parallel request might have created the partner just now
                        _logger.warning(f"Sync: Concurrency conflict for {customer_data.get('name')}. Searching again.")
                        new_partner = Partner.search([
                            ('mobile_local_id', '=', customer_data.get('local_id')),
                            ('mobile_local_id', '!=', False)
                        ], limit=1)
                        if not new_partner:
                            _logger.error(f"Sync: IntegrityError but no partner found for {customer_data.get('local_id')}")
                            raise ValueError(f"Constraint violation: Data integrity error for {customer_data.get('name')}")
                    except Exception as e:
                        _logger.error(f"Sync: Failed to create res.partner: {e}")
                        raise ValueError(f"Failed to create customer record: {e}")

            # 3. Handle location logging
            if customer_data.get('latitude') is not None and customer_data.get('longitude') is not None:
                _logger.info(f"Sync: Location saved in creation for partner {new_partner.id}: Lat={customer_data['latitude']}, Lng={customer_data['longitude']}")
            else:
                 _logger.warning(f"Sync: No location data provided in payload: {customer_data.keys()}")

            result = {
                'partner_odoo_id': new_partner.id,
                'partner_name': new_partner.name
            }

            # 4. Link to Route (sales.route.customer) if route_id provided
            if route_id:
                try:
                    route_id_int = int(route_id)
                    RouteCustomer = request.env['sales.route.customer'].sudo()
                    
                    # Check if link already exists? (Unlikely for new partner)
                    
                    new_route_customer = RouteCustomer.create([{
                        'route_id': route_id_int,
                        'partner_id': new_partner.id,
                        # 'name' field does not exist in backend model (it's in local DB only)
                        'sequence': 999, # Append to end
                    }])[0]
                    result['route_customer_odoo_id'] = new_route_customer.id
                    _logger.info(f"Sync: Linked new customer {new_partner.name} to route {route_id}")
                except Exception as e:
                    _logger.error(f"Sync: Failed to link to route {route_id}: {e}")
                    # Don't fail the whole action if route linking fails, but it's important.
                    # We should probably raise validation error or log it clearly.
                    result['warning'] = f"Customer created but failed to link to route: {e}"

            return result

        except Exception as e:
            _logger.error(f"Sync: _action_create_customer CRITICAL ERROR: {e}", exc_info=True)
            raise e


    def _action_upload_attachment(self, payload, user):
        res_model = payload.get('res_model', 'sale.order')
        res_id = payload.get('res_id')
        file_base64 = payload.get('file_base64')
        file_name = payload.get('file_name', 'attachment.jpg')

        if not res_id:
            raise ValueError("Missing 'res_id' in upload_attachment payload")
        if not file_base64:
            raise ValueError("Missing 'file_base64' in upload_attachment payload")

        # If res_id is a string (UUID/local_id), resolve the Odoo ID
        if isinstance(res_id, str) and res_model == 'sale.order':
            order = request.env['sale.order'].with_user(user).search([('mobile_local_id', '=', res_id)], limit=1)
            if not order.exists():
                return {'success': False, 'error': f"Order not found for local_id: {res_id}, retrying later."}
            res_id_resolved = order.id
        else:
            try:
                res_id_resolved = int(res_id)
            except ValueError:
                res_id_resolved = res_id

        # Check if attachment already exists to avoid duplicates
        existing = request.env['ir.attachment'].with_user(user).search([
            ('res_model', '=', res_model),
            ('res_id', '=', res_id_resolved),
            ('name', '=', file_name)
        ], limit=1)

        if existing:
            return {'success': True, 'attachment_id': existing.id, 'message': 'Attachment already exists'}

        attachment = request.env['ir.attachment'].with_user(user).create({
            'name': file_name,
            'datas': file_base64,
            'res_model': res_model,
            'res_id': res_id_resolved,
            'type': 'binary',
        })
        
        return {'success': True, 'attachment_id': attachment.id}

    def _action_delete_attachment(self, payload, user):
        res_model = payload.get('res_model', 'sale.order')
        res_id = payload.get('res_id')
        file_name = payload.get('file_name')

        if not res_id:
            raise ValueError("Missing 'res_id' in delete_attachment payload")
        if not file_name:
            raise ValueError("Missing 'file_name' in delete_attachment payload")

        # Resolve res_id
        if isinstance(res_id, str) and res_model == 'sale.order':
            order = request.env['sale.order'].with_user(user).search([('mobile_local_id', '=', res_id)], limit=1)
            if not order.exists():
                return {'success': False, 'error': f"Order not found for local_id: {res_id}"}
            res_id_resolved = order.id
        else:
            try:
                res_id_resolved = int(res_id)
            except ValueError:
                res_id_resolved = res_id

        # Find attachment
        attachment = request.env['ir.attachment'].with_user(user).search([
            ('res_model', '=', res_model),
            ('res_id', '=', res_id_resolved),
            ('name', '=', file_name)
        ], limit=1)

        if attachment.exists():
            attachment.unlink()
            return {'success': True, 'message': 'Attachment deleted successfully'}
        
        return {'success': True, 'message': 'Attachment did not exist on server'}

    def _action_reorder_route_customers(self, payload, user):
        route_id = payload.get('route_id')
        sequences = payload.get('sequences', [])
        _logger.info(f"Sync Action: Reordering route {route_id} customers: {sequences}")
        
        for seq_info in sequences:
            rc_id = seq_info.get('route_customer_id')
            local_rc_id = seq_info.get('local_route_customer_id')
            seq = seq_info.get('sequence')
            
            if seq is None:
                continue

            rc = request.env['sales.route.customer'].with_user(user)
            rc_record = False
            
            # 1. Try to resolve via Odoo ID
            if rc_id:
                try:
                    rc_record = rc.browse(int(rc_id))
                    if not rc_record.exists():
                        rc_record = False
                except (ValueError, TypeError):
                    rc_record = False
            
            # 2. Try to resolve via route_id and partner's mobile_local_id if partner was created offline
            if not rc_record:
                partner_local_id = seq_info.get('partner_local_id')
                if partner_local_id and route_id:
                    partner = request.env['res.partner'].sudo().search([('mobile_local_id', '=', partner_local_id)], limit=1)
                    if partner:
                        rc_record = rc.search([
                            ('route_id', '=', int(route_id)),
                            ('partner_id', '=', partner.id)
                        ], limit=1)
            
            if rc_record:
                rc_record.write({'sequence': seq})
                
        return {'success': True}
