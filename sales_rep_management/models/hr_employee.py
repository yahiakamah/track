import logging
import random

from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)


class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    # The mobile login identifies a representative by this e-mail (master server
    # flow), so it must be unique. It is no longer mandatory for every employee
    # of the database: only employees linked to a sales representative need it
    # (enforced on sales.representative).
    work_email = fields.Char(string='Work Email')

    _sql_constraints = [
        ('work_email_unique', 'unique(work_email)', 'Work Email must be unique!'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('work_email') or not vals.get('user_id'):
                continue
            user = self.env['res.users'].browse(vals['user_id'])
            email = (user.login or '').strip()
            if '@' not in email:
                continue
            local, domain = email.split('@', 1)
            candidate = email
            while self.search_count([('work_email', '=ilike', candidate)]):
                candidate = f"{local}_{random.randint(1000, 9999)}@{domain}"
            vals['work_email'] = candidate
        return super().create(vals_list)

    @api.constrains('work_email')
    def _check_work_email_unique(self):
        for employee in self:
            if not employee.work_email:
                continue
            domain = [
                ('work_email', '=ilike', employee.work_email),
                ('id', '!=', employee.id),
            ]
            if self.search_count(domain):
                raise ValidationError(
                    _("The work email '%s' is already in use by another employee.", employee.work_email))
