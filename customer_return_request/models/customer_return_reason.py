# -*- coding: utf-8 -*-
from odoo import fields, models


class CustomerReturnReason(models.Model):
    """Configurable master data for customer return reasons.

    Managers maintain this list from Inventory > Configuration > Customer Returns
    so reasons can be created / edited / archived / reordered without touching
    the code (no hard-coded selection field).
    """

    _name = 'customer.return.reason'
    _description = 'Customer Return Reason'
    _order = 'sequence, name'

    name = fields.Char(string='Reason', required=True, translate=True)
    sequence = fields.Integer(string='Sequence', default=10)
    active = fields.Boolean(string='Active', default=True)
    description = fields.Text(string='Description', translate=True)
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        help="Leave empty to share this reason across all companies.",
    )

    _sql_constraints = [
        ('name_company_uniq',
         'unique(name, company_id)',
         'A return reason with the same name already exists for this company.'),
    ]
