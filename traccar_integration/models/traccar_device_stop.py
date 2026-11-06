import math
from odoo import models, fields, api
from datetime import datetime


def _haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in meters between two lat/lon points."""
    R = 6371000.0  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return R * c


class TraccarDeviceStop(models.Model):
    _name = 'traccar.device.stop'
    _description = 'Traccar Device Stop Report'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'start_time desc'

    device_id = fields.Many2one(
        'traccar.device',
        string='Device',
        required=True,
        ondelete='cascade'
    )
    start_time = fields.Datetime(
        string='Start Time',
        required=True,
        index=True
    )
    end_time = fields.Datetime(
        string='End Time',
        required=True,
        index=True
    )
    duration = fields.Float(
        string='Waiting Time (Minutes)',
        compute='_compute_duration',
        store=True
    )
    duration_formatted = fields.Char(
        string='Waiting Time',
        compute='_compute_duration',
        store=True
    )
    latitude = fields.Float(
        string='Latitude',
        digits=(10, 6)
    )
    longitude = fields.Float(
        string='Longitude',
        digits=(10, 6)
    )
    position_ids = fields.One2many(
        'traccar.position',
        'stop_id',
        string='Positions during Stop'
    )
    alert_sent = fields.Boolean(
        string='Alert Sent',
        default=False
    )

    @api.depends('start_time', 'end_time')
    def _compute_duration(self):
        for rec in self:
            if rec.start_time and rec.end_time:
                delta = rec.end_time - rec.start_time
                total_sec = int(delta.total_seconds())
                rec.duration = total_sec / 60.0

                hours = total_sec // 3600
                minutes = (total_sec % 3600) // 60
                seconds = total_sec % 60
                rec.duration_formatted = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                rec.duration = 0.0
                rec.duration_formatted = "00:00:00"

    @api.model
    def calculate_all_stops(self):
        """Scans all positions, detects periods where device was stopped (speed_kmh < 2.0),
        merges consecutive stops at the same location (gap <= 5 min & distance <= 150m),
        and creates clean traccar.device.stop records."""
        config = self.env['traccar.config'].sudo().search([('active', '=', True)], limit=1)
        min_duration = config.alert_time if config and config.alert_time > 0 else 1.0

        devices = self.env['traccar.device'].search([])
        for device in devices:
            positions = self.env['traccar.position'].search([
                ('device_id', '=', device.id)
            ], order='device_time asc')

            # 1. Group positions into initial raw stop segments (speed < 2.0 km/h)
            raw_stop_groups = []
            current_group = []
            for pos in positions:
                speed_kmh = pos.speed_kmh or (pos.speed * 1.852)
                is_stopped = (speed_kmh < 2.0)
                if is_stopped:
                    current_group.append(pos)
                else:
                    if current_group:
                        raw_stop_groups.append(current_group)
                        current_group = []
            if current_group:
                raw_stop_groups.append(current_group)

            if not raw_stop_groups:
                self.search([('device_id', '=', device.id)]).unlink()
                continue

            # 2. Merge adjacent stop groups if gap <= 5 minutes AND distance <= 150 meters
            merged_groups = []
            for grp in raw_stop_groups:
                if not merged_groups:
                    merged_groups.append(grp)
                else:
                    last_grp = merged_groups[-1]
                    last_end = last_grp[-1].device_time
                    curr_start = grp[0].device_time
                    gap_minutes = (curr_start - last_end).total_seconds() / 60.0

                    last_mid = last_grp[len(last_grp) // 2]
                    curr_mid = grp[len(grp) // 2]
                    dist_meters = _haversine_distance(last_mid.latitude, last_mid.longitude, curr_mid.latitude, curr_mid.longitude)

                    # If gap is short (<= 5 min) and location is practically the same (<= 150m), merge them!
                    if gap_minutes <= 5.0 and dist_meters <= 150.0:
                        merged_groups[-1].extend(grp)
                    else:
                        merged_groups.append(grp)

            # 3. Update or create stop records for merged_groups
            existing_stops = self.search([('device_id', '=', device.id)])
            processed_stop_ids = []
            
            for grp in merged_groups:
                if not grp or len(grp) < 2:
                    continue
                start_time = grp[0].device_time
                end_time = grp[-1].device_time
                duration_min = (end_time - start_time).total_seconds() / 60.0

                if duration_min < min_duration:
                    continue

                mid_pos = grp[len(grp) // 2]
                
                # Find an existing stop with the same start_time, or that overlaps with this segment
                matching_stop = existing_stops.filtered(lambda s: s.start_time == start_time)
                if not matching_stop:
                    # Fallback to overlap check: check if the new start_time falls within an existing stop, or vice versa
                    matching_stop = existing_stops.filtered(lambda s: (s.start_time <= start_time <= s.end_time) or (start_time <= s.start_time <= end_time))
                
                if matching_stop:
                    stop_rec = matching_stop[0]
                    stop_rec.write({
                        'start_time': start_time,
                        'end_time': end_time,
                        'latitude': mid_pos.latitude,
                        'longitude': mid_pos.longitude,
                    })
                    for pos in grp:
                        pos.stop_id = stop_rec.id
                    processed_stop_ids.append(stop_rec.id)
                else:
                    stop = self.create({
                        'device_id': device.id,
                        'start_time': start_time,
                        'end_time': end_time,
                        'latitude': mid_pos.latitude,
                        'longitude': mid_pos.longitude,
                    })
                    for pos in grp:
                        pos.stop_id = stop.id
                    processed_stop_ids.append(stop.id)

            # Delete old stops that are no longer valid or present in the current calculation
            stops_to_delete = existing_stops.filtered(lambda s: s.id not in processed_stop_ids)
            stops_to_delete.unlink()

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Success',
                'message': f'Waiting times and stops recalculated successfully!',
                'type': 'success',
            }
        }

    def _create_merged_stop_record(self, device, pos_list, min_duration):
        if not pos_list or len(pos_list) < 2:
            return
        start_time = pos_list[0].device_time
        end_time = pos_list[-1].device_time
        duration_min = (end_time - start_time).total_seconds() / 60.0

        if duration_min < min_duration:
            return

        mid_pos = pos_list[len(pos_list) // 2]
        stop = self.create({
            'device_id': device.id,
            'start_time': start_time,
            'end_time': end_time,
            'latitude': mid_pos.latitude,
            'longitude': mid_pos.longitude,
        })
        for pos in pos_list:
            pos.stop_id = stop.id

    @api.model
    def cron_process_stops_and_alert(self):
        """Cron job to sync positions from API, calculate stops, and send notifications/alerts if waiting threshold is exceeded."""
        # 1. Trigger API sync
        try:
            self.env['traccar.api'].sudo().sync_data()
        except Exception:
            pass

        # 2. Recalculate stops with merging
        self.calculate_all_stops()

        # 3. Process alerts
        config = self.env['traccar.config'].sudo().search([('active', '=', True)], limit=1)
        if not config or not config.notification_type or not config.alert_user_ids:
            return

        min_alert_time = config.alert_time or 30
        stops_to_alert = self.search([
            ('alert_sent', '=', False),
            ('duration', '>=', min_alert_time)
        ])

        activity_type = self.env.ref('mail.mail_activity_data_todo', raise_if_not_found=False)

        for stop in stops_to_alert:
            summary = f"Device Stopped Alert: {stop.device_id.name}"
            note = (f"<p>Device <b>{stop.device_id.name}</b> has been stopped for <b>{stop.duration_formatted}</b>.<br/>"
                    f"<b>Start Time:</b> {stop.start_time}<br/>"
                    f"<b>Location:</b> ({stop.latitude}, {stop.longitude})</p>")

            for user in config.alert_user_ids:
                if config.notification_type in ['activity', 'both'] and activity_type:
                    self.env['mail.activity'].sudo().create({
                        'activity_type_id': activity_type.id,
                        'summary': summary,
                        'note': note,
                        'res_id': stop.id,
                        'res_model_id': self.env['ir.model']._get_id('traccar.device.stop'),
                        'user_id': user.id,
                    })

                if config.notification_type in ['email', 'both'] and user.email:
                    self.env['mail.mail'].sudo().create({
                        'subject': summary,
                        'body_html': note,
                        'email_to': user.email,
                    }).send()

            stop.alert_sent = True



