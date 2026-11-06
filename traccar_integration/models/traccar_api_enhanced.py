import json
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class TraccarAPIEnhanced(models.TransientModel):
    _inherit = 'traccar.api'

    def get_device_status(self, device_id):
        """Get real-time device status"""
        try:
            response = self._make_request(f'devices/{device_id}')
            return response.get('status', 'unknown')
        except Exception as e:
            _logger.error(f"Failed to get device status: {str(e)}")
            return 'unknown'

    def send_command_to_device(self, device_id, command_type, attributes=None):
        """Send command to specific device"""
        commands = {
            'positionSingle': 'Request position update',
            'positionPeriodic': 'Set periodic reporting',
            'positionStop': 'Stop position reporting',
            'engineStop': 'Stop engine',
            'engineResume': 'Resume engine',
            'alarmArm': 'Arm alarm',
            'alarmDisarm': 'Disarm alarm',
            'setTimezone': 'Set timezone',
            'requestPhoto': 'Request photo',
            'powerOff': 'Power off device',
            'rebootDevice': 'Reboot device',
            'factoryReset': 'Factory reset',
            'sosNumber': 'Set SOS number',
            'silenceTime': 'Set silence time',
            'setPhonebook': 'Set phonebook',
            'message': 'Send message'
        }

        if command_type not in commands:
            raise UserError(f"Unknown command type: {command_type}")

        data = {
            'deviceId': device_id,
            'type': command_type,
            'attributes': attributes or {}
        }

        try:
            response = self._make_request('commands/send', method='POST', data=data)
            return {
                'success': True,
                'message': f"Command '{commands[command_type]}' sent successfully",
                'response': response
            }
        except Exception as e:
            return {
                'success': False,
                'message': f"Failed to send command: {str(e)}"
            }

    def get_device_events(self, device_id, from_date, to_date):
        """Get device events for specified period"""
        params = {
            'deviceId': device_id,
            'from': from_date.isoformat() + 'Z',
            'to': to_date.isoformat() + 'Z'
        }
        
        try:
            events = self._make_request(f"events?{self._build_query_string(params)}")
            return events
        except Exception as e:
            _logger.error(f"Failed to get device events: {str(e)}")
            return []

    def get_geofences(self):
        """Get all geofences from Traccar"""
        try:
            return self._make_request('geofences')
        except Exception as e:
            _logger.error(f"Failed to get geofences: {str(e)}")
            return []

    def create_geofence(self, name, description, area):
        """Create geofence in Traccar"""
        data = {
            'name': name,
            'description': description,
            'area': area,
            'attributes': {}
        }
        
        try:
            return self._make_request('geofences', method='POST', data=data)
        except Exception as e:
            _logger.error(f"Failed to create geofence: {str(e)}")
            raise UserError(f"Failed to create geofence: {str(e)}")
