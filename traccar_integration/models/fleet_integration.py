from odoo import models, fields, api

class FleetVehicle(models.Model):
    _inherit = 'fleet.vehicle'

    traccar_device_id = fields.Many2one(
        'traccar.device',
        string='GPS Device',
        help='Associated GPS tracking device'
    )
    current_location = fields.Char(
        string='Current Location',
        compute='_compute_current_location'
    )
    last_position_time = fields.Datetime(
        string='Last Position Time',
        compute='_compute_current_location'
    )
    tracking_active = fields.Boolean(
        string='Tracking Active',
        compute='_compute_tracking_status'
    )

    @api.depends('traccar_device_id.latest_position_id')
    def _compute_current_location(self):
        for vehicle in self:
            if vehicle.traccar_device_id and vehicle.traccar_device_id.latest_position_id:
                position = vehicle.traccar_device_id.latest_position_id
                vehicle.current_location = f"Lat: {position.latitude}, Lng: {position.longitude}"
                vehicle.last_position_time = position.device_time
            else:
                vehicle.current_location = "No GPS data"
                vehicle.last_position_time = False

    @api.depends('traccar_device_id.status')
    def _compute_tracking_status(self):
        for vehicle in self:
            vehicle.tracking_active = (
                vehicle.traccar_device_id and 
                vehicle.traccar_device_id.status == 'online'
            )

    def open_tracking_data(self):
        """Open tracking data for this vehicle"""
        if not self.traccar_device_id:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'No GPS Device',
                    'message': 'No GPS device linked to this vehicle',
                    'type': 'warning',
                }
            }
        
        return {
            'type': 'ir.actions.act_window',
            'name': f'GPS Tracking - {self.name}',
            'res_model': 'traccar.position',
            'view_mode': 'list,form,leaflet_map',
            'domain': [('device_id', '=', self.traccar_device_id.id)],
            'context': {'default_device_id': self.traccar_device_id.id}
        }

class TraccarDevice(models.Model):
    _inherit = 'traccar.device'

    fleet_vehicle_ids = fields.One2many(
        'fleet.vehicle',
        'traccar_device_id',
        string='Fleet Vehicles'
    )
