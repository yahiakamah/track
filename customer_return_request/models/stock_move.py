# -*- coding: utf-8 -*-
from odoo import fields, models


class StockMove(models.Model):
    _inherit = 'stock.move'

    customer_return_line_id = fields.Many2one(
        'customer.return.request.line', string='Customer Return Line',
        copy=False, index=True, ondelete='set null',
    )

    def _get_price_unit(self):
        """Inject the manager-approved inventory unit cost into the valuation.

        For AVCO / FIFO products the incoming stock valuation layer is valued
        at ``_get_price_unit``. By returning the cost entered on the return
        line (converted to the product reference UoM) we make the SVL use
        ``quantity * inventory_unit_cost`` instead of zero, without creating
        any custom stock move or manual journal entry.

        Standard-cost products keep using the product standard price (Odoo
        ignores the price unit for them), which is the expected behaviour.

        Note: Odoo 18 requires this method to return a dict of the form
        {stock.lot: float} rather than a plain float.  We mirror the format
        used by the core for non-lot-valuated products.
        """
        self.ensure_one()
        line = self.customer_return_line_id
        if line and line.inventory_unit_cost:
            price = line.product_uom_id._compute_price(
                line.inventory_unit_cost, line.product_id.uom_id)
            # Odoo 18 core expects a dict {lot_record: price}; use the empty
            # stock.lot singleton as the key for non-lot-valuated products.
            return {self.env['stock.lot']: price}
        return super()._get_price_unit()
