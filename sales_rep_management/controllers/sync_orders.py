# -*- coding: utf-8 -*-
"""Uploaded sales orders: server-side lines/prices, van stock guard and the
order lifecycle (confirm -> deliver -> invoice) with a savepoint per stage."""
from odoo import http, fields, _
from odoo.exceptions import UserError
from odoo.http import request
import json
import logging
import base64
import traceback
from datetime import datetime, timedelta
from psycopg2 import IntegrityError

_logger = logging.getLogger(__name__)


class SyncOrdersMixin:
    """Mixed into SyncController (controllers/sync.py); no routes here."""

    def _get_current_route_id(self, sales_rep):
        """Today's running route of the rep (fallback: its latest running route)."""
        Route = request.env['sales.rep.route'].sudo()
        base = [('sales_rep_id', '=', sales_rep.id), ('state', '=', 'in_progress')]
        today = fields.Date.context_today(sales_rep.user_id or request.env.user)
        route = Route.search(base + [('date', '=', today)], limit=1) or Route.search(base, order='date desc', limit=1)
        return route.id or False

    def _create_upload_order_lines(self, order, order_data, user, sales_rep, ack, local_id):
        """Create order lines from the app with server-side pricing.

        The price comes from the order's pricelist. A different client price is
        kept only when the representative has the `access_discount` right
        (price overrides are discounts); otherwise the server price wins and
        the difference is reported back in ack['order_warnings'].
        """
        from odoo.tools import float_compare
        SOL = request.env['sale.order.line'].with_user(user)
        discount_product = sales_rep.discount_product_id
        notes = []
        for line in order_data.get('lines', []):
            if line.get('is_reward_line') or not line.get('product_id'):
                continue
            line_vals = {
                'order_id': order.id,
                'product_id': int(line['product_id']),
                'product_uom_qty': line.get('quantity'),
            }
            loc_id = line.get('location_id')
            if loc_id:
                line_vals['location_id'] = int(loc_id)
            uom_id = line.get('product_uom_id')
            if uom_id:
                uom = request.env['uom.uom'].with_user(user).browse(int(uom_id))
                if uom.exists():
                    line_vals['product_uom'] = uom.id
            client_price = line.get('price_unit')
            is_discount_line = bool(discount_product) and line_vals['product_id'] == discount_product.id
            if is_discount_line:
                if not sales_rep.access_discount:
                    notes.append(_("Discount line ignored: discount right not granted."))
                    continue
                line_vals['price_unit'] = client_price
                SOL.create(line_vals)
                continue
            sol = SOL.create(line_vals)
            if client_price is None:
                continue
            try:
                client_price = float(client_price)
            except (TypeError, ValueError):
                continue
            if float_compare(client_price, sol.price_unit, precision_rounding=order.currency_id.rounding) != 0:
                if sales_rep.access_discount and client_price < sol.price_unit:
                    sol.price_unit = client_price
                else:
                    notes.append(_("%(product)s: price %(client)s replaced by price list %(server)s",
                                   product=sol.product_id.display_name, client=client_price, server=sol.price_unit))
        if notes:
            ack.setdefault('order_warnings', {}).setdefault(local_id, {})['pricing'] = notes
        # A header discount without a discount line is converted into a line so
        # that the order, its invoice and taxes stay consistent.
        if order.discount_total and discount_product and not order.order_line.filtered(
                lambda l: l.product_id == discount_product):
            SOL.create({
                'order_id': order.id,
                'product_id': discount_product.id,
                'product_uom_qty': 1.0,
                'price_unit': -order.discount_total,
                'sequence': 999,
            })

    # ------------------------------------------------------------------
    # Delivery / lifecycle helpers (shared by upload and pending actions)
    # ------------------------------------------------------------------
    def _allow_negative_van_stock(self):
        return request.env['ir.config_parameter'].sudo().get_param(
            'sales_rep_management.allow_negative_van_stock', 'False') == 'True'

    def _assert_van_stock(self, picking):
        """Refuse to validate an outgoing delivery that would push the van negative."""
        if picking.picking_type_code != 'outgoing' or self._allow_negative_van_stock():
            return
        from odoo.tools import float_compare
        required = {}
        for ml in picking.move_line_ids:
            product = ml.product_id
            if not product.is_storable:
                continue
            qty = ml.product_uom_id._compute_quantity(ml.quantity, product.uom_id, rounding_method='HALF-UP')
            key = (product, ml.location_id)
            required[key] = required.get(key, 0.0) + qty
        shortages = []
        Quant = request.env['stock.quant'].sudo()
        for (product, location), qty in required.items():
            on_hand = sum(Quant.search([
                ('product_id', '=', product.id),
                ('location_id', 'child_of', location.id),
            ]).mapped('quantity'))
            if float_compare(qty, on_hand, precision_rounding=product.uom_id.rounding) > 0:
                shortages.append(f"{product.display_name}: {on_hand:g} available, {qty:g} required ({product.uom_id.name})")
        if shortages:
            raise UserError(_("Not enough stock in %(loc)s to deliver %(pick)s:\n%(lines)s",
                              loc=picking.location_id.display_name, pick=picking.name,
                              lines="\n".join(shortages)))

    def _validate_van_delivery(self, picking):
        """Deliver the full demand from the van, refusing to create negative stock.

        Quantities are reserved, the full demand is set as done, and
        `_assert_van_stock` refuses the validation with a clear message when
        the van does not hold enough stock (unless the
        `allow_negative_van_stock` setting is enabled), instead of silently
        creating negative quants.
        """
        from odoo.tools import float_compare
        if picking.state == 'draft':
            picking.action_confirm()
        if picking.state in ('confirmed', 'waiting', 'partially_available'):
            picking.action_assign()

        for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            if move.move_line_ids:
                total_ml_qty = sum(move.move_line_ids.mapped('quantity'))
                if float_compare(total_ml_qty, move.product_uom_qty,
                                 precision_rounding=move.product_uom.rounding) < 0:
                    first_ml = move.move_line_ids[0]
                    first_ml.write({'quantity': move.product_uom_qty, 'product_uom_id': move.product_uom.id})
                    (move.move_line_ids - first_ml).write({'quantity': 0})
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
            move.picked = True
        request.env.flush_all()
        self._assert_van_stock(picking)
        picking.with_context(
            skip_backorder=True,
            skip_sms=True,
            mail_notrack=True,
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
            no_reset_password=True,
        ).button_validate()
        picking.invalidate_recordset(['state'])
        _logger.info("Sync: Delivery %s -> %s", picking.name, picking.state)

    def _upload_order_lifecycle(self, order, sales_rep, user, local_id, upload_data, ack, quick=False):
        """Confirm -> deliver -> invoice -> (cash) pay for an uploaded order.

        Each stage runs in its own savepoint: a failing stage is rolled back
        cleanly (never leaving the cursor in an aborted state) and reported to
        the app in ack['order_warnings'] instead of being silently swallowed.
        """
        warnings = ack.setdefault('order_warnings', {})
        try:
            with request.env.cr.savepoint():
                self._fix_loyalty_config(request.env)
                order.action_confirm()
            _logger.info("Sync: Confirmed Order %s", order.name)
        except Exception as e:
            _logger.error("Sync: Confirmation failed for %s: %s", order.name, e)
            warnings.setdefault(local_id, {}).update({'stage': 'confirm', 'message': str(e)})
            return

        # Quick invoice (POS-style) always delivers from the van and invoices at once.
        if sales_rep.auto_delivery or quick:
            try:
                with request.env.cr.savepoint():
                    order.invalidate_recordset(['picking_ids'])
                    for picking in order.picking_ids.filtered(lambda p: p.state not in ('done', 'cancel')):
                        self._validate_van_delivery(picking.sudo())
            except Exception as e:
                _logger.warning("Sync: Delivery skipped for %s: %s", order.name, e)
                warnings.setdefault(local_id, {}).update({'stage': 'delivery', 'message': str(e)})
                return

            try:
                with request.env.cr.savepoint():
                    invoices = order.with_user(user)._create_invoices()
                    for inv in invoices:
                        inv.action_post()
                        if not order.is_cash:
                            continue
                        has_order_collection = any(
                            (c.get('order_id') == order.id or c.get('order_local_id') == local_id or c.get('order_id') == local_id)
                            for c in upload_data.get('collections', [])
                        )
                        if (not has_order_collection and inv.payment_state not in ('paid', 'in_payment')
                                and inv.amount_residual == inv.amount_total):
                            self._reconcile_cash_invoice(inv, order, sales_rep)
                    if invoices:
                        _logger.info("Sync: Invoice %s posted for %s", invoices[0].name, order.name)
            except Exception as inv_err:
                _logger.warning("Sync: Invoice creation skipped for %s: %s", order.name, inv_err)
                warnings.setdefault(local_id, {}).update({'stage': 'invoice', 'message': str(inv_err)})

    def _auto_deliver_and_invoice(self, order, user, sales_rep=None):
        """Helper: process delivery + create invoice for an order."""
        try:
            # Re-fetch picking_ids in case promotions added new moves/pickings
            order.invalidate_recordset(['picking_ids'])
            for picking in order.picking_ids:
                if picking.state in ('done', 'cancel'):
                    continue
                self._validate_van_delivery(picking.sudo())
            invoices = order.with_user(user)._create_invoices()
            if invoices:
                for inv in invoices:
                    inv.action_post()
                    # ADDED: Check cash status for standalone confirmation actions
                    if order.is_cash:
                        if inv.payment_state not in ('paid', 'in_payment') and inv.amount_residual == inv.amount_total:
                            _logger.info(f"Sync: Order {order.name} is CASH with no prior payments, triggering auto-payment for invoice {inv.name} in standalone confirm")
                            self._reconcile_cash_invoice(inv, order, sales_rep)
                        else:
                            _logger.info(f"Sync: Order {order.name} is CASH but has payments/reduced residual, skipping auto-payment in standalone confirm")
                    else:
                        _logger.info(f"Sync: Order {order.name} is CREDIT, skipping auto-payment in standalone confirm")
        except Exception as e:
            _logger.warning(f"Auto deliver/invoice failed for {order.name}: {e}")
