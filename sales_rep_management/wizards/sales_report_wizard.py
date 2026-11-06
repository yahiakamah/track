# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError

class SalesReportWizard(models.TransientModel):
    _name = 'sales.report.wizard'
    _description = 'Sales Report Wizard'

    company_id = fields.Many2one(
        'res.company', 
        string='Company', 
        required=True, 
        default=lambda self: self.env.company
    )
    is_branch = fields.Boolean(string='Is Branch', compute='_compute_is_branch')
    sales_rep_ids = fields.Many2many('sales.representative', string='Sales Representatives')
    date_from = fields.Date(string='From Date', required=True, default=fields.Date.context_today)
    date_to = fields.Date(string='To Date', required=True, default=fields.Date.context_today)

    @api.depends('company_id')
    def _compute_is_branch(self):
        for wizard in self:
            wizard.is_branch = bool(wizard.company_id.parent_id)

    def get_sales_rep_names(self):
        if self.sales_rep_ids:
            return ", ".join(self.sales_rep_ids.mapped('name'))
        return "All"

    def get_orders(self):
        sales_reps = self.sales_rep_ids.ids if self.sales_rep_ids else self.env['sales.representative'].search([('company_id', '=', self.company_id.id)]).ids
        
        # Build domain for sale.order
        # Use start of day for date_from and end of day for date_to
        date_from_dt = fields.Datetime.to_datetime(self.date_from)
        date_to_dt = fields.Datetime.to_datetime(self.date_to).replace(hour=23, minute=59, second=59)
        
        domain = [
            ('date_order', '>=', date_from_dt),
            ('date_order', '<=', date_to_dt),
            ('state', 'in', ['sale', 'done']),
            ('sales_rep_id', 'in', sales_reps),
            ('company_id', '=', self.company_id.id)
        ]
        return self.env['sale.order'].search(domain, order='date_order asc')

    def action_print_pdf(self):
        self.ensure_one()
        orders = self.get_orders()
        
        if not orders:
            raise UserError(_("No sales found for the selected criteria."))

        data = {
            'order_ids': orders.ids,
        }
        return self.env.ref('sales_rep_management.action_report_sales_report').report_action(self, data=data)

    def action_print_excel(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/sales_rep_management/sales_report_excel/{self.id}',
            'target': 'new',
        }
