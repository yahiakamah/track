{
    'name': 'Employees Fleet License',
    'version': '18.0.1.0.0',
    'category': '',
    'summary': '',
    'description': "",
    'author': 'PUG',
    'website': 'https://www.yourcompany.com',
    'depends': [
        'base',
        'hr',
        'fleet',
    ],
    'data': [
        'data/scheduled_actions.xml',
        'views/hr_employee_view.xml',
        'views/fleet_vehicle_view.xml',
    ],
    'demo': [

    ],
    'installable': True,
    'auto_install': False,
    'application': False,
    'license': 'LGPL-3',
}