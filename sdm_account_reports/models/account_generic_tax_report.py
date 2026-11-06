# -*- coding: utf-8 -*-

import ast
from collections import defaultdict

from odoo import models, api, fields, Command, _
from odoo.addons.web.controllers.utils import clean_action
from odoo.exceptions import UserError, RedirectWarning
from odoo.osv import expression
from odoo.tools.misc import get_lang


class AccountTaxReportHandler(models.AbstractModel):
    _name = 'account.tax.report.handler'
    _inherit = 'account.report.custom.handler'
    _description = 'Account Report Handler for Tax Reports'



    def _custom_options_initializer(self, report, options, previous_options=None):
        options['buttons'].append({'name': _('Closing Entry'), 'action': 'action_periodic_vat_entries', 'sequence': 110, 'always_show': True})

        if not previous_options or not previous_options.get('disable_archived_tag_test'):
            tables, where_clause, where_params = report._query_get(options, 'strict_range')
            self._cr.execute(f"""
                SELECT 1
                FROM {tables}
                JOIN account_account_tag_account_move_line_rel aml_tag
                    ON account_move_line.id = aml_tag.account_move_line_id
                JOIN account_account_tag tag
                    ON aml_tag.account_account_tag_id = tag.id
                WHERE {where_clause}
                AND NOT tag.active
                LIMIT 1
            """, where_params)

            options['contains_archived_tag'] = bool(self._cr.fetchone())

        self._enable_export_buttons_for_common_vat_groups_in_branches(options)


    def action_periodic_vat_entries(self, options):
        report = self.env.ref('account.generic_tax_report')
        moves = self.env['account.move']
        end_date = fields.Date.from_string(options['date']['date_to'])
        companies = self.env['res.company'].browse(report.get_report_company_ids(options))
        tax_locked_companies = companies.filtered(lambda c: c.tax_lock_date and c.tax_lock_date >= end_date)
        locked_companies_moves = self._get_tax_closing_entries_for_closed_period(report, options, tax_locked_companies, posted_only=False)
        posted_locked_moves = locked_companies_moves.filtered(lambda x: x.state == 'posted')
        moves += posted_locked_moves

        non_tax_locked_companies = companies - tax_locked_companies
        draft_locked_moves = locked_companies_moves.filtered(lambda x: x.state == 'draft')
        draft_closing_moves = self._get_tax_closing_entries_for_closed_period(report, options, non_tax_locked_companies, posted_only=False) \
                              + draft_locked_moves
        companies_to_regenerate = non_tax_locked_companies + draft_locked_moves.company_id
        moves += self._generate_tax_closing_entries(report, options, companies=companies_to_regenerate, closing_moves=draft_closing_moves)

        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_journal_line")
        action = clean_action(action, env=self.env)

        if len(moves) == 1:
            action['views'] = [(self.env.ref('account.view_move_form').id, 'form')]
            action['res_id'] = moves.id
        else:
            action['domain'] = [('id', 'in', moves.ids)]
            action['context'] = dict(ast.literal_eval(action['context']))
            action['context'].pop('search_default_posted', None)
        return action

    def _generate_tax_closing_entries(self, report, options, closing_moves=None, companies=None):

        if companies is None:
            companies = self.env['res.company'].browse(report.get_report_company_ids(options))
        end_date = fields.Date.from_string(options['date']['date_to'])

        closing_moves_by_company = defaultdict(lambda: self.env['account.move'])
        if closing_moves:
            for move in closing_moves.filtered(lambda x: x.state == 'draft'):
                closing_moves_by_company[move.company_id] |= move
        else:
            closing_moves = self.env['account.move']
            for company in companies:
                include_domestic, fiscal_positions = self._get_fpos_info_for_tax_closing(company, report, options)
                company_closing_moves = company._get_and_update_tax_closing_moves(end_date, fiscal_positions=fiscal_positions, include_domestic=include_domestic)
                closing_moves_by_company[company] = company_closing_moves
                closing_moves += company_closing_moves

        for company, company_closing_moves in closing_moves_by_company.items():
            countries = self.env['res.country']
            for move in company_closing_moves:
                if move.fiscal_position_id.foreign_vat:
                    countries |= move.fiscal_position_id.country_id
                else:
                    countries |= company.account_fiscal_country_id

            if self.env['account.tax.group']._check_misconfigured_tax_groups(company, countries):
                self._redirect_to_misconfigured_tax_groups(company, countries)

            for move in company_closing_moves:
                move_options = {**options, 'fiscal_position': move.fiscal_position_id.id if move.fiscal_position_id else 'domestic'}
                line_ids_vals, tax_group_subtotal = self._compute_vat_closing_entry(company, move_options)

                line_ids_vals += self._add_tax_group_closing_items(tax_group_subtotal, move)

                if move.line_ids:
                    line_ids_vals += [Command.delete(aml.id) for aml in move.line_ids]

                move_vals = {}
                if line_ids_vals:
                    move_vals['line_ids'] = line_ids_vals

                move.write(move_vals)

        return closing_moves

    def _get_tax_closing_entries_for_closed_period(self, report, options, companies, posted_only=True):

        end_date = fields.Date.from_string(options['date']['date_to'])
        closing_moves = self.env['account.move']
        for company in companies:
            include_domestic, fiscal_positions = self._get_fpos_info_for_tax_closing(company, report, options)
            fiscal_position_ids = fiscal_positions.ids + ([False] if include_domestic else [])
            state_domain = ('state', '=', 'posted') if posted_only else ('state', '!=', 'cancel')
            closing_moves += self.env['account.move'].search([
                ('company_id', '=', company.id),
                ('fiscal_position_id', 'in', fiscal_position_ids),
                ('tax_closing_end_date', '=', end_date),
                state_domain,
            ], limit=1)

        return closing_moves

    @api.model
    def _compute_vat_closing_entry(self, company, options):

        self = self.with_company(company)


        self.env['account.tax'].flush_model(['name', 'tax_group_id'])
        self.env['account.tax.repartition.line'].flush_model(['use_in_tax_closing'])
        self.env['account.move.line'].flush_model(['account_id', 'debit', 'credit', 'move_id', 'tax_line_id', 'date', 'company_id', 'display_type', 'parent_state'])
        self.env['account.move'].flush_model(['state'])
        lang = self.env.user.lang or get_lang(self.env).code
        tax_name = f"COALESCE(tax.name->>'{lang}', tax.name->>'en_US')" if \
            self.pool['account.tax'].name.translate else 'tax.name'

        sql = f"""
            SELECT "account_move_line".tax_line_id as tax_id,
                    tax.tax_group_id as tax_group_id,
                    {tax_name} as tax_name,
                    "account_move_line".account_id,
                    COALESCE(SUM("account_move_line".balance), 0) as amount
            FROM account_tax tax, account_tax_repartition_line repartition, %s
            WHERE %s
              AND tax.id = "account_move_line".tax_line_id
              AND repartition.id = "account_move_line".tax_repartition_line_id
              AND repartition.use_in_tax_closing
            GROUP BY tax.tax_group_id, "account_move_line".tax_line_id, tax.name, "account_move_line".account_id
        """

        new_options = {
            **options,
            'all_entries': False,
            'date': dict(options['date']),
        }

        period_start, period_end = company._get_tax_closing_period_boundaries(fields.Date.from_string(options['date']['date_to']))
        new_options['date']['date_from'] = fields.Date.to_string(period_start)
        new_options['date']['date_to'] = fields.Date.to_string(period_end)
        new_options['date']['period_type'] = 'custom'
        new_options['date']['filter'] = 'custom'
        report = self.env['account.report'].browse(options['report_id'])
        new_options = report.with_context(allowed_company_ids=company.ids).get_options(previous_options=new_options)

        new_options['fiscal_position'] = options['fiscal_position']

        tables, where_clause, where_params = self.env.ref('account.generic_tax_report')._query_get(
            new_options,
            'strict_range',
            domain=self._get_vat_closing_entry_additional_domain()
        )
        query = sql % (tables, where_clause)
        self.env.cr.execute(query, where_params)
        results = self.env.cr.dictfetchall()
        results = self._postprocess_vat_closing_entry_results(company, new_options, results)

        tax_group_ids = [r['tax_group_id'] for r in results]
        tax_groups = {}
        for tg, result in zip(self.env['account.tax.group'].browse(tax_group_ids), results):
            if tg not in tax_groups:
                tax_groups[tg] = {}
            if result.get('tax_id') not in tax_groups[tg]:
                tax_groups[tg][result.get('tax_id')] = []
            tax_groups[tg][result.get('tax_id')].append((result.get('tax_name'), result.get('account_id'), result.get('amount')))


        move_vals_lines = []
        tax_group_subtotal = {}
        currency = self.env.company.currency_id
        for tg, values in tax_groups.items():
            total = 0
            if not tg.tax_receivable_account_id or not tg.tax_payable_account_id:
                continue
            for dummy, value in values.items():
                for v in value:
                    tax_name, account_id, amt = v
                    move_vals_lines.append((0, 0, {'name': tax_name, 'debit': abs(amt) if amt < 0 else 0, 'credit': amt if amt > 0 else 0, 'account_id': account_id}))
                    total += amt

            if not currency.is_zero(total):
                key = (tg.advance_tax_payment_account_id.id or False, tg.tax_receivable_account_id.id, tg.tax_payable_account_id.id)

                if tax_group_subtotal.get(key):
                    tax_group_subtotal[key] += total
                else:
                    tax_group_subtotal[key] = total


        if len(move_vals_lines) == 0:
            rep_ln_in = self.env['account.tax.repartition.line'].search([
                *self.env['account.tax.repartition.line']._check_company_domain(company),
                ('account_id.deprecated', '=', False),
                ('repartition_type', '=', 'tax'),
                ('document_type', '=', 'invoice'),
                ('tax_id.type_tax_use', '=', 'purchase')
            ], limit=1)
            rep_ln_out = self.env['account.tax.repartition.line'].search([
                *self.env['account.tax.repartition.line']._check_company_domain(company),
                ('account_id.deprecated', '=', False),
                ('repartition_type', '=', 'tax'),
                ('document_type', '=', 'invoice'),
                ('tax_id.type_tax_use', '=', 'sale')
            ], limit=1)

            if rep_ln_out.account_id and rep_ln_in.account_id:
                move_vals_lines = [
                    Command.create({
                        'name': _('Tax Received Adjustment'),
                        'debit': 0,
                        'credit': 0.0,
                        'account_id': rep_ln_out.account_id.id
                    }),

                    Command.create({
                        'name': _('Tax Paid Adjustment'),
                        'debit': 0.0,
                        'credit': 0,
                        'account_id': rep_ln_in.account_id.id
                    })
                ]

        return move_vals_lines, tax_group_subtotal

    def _get_vat_closing_entry_additional_domain(self):
        return []

    def _postprocess_vat_closing_entry_results(self, company, options, results):
        return results

    @api.model
    def _add_tax_group_closing_items(self, tax_group_subtotal, closing_move):

        def _add_line(account, name, company_currency):
            self.env.cr.execute(sql_account, (
                account,
                closing_move.tax_closing_end_date,
                closing_move.company_id.id,
            ))
            result = self.env.cr.dictfetchone()
            advance_balance = result.get('balance') or 0
            if not company_currency.is_zero(advance_balance):
                line_ids_vals.append((0, 0, {
                    'name': name,
                    'debit': abs(advance_balance) if advance_balance < 0 else 0,
                    'credit': abs(advance_balance) if advance_balance > 0 else 0,
                    'account_id': account
                }))
            return advance_balance

        currency = closing_move.company_id.currency_id
        sql_account = '''
            SELECT SUM(aml.balance) AS balance
            FROM account_move_line aml
            LEFT JOIN account_move move ON move.id = aml.move_id
            WHERE aml.account_id = %s
              AND aml.date <= %s
              AND move.state = 'posted'
              AND aml.company_id = %s
        '''
        line_ids_vals = []
        account_already_balanced = []
        for key, value in tax_group_subtotal.items():
            total = value
            if key[0] and key[0] not in account_already_balanced:
                total += _add_line(key[0], _('Balance tax advance payment account'), currency)
                account_already_balanced.append(key[0])
            if key[1] and key[1] not in account_already_balanced:
                total += _add_line(key[1], _('Balance tax current account (receivable)'), currency)
                account_already_balanced.append(key[1])
            if key[2] and key[2] not in account_already_balanced:
                total += _add_line(key[2], _('Balance tax current account (payable)'), currency)
                account_already_balanced.append(key[2])
            if not currency.is_zero(total):
                line_ids_vals.append(Command.create({
                    'name': _('Payable tax amount') if total < 0 else _('Receivable tax amount'),
                    'debit': total if total > 0 else 0,
                    'credit': abs(total) if total < 0 else 0,
                    'account_id': key[2] if total < 0 else key[1]
                }))
        return line_ids_vals

    @api.model
    def _redirect_to_misconfigured_tax_groups(self, company, countries):

        need_config_action = {
            'type': 'ir.actions.act_window',
            'name': 'Tax groups',
            'res_model': 'account.tax.group',
            'view_mode': 'tree',
            'views': [[False, 'list']],
            'context': len(countries) == 1 and {'search_default_country_id': countries.ids or {}},
        }

        raise RedirectWarning(
            _('Please specify the accounts necessary for the Tax Closing Entry.'),
            need_config_action,
            _('Configure your TAX accounts - %s', company.display_name),
        )

    def _get_fpos_info_for_tax_closing(self, company, report, options):

        if options['fiscal_position'] == 'domestic':
            fiscal_positions = self.env['account.fiscal.position']
        elif options['fiscal_position'] == 'all':
            fiscal_positions = self.env['account.fiscal.position'].search([
                *self.env['account.fiscal.position']._check_company_domain(company),
                ('foreign_vat', '!=', False),
            ])
        else:
            fpos_ids = [options['fiscal_position']]
            fiscal_positions = self.env['account.fiscal.position'].browse(fpos_ids)

        if options['fiscal_position'] == 'all':
            fiscal_country = company.account_fiscal_country_id
            include_domestic = not fiscal_positions \
                               or not report.country_id \
                               or fiscal_country == fiscal_positions[0].country_id
        else:
            include_domestic = options['fiscal_position'] == 'domestic'

        return include_domestic, fiscal_positions

    def _get_amls_with_archived_tags_domain(self, options):
        domain = [
            ('tax_tag_ids.active', '=', False),
            ('parent_state', '=', 'posted'),
            ('date', '>=', options['date']['date_from']),
        ]
        if options['date']['mode'] == 'single':
            domain.append(('date', '<=', options['date']['date_to']))
        return domain

    def action_open_amls_with_archived_tags(self, options, params=None):
        return {
            'name': _("Journal items with archived tax tags"),
            'type': 'ir.actions.act_window',
            'res_model': 'account.move.line',
            'domain': self._get_amls_with_archived_tags_domain(options),
            'context': {'active_test': False},
            'views': [(self.env.ref('sdm_account_reports.view_archived_tag_move_tree').id, 'list')],
        }


