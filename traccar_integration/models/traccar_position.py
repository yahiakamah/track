from odoo import models, fields, api


class TraccarPosition(models.Model):
    _name = 'traccar.position'
    _description = 'GPS Position Data'
    _order = 'device_time desc'

    map_widget = fields.Char("Map", compute="_compute_map_widget")

    def _compute_map_widget(self):
        for rec in self:
            rec.map_widget = "Map for device %s" % (rec.id)

    traccar_id = fields.Integer(
        string='Traccar Position ID',
        required=True,
        index=True
    )
    device_id = fields.Many2one(
        'traccar.device',
        string='Device',
        required=True,
        ondelete='cascade'
    )
    protocol = fields.Char(string='Protocol')
    device_time = fields.Datetime(
        string='Device Time',
        required=True,
        index=True
    )
    fix_time = fields.Datetime(string='Fix Time')
    server_time = fields.Datetime(string='Server Time')
    outdated = fields.Boolean(string='Outdated')
    valid = fields.Boolean(string='Valid')
    latitude = fields.Float(
        string='Latitude',
        digits=(10, 6),
        required=True
    )
    longitude = fields.Float(
        string='Longitude',
        digits=(10, 6),
        required=True
    )
    altitude = fields.Float(string='Altitude (m)')
    speed = fields.Float(string='Speed (knots)')
    speed_kmh = fields.Float(
        string='Speed (km/h)',
        compute='_compute_speed_kmh',
        store=True
    )
    course = fields.Float(string='Course (degrees)')
    accuracy = fields.Float(string='Accuracy')
    network = fields.Text(string='Network Info')
    attributes = fields.Text(string='Attributes')
    stop_id = fields.Many2one(
        'traccar.device.stop',
        string='Stop Event',
        ondelete='set null',
        index=True
    )

    # Additional computed fields
    address = fields.Char(
        string='Address',
        compute='_compute_address'
    )

    # Status field for map widget compatibility
    status = fields.Char(
        string='Status',
        compute='_compute_status',
        store=False
    )

    @api.depends('speed')
    def _compute_speed_kmh(self):
        for position in self:
            if position.speed:
                position.speed_kmh = position.speed * 1.852  # Convert knots to km/h
            else:
                position.speed_kmh = 0

    def _compute_address(self):
        """Reverse geocoding to get address (placeholder)"""
        for position in self:
            # This would typically use a geocoding service
            position.address = f"Lat: {position.latitude}, Lng: {position.longitude}"

    def _compute_status(self):
        """Compute status based on device status for map widget compatibility"""
        for position in self:
            if position.device_id:
                position.status = position.device_id.status
            else:
                position.status = 'unknown'

    def open_map_view(self):
        """Open Live Map view for this position"""
        self.ensure_one()
        if self.latitude and self.longitude:
            action = self.env.ref('traccar_integration.action_traccar_live_tracking', raise_if_not_found=False)
            dummy = self.env.ref('traccar_integration.traccar_live_tracking_dummy', raise_if_not_found=False)
            action_id = action.id if action else ''
            dummy_id = dummy.id if dummy else ''
            url = f"/odoo/action-{action_id}/{dummy_id}?focus_position_id={self.id}"
            return {
                'type': 'ir.actions.act_url',
                'url': url,
                'target': 'self',
            }
        return False

    @api.model_create_multi
    def create(self, vals_list):
        return super().create(vals_list)