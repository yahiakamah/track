from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
from odoo.fields import Date
from dateutil.relativedelta import relativedelta

class HrEmployee(models.Model):
    _inherit = 'hr.employee'

    is_driver = fields.Boolean(
        string="Is Driver",
        help="Check this box if the employee is a driver.",
        tracking=True
    )

    license_number = fields.Char(string="License Number")
    license_issue_date = fields.Date(string="Issue Date")
    license_expiry_date = fields.Date(string="Expiry Date")
    license_attachment = fields.Binary(string="License Scan", attachment=True)

    get_notify = fields.Boolean(
        string="Get Notifications",
        help="Check this box if you want to get notification before the expiration",
        default=False,
        tracking=True
    )

    notify_before_days = fields.Integer(
        string="Notify Before (Days)",
        default=7,
        help="Number of days before the expiration date to send the notification."
    )

    notification_recipient_ids = fields.Many2many(
        'res.users',
        'employee_user_notification_rel',     # relation table name
        'employee_id',                        # column name
        'user_id',                            # column name
        string="Recipients",
        help="Users who will receive the notification."
    )

    notification_type = fields.Selection([
        ('odoo', 'Odoo Activity'),
        ('email', 'Email'),
        ('both', 'Activity and Email')
    ], string="Notification Method", default='odoo')

    @api.model
    def _check_license_expirations(self):
        today = Date.today()
        drivers = self.search([
            ('is_driver', '=', True),
            ('get_notify', '=', True),
            ('license_expiry_date', '!=', False)
        ])

        for driver in drivers:
            notification_date = driver.license_expiry_date - relativedelta(days=driver.notify_before_days)
            if today == notification_date:
                self._send_expiration_notification(driver)

    def _send_expiration_notification(self, driver):
        if not driver.notification_recipient_ids:
            return

        activity_type_id = self.env.ref('mail.mail_activity_data_todo').id
        subject = f"License Expiration Warning: {driver.name}"
        body_html = f"""
            <p>Hello,</p>
            <p>This is a reminder that the driver's license for employee <b>{driver.name}</b>
            (License #: {driver.license_number or 'N/A'}) is set to expire on
            <b>{driver.license_expiry_date.strftime('%d-%b-%Y')}</b>.</p>
            <p>Please take the necessary action.</p>
        """

        for recipient in driver.notification_recipient_ids:

            # --- Create Odoo Activity if type is 'odoo' or 'both' ---
            if driver.notification_type in ('odoo', 'both'):
                self.env['mail.activity'].create({
                    'res_id': driver.id,
                    'res_model_id': self.env['ir.model']._get_id(self._name),
                    'activity_type_id': activity_type_id,
                    'summary': subject,
                    'note': body_html,
                    'user_id': recipient.id,  # CHANGED: Use recipient.id directly
                    'date_deadline': driver.license_expiry_date,
                })

            # --- Send Email if type is 'email' or 'both' ---
            if driver.notification_type in ('email', 'both'):
                # CHANGED: Users have an 'email' field, not 'work_email'
                if recipient.email:
                    mail_values = {
                        'subject': subject,
                        'body_html': body_html,
                        'email_to': recipient.email,
                    }
                    self.env['mail.mail'].sudo().create(mail_values).send()

    def action_view_attachment(self):
        """This method is used to view the attachment in a new tab."""
        self.ensure_one()
        if not self.license_attachment:
            raise ValidationError(_("There is no attachment to view."))

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{self._name}/{self.id}/license_attachment',
            'target': 'new',
        }

class HrEmployeePublic(models.Model):
    _inherit = 'hr.employee.public'

    is_driver = fields.Boolean(related='employee_id.is_driver', readonly=True)
    license_number = fields.Char(related='employee_id.license_number', readonly=True)
    license_issue_date = fields.Date(related='employee_id.license_issue_date', readonly=True)
    license_expiry_date = fields.Date(related='employee_id.license_expiry_date', readonly=True)
    get_notify = fields.Boolean(related='employee_id.get_notify', readonly=True)
    notify_before_days = fields.Integer(related='employee_id.notify_before_days', readonly=True)
    notification_type = fields.Selection(related='employee_id.notification_type', readonly=True)
