from odoo import models, fields, api
import logging
import requests
import threading
import odoo

_logger = logging.getLogger(__name__)

class ResPartner(models.Model):
    _inherit = 'res.partner'

    mobile_local_id = fields.Char(string='Mobile Local ID', index=True)
    is_cash = fields.Boolean(string='Cash Customer', default=True)
    area = fields.Char(string='Area')

    currency_id = fields.Many2one(
        'res.currency', 
        related='company_id.currency_id', 
        string='Currency'
    )

    @api.model
    def _sales_rep_due_map(self, partner_ids, company_id=None):
        """Receivable balance per partner in ONE grouped query.

        Same rule as _get_total_due (posted receivable lines of the company,
        negative balances clamp to 0) but for many partners at once, so the
        mobile sync no longer runs one search per customer.
        """
        partner_ids = [pid for pid in set(partner_ids or []) if pid]
        if not partner_ids:
            return {}
        groups = self.env['account.move.line'].sudo()._read_group(
            [('partner_id', 'in', partner_ids),
             ('move_id.state', '=', 'posted'),
             ('company_id', '=', company_id or self.env.company.id),
             ('account_id.account_type', '=', 'asset_receivable')],
            ['partner_id'], ['balance:sum'])
        due = {pid: 0.0 for pid in partner_ids}
        for partner, balance in groups:
            due[partner.id] = max(balance or 0.0, 0.0)
        return due

    def _get_total_due(self):
        """حساب إجمالي المستحق للعميل"""
        self.ensure_one()
        
        # if 'total_due' in self._fields:
        #     return self.total_due or 0.0

        # ابحث عن الحركات غير المصفاة في أي حساب ذمم
        receivables = self.env['account.move.line'].sudo().search([
            ('partner_id', '=', self.id),
            ('move_id.state', '=', 'posted'),
            ('company_id', '=', self.env.company.id),
            # ('reconciled', '=', False),
            ('account_id.account_type', '=', 'asset_receivable'),
        ])
        
        total = sum(receivables.mapped('balance'))
        return total if total > 0 else 0.0

    _sql_constraints = [
        ('mobile_local_id_unique', 'unique(mobile_local_id)', 'The Mobile Local ID must be unique per partner!'),
    ]
    
    @api.model
    def _default_created_sales_rep_id(self):
        return self.env['sales.representative'].search([('user_id', '=', self.env.user.id)], limit=1)

    created_sales_rep_id = fields.Many2one(
        'sales.representative', 
        string='Created By Sales Rep', 
        # default=_default_created_sales_rep_id,
        help="The sales representative who created this customer record."
    )

    enable_location = fields.Boolean(
        string='Enable Location',
        default=lambda self: self._default_enable_location(),
        help="Enable location tracking for this customer."
    )
    
    location_radius = fields.Float(
        string='Location Radius',
        default=lambda self: self._default_location_radius(),
        help="Radius (in meters) around the customer location for valid visits."
    )


    is_location_tracking_enabled = fields.Boolean(
        string="Location Tracking Enabled",
        compute='_compute_is_location_tracking_enabled'
    )

    is_location_restriction_enabled = fields.Boolean(
        string="Location Restriction Enabled",
        compute='_compute_is_location_restriction_enabled'
    )

    visit_latitude = fields.Float(string='Visit Latitude', digits=(16, 7))
    visit_longitude = fields.Float(string='Visit Longitude', digits=(16, 7))
    location_type = fields.Selection([
        ('customer', 'Customer'),
        ('house', 'House'),
        ('company', 'Company'),
        ('other', 'Other')
    ], string='Location Type', default='customer')

    @api.model_create_multi
    def create(self, vals_list):
        ICPSudo = self.env['ir.config_parameter'].sudo()
        is_tracking_enabled = ICPSudo.get_param('sales_rep_management.is_location_tracking_enabled', 'False') == 'True'
        location_restriction_global = ICPSudo.get_param('sales_rep_management.location_restriction', 'False') == 'True'
        for vals in vals_list:
            if (not is_tracking_enabled or not location_restriction_global) and vals.get('enable_location'):
                vals['enable_location'] = False
            if not vals.get('created_sales_rep_id') and vals.get('mobile_local_id'):
                current_user_rep = self.env['sales.representative'].search([('user_id', '=', self.env.user.id)], limit=1)
                if current_user_rep:
                    vals['created_sales_rep_id'] = current_user_rep.id
        return super().create(vals_list)

    def write(self, vals):
        if vals.get('enable_location'):
            ICPSudo = self.env['ir.config_parameter'].sudo()
            is_tracking_enabled = ICPSudo.get_param('sales_rep_management.is_location_tracking_enabled', 'False') == 'True'
            location_restriction_global = ICPSudo.get_param('sales_rep_management.location_restriction', 'False') == 'True'
            if not is_tracking_enabled or not location_restriction_global:
                vals['enable_location'] = False
        return super().write(vals)

    @api.depends_context('company')
    def _compute_is_location_tracking_enabled(self):
        ICPSudo = self.env['ir.config_parameter'].sudo()
        is_enabled = ICPSudo.get_param('sales_rep_management.is_location_tracking_enabled', 'False')
        for record in self:
            record.is_location_tracking_enabled = (is_enabled == 'True')

    @api.depends_context('company')
    def _compute_is_location_restriction_enabled(self):
        ICPSudo = self.env['ir.config_parameter'].sudo()
        is_tracking_enabled = ICPSudo.get_param('sales_rep_management.is_location_tracking_enabled', 'False') == 'True'
        is_enabled = ICPSudo.get_param('sales_rep_management.location_restriction', 'False') == 'True'
        for record in self:
            record.is_location_restriction_enabled = is_tracking_enabled and is_enabled

    @api.model
    def _default_enable_location(self):
        ICPSudo = self.env['ir.config_parameter'].sudo()
        is_tracking_enabled = ICPSudo.get_param('sales_rep_management.is_location_tracking_enabled', 'False') == 'True'
        is_enabled = ICPSudo.get_param('sales_rep_management.location_restriction', 'False') == 'True'
        return is_tracking_enabled and is_enabled

    @api.model
    def _default_location_radius(self):
        """Fetch default radius from settings."""
        ICPSudo = self.env['ir.config_parameter'].sudo()
        try:
            return float(ICPSudo.get_param('sales_rep_management.location_radius', 50.0))
        except ValueError:
            return 50.0

    def action_reverse_geocode(self, background=False):
        """Action to perform reverse geocoding from visit_latitude/longitude using a background thread."""
        for record in self:
            if record.visit_latitude and record.visit_longitude:
                if background:
                    db_name = self.env.cr.dbname
                    # Start background thread to avoid blocking sync
                    thread = threading.Thread(target=self._threaded_reverse_geocode, args=(db_name, record.id, record.visit_latitude, record.visit_longitude))
                    thread.start()
                else:
                    # Execute synchronously to update UI immediately
                    record._reverse_geocode_location(record.visit_latitude, record.visit_longitude)
        
        if not background:
            # Return an action to reload the record so changes are visible
            return {'type': 'ir.actions.client', 'tag': 'soft_reload'}

    def _threaded_reverse_geocode(self, db_name, partner_id, lat, lng):
        """Method to be executed in a background thread."""
        try:
            registry = odoo.modules.registry.Registry(db_name)
            with registry.cursor() as cr:
                env = odoo.api.Environment(cr, odoo.SUPERUSER_ID, {})
                partner = env['res.partner'].browse(partner_id)
                if partner.exists():
                    partner._reverse_geocode_location(lat, lng)
        except Exception as e:
            _logger.error(f"Geocoding Thread Error for partner {partner_id}: {e}")

    def _reverse_geocode_location(self, lat, lng):
        """Helper to call Nominatim and update address fields."""
        url = "https://nominatim.openstreetmap.org/reverse"
        params = {
            'lat': lat,
            'lon': lng,
            'format': 'json',
            'addressdetails': 1,
            'accept-language': 'en',
            'zoom': 18,
            'extratags': 1
        }
        headers = {
            'User-Agent': 'Odoo-SalesRepManagement/1.0'
        }
        try:
            # Reduced timeout to 5 seconds to be more responsive
            response = requests.get(url, params=params, headers=headers, timeout=5)
            if response.status_code == 200:
                data = response.json()
                address = data.get('address', {})
                _logger.info(f"Geocoding: Address for partner {self.id}: {address}")
                
                # 1. Prepare Street
                road = address.get('road', '')
                house_number = address.get('house_number', '')
                neighbourhood = address.get('neighbourhood', '')
                suburb = address.get('suburb', '')
                
                parts = [p for p in [house_number, road, neighbourhood, suburb] if p]
                street = ", ".join(parts)
                
                # 2. Prepare City
                city = address.get('city') or address.get('town') or address.get('village') or address.get('suburb') or address.get('county', '')
                
                # 3. Zip
                zip_code = address.get('postcode', '')
                
                # 4. Country
                country_code = address.get('country_code', '').upper()
                country = self.env['res.country'].search([('code', '=', country_code)], limit=1)

                # 5. Area
                area = address.get('suburb') or address.get('neighbourhood') or address.get('city_district') or address.get('quarter') or address.get('hamlet') or address.get('borough') or address.get('municipality') or address.get('state_district', '')

                vals = {}
                if street: vals['street'] = street
                if city: vals['city'] = city
                if zip_code: vals['zip'] = zip_code
                if country: vals['country_id'] = country.id
                if area: vals['area'] = area
                
                if vals:
                    self.sudo().write(vals)
                    _logger.info(f"Geocoding: Updated partner {self.id} with vals {vals}")
            else:
                _logger.warning(f"Geocoding: API returned {response.status_code}")
        except Exception as e:
            _logger.error(f"Geocoding: Failed for partner {self.id}: {e}")
