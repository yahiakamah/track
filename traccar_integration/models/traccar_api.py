import requests
import json
import urllib3
from datetime import datetime, timedelta
from odoo import models, fields, api
from odoo.exceptions import UserError
import logging
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT

# Silence insecure request warnings (self-signed certificates)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_logger = logging.getLogger(__name__)


class TraccarAPI(models.TransientModel):
    _name = 'traccar.api'
    _description = 'Traccar API Interface'

    config_id = fields.Many2one('traccar.config', string='Configuration', required=True)

    def _get_auth(self):
        """Get authentication tuple"""
        return (self.config_id.username, self.config_id.password)

    def _make_request(self, endpoint, method='GET', data=None, params=None):
        """Make API request to Traccar server"""
        url = f"{self.config_id.server_url.rstrip('/')}/api/{endpoint.lstrip('/')}"

        try:
            response = requests.request(
                method,
                url,
                auth=self._get_auth(),
                json=data,
                params=params,
                timeout=15,
                verify=False  # Disable for testing, enable in production
            )
            response.raise_for_status()
            return response.json() if response.content else []

        except requests.exceptions.RequestException as e:
            _logger.error(f"Traccar API request failed: {str(e)}")
            raise UserError(f"Failed to communicate with Traccar server: {str(e)}")

    def _parse_traccar_datetime(self, dt_str):
        """Convert Traccar datetime string to Odoo format"""
        if not dt_str:
            return False

        try:
            # Parse ISO format with timezone
            dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S.%f%z")
            # Convert to naive datetime in UTC
            dt_utc = dt.astimezone(tz=None).replace(tzinfo=None)
            return dt_utc.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
        except ValueError:
            try:
                # Fallback for different format
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                return dt.strftime(DEFAULT_SERVER_DATETIME_FORMAT)
            except Exception:
                _logger.warning(f"Failed to parse datetime: {dt_str}")
                return False

    def sync_devices(self):
        """Synchronize existing devices from Traccar (Targeted Update)"""
        # Get existing devices to know what to sync
        existing_devices = self.env['traccar.device'].search([])
        if not existing_devices:
            _logger.info("No devices registered in Odoo for synchronization.")
            return

        # Fetch all devices from Traccar to match with Odoo records
        # This is the most reliable way to link devices by Unique ID
        _logger.info("Fetching all devices from Traccar for matching...")
        devices_data = self._make_request('devices', params={'all': 'true'})
        
        if not devices_data:
            _logger.warning("No devices returned from Traccar server.")
            return

        odoo_unique_ids = {d.unique_id: d for d in existing_devices if d.unique_id}
        odoo_traccar_ids = {d.traccar_id: d for d in existing_devices if d.traccar_id}

        updated_count = 0
        for device_data in devices_data:
            tr_id = device_data['id']
            un_id = device_data.get('uniqueId')

            # Check if this device is wanted in Odoo
            device = odoo_traccar_ids.get(tr_id) or odoo_unique_ids.get(un_id)
            
            if device:
                vals = {
                    'traccar_id': tr_id,
                    'name': device_data.get('name', device.name),
                    'unique_id': un_id,
                    'status': device_data.get('status', 'unknown'),
                    'last_update': self._parse_traccar_datetime(device_data.get('lastUpdate')),
                    'group_id': device_data.get('groupId'),
                    'phone': device_data.get('phone', ''),
                    'model': device_data.get('model', ''),
                    'contact': device_data.get('contact', ''),
                    'category': device_data.get('category'),
                    'disabled': device_data.get('disabled', False),
                    'attributes': json.dumps(device_data.get('attributes', {})),
                }
                device.write(vals)
                updated_count += 1

        _logger.info(f"Successfully matched and updated {updated_count} devices from Traccar")


    def sync_positions(self, device_id=None, from_date=None, to_date=None):
        """Synchronize positions from Traccar for existing devices only"""
        if not from_date:
            from_date = datetime.now() - timedelta(days=1)
        if not to_date:
            to_date = fields.Datetime.now()

        # Format dates for Traccar (ensure they are in ISO format with Z)
        from_str = from_date.strftime('%Y-%m-%dT%H:%M:%SZ')
        to_str = to_date.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Get existing devices to restrict synchronization
        if device_id:
            target_devices = self.env['traccar.device'].search([('traccar_id', '=', device_id)])
        else:
            target_devices = self.env['traccar.device'].search([('traccar_id', '!=', False)])

        if not target_devices:
            _logger.info("No devices with Traccar IDs found for position synchronization. Please sync devices first.")
            return

        _logger.info(f"Starting position sync for {len(target_devices)} devices from {from_str} to {to_str}")
        
        created_count = 0
        for device in target_devices:
            params = {
                'from': from_str,
                'to': to_str,
                'deviceId': device.traccar_id
            }

            _logger.info(f"Requesting positions for {device.name} (ID: {device.traccar_id})")
            try:
                positions_data = self._make_request('positions', params=params)
                
                if not positions_data:
                    _logger.info(f"No positions found for device {device.name} in this period.")
                    continue
                
                _logger.info(f"Found {len(positions_data)} positions for {device.name}")
                
                for position_data in positions_data:
                    # Check for duplicates by Traccar ID
                    existing = self.env['traccar.position'].search([
                        ('traccar_id', '=', position_data['id'])
                    ], limit=1)

                    if not existing:
                        vals = {
                            'traccar_id': position_data['id'],
                            'device_id': device.id,
                            'protocol': position_data.get('protocol'),
                            'device_time': self._parse_traccar_datetime(position_data.get('deviceTime')),
                            'fix_time': self._parse_traccar_datetime(position_data.get('fixTime')),
                            'server_time': self._parse_traccar_datetime(position_data.get('serverTime')),
                            'outdated': position_data.get('outdated', False),
                            'valid': position_data.get('valid', True),
                            'latitude': position_data.get('latitude', 0),
                            'longitude': position_data.get('longitude', 0),
                            'altitude': position_data.get('altitude'),
                            'speed': position_data.get('speed'),
                            'course': position_data.get('course'),
                            'accuracy': position_data.get('accuracy'),
                            'attributes': json.dumps(position_data.get('attributes', {})),
                        }
                        self.env['traccar.position'].create(vals)
                        created_count += 1
            except Exception as e:
                _logger.error(f"Failed to sync positions for device {device.name}: {str(e)}")

        _logger.info(f"Created {created_count} new positions for {len(target_devices)} devices from Traccar")

        # After positions are synced, trigger update of computed fields on devices
        if created_count > 0:
            target_devices.update_computed_fields()


    def send_command(self, device_id, command_type, attributes=None):
        """Send command to device"""
        data = {
            'deviceId': device_id,
            'type': command_type,
            'attributes': attributes or {}
        }

        return self._make_request('commands/send', method='POST', data=data)