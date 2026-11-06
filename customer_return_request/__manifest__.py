# -*- coding: utf-8 -*-
{
    'name': 'Customer Return Request',
    'version': '18.0.1.0.0',
    'category': 'Inventory/Inventory',
    'summary': 'Commercial customer returns without an originating Sale Order, '
               'Delivery Order or Customer Invoice.',
    'description': """
Customer Return Request
=======================

Handle commercial customer returns that originate **outside** the normal Odoo
sales flow (no Sale Order / Delivery / Invoice), while keeping inventory
valuation and accounting fully consistent with standard Odoo behaviour.

Highlights
----------
* Configurable return reasons (master data, no hard-coded selection).
* Full approval workflow tracked in the chatter
  (Draft -> Submitted -> Waiting Approval -> Approved/Rejected -> Done/Cancelled).
* Two security groups (User / Manager) with proper record rules.
* On approval the module creates a **standard incoming picking**
  (Customer Location -> configured destination) - no custom stock moves.
* Stock Valuation Layers use the *Inventory Unit Cost* entered by the manager
  (works for AVCO / FIFO; Standard cost keeps using the product standard price,
  which is the expected Odoo behaviour) - no manual journal entries.
* Optionally creates a **standard Customer Credit Note** (out_refund) using the
  entered sale prices, so taxes, receivable and customer balance stay correct.
* Multi-company, multi-currency, lot/serial aware.
""",
    'author': 'PomoTech',
    'website': 'https://www.pomotech-eg.com',
    'license': 'LGPL-3',
    'depends': [
        'stock_account',  # pulls stock + account, gives stock valuation layers
        'mail',
    ],
    'data': [
        'security/customer_return_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'wizard/customer_return_reject_wizard_views.xml',
        'views/customer_return_reason_views.xml',
        'views/customer_return_request_views.xml',
        'views/customer_return_menus.xml',
        'report/customer_return_request_report.xml',
    ],
    'demo': [
        'demo/customer_return_demo.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
