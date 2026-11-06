# -*- coding: utf-8 -*-
"""Field activity report: one set of filters, every flow of the field team.

Filters: period, representatives, supervisor, areas (with their sub-areas),
customers, customer tags, company. Sections: visits, sales, invoices,
collections, shipping & delivery, tasks. Each section gives its KPIs, a
breakdown by the chosen axis (representative, area, supervisor, customer,
day, week or month), a second breakdown that matters for that flow (visit
results, top products, debt aging, journals, transfer kinds, task types) and,
optionally, the document lines. Output: PDF, Excel, or the interactive
pivot / graph analysis with the same filters.
"""
from datetime import date

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.misc import format_amount, format_date
from odoo.tools.translate import LazyTranslate

_lt = LazyTranslate(__name__)

GROUP_BY = [
    ('sales_rep_id', 'Representative'),
    ('sales_area_id', 'Area'),
    ('supervisor_id', 'Supervisor'),
    ('partner_id', 'Customer'),
    ('date:day', 'Day'),
    ('date:week', 'Week'),
    ('date:month', 'Month'),
]

# key: (model, title, measures [(field, label, kind)], base domain, extra breakdown (groupby, title, measures), detail fields)
SECTIONS = {
    'visits': {
        'model': 'sales.rep.report.visit',
        'title': _lt('Visits'),
        'measures': [
            ('nbr_planned', _lt('Planned'), 'int'), ('nbr_plan_done', _lt('Planned Visited'), 'int'),
            ('nbr_missed', _lt('Missed'), 'int'), ('nbr_unplanned', _lt('Unplanned'), 'int'),
            ('nbr_productive', _lt('Productive'), 'int'), ('nbr_successful', _lt('Successful'), 'int'),
            ('duration_hours', _lt('Hours'), 'float'), ('order_count', _lt('Orders'), 'int'),
            ('sale_amount', _lt('Sales'), 'money'), ('collected_amount', _lt('Collected'), 'money'),
        ],
        'domain': [('status', '!=', 'cancelled')],
        'breakdown': ('visit_result', _lt('By Visit Result'), ['nbr_visited', 'sale_amount']),
        'details': ['date', 'sales_rep_id', 'partner_id', 'sales_area_id', 'origin', 'status', 'visit_result',
                    'duration_hours', 'sale_amount', 'collected_amount'],
    },
    'sales': {
        'model': 'sales.rep.report.sale',
        'title': _lt('Sales'),
        'measures': [
            ('order_count', _lt('Orders'), 'int'), ('qty_ordered', _lt('Qty'), 'float'), ('qty_delivered', _lt('Delivered'), 'float'),
            ('qty_free', _lt('Free Qty'), 'float'), ('gross_amount', _lt('Gross'), 'money'), ('discount_amount', _lt('Discount'), 'money'),
            ('amount_untaxed', _lt('Untaxed'), 'money'), ('amount_total', _lt('Total'), 'money'),
        ],
        'domain': [('state', '=', 'sale')],
        'breakdown': ('product_id', _lt('Top Products'), ['qty_ordered', 'amount_untaxed']),
        'breakdown_limit': 15,
        'breakdown_order': 'amount_untaxed:sum desc',
        'details': ['date', 'order_id', 'sales_rep_id', 'partner_id', 'product_id', 'qty_ordered', 'qty_delivered',
                    'discount_amount', 'amount_untaxed'],
    },
    'invoices': {
        'model': 'sales.rep.report.invoice',
        'title': _lt('Invoices & Debts'),
        'measures': [
            ('invoice_count', _lt('Invoices'), 'int'), ('amount_total', _lt('Invoiced'), 'money'), ('amount_paid', _lt('Paid'), 'money'),
            ('amount_residual', _lt('Due'), 'money'), ('nbr_overdue', _lt('Overdue #'), 'int'), ('amount_overdue', _lt('Overdue'), 'money'),
        ],
        'domain': [],
        'breakdown': ('aging', _lt('Debt Aging'), ['amount_residual']),
        'details': ['date', 'move_id', 'move_type', 'sales_rep_id', 'partner_id', 'invoice_date_due', 'payment_state',
                    'amount_total', 'amount_residual', 'days_overdue'],
    },
    'collections': {
        'model': 'sales.rep.report.collection',
        'title': _lt('Collections'),
        'measures': [
            ('payment_count', _lt('Payments'), 'int'), ('amount', _lt('Collected'), 'money'), ('amount_cash', _lt('Cash'), 'money'),
            ('amount_bank', _lt('Bank / Other'), 'money'), ('amount_not_handed', _lt('Cash Not Handed Over'), 'money'),
        ],
        'domain': [],
        'breakdown': ('journal_id', _lt('By Journal'), ['payment_count', 'amount']),
        'details': ['date', 'payment_id', 'sales_rep_id', 'partner_id', 'journal_id', 'state', 'handed_over', 'amount'],
    },
    'deliveries': {
        'model': 'sales.rep.report.delivery',
        'title': _lt('Shipping & Delivery'),
        'measures': [
            ('picking_count', _lt('Transfers'), 'int'), ('nbr_done', _lt('Done'), 'int'), ('nbr_open', _lt('Open'), 'int'),
            ('nbr_late', _lt('Late'), 'int'), ('nbr_on_time', _lt('On Time'), 'int'), ('qty_demand', _lt('Qty Demanded'), 'float'),
            ('qty_done', _lt('Qty Done'), 'float'),
        ],
        'domain': [('state', '!=', 'cancel')],
        'breakdown': ('kind', _lt('By Type'), ['picking_count', 'nbr_done', 'nbr_late']),
        'details': ['date', 'picking_id', 'kind', 'sales_rep_id', 'partner_id', 'state', 'scheduled_date', 'date_done',
                    'is_late', 'qty_demand', 'qty_done'],
    },
    'tasks': {
        'model': 'sales.rep.report.task',
        'title': _lt('Tasks'),
        'measures': [
            ('task_count', _lt('Tasks'), 'int'), ('nbr_done', _lt('Done'), 'int'), ('nbr_on_time', _lt('On Time'), 'int'),
            ('nbr_failed', _lt('Failed'), 'int'), ('nbr_open', _lt('Open'), 'int'), ('nbr_overdue', _lt('Overdue'), 'int'),
            ('amount_due', _lt('To Collect'), 'money'),
        ],
        'domain': [('state', '!=', 'cancelled')],
        'breakdown': ('task_type', _lt('By Type'), ['task_count', 'nbr_done', 'nbr_overdue']),
        'details': ['date', 'task_id', 'task_type', 'sales_rep_id', 'partner_id', 'state', 'done_date', 'days_late'],
    },
}
AGGREGATE = {'order_count': 'count_distinct', 'days_overdue': 'max', 'days_late': 'max'}


