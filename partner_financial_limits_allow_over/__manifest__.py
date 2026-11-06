# -*- coding: utf-8 -*-
{
    'name': 'Partner Financial Limits',
    'version': '18.0',
    'category': 'Accounting/Accounting',
    'summary': 'حدود الاستحقاق المالي للشركاء - Customer Credit & Vendor Debit Limits',
    'description': """
Partner Financial Limits - حدود الاستحقاق المالي للشركاء
=========================================================

This module adds financial control mechanisms on partners:

**Features:**
- Sale Credit Limit (حد الاستحقاق في المبيعات): Restricts sales based on customer outstanding receivables
- Purchase Debit Limit (حد الاستحقاق في المشتريات): Restricts purchases based on vendor outstanding payables
- Allow Over Credit/Debit: Per-partner toggle to allow exceeding limits
- Blocking mechanism on Sales Orders, Purchase Orders, and Invoices/Bills
- Optional override permission for authorized users
- Multi-company support
- Arabic validation messages

**Business Logic:**
- Validates limits when confirming Sales/Purchase Orders
- Validates limits when posting Customer Invoices/Vendor Bills
- Considers only posted and unpaid documents
- Shows current usage vs limit on partner form
    """,
    'author': 'Custom Development',
    'website': '',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'sale',
        'purchase',
        'account',
    ],
    'data': [
        'security/partner_limit_security.xml',
        'security/ir.model.access.csv',
        'views/res_partner_views.xml',
        'views/sale_order_views.xml',
        'views/purchase_order_views.xml',
        'views/account_move_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