class GenericTaxReportCustomHandler(models.AbstractModel):
    _name = 'account.generic.tax.report.handler'
    _inherit = 'account.tax.report.handler'
    _description = 'Generic Tax Report Custom Handler'

    def _dynamic_lines_generator(self, report, options, all_column_groups_expression_totals, warnings=None):
        return self._get_dynamic_lines(report, options, 'default')

    def _caret_options_initializer(self):
        return {
            'generic_tax_report': [
                {'name': _("Audit"), 'action': 'caret_option_audit_tax'},
            ]
        }

    def _get_dynamic_lines(self, report, options, grouping):

        options_by_column_group = report._split_options_per_column_group(options)

        if grouping == 'tax_account':
            groupby_fields = [('src_tax', 'type_tax_use'), ('src_tax', 'id'), ('account', 'id')]
            comodels = [None, 'account.tax', 'account.account']
        elif grouping == 'account_tax':
            groupby_fields = [('src_tax', 'type_tax_use'), ('account', 'id'), ('src_tax', 'id')]
            comodels = [None, 'account.account', 'account.tax']
        else:
            groupby_fields = [('src_tax', 'type_tax_use'), ('src_tax', 'id')]
            comodels = [None, 'account.tax']

        if grouping in ('tax_account', 'account_tax'):
            tax_amount_hierarchy = self._read_generic_tax_report_amounts(report, options_by_column_group, groupby_fields)
        else:
            tax_amount_hierarchy = self._read_generic_tax_report_amounts_no_tax_details(report, options, options_by_column_group)



        record_ids_gb = [set() for dummy in groupby_fields]

        def populate_record_ids_gb_recursively(node, level=0):
            for k, v in node.items():
                if k:
                    record_ids_gb[level].add(k)
                    if v.get('children'):
                        populate_record_ids_gb_recursively(v['children'], level=level + 1)

        populate_record_ids_gb_recursively(tax_amount_hierarchy)

        sorting_map_list = []
        for i, comodel in enumerate(comodels):
            if comodel:
                records = self.env[comodel].with_context(active_test=False).search([('id', 'in', tuple(record_ids_gb[i]))])
                sorting_map = {r.id: (r, j) for j, r in enumerate(records)}
                sorting_map_list.append(sorting_map)
            else:
                selection = self.env['account.tax']._fields['type_tax_use'].selection
                sorting_map_list.append({v[0]: (v, j) for j, v in enumerate(selection) if v[0] in record_ids_gb[i]})

        lines = []
        self._populate_lines_recursively(
            report,
            options,
            lines,
            sorting_map_list,
            groupby_fields,
            tax_amount_hierarchy,
        )
        return lines



    @api.model
    def _read_generic_tax_report_amounts_no_tax_details(self, report, options, options_by_column_group):

        self._cr.execute(
            '''
                SELECT
                    group_tax.id,
                    group_tax.type_tax_use,
                    ARRAY_AGG(child_tax.id) AS child_tax_ids,
                    ARRAY_AGG(DISTINCT child_tax.type_tax_use) AS child_types
                FROM account_tax_filiation_rel group_tax_rel
                JOIN account_tax group_tax ON group_tax.id = group_tax_rel.parent_tax
                JOIN account_tax child_tax ON child_tax.id = group_tax_rel.child_tax
                WHERE group_tax.amount_type = 'group' AND group_tax.company_id IN %s
                GROUP BY group_tax.id
            ''',
            [tuple(report.get_report_company_ids(options))],
        )
        group_of_taxes_info = {}
        child_to_group_of_taxes = {}
        for row in self._cr.dictfetchall():
            row['to_expand'] = row['child_types'] != ['none']
            group_of_taxes_info[row['id']] = row
            for child_id in row['child_tax_ids']:
                child_to_group_of_taxes[child_id] = row['id']

        results = defaultdict(lambda: {  # key: type_tax_use
            'base_amount': {column_group_key: 0.0 for column_group_key in options['column_groups']},
            'tax_amount': {column_group_key: 0.0 for column_group_key in options['column_groups']},
            'children': defaultdict(lambda: {  # key: tax_id
                'base_amount': {column_group_key: 0.0 for column_group_key in options['column_groups']},
                'tax_amount': {column_group_key: 0.0 for column_group_key in options['column_groups']},
            }),
        })

        for column_group_key, options in options_by_column_group.items():
            tables, where_clause, where_params = report._query_get(options, 'strict_range')

            self._cr.execute(f'''
                SELECT
                    tax.id AS tax_id,
                    tax.type_tax_use AS tax_type_tax_use,
                    src_group_tax.id AS src_group_tax_id,
                    src_group_tax.type_tax_use AS src_group_tax_type_tax_use,
                    src_tax.id AS src_tax_id,
                    src_tax.type_tax_use AS src_tax_type_tax_use,
                    SUM(account_move_line.balance) AS base_amount
                FROM {tables}
                JOIN account_move_line_account_tax_rel tax_rel ON account_move_line.id = tax_rel.account_move_line_id
                JOIN account_tax tax ON tax.id = tax_rel.account_tax_id
                LEFT JOIN account_tax src_tax ON src_tax.id = account_move_line.tax_line_id
                LEFT JOIN account_tax src_group_tax ON src_group_tax.id = account_move_line.group_tax_id
                WHERE {where_clause}
                    AND (
                        /* CABA */
                        account_move_line__move_id.always_tax_exigible
                        OR account_move_line__move_id.tax_cash_basis_rec_id IS NOT NULL
                        OR tax.tax_exigibility != 'on_payment'
                    )
                    AND (
                        (
                            /* Tax lines affecting the base of others. */
                            account_move_line.tax_line_id IS NOT NULL
                            AND (
                                src_tax.type_tax_use IN ('sale', 'purchase')
                                OR src_group_tax.type_tax_use IN ('sale', 'purchase')
                            )
                        )
                        OR
                        (
                            /* For regular base lines. */
                            account_move_line.tax_line_id IS NULL
                            AND tax.type_tax_use IN ('sale', 'purchase')
                        )
                    )
                GROUP BY tax.id, src_group_tax.id, src_tax.id
                ORDER BY src_group_tax.sequence, src_group_tax.id, src_tax.sequence, src_tax.id, tax.sequence, tax.id
            ''', where_params)

            group_of_taxes_with_extra_base_amount = set()
            for row in self._cr.dictfetchall():
                is_tax_line = bool(row['src_tax_id'])
                if is_tax_line:
                    if row['src_group_tax_id'] \
                            and not group_of_taxes_info[row['src_group_tax_id']]['to_expand'] \
                            and row['tax_id'] in group_of_taxes_info[row['src_group_tax_id']]['child_tax_ids']:

                        pass
                    elif row['tax_type_tax_use'] == 'none' and child_to_group_of_taxes.get(row['tax_id']):

                        group_tax_id = child_to_group_of_taxes[row['tax_id']]
                        if group_tax_id not in group_of_taxes_with_extra_base_amount:
                            group_tax_info = group_of_taxes_info[group_tax_id]
                            results[group_tax_info['type_tax_use']]['children'][group_tax_id]['base_amount'][column_group_key] += row['base_amount']
                            group_of_taxes_with_extra_base_amount.add(group_tax_id)
                    else:
                        tax_type_tax_use = row['src_group_tax_type_tax_use'] or row['src_tax_type_tax_use']
                        results[tax_type_tax_use]['children'][row['tax_id']]['base_amount'][column_group_key] += row['base_amount']
                else:
                    if row['tax_id'] in group_of_taxes_info and group_of_taxes_info[row['tax_id']]['to_expand']:
                        group_info = group_of_taxes_info[row['tax_id']]
                        for child_tax_id in group_info['child_tax_ids']:
                            results[group_info['type_tax_use']]['children'][child_tax_id]['base_amount'][column_group_key] += row['base_amount']
                    else:
                        results[row['tax_type_tax_use']]['children'][row['tax_id']]['base_amount'][column_group_key] += row['base_amount']

            self._cr.execute(f'''
                SELECT
                    tax.id AS tax_id,
                    tax.type_tax_use AS tax_type_tax_use,
                    group_tax.id AS group_tax_id,
                    group_tax.type_tax_use AS group_tax_type_tax_use,
                    SUM(account_move_line.balance) AS tax_amount
                FROM {tables}
                JOIN account_tax tax ON tax.id = account_move_line.tax_line_id
                LEFT JOIN account_tax group_tax ON group_tax.id = account_move_line.group_tax_id
                WHERE {where_clause}
                    AND (
                        /* CABA */
                        account_move_line__move_id.always_tax_exigible
                        OR account_move_line__move_id.tax_cash_basis_rec_id IS NOT NULL
                        OR tax.tax_exigibility != 'on_payment'
                    )
                    AND (
                        (group_tax.id IS NULL AND tax.type_tax_use IN ('sale', 'purchase'))
                        OR
                        (group_tax.id IS NOT NULL AND group_tax.type_tax_use IN ('sale', 'purchase'))
                    )
                GROUP BY tax.id, group_tax.id
            ''', where_params)

            for row in self._cr.dictfetchall():

                tax_id = row['tax_id']
                if row['group_tax_id']:
                    tax_type_tax_use = row['group_tax_type_tax_use']
                    if not group_of_taxes_info[row['group_tax_id']]['to_expand']:
                        tax_id = row['group_tax_id']
                else:
                    tax_type_tax_use = row['group_tax_type_tax_use'] or row['tax_type_tax_use']

                results[tax_type_tax_use]['tax_amount'][column_group_key] += row['tax_amount']
                results[tax_type_tax_use]['children'][tax_id]['tax_amount'][column_group_key] += row['tax_amount']

        return results

    def _read_generic_tax_report_amounts(self, report, options_by_column_group, groupby_fields):

        fetch_group_of_taxes = False

        select_clause_list = []
        groupby_query_list = []
        for alias, field in groupby_fields:
            select_clause_list.append(f'{alias}.{field} AS {alias}_{field}')
            groupby_query_list.append(f'{alias}.{field}')

            # Fetch both info from the originator tax and the child tax to manage the group of taxes.
            if alias == 'src_tax':
                select_clause_list.append(f'tax.{field} AS tax_{field}')
                groupby_query_list.append(f'tax.{field}')
                fetch_group_of_taxes = True

        select_clause_str = ','.join(select_clause_list)
        groupby_query_str = ','.join(groupby_query_list)


        group_of_taxes_to_expand = set()
        if fetch_group_of_taxes:
            group_of_taxes = self.env['account.tax'].with_context(active_test=False).search([('amount_type', '=', 'group')])
            for group in group_of_taxes:
                if set(group.children_tax_ids.mapped('type_tax_use')) != {'none'}:
                    group_of_taxes_to_expand.add(group.id)

        res = {}
        for column_group_key, options in options_by_column_group.items():
            tables, where_clause, where_params = report._query_get(options, 'strict_range')
            tax_details_query, tax_details_params = self.env['account.move.line']._get_query_tax_details(tables, where_clause, where_params)

            row_keys = set()

            self._cr.execute(f'''
                SELECT
                    {select_clause_str},
                    trl.document_type = 'refund' AS is_refund,
                    SUM(tdr.base_amount) AS base_amount,
                    SUM(tdr.tax_amount) AS tax_amount
                FROM ({tax_details_query}) AS tdr
                JOIN account_tax_repartition_line trl ON trl.id = tdr.tax_repartition_line_id
                JOIN account_tax tax ON tax.id = tdr.tax_id
                JOIN account_tax src_tax ON
                    src_tax.id = COALESCE(tdr.group_tax_id, tdr.tax_id)
                    AND src_tax.type_tax_use IN ('sale', 'purchase')
                JOIN account_account account ON account.id = tdr.base_account_id
                WHERE tdr.tax_exigible
                GROUP BY tdr.tax_repartition_line_id, trl.document_type, tdr.display_type, {groupby_query_str}
                ORDER BY src_tax.sequence, src_tax.id, tax.sequence, tax.id
            ''', tax_details_params)

            for row in self._cr.dictfetchall():
                node = res
                cumulated_row_key = [row['is_refund']]

                for alias, field in groupby_fields:
                    grouping_key = f'{alias}_{field}'

                    if grouping_key == 'src_tax_id' and row['src_tax_id'] in group_of_taxes_to_expand:

                        cumulated_row_key.append(row[grouping_key])

                        grouping_key = 'tax_id'

                    row_key = row[grouping_key]
                    cumulated_row_key.append(row_key)
                    cumulated_row_key_tuple = tuple(cumulated_row_key)

                    node.setdefault(row_key, {
                        'base_amount': {column_group_key: 0.0 for column_group_key in options['column_groups']},
                        'tax_amount': {column_group_key: 0.0 for column_group_key in options['column_groups']},
                        'children': {},
                    })
                    sub_node = node[row_key]

                    # Add amounts.
                    if cumulated_row_key_tuple not in row_keys:
                        sub_node['base_amount'][column_group_key] += row['base_amount']
                    sub_node['tax_amount'][column_group_key] += row['tax_amount']

                    node = sub_node['children']
                    row_keys.add(cumulated_row_key_tuple)

        return res

    def _populate_lines_recursively(self, report, options, lines, sorting_map_list, groupby_fields, values_node, index=0, type_tax_use=None, parent_line_id=None):

        if index == len(groupby_fields):
            return

        alias, field = groupby_fields[index]
        groupby_key = f'{alias}_{field}'

        sorting_map = sorting_map_list[index]
        sorted_keys = sorted(list(values_node.keys()), key=lambda x: sorting_map[x][1])

        for key in sorted_keys:

            if groupby_key == 'src_tax_type_tax_use':
                type_tax_use = key
            sign = -1 if type_tax_use == 'sale' else 1

            tax_amount_dict = values_node[key]
            columns = []
            tax_base_amounts = tax_amount_dict['base_amount']
            tax_amounts = tax_amount_dict['tax_amount']

            for column in options['columns']:
                tax_base_amount = tax_base_amounts[column['column_group_key']]
                tax_amount = tax_amounts[column['column_group_key']]

                expr_label = column.get('expression_label')

                if expr_label == 'net':
                    col_value = sign * tax_base_amount if index == len(groupby_fields) - 1 else ''

                if expr_label == 'tax':
                    col_value = sign * tax_amount

                columns.append(report._build_column_dict(col_value, column, options=options))

            default_vals = {
                'columns': columns,
                'level': index if index == 0 else index + 1,
                'unfoldable': False,
            }
            report_line = self._build_report_line(report, options, default_vals, groupby_key, sorting_map[key][0], parent_line_id)

            if groupby_key == 'src_tax_id':
                report_line['caret_options'] = 'generic_tax_report'

            lines.append((0, report_line))

            self._populate_lines_recursively(
                report,
                options,
                lines,
                sorting_map_list,
                groupby_fields,
                tax_amount_dict.get('children'),
                index=index + 1,
                type_tax_use=type_tax_use,
                parent_line_id=report_line['id'],
            )

    def _build_report_line(self, report, options, default_vals, groupby_key, value, parent_line_id):

        report_line = dict(default_vals)
        if parent_line_id is not None:
            report_line['parent_id'] = parent_line_id

        if groupby_key == 'src_tax_type_tax_use':
            type_tax_use_option = value
            report_line['id'] = report._get_generic_line_id(None, None, markup=type_tax_use_option[0], parent_line_id=parent_line_id)
            report_line['name'] = type_tax_use_option[1]

        elif groupby_key == 'src_tax_id':
            tax = value
            report_line['id'] = report._get_generic_line_id(tax._name, tax.id, parent_line_id=parent_line_id)

            if tax.amount_type == 'percent':
                report_line['name'] = f"{tax.name} ({tax.amount}%)"
            elif tax.amount_type == 'fixed':
                report_line['name'] = f"{tax.name} ({tax.amount})"
            else:
                report_line['name'] = tax.name

            if options.get('multi-company'):
                report_line['name'] = f"{report_line['name']} - {tax.company_id.display_name}"

        elif groupby_key == 'account_id':
            account = value
            report_line['id'] = report._get_generic_line_id(account._name, account.id, parent_line_id=parent_line_id)

            if options.get('multi-company'):
                report_line['name'] = f"{account.display_name} - {account.company_id.display_name}"
            else:
                report_line['name'] = account.display_name

        return report_line

    def caret_option_audit_tax(self, options, params):
        report = self.env['account.report'].browse(options['report_id'])
        model, tax_id = report._get_model_info_from_id(params['line_id'])

        if model != 'account.tax':
            raise UserError(_("Cannot audit tax from another model than account.tax."))

        tax = self.env['account.tax'].browse(tax_id)

        if tax.amount_type == 'group':
            tax_affecting_base_domain = [
                ('tax_ids', 'in', tax.children_tax_ids.ids),
                ('tax_repartition_line_id', '!=', False),
            ]
        else:
            tax_affecting_base_domain = [
                ('tax_ids', '=', tax.id),
                ('tax_ids.type_tax_use', '=', tax.type_tax_use),
                ('tax_repartition_line_id', '!=', False),
            ]

        domain = report._get_options_domain(options, 'strict_range') + expression.OR((
            [
                ('tax_ids', 'in', tax.ids),
                ('tax_ids.type_tax_use', '=', tax.type_tax_use),
                ('tax_repartition_line_id', '=', False),
            ],
            [
                ('group_tax_id', '=', tax.id) if tax.amount_type == 'group' else ('tax_line_id', '=', tax.id),
            ],
            tax_affecting_base_domain,
        ))

        ctx = self._context.copy()
        ctx.update({'search_default_group_by_account': 2, 'expand': 1})

        return {
            'type': 'ir.actions.act_window',
            'name': _('Journal Items for Tax Audit'),
            'res_model': 'account.move.line',
            'views': [[self.env.ref('account.view_move_line_tax_audit_tree').id, 'list']],
            'domain': domain,
            'context': ctx,
        }


class GenericTaxReportCustomHandlerAT(models.AbstractModel):
    _name = 'account.generic.tax.report.handler.account.tax'
    _inherit = 'account.generic.tax.report.handler'
    _description = 'Generic Tax Report Custom Handler (Account -> Tax)'

    def _dynamic_lines_generator(self, report, options, all_column_groups_expression_totals, warnings=None):
        return super()._get_dynamic_lines(report, options, 'account_tax')


class GenericTaxReportCustomHandlerTA(models.AbstractModel):
    _name = 'account.generic.tax.report.handler.tax.account'
    _inherit = 'account.generic.tax.report.handler'
    _description = 'Generic Tax Report Custom Handler (Tax -> Account)'

    def _dynamic_lines_generator(self, report, options, all_column_groups_expression_totals, warnings=None):
        return super()._get_dynamic_lines(report, options, 'tax_account')
