# -*- coding: utf-8 -*-
from odoo import models, fields, api

class SalesRepReturnReason(models.Model):
    _name = 'sales.rep.return.reason'
    _description = 'Sales Representative Return Reason'
    _order = 'sequence, name'

    name = fields.Char(string='Reason', required=True, translate=True)
    active = fields.Boolean(default=True, help="Set active to false to archive the return reason.")
    sequence = fields.Integer(default=10, help="Gives the sequence order when displaying a list of return reasons.")

    @api.model_create_multi
    def create(self, vals_list):
        return super().create(vals_list)

    def write(self, vals):
        return super().write(vals)
