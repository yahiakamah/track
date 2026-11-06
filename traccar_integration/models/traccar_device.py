from odoo import models, fields, api
from odoo.exceptions import ValidationError
import json


class TraccarDevice(models.Model):
    _name = 'traccar.device'
    _description = 'Traccar GPS Device'
    _rec_name = 'name'

    traccar_id = fields.Integer(
        string='Traccar Device ID',
        index=True
    )
    name = fields.Char(
        string='Device Name',
        required=True
    )
    unique_id = fields.Char(
        string='Unique ID',
        required=True
    )
    status = fields.Selection([
        ('online', 'Online'),
        ('offline', 'Offline'),
        ('unknown', 'Unknown')
    ], string='Status', default='unknown')
    last_update = fields.Datetime(string='Last Update')
    group_id = fields.Integer(string='Group ID')
    phone = fields.Char(string='Phone')
    model = fields.Char(string='Model')
    contact = fields.Char(string='Contact')
    category = fields.Char(string='Category')
    disabled = fields.Boolean(string='Disabled')
    attributes = fields.Text(string='Attributes')
    position_ids = fields.One2many(
        'traccar.position',
        'device_id',
        string='Positions'
    )
    latest_position_id = fields.Many2one(
        'traccar.position',
        string='Latest Position',
        compute='_compute_latest_position',
        store=True
    )
    latitude = fields.Float(
        string='Latest Latitude',
        digits=(10, 6),
        compute='_compute_latest_coordinates',
        store=True
    )
    longitude = fields.Float(
        string='Latest Longitude',
        digits=(10, 6),
        compute='_compute_latest_coordinates',
        store=True
    )
    fleet_vehicle_id = fields.Many2one(
        'fleet.vehicle',
        string='Fleet Vehicle',
        help='Link to fleet vehicle if applicable'
    )
    battery = fields.Integer(
        string='Battery Level',
        compute='_compute_battery_level',
        store=True
    )

    # -------------------------------
    # Prevent Empty Record Creation
    # -------------------------------
    @api.model
    def create(self, vals):
        """Prevent creation of devices without required data"""
        if not vals.get('name') or not vals.get('unique_id'):
            raise ValidationError(
                "You cannot create a device without 'Device Name' and 'Unique ID'."
            )
        return super().create(vals)

    def write(self, vals):
        """Prevent clearing mandatory fields on update"""
        for rec in self:
            new_name = vals.get('name', rec.name)
            new_unique_id = vals.get('unique_id', rec.unique_id)
            if not new_name or not new_unique_id:
                raise ValidationError(
                    "Device must always have 'Device Name' and 'Unique ID'."
                )
        return super().write(vals)

    def unlink(self):
        """Prevent deletion of the dashboard dummy record"""
        for rec in self:
            if rec.unique_id == 'DASHBOARD_ONLY':
                raise ValidationError(
                    "You cannot delete the 'Live Dashboard Container' record as it is required for the dashboard system."
                )
        return super().unlink()

    # -------------------------------
    # Compute Methods
    # -------------------------------
    @api.depends('position_ids')
    def _compute_latest_position(self):
        for device in self:
            latest = device.position_ids.sorted('device_time', reverse=True)[:1]
            device.latest_position_id = latest.id if latest else False

    @api.depends('position_ids')
    def _compute_latest_coordinates(self):
        for device in self:
            latest = device.position_ids.sorted('device_time', reverse=True)[:1]
            if latest:
                device.latitude = latest.latitude
                device.longitude = latest.longitude
            else:
                device.latitude = False
                device.longitude = False

    def update_computed_fields(self):
        """Force recomputation of latest position and coordinates"""
        self._compute_latest_position()
        self._compute_latest_coordinates()
        self._compute_battery_level()

    @api.depends('latest_position_id.attributes')
    def _compute_battery_level(self):
        for device in self:
            level = 0
            if device.latest_position_id and device.latest_position_id.attributes:
                try:
                    attrs = json.loads(device.latest_position_id.attributes)
                    # Try common battery attribute keys from different GPS protocols
                    level = attrs.get('batteryLevel', attrs.get('battery', attrs.get('power', 0)))
                    # Handle string values or mixed types safely
                    if isinstance(level, (int, float)):
                        level = int(level)
                    elif isinstance(level, str):
                        level = int(''.join(filter(str.isdigit, level)) or 0)
                    else:
                        level = 0
                except Exception:
                    level = 0
            device.battery = level

    # -------------------------------
    # Actions
    # -------------------------------
    def open_positions(self):
        """Open positions view for this device"""
        return {
            'type': 'ir.actions.act_window',
            'name': f'Positions for {self.name}',
            'res_model': 'traccar.position',
            'view_mode': 'list,form,leaflet_map',
            'domain': [('device_id', '=', self.id)],
            'context': {'default_device_id': self.id}
        }

    def sync_all_data(self):
        """Sync all data from Traccar - method for the live tracking view"""
        config = self.env['traccar.config'].search([('active', '=', True)], limit=1)
        if not config:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': 'No active Traccar configuration found',
                    'type': 'danger',
                }
            }

        try:
            api = self.env['traccar.api'].create({'config_id': config.id})
            api.sync_devices()  # Now targeted (only fetches existing devices)
            api.sync_positions()
            config.last_sync = fields.Datetime.now()

            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Success',
                    'message': 'Data synchronized successfully',
                    'type': 'success',
                }
            }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': f'Synchronization failed: {str(e)}',
                    'type': 'danger',
                }
            }

    def open_devices(self):
        """Open devices view - method for the live tracking view"""
        return {
            'type': 'ir.actions.act_window',
            'name': 'GPS Devices',
            'res_model': 'traccar.device',
            'view_mode': 'list,form',
            'domain': [],
            'context': {}
        }
