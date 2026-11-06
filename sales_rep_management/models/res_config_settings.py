from odoo import models, fields, api

class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    is_location_tracking_enabled = fields.Boolean(
        string="Enable Location Tracking",
        config_parameter='sales_rep_management.is_location_tracking_enabled',
        help="Enable tracking of sales representative locations during visits."
    )

    location_restriction = fields.Boolean(
        string="Location restriction",
        config_parameter='sales_rep_management.location_restriction',
        help="Enable tracking of customer during visits."
    )

    location_radius = fields.Float(
        string="Location Radius",
        config_parameter='sales_rep_management.location_radius',
        default=50.0,
        help="Default radius (in meters) around the customer location to consider a visit valid."
    )

    hr_contract_deactive = fields.Boolean(
        string="HR Contract Deactive",
        config_parameter='sales_rep_management.hr_contract_deactive',
        help="Deactivate the contract of the sales representative if the contract is expired or cancelled."
    )

    mobile_service_logins = fields.Char(
        string="Mobile Service Logins",
        config_parameter='sales_rep_management.mobile_service_logins',
        help="Comma-separated logins of the shared service user(s) the master server hands to the app. "
             "Only these users may identify a representative by e-mail at mobile login. "
             "Any other user is always bound to the representative linked to it.")

    mobile_token_days = fields.Integer(
        string='Mobile Access Days',
        config_parameter='sales_rep_management.mobile_token_days',
        default=30,
        help="Days a phone stays signed in without being used. 0 = no limit.",
    )
    mobile_device_binding = fields.Boolean(
        string='One Phone per Representative',
        config_parameter='sales_rep_management.mobile_device_binding',
        default=True,
        help="Refuse requests from another phone than the one used at the last login.",
    )
    allow_negative_van_stock = fields.Boolean(
        string="Allow Negative Van Stock",
        config_parameter='sales_rep_management.allow_negative_van_stock',
        help="When disabled (recommended), a delivery from a van is refused if the van does not hold the quantity.")

    is_hr_contract_installed = fields.Boolean(
        default=lambda self: self.env['ir.module.module'].sudo().search_count([
            ('name', '=', 'hr_contract'),
            ('state', '=', 'installed')
        ]) > 0
    )


    # Compatibility fields for Odoo 18 transition
    # These fields are required because stale views in the database (from Odoo 17)
    # may still reference them, causing Owl framework crashes if not defined.
    default_picking_policy = fields.Selection([
        ('direct', 'Deliver each product when available'),
        ('one', 'Deliver all products at once')
    ], string='Shipping Policy', default='direct', help='Compatibility field for un-migrated views')

    currency_provider = fields.Selection([
        ('ecb', 'European Central Bank'),
    ], string='Service', help='Compatibility field for un-migrated views', default='ecb')
    currency_interval_unit = fields.Selection([
        ('manually', 'Manually'),
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
    ], string='Interval Unit', help='Compatibility field for un-migrated views', default='manually')
    currency_next_execution_date = fields.Date(string='Next Execution Date', help='Compatibility field for un-migrated views', default=fields.Date.today())

    def set_values(self):
        if not self.is_location_tracking_enabled:
            self.location_restriction = False
        super().set_values()
        if self.location_restriction:
            self.env['res.partner'].sudo().search([('enable_location', '=', False)]).write({'enable_location': True})
        else:
            self.env['res.partner'].sudo().search([('enable_location', '=', True)]).write({'enable_location': False})
        
        if self.hr_contract_deactive and 'hr.contract' in self.env:
            active_reps = self.env['sales.representative'].sudo().search([('active', '=', True), ('employee_id', '!=', False)])
            reps_to_deactivate = self.env['sales.representative']
            for rep in active_reps:
                contracts = self.env['hr.contract'].sudo().search([('employee_id', '=', rep.employee_id.id)])
                if contracts and not any(c.state == 'open' for c in contracts):
                    reps_to_deactivate |= rep
            if reps_to_deactivate:
                reps_to_deactivate.write({'active': False})