class SalesRepFieldReportWizard(models.TransientModel):
    _name = 'sales.rep.field.report.wizard'
    _description = 'Field Activity Report'

    @api.model
    def _default_date_from(self):
        return fields.Date.context_today(self).replace(day=1)

    date_from = fields.Date(required=True, default=_default_date_from)
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    sales_rep_ids = fields.Many2many('sales.representative', string='Representatives')
    supervisor_id = fields.Many2one('sales.representative', string='Supervisor (whole team)')
    area_ids = fields.Many2many('sales.area', string='Areas', help='Includes the sub-areas.')
    partner_ids = fields.Many2many('res.partner', string='Customers')
    partner_category_ids = fields.Many2many('res.partner.category', string='Customer Tags')
    group_by = fields.Selection(GROUP_BY, default='sales_rep_id', required=True)
    show_details = fields.Boolean('Include Document Lines', default=False)
    detail_limit = fields.Integer('Max Lines per Section', default=300)

    include_visits = fields.Boolean('Visits', default=True)
    include_sales = fields.Boolean('Sales', default=True)
    include_invoices = fields.Boolean('Invoices & Debts', default=True)
    include_collections = fields.Boolean('Collections', default=True)
    include_deliveries = fields.Boolean('Shipping & Delivery', default=True)
    include_tasks = fields.Boolean('Tasks', default=True)

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for wiz in self:
            if wiz.date_from > wiz.date_to:
                raise UserError(_('The start date must be before the end date.'))

    # ------------------------------------------------------------------ domains
    def _sections(self):
        self.ensure_one()
        return [k for k in SECTIONS if self[f'include_{k}']]

    def _rep_ids(self):
        reps = self.sales_rep_ids
        if self.supervisor_id:
            team = self.supervisor_id.sudo()._team_reps()
            reps = (reps & team) if reps else team
        return reps.ids

    def _filter_domain(self):
        self.ensure_one()
        domain = [('company_id', '=', self.company_id.id),
                  ('date', '>=', self.date_from), ('date', '<=', self.date_to)]
        rep_ids = self._rep_ids()
        if self.sales_rep_ids or self.supervisor_id:
            domain.append(('sales_rep_id', 'in', rep_ids or [0]))
        if self.area_ids:
            domain.append(('sales_area_id', 'child_of', self.area_ids.ids))
        if self.partner_ids:
            domain.append(('partner_id', 'child_of', self.partner_ids.ids))
        if self.partner_category_ids:
            domain.append(('partner_id.category_id', 'in', self.partner_category_ids.ids))
        return domain

    def _section_domain(self, key):
        return self._filter_domain() + SECTIONS[key]['domain']

    # --------------------------------------------------------------------- data
    @staticmethod
    def _agg(field):
        return f"{field}:{AGGREGATE.get(field, 'sum')}"

    def _label(self, model, groupby, value):
        if value is None or value is False or (hasattr(value, '_name') and not value):
            return _('Undefined')
        fname = groupby.split(':')[0]
        field = model._fields[fname]
        if field.type == 'many2one':
            return value.display_name
        if field.type == 'selection':
            return dict(field._description_selection(self.env)).get(value, value)
        if isinstance(value, date):
            if groupby.endswith(':month'):
                return format_date(self.env, value, date_format='MMMM yyyy')
            if groupby.endswith(':week'):
                return _('Week of %s', format_date(self.env, value))
            return format_date(self.env, value)
        return str(value)

    def _fmt(self, value, kind):
        value = value or 0
        if kind == 'money':
            return format_amount(self.env, value, self.company_id.currency_id)
        if kind == 'pct':
            return f'{value:.1f} %'
        if kind == 'float':
            return f'{value:,.2f}'
        return f'{int(value):,}'

    def _grouped(self, model, domain, groupby, measures, kinds, limit=None, order=None):
        aggs = [self._agg(m) for m in measures]
        rows = model._read_group(domain, [groupby], aggs, limit=limit, order=order or groupby)
        return [{'label': self._label(model, groupby, r[0]), 'values': [v or 0 for v in r[1:]],
                 'cells': [self._fmt(v, k) for v, k in zip(r[1:], kinds)]} for r in rows]

    def _derived_kpis(self, key, t):
        def pct(a, b):
            return round(100.0 * a / b, 1) if b else 0.0
        if key == 'visits':
            visited = t['nbr_plan_done'] + t['nbr_unplanned']
            return [(_('Journey compliance'), pct(t['nbr_plan_done'], t['nbr_planned']), 'pct'),
                    (_('Strike rate'), pct(t['nbr_productive'], visited), 'pct'),
                    (_('Avg. visit (min)'), round(60.0 * t['duration_hours'] / visited, 1) if visited else 0.0, 'float')]
        if key == 'sales':
            return [(_('Drop size'), round(t['amount_untaxed'] / t['order_count'], 2) if t['order_count'] else 0.0, 'money'),
                    (_('Discount %'), pct(t['discount_amount'], t['gross_amount']), 'pct'),
                    (_('Delivered %'), pct(t['qty_delivered'], t['qty_ordered']), 'pct')]
        if key == 'invoices':
            return [(_('Paid %'), pct(t['amount_paid'], t['amount_total']), 'pct'),
                    (_('Overdue ratio'), pct(t['amount_overdue'], t['amount_residual']), 'pct')]
        if key == 'collections':
            return [(_('Cash share'), pct(t['amount_cash'], t['amount']), 'pct'),
                    (_('Avg. payment'), round(t['amount'] / t['payment_count'], 2) if t['payment_count'] else 0.0, 'money')]
        if key == 'deliveries':
            return [(_('On-time rate'), pct(t['nbr_on_time'], t['nbr_done']), 'pct'),
                    (_('Fill rate'), pct(t['qty_done'], t['qty_demand']), 'pct')]
        if key == 'tasks':
            closed = t['nbr_done'] + t['nbr_failed']
            return [(_('Completion'), pct(t['nbr_done'], closed), 'pct'),
                    (_('On-time'), pct(t['nbr_on_time'], t['nbr_done']), 'pct')]
        return []

    def _section_data(self, key, with_details=None, limit=None):
        spec = SECTIONS[key]
        model = self.env[spec['model']]
        domain = self._section_domain(key)
        fields_ = [m[0] for m in spec['measures']]
        total_row = model._read_group(domain, [], [self._agg(f) for f in fields_])[0]
        totals = {f: (v or 0) for f, v in zip(fields_, total_row)}
        kinds = [m[2] for m in spec['measures']]
        bd_field, bd_title, bd_measures = spec['breakdown']
        bd_kinds = ['money' if m in ('sale_amount', 'amount_untaxed', 'amount_residual', 'amount') else
                    ('float' if model._fields[m].type == 'float' else 'int') for m in bd_measures]
        kpis = self._derived_kpis(key, totals)
        with_details = self.show_details if with_details is None else with_details
        limit = limit or self.detail_limit or 300
        details = []
        if with_details:
            detail_fields = spec['details']
            for rec in model.search(domain, limit=limit, order='date desc, id desc'):
                row = []
                for f in detail_fields:
                    fld, val = model._fields[f], rec[f]
                    if fld.type == 'many2one':
                        val = val.display_name or ''
                    elif fld.type == 'selection':
                        val = dict(fld._description_selection(self.env)).get(val, val or '')
                    elif fld.type == 'boolean':
                        val = _('Yes') if val else ''
                    elif fld.type == 'float':
                        val = f'{val:,.2f}'
                    elif fld.type in ('date', 'datetime'):
                        val = fields.Date.to_string(val) if fld.type == 'date' and val else (
                            fields.Datetime.to_string(fields.Datetime.context_timestamp(self, val)) if val else '')
                    row.append(val)
                details.append(row)
        return {
            'key': key,
            'title': self.env._(spec['title']),
            'model': spec['model'],
            'measures': [(f, self.env._(label), kind) for f, label, kind in spec['measures']],
            'totals': totals,
            'totals_cells': [self._fmt(totals[f], k) for f, k in zip(fields_, kinds)],
            'kpis': [(label, value, kind, self._fmt(value, kind)) for label, value, kind in kpis],
            'groups': self._grouped(model, domain, self.group_by, fields_, kinds),
            'breakdown': {
                'title': self.env._(bd_title),
                'measures': [(m, model._fields[m]._description_string(self.env), k) for m, k in zip(bd_measures, bd_kinds)],
                'rows': self._grouped(model, domain, bd_field, bd_measures, bd_kinds, limit=spec.get('breakdown_limit'),
                                      order=spec.get('breakdown_order')),
            },
            'detail_headers': [model._fields[f]._description_string(self.env) for f in spec['details']] if with_details else [],
            'details': details,
            'detail_truncated': bool(with_details) and len(details) >= limit,
        }

    def get_report_data(self, with_details=None, limit=None):
        self.ensure_one()
        if not self._sections():
            raise UserError(_('Choose at least one section.'))
        filters = [(_('Period'), f"{format_date(self.env, self.date_from)} → {format_date(self.env, self.date_to)}"),
                   (_('Company'), self.company_id.name)]
        if self.supervisor_id:
            filters.append((_('Supervisor'), self.supervisor_id.name))
        if self.sales_rep_ids:
            filters.append((_('Representatives'), ', '.join(self.sales_rep_ids.mapped('name'))))
        if self.area_ids:
            filters.append((_('Areas'), ', '.join(self.area_ids.mapped('complete_name'))))
        if self.partner_ids:
            filters.append((_('Customers'), ', '.join(self.partner_ids.mapped('name'))))
        if self.partner_category_ids:
            filters.append((_('Customer Tags'), ', '.join(self.partner_category_ids.mapped('name'))))
        filters.append((_('Grouped by'), dict(self._fields['group_by']._description_selection(self.env))[self.group_by]))
        return {
            'filters': filters,
            'group_label': dict(self._fields['group_by']._description_selection(self.env))[self.group_by],
            'currency': self.company_id.currency_id,
            'sections': [self._section_data(k, with_details, limit) for k in self._sections()],
        }

    # ------------------------------------------------------------------ actions
    def action_print_pdf(self):
        self.ensure_one()
        self.get_report_data()  # validate before rendering
        return self.env.ref('sales_rep_management.action_report_field_activity').report_action(self)

    def action_export_xlsx(self):
        self.ensure_one()
        self.get_report_data()
        return {'type': 'ir.actions.act_url', 'target': 'self',
                'url': f'/sales_rep_management/field_report_excel/{self.id}'}

    def _open(self, key):
        self.ensure_one()
        spec = SECTIONS[key]
        action = self.env['ir.actions.act_window']._for_xml_id(f'sales_rep_management.action_report_{key}')
        action['domain'] = self._section_domain(key)
        groupby = self.group_by
        action['context'] = {'pivot_row_groupby': [groupby], 'graph_groupbys': [groupby], 'group_by': []}
        action['name'] = _('%(title)s Analysis', title=self.env._(spec['title']))
        return action

    def action_open_visits(self):
        return self._open('visits')

    def action_open_sales(self):
        return self._open('sales')

    def action_open_invoices(self):
        return self._open('invoices')

    def action_open_collections(self):
        return self._open('collections')

    def action_open_deliveries(self):
        return self._open('deliveries')

    def action_open_tasks(self):
        return self._open('tasks')


class ReportFieldActivity(models.AbstractModel):
    _name = 'report.sales_rep_management.report_field_activity'
    _description = 'Field Activity Report (PDF)'

    @api.model
    def _get_report_values(self, docids, data=None):
        docs = self.env['sales.rep.field.report.wizard'].browse(docids)
        return {'doc_ids': docids, 'doc_model': 'sales.rep.field.report.wizard', 'docs': docs,
                'report_data': {w.id: w.get_report_data() for w in docs}}
