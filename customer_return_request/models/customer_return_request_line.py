# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class CustomerReturnRequestLine(models.Model):
    _name = 'customer.return.request.line'
    _description = 'Customer Return Request Line'
    _check_company_auto = True

    request_id = fields.Many2one(
        'customer.return.request', string='Return Request', required=True,
        ondelete='cascade', index=True,
    )
    company_id = fields.Many2one(
        related='request_id.company_id', store=True, index=True,
    )
    currency_id = fields.Many2one(
        related='request_id.currency_id', store=True,
    )
    company_currency_id = fields.Many2one(
        related='request_id.company_id.currency_id', store=True,
    )
    state = fields.Selection(related='request_id.state', store=True)

    product_id = fields.Many2one(
        'product.product', string='Product', required=True,
        check_company=True,
        domain="[('type', '=', 'consu')]",
    )
    product_uom_category_id = fields.Many2one(
        related='product_id.uom_id.category_id',
    )
    product_uom_id = fields.Many2one(
        'uom.uom', string='Unit of Measure', required=True,
        domain="[('category_id', '=', product_uom_category_id)]",
    )
    quantity = fields.Float(
        string='Quantity', default=1.0, required=True,
        digits='Product Unit of Measure',
    )
    tracking = fields.Selection(related='product_id.tracking')
    lot_id = fields.Many2one(
        'stock.lot', string='Lot/Serial', check_company=True,
        domain="[('product_id', '=', product_id)]",
    )
    inventory_unit_cost = fields.Float(
        string='Inventory Unit Cost', digits='Product Price',
        help="Unit cost used for the stock valuation layer when the request "
             "is approved. Expressed in the line unit of measure.",
    )
    credit_note_unit_price = fields.Float(
        string='Credit Note Unit Price', digits='Product Price',
        help="Sale price used on the customer credit note (untaxed).",
    )
    inventory_value = fields.Monetary(
        string='Inventory Value', compute='_compute_amounts', store=True,
        currency_field='company_currency_id',
    )
    price_subtotal = fields.Monetary(
        string='Subtotal', compute='_compute_amounts', store=True,
        currency_field='currency_id',
    )
    reason_notes = fields.Char(string='Reason Notes')

    # ------------------------------------------------------------------
    # Computes / onchange / constraints
    # ------------------------------------------------------------------
    @api.depends('quantity', 'inventory_unit_cost', 'credit_note_unit_price')
    def _compute_amounts(self):
        for line in self:
            line.inventory_value = line.quantity * line.inventory_unit_cost
            line.price_subtotal = line.quantity * line.credit_note_unit_price

    @api.onchange('product_id')
    def _onchange_product_id(self):
        if self.product_id:
            self.product_uom_id = self.product_id.uom_id
            self.inventory_unit_cost = self.product_id.standard_price
            self.credit_note_unit_price = self.product_id.lst_price
            if self.product_id.tracking == 'none':
                self.lot_id = False

    @api.constrains('product_id', 'product_uom_id')
    def _check_uom_category(self):
        for line in self:
            if (line.product_id and line.product_uom_id
                    and line.product_id.uom_id.category_id
                    != line.product_uom_id.category_id):
                raise ValidationError(_(
                    "The unit of measure of %s must belong to the same "
                    "category as the product reference unit of measure.",
                    line.product_id.display_name,
                ))

    @api.constrains('quantity', 'product_id', 'lot_id')
    def _check_serial_quantity(self):
        for line in self:
            if (line.product_id.tracking == 'serial' and line.lot_id
                    and line.quantity > 1):
                raise ValidationError(_(
                    "Product %s is tracked by serial number, the quantity "
                    "cannot exceed 1 per line.", line.product_id.display_name,
                ))

    # ------------------------------------------------------------------
    # Value preparation for the generated documents
    # ------------------------------------------------------------------
    def _prepare_stock_move_vals(self, picking_type, src_location, dest_location):
        self.ensure_one()
        # Convert the entered cost to the product reference UoM, because the
        # stock.move.price_unit / stock valuation work in the reference UoM.
        price_unit = self.product_uom_id._compute_price(
            self.inventory_unit_cost, self.product_id.uom_id)
        return {
            'name': self.product_id.display_name,
            'product_id': self.product_id.id,
            'product_uom_qty': self.quantity,
            'product_uom': self.product_uom_id.id,
            'location_id': src_location.id,
            'location_dest_id': dest_location.id,
            'picking_type_id': picking_type.id,
            'company_id': self.request_id.company_id.id,
            'customer_return_line_id': self.id,
            'price_unit': price_unit,
        }

    def _prepare_credit_note_line_vals(self):
        self.ensure_one()
        return {
            'product_id': self.product_id.id,
            'quantity': self.quantity,
            'product_uom_id': self.product_uom_id.id,
            'price_unit': self.credit_note_unit_price,
            'name': self.reason_notes or self.product_id.display_name,
        }
