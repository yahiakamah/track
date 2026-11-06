from odoo import models, fields, api
from odoo.exceptions import ValidationError
import requests
import logging

_logger = logging.getLogger(__name__)

class TraccarConfig(models.Model):
    _name = 'traccar.config'
    _description = 'Traccar Server Configuration'
    _rec_name = 'server_url'

    server_url = fields.Char(
        string='Traccar Server URL',
        required=True,
        default='http://localhost:8082',
        help='URL of your Traccar server (e.g., http://your-server:8082)'
    )
    username = fields.Char(
        string='Username',
        required=True,
        help='Traccar username for API access'
    )
    password = fields.Char(
        string='Password',
        required=True,
        help='Traccar password for API access'
    )
    active = fields.Boolean(
        string='Active',
        default=True
    )
    last_sync = fields.Datetime(
        string='Last Synchronization',
        readonly=True
    )
    sync_interval = fields.Integer(
        string='Sync Interval (minutes)',
        default=2,
        help='Automatic synchronization interval in minutes'
    )
    browser_sync_interval = fields.Integer(
        string='Live Map Sync Interval (seconds)',
        default=10,
        help='How often the Live Map triggers an API sync from the browser'
    )
    notification_type = fields.Selection([
        ('activity', 'Activity'),
        ('email', 'Email'),
        ('both', 'Activity and Email')
    ], string='Notification Type', default='activity')
    alert_time = fields.Integer(
        string='Alerting Time (minutes)',
        default=30,
        help='Alert if the device position is in the same place for this many minutes'
    )
    alert_user_ids = fields.Many2many(
        'res.users',
        'traccar_config_alert_users_rel',
        'config_id',
        'user_id',
        string='Alert Users'
    )

    @api.constrains('server_url')
    def _check_server_url(self):
        for record in self:
            if not record.server_url.startswith(('http://', 'https://')):
                raise ValidationError("Server URL must start with http:// or https://")

    def test_connection(self):
        """Test connection to Traccar server"""
        try:
            response = requests.get(
                f"{self.server_url}/api/server",
                auth=(self.username, self.password),
                timeout=10
            )
            if response.status_code == 200:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Success',
                        'message': 'Connection to Traccar server successful!',
                        'type': 'success',
                    }
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Error',
                        'message': f'Connection failed: {response.status_code}',
                        'type': 'danger',
                    }
                }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': f'Connection failed: {str(e)}',
                    'type': 'danger',
                }
            }

    def sync_all_data(self):
        """Synchronize all data from Traccar"""
        api = self.env['traccar.api'].create({'config_id': self.id})
        api.sync_devices()  # Now targeted (only fetches existing devices)
        api.sync_positions()
        self.last_sync = fields.Datetime.now()

    # ADD THIS METHOD FOR CRON JOB SUPPORT
    def cron_sync_all_data(self):
        """Method for cron job to sync all data"""
        for config in self.search([('active', '=', True)]):
            try:
                api = self.env['traccar.api'].create({'config_id': config.id})
                # sync_devices is now targeted and only fetches existing records
                api.sync_devices()
                
                # Sync positions for the last 24 hours (for existing devices only)
                from datetime import datetime, timedelta
                from_date = datetime.now() - timedelta(hours=24)
                api.sync_positions(from_date=from_date)
                
                config.last_sync = fields.Datetime.now()
                _logger.info("Cron sync completed for config: %s", config.server_url)
            except Exception as e:
                _logger.error("Cron sync failed for config %s: %s", config.server_url, str(e))

    @api.model
    def action_browser_sync(self):
        """Method called by browser to trigger a quick sync while watching the map"""
        config = self.search([('active', '=', True)], limit=1)
        if not config:
            return False

        # Throttling: only sync if last sync was more than the configured interval
        interval = config.browser_sync_interval or 30
        now = fields.Datetime.now()
        if config.last_sync and (now - config.last_sync).total_seconds() < interval:
            return False

        try:
            # Use FOR UPDATE NOWAIT to prevent multiple concurrent syncs from overlapping
            self.env.cr.execute('SELECT id FROM traccar_config WHERE id=%s FOR UPDATE NOWAIT', (config.id,))
            
            api = self.env['traccar.api'].create({'config_id': config.id})
            
            # 1. Sync device statuses (online/offline)
            api.sync_devices()
            
            # 2. Sync only recent positions for speed
            from datetime import datetime, timedelta
            from_date = datetime.now() - timedelta(minutes=5)
            api.sync_positions(from_date=from_date)
            
            config.last_sync = now
            return True
        except Exception as e:
            # psycopg2.errors.LockNotAvailable or similar if NOWAIT hits
            _logger.debug("Browser sync skipped or failed (likely concurrent update): %s", str(e))
            return False

    @api.model
    def get_browser_sync_interval(self):
        """Method for the browser to fetch its sync interval configuration"""
        config = self.search([('active', '=', True)], limit=1)
        return config.browser_sync_interval or 30


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    traccar_server_url = fields.Char(
        string='Traccar Server URL',
        default='http://localhost:8082',
        help='URL of your Traccar server (e.g., http://your-server:8082)'
    )
    traccar_username = fields.Char(
        string='Username',
        help='Traccar username for API access'
    )
    traccar_password = fields.Char(
        string='Password',
        help='Traccar password for API access'
    )
    traccar_active = fields.Boolean(
        string='Active',
        default=True
    )
    traccar_last_sync = fields.Datetime(
        string='Last Synchronization',
        readonly=True
    )
    traccar_sync_interval = fields.Integer(
        string='Sync Interval (minutes)',
        default=2,
        help='Automatic synchronization interval in minutes'
    )
    traccar_browser_sync_interval = fields.Integer(
        string='Live Map Sync Interval (seconds)',
        default=10,
        help='How often the Live Map triggers an API sync from the browser'
    )
    traccar_notification_type = fields.Selection([
        ('activity', 'Activity'),
        ('email', 'Email'),
        ('both', 'Activity and Email')
    ], string='Notification Type', default='activity')
    traccar_alert_time = fields.Integer(
        string='Alerting Time (minutes)',
        default=30
    )
    traccar_alert_user_ids = fields.Many2many(
        'res.users',
        string='Alert Users'
    )

    def get_values(self):
        res = super().get_values()
        config = self.env['traccar.config'].sudo().search([], limit=1)
        if config:
            res.update(
                traccar_server_url=config.server_url,
                traccar_username=config.username,
                traccar_password=config.password,
                traccar_active=config.active,
                traccar_last_sync=config.last_sync,
                traccar_sync_interval=config.sync_interval,
                traccar_browser_sync_interval=config.browser_sync_interval,
                traccar_notification_type=config.notification_type,
                traccar_alert_time=config.alert_time,
                traccar_alert_user_ids=[(6, 0, config.alert_user_ids.ids)],
            )
        else:
            res.update(
                traccar_server_url='http://localhost:8082',
                traccar_active=True,
                traccar_sync_interval=2,
                traccar_browser_sync_interval=10,
                traccar_notification_type='activity',
                traccar_alert_time=30,
                traccar_alert_user_ids=[(5, 0, 0)],
            )
        return res

    def set_values(self):
        super().set_values()
        config = self.env['traccar.config'].sudo().search([], limit=1)
        vals = {
            'server_url': self.traccar_server_url,
            'username': self.traccar_username,
            'password': self.traccar_password,
            'active': self.traccar_active,
            'sync_interval': self.traccar_sync_interval,
            'browser_sync_interval': self.traccar_browser_sync_interval,
            'notification_type': self.traccar_notification_type,
            'alert_time': self.traccar_alert_time,
            'alert_user_ids': [(6, 0, self.traccar_alert_user_ids.ids)],
        }
        if config:
            config.sudo().write(vals)
        else:
            self.env['traccar.config'].sudo().create(vals)

    def test_connection(self):
        """Test connection to Traccar server"""
        server_url = self.traccar_server_url or ''
        if not server_url.startswith(('http://', 'https://')):
            raise ValidationError("Server URL must start with http:// or https://")
        try:
            response = requests.get(
                f"{server_url.rstrip('/')}/api/server",
                auth=(self.traccar_username, self.traccar_password),
                timeout=10
            )
            if response.status_code == 200:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Success',
                        'message': 'Connection to Traccar server successful!',
                        'type': 'success',
                    }
                }
            else:
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': 'Error',
                        'message': f'Connection failed: {response.status_code}',
                        'type': 'danger',
                    }
                }
        except Exception as e:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Error',
                    'message': f'Connection failed: {str(e)}',
                    'type': 'danger',
                }
            }

    def sync_all_data(self):
        """Synchronize all data from Traccar"""
        self.set_values()
        config = self.env['traccar.config'].sudo().search([], limit=1)
        if config:
            config.sync_all_data()
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Synchronization Initiated',
                    'message': 'Data synchronization completed successfully.',
                    'type': 'success',
                }
            }
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': 'Error',
                'message': 'No Traccar configuration found to sync.',
                'type': 'danger',
            }
        }