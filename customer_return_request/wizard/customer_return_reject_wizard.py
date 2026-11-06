# -*- coding: utf-8 -*-
from odoo import fields, models, _


class CustomerReturnRejectWizard(models.TransientModel):
    _name = 'customer.return.reject.wizard'
    _description = 'Customer Return Reject Wizard'

    request_id = fields.Many2one(
        'customer.return.request', string='Return Request', required=True,
    )
    reason = fields.Text(string='Rejection Reason', required=True)

    def action_confirm_reject(self):
        self.ensure_one()
        # Store the rejection reason in the manager notes for traceability.
        self.request_id.sudo().write({'manager_notes': self.reason})
        self.request_id.action_reject(reason=self.reason)
        return {'type': 'ir.actions.act_window_close'}
