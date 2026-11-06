# -*- coding: utf-8 -*-
from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    customer_return_request_id = fields.Many2one(
        'customer.return.request', string='Customer Return Request',
        copy=False, index=True, ondelete='set null',
    )

    def _action_done(self):
        res = super()._action_done()
        requests = self.customer_return_request_id
        if requests:
            requests._on_picking_done()
        return res
