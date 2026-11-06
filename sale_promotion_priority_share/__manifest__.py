{
    'name': 'Promotion Priority and Shareability',
    'version': '18.0.1.0.0',
    'category': 'Sales',
    'summary': 'Delay promotion application to order confirmation, add priority and shareable fields',
    'depends': ['sale_loyalty', 'sale'],
    'data': [
        'security/ir.model.access.csv',
        'views/sale_loyalty_program_views.xml',
        'views/sale_order_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'post_init_hook': 'assign_priorities',
    'license': 'LGPL-3',
}