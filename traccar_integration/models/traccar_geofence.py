from odoo import models, fields, api
import json

class TraccarGeofence(models.Model):
    _name = 'traccar.geofence'
    _description = 'Traccar Geofence'
    _rec_name = 'name'

    traccar_id = fields.Integer(
        string='Traccar Geofence ID',
        required=True,
        index=True
    )
    name = fields.Char(
        string='Name',
        required=True
    )
    description = fields.Text(string='Description')
    area = fields.Text(
        string='Area Coordinates',
        help='Polygon coordinates in WKT format'
    )
    calendar_id = fields.Integer(string='Calendar ID')
    attributes = fields.Text(string='Attributes')
    device_ids = fields.Many2many(
        'traccar.device',
        'traccar_geofence_device_rel',
        'geofence_id',
        'device_id',
        string='Devices'
    )
    active = fields.Boolean(string='Active', default=True)

    def sync_from_traccar(self):
        """Sync geofences from Traccar server"""
        config = self.env['traccar.config'].search([('active', '=', True)], limit=1)
        if not config:
            raise UserError("No active Traccar configuration found")
        
        api = self.env['traccar.api'].create({'config_id': config.id})
        geofences_data = api.get_geofences()
        
        for geofence_data in geofences_data:
            existing = self.search([('traccar_id', '=', geofence_data['id'])])
            
            vals = {
                'traccar_id': geofence_data['id'],
                'name': geofence_data.get('name', ''),
                'description': geofence_data.get('description', ''),
                'area': geofence_data.get('area', ''),
                'calendar_id': geofence_data.get('calendarId'),
                'attributes': json.dumps(geofence_data.get('attributes', {})),
            }
            
            if existing:
                existing.write(vals)
            else:
                self.create(vals)