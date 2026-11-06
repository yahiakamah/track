# -*- coding: utf-8 -*-
from odoo import models, api, _

class SalesReport(models.AbstractModel):
    _name = 'report.sales_rep_management.report_sales_report'
    _description = 'Sales Report PDF Parser'

    @api.model
    def _get_report_values(self, docids, data=None):
        form = data.get('form', {})
        order_ids = data.get('order_ids', [])
        orders = self.env['sale.order'].browse(order_ids)
        
        # Get sales reps names for the header
        sales_rep_ids = form.get('sales_rep_ids', [])
        if sales_rep_ids:
            reps = self.env['sales.representative'].browse(sales_rep_ids)
            rep_names = ", ".join(reps.mapped('name'))
        else:
            rep_names = "All"

        return {
            'doc_ids': docids,
            'doc_model': 'sales.report.wizard',
            'docs': self.env['sales.report.wizard'].browse(docids),
            'form': form,
            'orders': orders,
            'sales_rep_names': rep_names,
        }
