# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class CustomerDebtReportWizard(models.TransientModel):
    _name = 'customer.debt.report.wizard'
    _description = 'Customer Debt Report Wizard'

    company_id = fields.Many2one(
        'res.company', 
        string='Company', 
        required=True, 
        default=lambda self: self.env.company
    )
    is_branch = fields.Boolean(string='Is Branch', compute='_compute_is_branch')
    sales_rep_ids = fields.Many2many('sales.representative', string='Sales Representatives')
    
    @api.depends('company_id')
    def _compute_is_branch(self):
        for wizard in self:
            wizard.is_branch = bool(wizard.company_id.parent_id)

    def get_sales_rep_names(self):
        if self.sales_rep_ids:
            return ", ".join(self.sales_rep_ids.mapped('name'))
        return "All"

    def get_today_date(self):
        return fields.Date.context_today(self).strftime('%Y-%m-%d')

    def get_debt_data(self):
        sales_reps = self.sales_rep_ids.ids if self.sales_rep_ids else self.env['sales.representative'].search([('company_id', '=', self.company_id.id)]).ids
        
        _logger.info(f"Debt Report: Starting data retrieval for reps: {sales_reps}")
        
        # 1. Customers from Routes
        routes = self.env['sales.rep.route'].sudo().search([
            ('sales_rep_id', 'in', sales_reps),
            ('company_id', '=', self.company_id.id)
        ])
        route_partner_ids = self.env['sales.route.customer'].sudo().search([
            ('route_id', 'in', routes.ids),
            ('company_id', '=', self.company_id.id)
        ]).mapped('partner_id').ids
        _logger.info(f"Debt Report: Found {len(route_partner_ids)} partners from {len(routes)} routes")

        # 2. Customers from Territories (Partner Categories)
        rep_records = self.env['sales.representative'].sudo().browse(sales_reps)
        territory_ids = rep_records.mapped('territory_ids').ids
        territory_partner_ids = []
        if territory_ids:
            territory_partner_ids = self.env['res.partner'].sudo().search([
                ('category_id', 'in', territory_ids),
                '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)
            ]).ids
            _logger.info(f"Debt Report: Found {len(territory_partner_ids)} partners from territories {territory_ids}")

        # 3. Customers where rep's user is salesperson
        user_ids = rep_records.mapped('user_id').ids
        salesperson_partner_ids = self.env['res.partner'].sudo().search([
            ('user_id', 'in', user_ids),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)
        ]).ids
        _logger.info(f"Debt Report: Found {len(salesperson_partner_ids)} partners where rep is salesperson")

        # Combine all unique partner IDs
        all_partner_ids = list(set(route_partner_ids + territory_partner_ids + salesperson_partner_ids))
        _logger.info(f"Debt Report: Total unique partner IDs to check: {len(all_partner_ids)}")

        if not all_partner_ids:
            return self.env['res.partner']

        domain = [
            ('id', 'in', all_partner_ids),
            '|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)
        ]
        
        # Execute search with sudo
        partners_all = self.env['res.partner'].sudo().search(domain)
        _logger.info(f"Debt Report: Fetched {len(partners_all)} partner records from DB")
        
        # Filter for debt > 0 (one grouped query for all customers)
        due = self.env['res.partner']._sales_rep_due_map(partners_all.ids, self.company_id.id)
        partners = partners_all.filtered(lambda p: due.get(p.id, 0.0) > 0).sorted(key=lambda p: due.get(p.id, 0.0), reverse=True)
        _logger.info(f"Debt Report: Final count with debt > 0: {len(partners)}")
        
        return partners

    def get_debt_data_by_rep(self):
        """[(rep, partners, due_map)]: each rep's customers kept apart."""
        reps = self.sales_rep_ids or self.env['sales.representative'].search([('company_id', '=', self.company_id.id)])
        result = []
        for rep in reps:
            partners = self.new({'company_id': self.company_id.id, 'sales_rep_ids': [(6, 0, rep.ids)]}).get_debt_data()
            if partners:
                due = self.env['res.partner']._sales_rep_due_map(partners.ids, self.company_id.id)
                result.append((rep, partners, due))
        return result

    def get_open_invoices(self, partner):
        """Posted customer invoices/refunds of the partner that are not fully paid."""
        return self.env['account.move'].sudo().search([
            ('partner_id', '=', partner.id), ('company_id', '=', self.company_id.id),
            ('move_type', 'in', ('out_invoice', 'out_refund')), ('state', '=', 'posted'),
            ('payment_state', 'in', ('not_paid', 'partial')),
        ], order='invoice_date_due, invoice_date, id')

    def action_print_pdf(self):
        self.ensure_one()
        partners = self.get_debt_data()
        
        if not partners:
            raise UserError(_("No customers with debt found for the selected criteria."))

        data = {
            'partner_ids': partners.ids,
        }
        return self.env.ref('sales_rep_management.action_report_customer_debt_report').report_action(self, data=data)

    def action_print_excel(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'/sales_rep_management/customer_debt_report_excel/{self.id}',
            'target': 'new',
        }
