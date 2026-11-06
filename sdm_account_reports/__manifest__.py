# -*- coding: utf-8 -*-
#################################################################################
# Author      : Realweb Digital Agency (<https://realwebdigital.com>)
# Copyright(c): 2023-Realweb Digital Agency
# All Rights Reserved.
#
# This program is copyright property of the author mentioned above.
# You can`t redistribute it and/or modify it.
#
#################################################################################
{
    'name': 'Realweb Accounting Reports',
    'summary': 'View and create reports',
    'category': 'Accounting/Accounting',
    'description': """Odoo Accounting offers key financial reports like Profit & Loss, Balance Sheet, Cash Flow, and General Ledger, providing real-time insights to track financial performance and ensure accurate financial management.
""",
    'version': '18.0.1.0.0',
    'website': 'https://realwebdigital.com',
    'support': 'support@realwebdigital.com',
    'author': "Realweb Digital Agency",
    'depends': ['account'],
    'data': [
        'security/ir.model.access.csv',
        'data/pdf_export_templates.xml',
        'data/balance_sheet.xml',
        'data/cash_flow_report.xml',
        'data/executive_summary.xml',
        'data/profit_and_loss.xml',
        'data/aged_partner_balance.xml',
        'data/general_ledger.xml',
        'data/trial_balance.xml',
        'data/sales_report.xml',
        'data/partner_ledger.xml',
        'data/multicurrency_revaluation_report.xml',
        'data/deferred_reports.xml',
        'data/journal_report.xml',
        'data/generic_tax_report.xml',
        'views/account_report_view.xml',
        'data/account_report_actions.xml',
        'data/menuitems.xml',
        'data/mail_activity_type_data.xml',
        'views/account_move_views.xml',
        'views/res_company_views.xml',
        'views/partner_view.xml',
        'views/account_journal_dashboard_view.xml',
        'views/mail_activity_views.xml',
        'views/res_config_settings_views.xml',
        'views/report_template.xml',
        'wizard/multicurrency_revaluation.xml',
        'wizard/report_export_wizard.xml',
        'wizard/account_report_file_download_error_wizard.xml',
        'wizard/fiscal_year.xml',
        'wizard/mail_activity_schedule_views.xml',
        'views/account_activity.xml',
        'views/account_account_views.xml',
        'views/account_tax_views.xml',
    ],
    'auto_install': True,
    'installable': True,
    'license': 'LGPL-3',
    'post_init_hook': 'set_periodicity_journal_on_companies',
    'assets': {
        'sdm_account_reports.assets_pdf_export': [
            ('include', 'web._assets_helpers'),
            'web/static/src/scss/pre_variables.scss',
            'web/static/lib/bootstrap/scss/_variables.scss',
            ('include', 'web._assets_bootstrap_backend'),
            'web/static/fonts/fonts.scss',
            'sdm_account_reports/static/src/scss/**/*',
        ],
        'web.report_assets_common': [
            'sdm_account_reports/static/src/scss/account_pdf_export_template.scss',
        ],

        'web.assets_backend': [
            'sdm_account_reports/static/src/components/**/*',
            'sdm_account_reports/static/src/js/**/*',
            'sdm_account_reports/static/src/widgets/**/*',
        ],
        'web.assets_web_dark': [
            'sdm_account_reports/static/src/scss/*.dark.scss',
        ],
        'web.qunit_suite_tests': [
            'sdm_account_reports/static/tests/*.js',
        ],
        'web.assets_tests': [
            'sdm_account_reports/static/tests/tours/**/*',
        ],
    },
    "price": 69.00,
    "currency": 'USD',
    'images': ['static/description/accounting_banner.png'],
    'installable': True,
    'auto_install': False,
}
