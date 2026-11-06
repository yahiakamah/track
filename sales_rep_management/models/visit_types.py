# -*- coding: utf-8 -*-
from odoo import models, fields

class SalesRepVisitTypes(models.Model):
    _name = 'sales.rep.visit.types'
    _description = 'Visit types'
    _order = 'name'

    name = fields.Char(string='Visit type', required=True, translate=True)
    description = fields.Text(string='Description', translate=True)
