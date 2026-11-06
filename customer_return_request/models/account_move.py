# -*- coding: utf-8 -*-
from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    customer_return_request_id = fields.Many2one(
        'customer.return.request', string='Customer Return Request',
        copy=False, index=True, ondelete='set null',
    )
