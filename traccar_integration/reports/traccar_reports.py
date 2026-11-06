from odoo import models, fields, api
import io
import xlsxwriter
import base64
from datetime import datetime
from math import radians, cos, sin, asin, sqrt

def calculate_distance(lat1, lon1, lat2, lon2):
    """Calculate the great circle distance in kilometers between two points on the earth"""
    # Convert decimal degrees to radians
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    # Haversine formula
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    r = 6371  # Radius of earth in kilometers
    return c * r

class TraccarReportWizard(models.TransientModel):
    _name = 'traccar.report.wizard'
    _description = 'Traccar Reports Wizard'

    report_type = fields.Selection([
        ('device_summary', 'Device Summary'),
        # ('position_history', 'Position History'),
        # ('travel_report', 'Travel Report'),
        ('stop_report', 'Stop Report')
    ], string='Report Type', required=True, default='device_summary')
    
    device_ids = fields.Many2many('traccar.device', string='Devices')
    from_date = fields.Datetime(string='From Date', required=True)
    to_date = fields.Datetime(string='To Date', required=True)
    export_format = fields.Selection([
        ('xlsx', 'Excel'),
        ('pdf', 'PDF')
    ], string='Export Format', default='xlsx')

    def generate_report(self):
        """Generate the selected report"""
        if self.export_format == 'xlsx':
            return self._generate_excel_report()
        else:
            return self._generate_pdf_report()

    def _generate_pdf_report(self):
        """Generate PDF report"""
        return self.env.ref('traccar_integration.action_traccar_pdf_report').report_action(self)

    def _generate_excel_report(self):
        """Generate Excel report"""
        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output)
        
        if self.report_type == 'device_summary':
            self._create_device_summary_sheet(workbook)
        elif self.report_type == 'position_history':
            self._create_position_history_sheet(workbook)
        elif self.report_type == 'travel_report':
            self._create_travel_report_sheet(workbook)
        elif self.report_type == 'stop_report':
            self._create_stop_report_sheet(workbook)
        
        workbook.close()
        output.seek(0)
        
        # Create attachment
        filename = f"traccar_{self.report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(output.read()),
            'res_model': self._name,
            'res_id': self.id,
        })
        
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def _get_devices(self):
        return self.device_ids if self.device_ids else self.env['traccar.device'].search([('unique_id', '!=', 'DASHBOARD_ONLY')])

    def _create_device_summary_sheet(self, workbook):
        """Create device summary sheet"""
        worksheet = workbook.add_worksheet('Device Summary')
        
        # Headers
        headers = ['Device Name', 'Unique ID', 'Status', 'Last Position', 
                  'Total Positions', 'Total Distance (km)', 'Avg Speed (km/h)', 'Max Speed (km/h)']
        
        header_format = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3'})
        
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
        
        # Data
        devices = self._get_devices()
        
        for row, device in enumerate(devices, 1):
            positions = device.position_ids.filtered(
                lambda p: self.from_date <= p.device_time <= self.to_date
            ).sorted('device_time')
            
            # Distance
            distance = 0.0
            if len(positions) > 1:
                for k in range(len(positions) - 1):
                    p1 = positions[k]
                    p2 = positions[k+1]
                    # Only count if speed > 2 and dist > 50m to ignore drift
                    dist = calculate_distance(p1.latitude, p1.longitude, p2.latitude, p2.longitude)
                    if (p1.speed_kmh or 0) > 2 and dist > 0.05:
                        distance += dist

            worksheet.write(row, 0, device.name)
            worksheet.write(row, 1, device.unique_id)
            worksheet.write(row, 2, device.status)
            worksheet.write(row, 3, device.last_update.strftime('%Y-%m-%d %H:%M') if device.last_update else '')
            worksheet.write(row, 4, len(positions))
            worksheet.write(row, 5, round(distance, 2))
            worksheet.write(row, 6, round(sum(positions.mapped('speed_kmh')) / len(positions), 1) if positions else 0)
            worksheet.write(row, 7, round(max(positions.mapped('speed_kmh')), 1) if positions else 0)

    def _create_position_history_sheet(self, workbook):
        """Create position history sheet"""
        worksheet = workbook.add_worksheet('Position History')
        
        # Headers
        headers = ['Device', 'Time', 'Latitude', 'Longitude', 'Speed (km/h)', 
                  'Course', 'Altitude', 'Valid']
        
        header_format = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3'})
        
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
        
        # Data
        domain = [
            ('device_time', '>=', self.from_date),
            ('device_time', '<=', self.to_date)
        ]
        if self.device_ids:
            domain.append(('device_id', 'in', self.device_ids.ids))
        
        positions = self.env['traccar.position'].search(domain, order='device_time desc')
        
        for row, position in enumerate(positions, 1):
            worksheet.write(row, 0, position.device_id.name)
            worksheet.write(row, 1, position.device_time.strftime('%Y-%m-%d %H:%M:%S'))
            worksheet.write(row, 2, position.latitude)
            worksheet.write(row, 3, position.longitude)
            worksheet.write(row, 4, position.speed_kmh)
            worksheet.write(row, 5, position.course or 0)
            worksheet.write(row, 6, position.altitude or 0)
            worksheet.write(row, 7, 'Yes' if position.valid else 'No')

    # ── Calculation Helpers ──

    def _get_waiting_points_for_device(self, positions):
        """Find waiting points: points within 50m of each other with duration >= 1 minute"""
        waiting_points = []
        idx = 0
        while idx < len(positions):
            start_point = positions[idx]
            j = idx + 1
            while j < len(positions):
                dist = calculate_distance(
                    start_point.latitude, start_point.longitude,
                    positions[j].latitude, positions[j].longitude
                ) * 1000  # meters
                if dist <= 50:
                    j += 1
                else:
                    break
            
            end_point = positions[j - 1]
            duration = (end_point.device_time - start_point.device_time).total_seconds()
            
            if j > idx + 1 and duration >= 60:
                # Calculate average coordinates
                lat_avg = sum(p.latitude for p in positions[idx:j]) / (j - idx)
                lng_avg = sum(p.longitude for p in positions[idx:j]) / (j - idx)
                
                waiting_points.append({
                    'start_time': start_point.device_time,
                    'end_time': end_point.device_time,
                    'duration': duration,
                    'latitude': lat_avg,
                    'longitude': lng_avg,
                    'point_count': j - idx
                })
                idx = j
            else:
                idx += 1
        return waiting_points

    def _get_stops_for_device(self, positions):
        """Find stops using same logic as traccar.device.stop model:
        1. Group consecutive positions with speed < 2 km/h
        2. Merge adjacent groups if gap <= 5 min AND distance <= 150m
        3. Only keep stops with duration >= 5 minutes
        """
        if not positions:
            return []

        # 1. Group positions into raw stop segments (speed < 2.0 km/h)
        raw_stop_groups = []
        current_group = []
        for pos in positions:
            speed_kmh = pos.speed_kmh or (pos.speed * 1.852 if pos.speed else 0)
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
            return []

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
                dist_meters = calculate_distance(
                    last_mid.latitude, last_mid.longitude,
                    curr_mid.latitude, curr_mid.longitude
                ) * 1000  # km to meters

                if gap_minutes <= 5.0 and dist_meters <= 150.0:
                    merged_groups[-1].extend(grp)
                else:
                    merged_groups.append(grp)

        # 3. Build stop records from merged groups
        config = self.env['traccar.config'].sudo().search([('active', '=', True)], limit=1)
        min_duration_minutes = config.alert_time if config and config.alert_time > 0 else 1.0
        min_duration_seconds = min_duration_minutes * 60.0

        stops = []
        for grp in merged_groups:
            if len(grp) < 2:
                continue
            start_time = grp[0].device_time
            end_time = grp[-1].device_time
            duration = (end_time - start_time).total_seconds()

            if duration >= min_duration_seconds:
                mid_pos = grp[len(grp) // 2]
                stops.append({
                    'start_time': start_time,
                    'end_time': end_time,
                    'duration': duration,
                    'latitude': mid_pos.latitude,
                    'longitude': mid_pos.longitude,
                    'start_idx': 0,
                    'end_idx': 0
                })
        return stops

    def _get_travels_for_device(self, positions, stops):
        """Find travels: intervals between stops"""
        travels = []
        if not positions:
            return travels
            
        if not stops:
            # Entire timeline is one travel if distance is significant
            dist = 0.0
            for k in range(len(positions) - 1):
                dist += calculate_distance(positions[k].latitude, positions[k].longitude, positions[k+1].latitude, positions[k+1].longitude)
            
            duration = (positions[-1].device_time - positions[0].device_time).total_seconds()
            if dist > 0.1:
                speeds = positions.mapped('speed_kmh')
                travels.append({
                    'start_time': positions[0].device_time,
                    'end_time': positions[-1].device_time,
                    'duration': duration,
                    'start_lat': positions[0].latitude,
                    'start_lng': positions[0].longitude,
                    'end_lat': positions[-1].latitude,
                    'end_lng': positions[-1].longitude,
                    'distance': dist,
                    'avg_speed': sum(speeds) / len(speeds) if speeds else 0,
                    'max_speed': max(speeds) if speeds else 0
                })
            return travels

        # Timeline has stops. Let's find intervals between stops.
        last_end_idx = -1
        
        # Add travel before the first stop
        if stops[0]['start_idx'] > 0:
            sub_pos = positions[0:stops[0]['start_idx'] + 1]
            self._append_travel(travels, sub_pos)
            
        # Add travels between stops
        for i in range(len(stops) - 1):
            start_idx = stops[i]['end_idx']
            end_idx = stops[i+1]['start_idx']
            if end_idx > start_idx:
                sub_pos = positions[start_idx:end_idx + 1]
                self._append_travel(travels, sub_pos)
                
        # Add travel after the last stop
        if stops[-1]['end_idx'] < len(positions) - 1:
            sub_pos = positions[stops[-1]['end_idx']:]
            self._append_travel(travels, sub_pos)
            
        return travels

    def _append_travel(self, travels, sub_pos):
        if len(sub_pos) < 2:
            return
        dist = 0.0
        for k in range(len(sub_pos) - 1):
            dist += calculate_distance(sub_pos[k].latitude, sub_pos[k].longitude, sub_pos[k+1].latitude, sub_pos[k+1].longitude)
        
        if dist > 0.1:
            duration = (sub_pos[-1].device_time - sub_pos[0].device_time).total_seconds()
            speeds = sub_pos.mapped('speed_kmh')
            travels.append({
                'start_time': sub_pos[0].device_time,
                'end_time': sub_pos[-1].device_time,
                'duration': duration,
                'start_lat': sub_pos[0].latitude,
                'start_lng': sub_pos[0].longitude,
                'end_lat': sub_pos[-1].latitude,
                'end_lng': sub_pos[-1].longitude,
                'distance': dist,
                'avg_speed': sum(speeds) / len(speeds) if speeds else 0,
                'max_speed': max(speeds) if speeds else 0
            })

    # ── Sheets Writing ──

    def _create_travel_report_sheet(self, workbook):
        worksheet = workbook.add_worksheet('Travel Report')
        headers = ['Device', 'Start Time', 'End Time', 'Duration', 
                  'Start Lat', 'Start Lng', 'End Lat', 'End Lng', 
                  'Distance (km)', 'Avg Speed (km/h)', 'Max Speed (km/h)']
        header_format = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3'})
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
            
        row = 1
        for device in self._get_devices():
            positions = device.position_ids.filtered(
                lambda p: self.from_date <= p.device_time <= self.to_date
            ).sorted('device_time')
            stops = self._get_stops_for_device(positions)
            travels = self._get_travels_for_device(positions, stops)
            
            for t in travels:
                duration_str = str(t['end_time'] - t['start_time'])
                worksheet.write(row, 0, device.name)
                worksheet.write(row, 1, t['start_time'].strftime('%Y-%m-%d %H:%M:%S'))
                worksheet.write(row, 2, t['end_time'].strftime('%Y-%m-%d %H:%M:%S'))
                worksheet.write(row, 3, duration_str)
                worksheet.write(row, 4, t['start_lat'])
                worksheet.write(row, 5, t['start_lng'])
                worksheet.write(row, 6, t['end_lat'])
                worksheet.write(row, 7, t['end_lng'])
                worksheet.write(row, 8, round(t['distance'], 2))
                worksheet.write(row, 9, round(t['avg_speed'], 1))
                worksheet.write(row, 10, round(t['max_speed'], 1))
                row += 1

    def _create_stop_report_sheet(self, workbook):
        worksheet = workbook.add_worksheet('Stop Report')
        headers = ['Device', 'Start Time', 'End Time', 'Duration', 
                  'Latitude', 'Longitude', 'view on map', 'contact name', 'contact icon']
        header_format = workbook.add_format({'bold': True, 'bg_color': '#D3D3D3'})
        for col, header in enumerate(headers):
            worksheet.write(0, col, header, header_format)
            
        # Pre-fetch all partners with active locations for speed
        partners = self.env['res.partner'].sudo().search([
            '|',
            '|',
            ('partner_latitude', '!=', 0.0),
            ('partner_longitude', '!=', 0.0),
            '|',
            ('visit_latitude', '!=', 0.0),
            ('visit_longitude', '!=', 0.0)
        ])
            
        row = 1
        for device in self._get_devices():
            stops = self.env['traccar.device.stop'].sudo().search([
                ('device_id', '=', device.id),
                ('start_time', '>=', self.from_date),
                ('end_time', '<=', self.to_date)
            ], order='start_time asc')
            
            for s in stops:
                total_sec = int(s.duration * 60.0) if s.duration else 0
                hours = total_sec // 3600
                minutes = (total_sec % 3600) // 60
                seconds = total_sec % 60
                duration_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                
                # Identify contact by location zones
                matched_contact = None
                for partner in partners:
                    lat = partner.visit_latitude or partner.partner_latitude
                    lng = partner.visit_longitude or partner.partner_longitude
                    radius = partner.location_radius or 50.0  # Default 50m
                    
                    dist_meters = calculate_distance(s.latitude, s.longitude, lat, lng) * 1000.0
                    if dist_meters <= radius:
                        matched_contact = partner
                        break

                gmap_url = f"https://www.google.com/maps/search/?api=1&query={s.latitude},{s.longitude}"
                
                worksheet.write(row, 0, device.name)
                worksheet.write(row, 1, s.start_time.strftime('%Y-%m-%d %H:%M:%S'))
                worksheet.write(row, 2, s.end_time.strftime('%Y-%m-%d %H:%M:%S'))
                worksheet.write(row, 3, duration_str)
                worksheet.write(row, 4, s.latitude)
                worksheet.write(row, 5, s.longitude)
                worksheet.write_url(row, 6, gmap_url, string='view on map')
                worksheet.write(row, 7, matched_contact.name if matched_contact else 'unknown')
                worksheet.write(row, 8, dict(self.env['res.partner']._fields['location_type'].selection).get(matched_contact.location_type, 'unknown') if (matched_contact and matched_contact.location_type) else 'unknown')
                row += 1