# -*- coding: utf-8 -*-
from odoo import http, fields
from odoo.http import request
import json
import logging
from .utils import SalesRepUtils

_logger = logging.getLogger(__name__)

class AccountController(http.Controller, SalesRepUtils):



    @http.route('/api/mobile/tax_rates', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_tax_rates(self, **kwargs):
        try:
             sales_rep, user = self._authenticate_request()
             if not sales_rep:
                 return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

             taxes = request.env['account.tax'].with_user(user).search_read([('type_tax_use', '=', 'sale')], ['id', 'name', 'amount', 'price_include'])
             return request.make_response(json.dumps({'success': True, 'taxes': taxes}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_tax_rates: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/invoices/<int:invoice_id>/register_payment', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def register_payment_for_invoice(self, invoice_id, **kwargs):
        """Register payment for an invoice and reconcile it to mark as paid"""
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            journal_id = data.get('journal_id')
            amount = data.get('amount')
            
            invoice = request.env['account.move'].with_user(user).browse(invoice_id)
            if not invoice.exists():
                return request.make_response(json.dumps({'success': False, 'message': 'Invoice not found'}), headers={'Content-Type': 'application/json'})
            
            # Check if invoice is posted
            if invoice.state != 'posted':
                return request.make_response(json.dumps({'success': False, 'message': 'Invoice must be posted before registering payment'}), headers={'Content-Type': 'application/json'})
            
            # Check if invoice already paid
            if invoice.payment_state == 'paid':
                return request.make_response(json.dumps({'success': False, 'message': 'Invoice is already fully paid'}), headers={'Content-Type': 'application/json'})
            
            # Determine payment amount
            payment_amount = float(amount) if amount else invoice.amount_residual
            
            # Cap payment at residual amount
            if payment_amount > invoice.amount_residual:
                payment_amount = invoice.amount_residual
            
            # Use the account.payment.register wizard which handles reconciliation automatically
            payment_register = request.env['account.payment.register'].with_context(
                active_model='account.move',
                active_ids=[invoice.id]
            ).create({
                'journal_id': int(journal_id),
                'amount': payment_amount,
                'payment_date': fields.Date.today(),
            })
            
            # Create payment and reconcile with invoice
            payment = payment_register._create_payments()
            
            # Reload invoice to get updated payment state
            invoice.invalidate_recordset()
            
            # Get payment method name
            payment_method_name = ''
            if payment and payment.payment_method_line_id:
                payment_method_name = payment.payment_method_line_id.name
            elif payment and payment.payment_method_id:
                payment_method_name = payment.payment_method_id.name
            
            # Get journal name
            journal_name = payment.journal_id.name if payment and payment.journal_id else ''
            
            return request.make_response(json.dumps({
                'success': True, 
                'payment_id': payment.id if payment else False,
                'payment_name': payment.name if payment else False,
                'payment_method': payment_method_name,
                'journal_name': journal_name,
                'amount_paid': payment_amount,
                'payment_state': invoice.payment_state,
                'amount_residual': invoice.amount_residual,
                'message': 'Payment registered and reconciled successfully'
            }), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in register_payment_for_invoice: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/invoices/<int:invoice_id>/refund', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def refund_invoice(self, invoice_id, **kwargs):
        try:
            sales_rep, user = self._authenticate_request()
            if not sales_rep:
                return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

            data = json.loads(request.httprequest.data)
            reason = data.get('reason')
            payment_journal_id = data.get('journal_id')
            payment_date = data.get('payment_date')
            return_lines = data.get('return_lines', [])  # [{product_id, quantity}]
            
            # Get original invoice
            move = request.env['account.move'].with_user(user).browse(invoice_id)
            if not move.exists():
                return request.make_response(json.dumps({'success': False, 'error': 'Invoice not found'}), headers={'Content-Type': 'application/json'})
            
            # Find the sales order linked to this invoice
            sale_order = None
            if move.invoice_origin:
                sale_order = request.env['sale.order'].search([('name', '=', move.invoice_origin)], limit=1)
            if not sale_order:
                # Try finding via invoice_line_ids sale_line_ids
                for line in move.invoice_line_ids:
                    if line.sale_line_ids:
                        sale_order = line.sale_line_ids[0].order_id
                        break
            
            # Create Credit Note manually with only the returned items
            credit_note_vals = {
                'move_type': 'out_refund',
                'partner_id': move.partner_id.id,
                'journal_id': move.journal_id.id,
                'invoice_date': fields.Date.today(),
                'ref': reason or f'Return for {move.name}',
                'reversed_entry_id': move.id,
                'fiscal_position_id': move.fiscal_position_id.id if move.fiscal_position_id else False,
                'invoice_origin': sale_order.name if sale_order else move.invoice_origin,
                'invoice_line_ids': [],
            }
            
            # Build credit note lines based on return_lines
            if return_lines:
                # Create lines only for returned products
                for ret_line in return_lines:
                    product_id = ret_line.get('product_id')
                    qty = ret_line.get('quantity', 0)
                    
                    if not product_id or qty <= 0:
                        continue
                    
                    # Find matching invoice line
                    inv_line = move.invoice_line_ids.filtered(
                        lambda l: l.product_id.id == product_id and (l.price_unit >= 0 and l.price_subtotal >= 0)
                    )[:1]
                    
                    if inv_line:
                        line_vals = {
                            'product_id': inv_line.product_id.id,
                            'name': inv_line.name,
                            'quantity': qty,
                            'product_uom_id': inv_line.product_uom_id.id,
                            'price_unit': inv_line.price_unit,
                            'discount': inv_line.discount,
                            'tax_ids': [(6, 0, inv_line.tax_ids.ids)] if inv_line.tax_ids else [],
                        }
                        # Link to sale order line if available
                        if inv_line.sale_line_ids:
                            line_vals['sale_line_ids'] = [(6, 0, inv_line.sale_line_ids.ids)]
                        credit_note_vals['invoice_line_ids'].append((0, 0, line_vals))

                # Calculate return ratio and include proportional discount / promotion lines
                orig_prod_lines = move.invoice_line_ids.filtered(
                    lambda l: l.display_type not in ['line_section', 'line_note'] and (l.price_subtotal > 0 or l.price_unit > 0)
                )
                orig_prod_total = sum(l.price_subtotal for l in orig_prod_lines)
                if orig_prod_total <= 0:
                    orig_prod_total = sum(l.quantity * l.price_unit for l in orig_prod_lines)

                ret_prod_total = sum(
                    l[2]['quantity'] * l[2]['price_unit'] * (1.0 - (l[2].get('discount', 0.0) / 100.0))
                    for l in credit_note_vals['invoice_line_ids']
                )

                ratio = min(1.0, max(0.0, ret_prod_total / orig_prod_total)) if orig_prod_total > 0 else 1.0

                if ratio > 0:
                    orig_disc_lines = move.invoice_line_ids.filtered(
                        lambda l: l.display_type not in ['line_section', 'line_note'] and (l.price_unit < 0 or l.price_subtotal < 0)
                    )
                    for disc_line in orig_disc_lines:
                        scaled_price = round(disc_line.price_unit * ratio, 2)
                        line_vals = {
                            'product_id': disc_line.product_id.id if disc_line.product_id else False,
                            'name': disc_line.name,
                            'quantity': 1.0,
                            'product_uom_id': disc_line.product_uom_id.id if disc_line.product_uom_id else False,
                            'price_unit': scaled_price,
                            'discount': disc_line.discount,
                            'tax_ids': [(6, 0, disc_line.tax_ids.ids)] if disc_line.tax_ids else [],
                        }
                        if disc_line.sale_line_ids:
                            line_vals['sale_line_ids'] = [(6, 0, disc_line.sale_line_ids.ids)]
                        credit_note_vals['invoice_line_ids'].append((0, 0, line_vals))
            else:
                # Fallback: Full reversal if no return_lines provided
                for inv_line in move.invoice_line_ids.filtered(lambda l: l.display_type not in ['line_section', 'line_note']):
                    line_vals = {
                        'product_id': inv_line.product_id.id,
                        'name': inv_line.name,
                        'quantity': inv_line.quantity,
                        'price_unit': inv_line.price_unit,
                        'discount': inv_line.discount,
                        'tax_ids': [(6, 0, inv_line.tax_ids.ids)] if inv_line.tax_ids else [],
                    }
                    if inv_line.sale_line_ids:
                        line_vals['sale_line_ids'] = [(6, 0, inv_line.sale_line_ids.ids)]
                    credit_note_vals['invoice_line_ids'].append((0, 0, line_vals))
            
            # Create and post credit note
            refund = request.env['account.move'].create(credit_note_vals)
            refund.action_post()
            
            # Link credit note to sales order
            if sale_order:
                # Use SQL to add to invoice_ids (Many2many)
                sale_order.write({'invoice_ids': [(4, refund.id)]})
            
            # Register Payment if requested
            if payment_journal_id and refund.amount_residual > 0:
                p_date = payment_date[:10] if payment_date else fields.Date.today()
                wiz = request.env['account.payment.register'].with_context(active_model='account.move', active_ids=[refund.id]).create({
                    'journal_id': payment_journal_id,
                    'amount': refund.amount_residual,
                    'payment_date': p_date,
                    'payment_type': 'outbound', 
                    'partner_type': 'customer',
                })
                wiz.action_create_payments()

            return request.make_response(json.dumps({'success': True, 'refund_id': refund.id, 'refund_amount': refund.amount_total}), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in refund_invoice: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})

    @http.route('/api/mobile/invoices/<int:invoice_id>', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_invoice_details(self, invoice_id, **kwargs):
        try:
             sales_rep, user = self._authenticate_request()
             if not sales_rep:
                 return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

             invoice = request.env['account.move'].with_user(user).browse(invoice_id)
             if not invoice.exists():
                 return request.make_response(json.dumps({'success': False, 'message': 'Invoice not found'}), headers={'Content-Type': 'application/json'})
             
            # Fetch lines
             lines = []
             for line in invoice.invoice_line_ids:
                 lines.append({
                     'id': line.id,
                     'product_id': line.product_id.id,
                     'name': line.name,
                     'quantity': line.quantity,
                     'price_unit': line.price_unit,
                     'price_subtotal': line.price_subtotal,
                     'discount': line.discount, 
                     'tax_ids': line.tax_ids.ids
                 })
             
             data = {
                 'id': invoice.id,
                 'name': invoice.name,
                 'state': invoice.state,
                 'payment_state': invoice.payment_state,
                 'invoice_date': invoice.invoice_date,
                 'amount_total': invoice.amount_total,
                 'amount_residual': invoice.amount_residual,
                 'amount_untaxed': invoice.amount_untaxed,
                 'amount_tax': invoice.amount_tax,
                 'partner': {
                     'name': invoice.partner_id.name,
                     'street': invoice.partner_id.street,
                     'street2': invoice.partner_id.street2,
                     'city': invoice.partner_id.city,
                     'zip': invoice.partner_id.zip,
                     'state_id': [invoice.partner_id.state_id.id, invoice.partner_id.state_id.name] if invoice.partner_id.state_id else False,
                     'country_id': [invoice.partner_id.country_id.id, invoice.partner_id.country_id.name] if invoice.partner_id.country_id else False,
                     'phone': invoice.partner_id.phone,
                     'email': invoice.partner_id.email,
                     'vat': invoice.partner_id.vat,
                 },
                 'company': {
                     'name': invoice.company_id.name,
                     'street': invoice.company_id.street,
                     'street2': invoice.company_id.street2,
                     'city': invoice.company_id.city,
                     'zip': invoice.company_id.zip,
                     'state_id': [invoice.company_id.state_id.id, invoice.company_id.state_id.name] if invoice.company_id.state_id else False,
                     'country_id': [invoice.company_id.country_id.id, invoice.company_id.country_id.name] if invoice.company_id.country_id else False,
                     'phone': invoice.company_id.phone,
                     'email': invoice.company_id.email,
                     'vat': invoice.company_id.vat,
                 },
                 'lines': lines
             }
             
             return request.make_response(json.dumps({'success': True, 'invoice': data}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
            _logger.error(f"Error in get_invoice_details: {str(e)}")
            return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})


    @http.route('/api/mobile/payments/today', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def get_payments_today(self, **kwargs):
        try:
             sales_rep, user = self._authenticate_request()
             if not sales_rep or not user:
                 return request.make_response(json.dumps({'success': False, 'message': 'Unauthorized'}), headers={'Content-Type': 'application/json'}, status=401)

             today = fields.Date.today()
             domain = [('date', '=', today), ('state', '=', 'posted')]
             domain.append(('create_uid', '=', user.id))
             
             payments = request.env['account.payment'].search_read(domain, ['id', 'name', 'amount', 'partner_id', 'journal_id', 'memo', 'date'])
             return request.make_response(json.dumps({'success': True, 'payments': payments}, default=str), headers={'Content-Type': 'application/json'})
        except Exception as e:
             _logger.error(f"Error in get_payments_today: {str(e)}")
             return request.make_response(json.dumps({'success': False, 'error': str(e)}), headers={'Content-Type': 'application/json'})
