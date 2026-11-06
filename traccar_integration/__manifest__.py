{
    'name': 'Traccar GPS Integration',
    'version': '18.0.1.0.0',
    'category': 'Fleet Management',
    'summary': 'Integration with Traccar GPS tracking server',
    'description': '''
        This module integrates Odoo with Traccar GPS tracking server.
        Features:
        - Fetch devices and positions from Traccar
        - Real-time location tracking
        - Historical tracking data
        - Device management
        - Geofencing alerts
        - Fleet integration
        - Command sending to devices
        - Dashboard and analytics
    ''',
    'author': 'Pomo Tech',
    'depends': ['base', 'fleet', 'web', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'security/traccar_security.xml',
        'views/traccar_config_views.xml',
        'views/traccar_device_views.xml',
        'views/traccar_position_views.xml',
        'views/traccar_menu_views.xml',
        # 'views/traccar_device_enhanced_views.xml',
        'views/fleet_vehicle_views.xml',
        'views/traccar_live_tracking_views.xml',
        'views/traccar_device_stop_views.xml',
        'wizards/traccar_sync_wizard_views.xml',
        'reports/traccar_report_views.xml',
        'reports/traccar_report_templates.xml',
        'data/ir_cron_data.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'traccar_integration/static/src/css/traccar_map.css',
            'traccar_integration/static/src/js/traccar_live_tracking.js',
        ],
    },
    'installable': True,
    'application': True,
    'auto_install': False,
    'license': 'LGPL-3',
}