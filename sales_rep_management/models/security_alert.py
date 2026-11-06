# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import api, models, fields, _


class SalesRepSecurityAlert(models.Model):
    _name = 'sales.rep.security.alert'
    _description = 'Sales Rep Security Alert'
    _order = 'detected_at desc'

    active = fields.Boolean(default=True, help='Uncheck to dismiss this alert from the dashboard.')
    sales_rep_id = fields.Many2one(
        'sales.representative',
        string='Sales Representative',
        required=True,
        ondelete='cascade',
        index=True,
    )
    detected_at = fields.Datetime(
        string='Detected At',
        required=True,
        default=fields.Datetime.now,
        index=True,
    )
    latitude = fields.Float(string='Latitude', digits=(10, 6))
    longitude = fields.Float(string='Longitude', digits=(10, 6))
    accuracy = fields.Float(string='Accuracy (m)')
    device_identifier = fields.Char(string='Device Identifier')
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='sales_rep_id.company_id',
        store=True,
        index=True,
    )
    alert_type = fields.Selection(
        string='Alert Type',
        selection=[
            ('mock_location', 'Mock Location'),
            ('developer_mode', 'Developer Mode'),
            ('unknown', 'Unknown'),
        ],
        required=True,
        index=True,
    )

    def action_dismiss(self):
        self.write({'active': False})

    @api.model_create_multi
    def create(self, vals_list):
        alerts = super().create(vals_list)
        alerts._notify_management()
        return alerts

    def _notify_management(self):
        """Tell the supervisor (or the sales managers) right away.

        One to-do per rep and alert type every 30 minutes, so a phone that
        reports the same mock location every few seconds does not flood
        anybody; every alert is still kept in the list and on the rep's chatter.
        """
        managers = self.env.ref('sales_rep_management.group_sales_manager', raise_if_not_found=False)
        for alert in self:
            rep = alert.sales_rep_id
            label = dict(self._fields['alert_type'].selection).get(alert.alert_type, alert.alert_type)
            where = (f" ({alert.latitude:.5f}, {alert.longitude:.5f})" if alert.latitude or alert.longitude else '')
            rep.message_post(body=_("Security alert: %(type)s detected on the rep's phone%(where)s.",
                                    type=label, where=where))
            recent = self.search_count([
                ('id', '!=', alert.id), ('sales_rep_id', '=', rep.id), ('alert_type', '=', alert.alert_type),
                ('detected_at', '>=', alert.detected_at - timedelta(minutes=30))])
            if recent:
                continue
            users = rep.supervisor_id.user_id or (managers.users if managers else self.env['res.users'])
            for user in users[:5]:
                rep.activity_schedule(
                    'mail.mail_activity_data_warning' if self.env.ref('mail.mail_activity_data_warning', raise_if_not_found=False)
                    else 'mail.mail_activity_data_todo',
                    user_id=user.id,
                    summary=_("%(type)s: %(rep)s", type=label, rep=rep.name),
                    note=_("Detected at %s. Check the rep's location history before approving today's visits.",
                           fields.Datetime.to_string(alert.detected_at)))
