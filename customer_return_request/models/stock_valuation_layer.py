# -*- coding: utf-8 -*-
from odoo import fields, models


class StockValuationLayer(models.Model):
    _inherit = 'stock.valuation.layer'

    customer_return_request_id = fields.Many2one(
        'customer.return.request',
        string='Customer Return Request',
        related='stock_move_id.customer_return_line_id.request_id',
        store=True, index=True,
    )
