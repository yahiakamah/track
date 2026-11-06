from odoo import models, fields, api, _
from odoo.exceptions import UserError
import datetime

class CollectionReportWizard(models.TransientModel):
    _name = 'collection.report.wizard'
    _description = 'Collection Report Wizard'

    company_id = fields.Many2one(
        'res.company', 
        string='Company', 
        required=True, 
        default=lambda self: self.env.company
    )
    is_branch = fields.Boolean(string='Is Branch', compute='_compute_is_branch')
    date_from = fields.Date(string='Date From', required=True, default=fields.Date.context_today)
    date_to = fields.Date(string='Date To', required=True, default=fields.Date.context_today)
    sales_rep_ids = fields.Many2many('sales.representative', string='Sales Representatives')

    @api.depends('company_id')
    def _compute_is_branch(self):
        for wizard in self:
            wizard.is_branch = bool(wizard.company_id.parent_id)

    def action_print_pdf(self):
        self.ensure_one()
        collections = self._get_collections()
        if not collections:
            raise UserError(_("No collections found for the selected criteria."))
        
        data = {
            'ids': self.ids,
            'model': self._name,
            'form': self.read()[0],
            'collection_ids': collections.ids,
        }
        return self.env.ref('sales_rep_management.action_report_collection_report').report_action(self, data=data)

    def action_print_excel(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/sales_rep_management/collection_report_excel/{self.id}',
            'target': 'new',
        }

    def _get_collections(self):
        domain = [
            ('date', '>=', self.date_from),
            ('date', '<=', self.date_to),
            ('state', 'in', ['paid', 'in_process', 'draft', 'partial']),
            ('payment_type', '=', 'inbound'),
            ('sales_rep_id', '!=', False),
            ('company_id', '=', self.company_id.id)
        ]
        if self.sales_rep_ids:
            domain.append(('sales_rep_id', 'in', self.sales_rep_ids.ids))
        
        return self.env['account.payment'].sudo().search(domain, order='date asc')

    def get_sales_rep_names(self):
        if not self.sales_rep_ids:
            return "All"
        return ", ".join(self.sales_rep_ids.mapped('name'))

    @api.model
    def get_today_date(self):
        return fields.Date.context_today(self).strftime('%Y-%m-%d')
