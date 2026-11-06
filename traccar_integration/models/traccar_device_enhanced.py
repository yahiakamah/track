from odoo import models, fields, api
import json
import logging

_logger = logging.getLogger(__name__)

class TraccarDeviceEnhanced(models.Model):
    _inherit = 'traccar.device'

    last_command_time = fields.Datetime(string='Last Command Time')
    last_command_result = fields.Text(string='Last Command Result')
    geofence_ids = fields.Many2many(
        'traccar.geofence',
        'traccar_geofence_device_rel',
        'device_id',
        'geofence_id',
        string='Geofences'
    )
    maintenance_odometer = fields.Float(string='Maintenance Odometer')
    fuel_level = fields.Float(string='Fuel Level (%)')
    battery_level = fields.Float(string='Battery Level (%)')
    engine_hours = fields.Float(string='Engine Hours')

    def send_position_request(self):
        """Request immediate position update"""
        return self._send_command('positionSingle')

    def send_engine_stop(self):
        """Send engine stop command"""
        return self._send_command('engineStop')

    def send_engine_resume(self):
        """Send engine resume command"""
        return self._send_command('engineResume')

    def send_custom_message(self, message):
        """Send custom message to device"""
        return self._send_command('message', {'message': message})

    def _send_command(self, command_type, attributes=None):
        """Send command to device via Traccar API"""
        config = self.env['traccar.config'].search([('active', '=', True)], limit=1)
        if not config:
            return {
                'success': False,
                'message': 'No active Traccar configuration found'
            }
        
        api = self.env['traccar.api'].create({'config_id': config.id})
        result = api.send_command_to_device(self.traccar_id, command_type, attributes)
        
        # Update command history
        self.last_command_time = fields.Datetime.now()
        self.last_command_result = json.dumps(result)
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Command Sent' if result['success'] else 'Command Failed',
                'message': result['message'],
                'type': 'success' if result['success'] else 'danger',
            }
        }

    @api.model
    def update_computed_fields(self):
        """Update status fields from latest position attributes"""
        for device in self.search([]):
            _logger.info(f"Updating computed fields for device {device.name}")
            if device.latest_position_id and device.latest_position_id.attributes:
                try:
                    attrs = json.loads(device.latest_position_id.attributes)
                    _logger.info(f"Attributes: {attrs}")
                    
                    # Update fuel level (supports fuel, fuelLevel)
                    fuel = attrs.get('fuel') or attrs.get('fuelLevel')
                    if fuel is not None:
                        device.fuel_level = float(fuel)
                    
                    # Update battery level (supports batteryLevel, battery)
                    battery = attrs.get('batteryLevel') or attrs.get('battery')
                    if battery is not None:
                        device.battery_level = float(battery)
                    
                    # Update odometer (supports totalDistance, odometer)
                    # Note: totalDistance is in meters in Traccar
                    odometer = attrs.get('totalDistance') or attrs.get('odometer')
                    if odometer is not None:
                        # If the value is > 1000000, it's likely meters; otherwise, it might be km already.
                        # This is a heuristic.
                        val = float(odometer)
                        device.maintenance_odometer = val / 1000 if val > 5000 else val
                    
                    # Update engine hours (supports engineHours, hours)
                    hours = attrs.get('engineHours') or attrs.get('hours')
                    if hours is not None:
                        device.engine_hours = float(hours)
                        
                except (json.JSONDecodeError, ValueError, KeyError):
                    pass
