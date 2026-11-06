# -*- coding: utf-8 -*-

from collections import defaultdict

from odoo import _, api, fields, models
from odoo.tools import get_lang


class ECSalesReportCustomHandler(models.AbstractModel):
    _name = 'account.ec.sales.report.handler'
    _inherit = 'account.report.custom.handler'
    _description = 'EC Sales Report Custom Handler'

    def _get_custom_display_config(self):
        return {
            'components': {
                'AccountReportFilters': 'sdm_account_reports.SalesReportFilters',
            },
        }

    def _dynamic_lines_generator(self, report, options, all_column_groups_expression_totals, warnings=None):

        lines = []
        totals_by_column_group = {
            column_group_key: {
                'balance': 0.0,
                'goods': 0.0,
                'triangular': 0.0,
                'services': 0.0,
                'vat_number': '',
                'country_code': '',
                'sales_type_code': '',
            }
            for column_group_key in options['column_groups']
        }

        operation_categories = options['sales_report_taxes'].get('operation_category', {})
        ec_tax_filter_selection = {v.get('id'): v.get('selected') for v in options.get('ec_tax_filter_selection', [])}
        for partner, results in self._query_partners(report, options, warnings):
            for tax_ec_category in ('goods', 'triangular', 'services'):
                if not ec_tax_filter_selection[tax_ec_category]:
                    continue
                partner_values = defaultdict(dict)
                country_specific_code = operation_categories.get(tax_ec_category)
                has_found_a_line = False
                for col_grp_key in options['column_groups']:
                    partner_sum = results.get(col_grp_key, {})
                    partner_values[col_grp_key]['vat_number'] = partner_sum.get('vat_number', 'UNKNOWN')
                    partner_values[col_grp_key]['country_code'] = partner_sum.get('country_code', 'UNKNOWN')
                    partner_values[col_grp_key]['sales_type_code'] = []
                    partner_values[col_grp_key]['balance'] = partner_sum.get(tax_ec_category, 0.0)
                    totals_by_column_group[col_grp_key]['balance'] += partner_sum.get(tax_ec_category, 0.0)
                    for i, operation_id in enumerate(partner_sum.get('tax_element_id', [])):
                        if operation_id in options['sales_report_taxes'][tax_ec_category]:
                            has_found_a_line = True
                            partner_values[col_grp_key]['sales_type_code'] += [
                                country_specific_code or
                                (partner_sum.get('sales_type_code') and partner_sum.get('sales_type_code')[i])
                                or None]
                    partner_values[col_grp_key]['sales_type_code'] = ', '.join(set(partner_values[col_grp_key]['sales_type_code']))
                if has_found_a_line:
                    lines.append((0, self._get_report_line_partner(report, options, partner, partner_values, markup=tax_ec_category)))

        # Report total line.
        if lines:
            lines.append((0, self._get_report_line_total(report, options, totals_by_column_group)))

        return lines

    def _caret_options_initializer(self):

        return {
            'ec_sales': [
                {'name': _("View Partner"), 'action': 'caret_option_open_record_form'}
            ],
        }

    def _custom_options_initializer(self, report, options, previous_options=None):

        super()._custom_options_initializer(report, options, previous_options=previous_options)
        self._init_core_custom_options(report, options, previous_options)
        options.update({
            'sales_report_taxes': {
                'goods': tuple(self.env['account.tax'].search([
                    *self.env['account.tax']._check_company_domain(self.env.company),
                    ('amount', '=', 0.0),
                    ('amount_type', '=', 'percent'),
                ]).ids),
                'services': tuple(),
                'triangular': tuple(),
                'use_taxes_instead_of_tags': True,

            }
        })
        country_ids = self.env['res.country'].search([
            ('code', 'in', tuple(self._get_ec_country_codes(options)))
        ]).ids
        other_country_ids = tuple(set(country_ids) - {self.env.company.account_fiscal_country_id.id})
        options.setdefault('forced_domain', []).append(('partner_id.country_id', 'in', other_country_ids))

        report._init_options_journals(options, previous_options=previous_options, additional_journals_domain=[('type', '=', 'sale')])

        self._enable_export_buttons_for_common_vat_groups_in_branches(options)

    def _init_core_custom_options(self, report, options, previous_options=None):

        default_tax_filter = [
            {'id': 'goods', 'name': _('Goods'), 'selected': True},
            {'id': 'triangular', 'name': _('Triangular'), 'selected': True},
            {'id': 'services', 'name': _('Services'), 'selected': True},
        ]
        options['ec_tax_filter_selection'] = (previous_options or {}).get('ec_tax_filter_selection', default_tax_filter)

    def _get_report_line_partner(self, report, options, partner, partner_values, markup=''):

        column_values = []
        for column in options['columns']:
            value = partner_values[column['column_group_key']].get(column['expression_label'])
            column_values.append(report._build_column_dict(value, column, options=options))

        return {
            'id': report._get_generic_line_id('res.partner', partner.id, markup=markup),
            'name': partner is not None and (partner.name or '')[:128] or _('Unknown Partner'),
            'columns': column_values,
            'level': 2,
            'trust': partner.trust if partner else None,
            'caret_options': 'ec_sales',
        }

    def _get_report_line_total(self, report, options, totals_by_column_group):

        column_values = []
        for column in options['columns']:
            col_value = totals_by_column_group[column['column_group_key']].get(column['expression_label'])
            col_value = col_value if column['figure_type'] == 'monetary' else ''

            column_values.append(report._build_column_dict(col_value, column, options=options))

        return {
            'id': report._get_generic_line_id(None, None, markup='total'),
            'name': _('Total'),
            'class': 'total',
            'level': 1,
            'columns': column_values,
        }

    def _query_partners(self, report, options, warnings=None):

        groupby_partners = {}

        def assign_sum(row):

            if not company_currency.is_zero(row['balance']):
                groupby_partners.setdefault(row['groupby'], defaultdict(lambda: defaultdict(float)))

                groupby_partners_keyed = groupby_partners[row['groupby']][row['column_group_key']]
                if row['tax_element_id'] in options['sales_report_taxes']['goods']:
                    groupby_partners_keyed['goods'] += row['balance']
                elif row['tax_element_id'] in options['sales_report_taxes']['triangular']:
                    groupby_partners_keyed['triangular'] += row['balance']
                elif row['tax_element_id'] in options['sales_report_taxes']['services']:
                    groupby_partners_keyed['services'] += row['balance']

                groupby_partners_keyed.setdefault('tax_element_id', []).append(row['tax_element_id'])
                groupby_partners_keyed.setdefault('sales_type_code', []).append(row['sales_type_code'])

                vat = row['vat_number'] or ''
                groupby_partners_keyed.setdefault('vat_number', vat[2:])
                groupby_partners_keyed.setdefault('full_vat_number', vat)
                groupby_partners_keyed.setdefault('country_code', vat[:2])

                if warnings is not None:
                    if row['country_code'] not in self._get_ec_country_codes(options):
                        warnings['sdm_account_reports.sales_report_warning_non_ec_country'] = {'alert_type': 'warning'}
                    elif not row.get('vat_number'):
                        warnings['sdm_account_reports.sales_report_warning_missing_vat'] = {'alert_type': 'warning'}
                    if row.get('same_country') and row['country_code']:
                        warnings['sdm_account_reports.sales_report_warning_same_country'] = {'alert_type': 'warning'}

        company_currency = self.env.company.currency_id

        query, params = self._get_query_sums(report, options)
        self._cr.execute(query, params)

        dictfetchall = self._cr.dictfetchall()
        for res in dictfetchall:
            assign_sum(res)

        if groupby_partners:
            partners = self.env['res.partner'].with_context(active_test=False).browse(groupby_partners.keys())
        else:
            partners = self.env['res.partner']

        return [(partner, groupby_partners[partner.id]) for partner in partners.sorted()]

    def _get_query_sums(self, report, options):

        params = []
        queries = []
        ct_query = report._get_query_currency_table(options)
        allowed_ids = self._get_tag_ids_filtered(options)



        lang = self.env.user.lang or get_lang(self.env).code
        if options.get('sales_report_taxes', {}).get('use_taxes_instead_of_tags'):
            tax_elem_table = 'account_tax'
            aml_rel_table = 'account_move_line_account_tax_rel'
            tax_elem_table_name = f"COALESCE(account_tax.name->>'{lang}', account_tax.name->>'en_US')" if \
                self.pool['account.tax'].name.translate else 'account_tax.name'
        else:
            tax_elem_table = 'account_account_tag'
            aml_rel_table = 'account_account_tag_account_move_line_rel'
            tax_elem_table_name = f"COALESCE(account_account_tag.name->>'{lang}', account_account_tag.name->>'en_US')" if \
                self.pool['account.account.tag'].name.translate else 'account_account_tag.name'


        for column_group_key, column_group_options in report._split_options_per_column_group(options).items():
            tables, where_clause, where_params = report._query_get(column_group_options, 'strict_range')
            params.append(column_group_key)
            params += where_params
            if allowed_ids:
                where_clause += f" AND {tax_elem_table}.id IN %s"  # Add the tax element filter.
                params.append(tuple(allowed_ids))
            queries.append(f"""
                SELECT
                    %s                              AS column_group_key,
                    account_move_line.partner_id    AS groupby,
                    res_partner.vat                 AS vat_number,
                    res_country.code                AS country_code,
                    -SUM(account_move_line.balance) AS balance,
                    {tax_elem_table_name}           AS sales_type_code,
                    {tax_elem_table}.id             AS tax_element_id,
                    (comp_partner.country_id = res_partner.country_id) AS same_country
                FROM {tables}
                JOIN {ct_query} ON currency_table.company_id = account_move_line.company_id
                JOIN {aml_rel_table} ON {aml_rel_table}.account_move_line_id = account_move_line.id
                JOIN {tax_elem_table} ON {aml_rel_table}.{tax_elem_table}_id = {tax_elem_table}.id
                JOIN res_partner ON account_move_line.partner_id = res_partner.id
                JOIN res_country ON res_partner.country_id = res_country.id
                JOIN res_company ON res_company.id = account_move_line.company_id
                JOIN res_partner comp_partner ON comp_partner.id = res_company.partner_id
                WHERE {where_clause}
                GROUP BY {tax_elem_table}.id, {tax_elem_table}.name, account_move_line.partner_id,
                res_partner.vat, res_country.code, comp_partner.country_id, res_partner.country_id
            """)
        return ' UNION ALL '.join(queries), params

    @api.model
    def _get_tag_ids_filtered(self, options):

        allowed_taxes = set()
        for operation_type in options.get('ec_tax_filter_selection', []):
            if operation_type.get('selected'):
                allowed_taxes.update(options['sales_report_taxes'][operation_type.get('id')])
        return allowed_taxes

    @api.model
    def _get_ec_country_codes(self, options):

        rslt = {'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR', 'HU',
                'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE'}

        if fields.Date.from_string(options['date']['date_from']) < fields.Date.from_string('2021-01-01'):
            rslt.add('GB')
        return rslt

    def get_warning_act_window(self, options, params):
        act_window = {'type': 'ir.actions.act_window', 'context': {}}
        if params['type'] == 'no_vat':
            aml_domains = [
                ('partner_id.vat', '=', None),
                ('partner_id.country_id.code', 'in', tuple(self._get_ec_country_codes(options))),
            ]
            act_window.update({
                'name': _("Entries with partners with no VAT"),
                'context': {'search_default_group_by_partner': 1, 'expand': 1}
            })
        elif params['type'] == 'non_ec_country':
            aml_domains = [('partner_id.country_id.code', 'not in', tuple(self._get_ec_country_codes(options)))]
            act_window['name'] = _("EC tax on non EC countries")
        else:
            aml_domains = [('partner_id.country_id.code', '=', options.get('same_country_warning'))]
            act_window['name'] = _("EC tax on same country")
        use_taxes_instead_of_tags = options.get('sales_report_taxes', {}).get('use_taxes_instead_of_tags')
        tax_or_tag_field = 'tax_ids.id' if use_taxes_instead_of_tags else 'tax_tag_ids.id'
        amls = self.env['account.move.line'].search([
            *aml_domains,
            *self.env['account.report']._get_options_date_domain(options, 'strict_range'),
            (tax_or_tag_field, 'in', tuple(self._get_tag_ids_filtered(options)))
        ])

        if params['model'] == 'move':
            act_window.update({
                'views': [[self.env.ref('account.view_move_tree').id, 'list'], (False, 'form')],
                'res_model': 'account.move',
                'domain': [('id', 'in', amls.move_id.ids)],
            })
        else:
            act_window.update({
                'views': [(False, 'list'), (False, 'form')],
                'res_model': 'res.partner',
                'domain': [('id', 'in', amls.move_id.partner_id.ids)],
            })

        return act_window
