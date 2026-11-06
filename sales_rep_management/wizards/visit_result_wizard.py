from odoo import models, fields, api, _
from odoo.exceptions import ValidationError


class VisitResultWizard(models.TransientModel):
    _name = 'visit.result.wizard'
    _description = 'Visit Result Wizard'
    _inherit = ['mail.thread']

    route_customer_id = fields.Many2one(
        'sales.route.customer',
        string='Route Customer',
        required=True,
        readonly=True
    )
    visit_id = fields.Many2one(
        'sales.rep.visit',
        string='Visit',
        required=True,
        readonly=True
    )
    visit_result = fields.Selection([
        ('successful', 'Successful'),
        ('customer_unavailable', 'Customer Unavailable'),
        ('refused', 'Refused'),
        ('closed', 'Location Closed'),
        ('rescheduled', 'Rescheduled')
    ], string='Visit Result', required=True)
    visit_notes = fields.Text(string='Visit Notes')
    follow_up_required = fields.Boolean(string='Follow-up Required')
    next_visit_date = fields.Date(string='Next Visit Date')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if self._context.get('default_route_customer_id'):
            customer = self.env['sales.route.customer'].browse(self._context['default_route_customer_id'])
            res['visit_result'] = customer.visit_result
            res['visit_notes'] = customer.visit_notes
        return res

    def action_confirm_visit(self):
        self.ensure_one()
        # Update route customer
        self.route_customer_id.write({
            'state': 'visited',
            'visit_result': self.visit_result,
            'visit_notes': self.visit_notes
        })

        # Update visit record
        self.visit_id.write({
            'state': 'completed',
            'visit_time': fields.Datetime.now(),
            'visit_result': self.visit_result,
            'notes': self.visit_notes,
            'follow_up_required': self.follow_up_required,
            'next_visit_date': self.next_visit_date
        })

        # Calculate duration
        if self.visit_id.planned_time and self.visit_id.visit_time:
            planned = self.visit_id.planned_time
            actual = self.visit_id.visit_time
            self.visit_id.duration = (actual - planned).total_seconds() / 3600  # in hours

        return {'type': 'ir.actions.act_window_close'}