 README.md
# Traccar GPS Integration for Odoo 17

This module provides comprehensive integration between Odoo 17 and Traccar GPS tracking server, allowing you to manage GPS devices and track positions directly within Odoo.

## Features

- **Device Management**: Sync and manage GPS devices from Traccar server
- **Real-time Position Tracking**: Fetch and display GPS positions
- **Historical Data**: View historical tracking data with filtering
- **Fleet Integration**: Link GPS devices to fleet vehicles
- **Automated Sync**: Automatic data synchronization with configurable intervals
- **Reports**: Generate Excel and PDF reports for tracking data
- **Map Integration**: Visual map display of device positions
- **API Integration**: Complete REST API integration with Traccar server

## Installation

1. **Copy the module** to your Odoo addons directory:
   ```bash
   cp -r traccar_integration /path/to/odoo/addons/
   ```

2. **Install dependencies**:
   ```bash
   pip install requests
   ```

3. **Update the app list** in Odoo and install the module:
   - Go to Apps → Update Apps List
   - Search for "Traccar GPS Integration"
   - Click Install

## Configuration

1. **Set up Traccar Server Connection**:
   - Go to GPS Tracking → Configuration → Server Settings
   - Enter your Traccar server URL (e.g., http://your-server:8082)
   - Provide username and password for Traccar API access
   - Test the connection

2. **Configure Synchronization**:
   - Set sync interval (default: 5 minutes)
   - Enable automatic synchronization

3. **Initial Data Sync**:
   - Use "Sync All Data" button or
   - Go to Configuration → Sync Data for advanced options

## Usage

### Device Management
- View all GPS devices in GPS Tracking → Devices
- Monitor device status (online/offline)
- Link devices to fleet vehicles
- View device details and attributes

### Position Tracking
- Real-time position updates in GPS Tracking → Positions
- Filter positions by device, date, validity
- View positions on map
- Export position data

### Reports
- Generate device summary reports
- Position history reports
- Travel and stop reports
- Export to Excel or PDF

## API Endpoints

The module integrates with Traccar's REST API:

- `GET /api/devices` - Fetch all devices
- `GET /api/positions` - Fetch positions
- `POST /api/commands/send` - Send commands to devices

## Troubleshooting

### Connection Issues
- Verify Traccar server is running and accessible
- Check firewall settings
- Ensure API credentials are correct

### Sync Issues
- Check cron job is active
- Verify server response times
- Monitor Odoo logs for errors

### Performance Tips
- Limit position sync to recent data
- Use device filters for large fleets
- Adjust sync intervals based on needs

## Advanced Configuration

### Custom Fields
Add custom device attributes by extending the model:

```python
class TraccarDevice(models.Model):
    _inherit = 'traccar.device'
    
    custom_field = fields.Char('Custom Field')
```

### Webhooks
Configure Traccar webhooks for real-time updates:

```json
{
    "url": "http://your-odoo-server/traccar/webhook",
    "type": "allEvents"
}
```

### Geofencing
Implement geofencing alerts by extending position model:

```python
class TraccarPosition(models.Model):
    _inherit = 'traccar.position'
    
    @api.model
    def create(self, vals):
        position = super().create(vals)
        self._check_geofences(position)
        return position
```

## Security

- API credentials are encrypted in database
- Access control via Odoo user groups
- Audit trail for all GPS data changes

## Support

For issues and feature requests:
- Check Odoo logs: Settings → Technical → Logging
- Verify Traccar server logs
- Test API connection manually

## License

This module is licensed under LGPL-3.

---

## Module Structure

```
traccar_integration/
├── __init__.py
├── __manifest__.py
├── models/
│   ├── __init__.py
│   ├── traccar_config.py
│   ├── traccar_device.py
│   ├── traccar_position.py
│   └── traccar_api.py
├── views/
│   ├── traccar_config_views.xml
│   ├── traccar_device_views.xml
│   ├── traccar_position_views.xml
│   └── traccar_menu_views.xml
├── wizards/
│   ├── __init__.py
│   ├── traccar_sync_wizard.py
│   └── traccar_sync_wizard_views.xml
├── reports/
│   ├── __init__.py
│   ├── traccar_reports.py
│   └── traccar_report_views.xml
├── security/
│   └── ir.model.access.csv
├── data/
│   └── ir_cron_data.xml
├── static/
│   ├── description/
│   │   └── icon.png
│   ├── src/
│   │   ├── js/
│   │   │   └── traccar_map_widget.js
│   │   └── css/
│   │       └── traccar_map.css
└── README.md
```