from odoo import models, fields, api
from datetime import datetime, timedelta

class TraccarSyncWizard(models.TransientModel):
    _name = 'traccar.sync.wizard'
    _description = 'Traccar Synchronization Wizard'

    config_id = fields.Many2one(
        'traccar.config',
        string='Configuration',
        required=True,
        default=lambda self: self.env['traccar.config'].search([('active', '=', True)], limit=1)
    )
    sync_devices = fields.Boolean(
        string='Sync Devices',
        default=True,
        help='Synchronize device information from Traccar'
    )
    sync_positions = fields.Boolean(
        string='Sync Positions',
        default=True,
        help='Synchronize position data from Traccar'
    )
    device_ids = fields.Many2many(
        'traccar.device',
        string='Specific Devices',
        help='Leave empty to sync all devices'
    )
    from_date = fields.Datetime(
        string='From Date',
        default=lambda self: datetime.now() - timedelta(days=1),
        help='Start date for position synchronization'
    )
    to_date = fields.Datetime(
        string='To Date',
        default=fields.Datetime.now,
        help='End date for position synchronization'
    )

    def action_sync_data(self):
        """Execute the synchronization"""
        api = self.env['traccar.api'].create({'config_id': self.config_id.id})

        results = {
            'devices_synced': 0,
            'positions_synced': 0,
            'errors': []
        }

        try:
            if self.sync_devices:
                api.sync_devices()
                results['devices_synced'] = len(self.env['traccar.device'].search([]))

            if self.sync_positions:
                if self.device_ids:
                    for device in self.device_ids:
                        api.sync_positions(device.traccar_id, self.from_date, self.to_date)
                else:
                    api.sync_positions(from_date=self.from_date, to_date=self.to_date)

                # Count positions created in the date range
                domain = [('device_time', '>=', self.from_date)]
                if self.to_date:
                    domain.append(('device_time', '<=', self.to_date))
                results['positions_synced'] = len(self.env['traccar.position'].search(domain))

            self.config_id.last_sync = fields.Datetime.now()

        except Exception as e:
            results['errors'].append(str(e))

        # Show results
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Synchronization Complete',
                'message': f"Devices: {results['devices_synced']}, Positions: {results['positions_synced']}",
                'type': 'success' if not results['errors'] else 'warning',
            }
        }