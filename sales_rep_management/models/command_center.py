# -*- coding: utf-8 -*-
"""Sales Command Center: one call that feeds the 3D management dashboard.

Every figure comes from the same KPI engine the mobile app uses
(``sales.representative._kpi_compute``), so the office and the reps see the
same numbers. Scope = chosen reps, or a supervisor's whole team, or the reps
of a sales area; period = date_from..date_to. Each section returns the
domain of its records so a click opens exactly what was counted.
"""
from datetime import timedelta

from odoo import _, api, fields, models


class SalesRepCommandCenter(models.AbstractModel):
    _name = 'sales.rep.command.center'
    _description = 'Sales Command Center'

    @api.model
    def _scope_reps(self, rep_ids=None, supervisor_id=None, area_id=None):
        Rep = self.env['sales.representative']  # record rules apply: a supervisor sees his team
        reps = Rep.search([('company_id', 'in', self.env.companies.ids)])
        if rep_ids:
            reps = reps & Rep.browse([int(r) for r in rep_ids])
        if supervisor_id:
            reps = reps & Rep.browse(int(supervisor_id)).sudo()._team_reps()
        if area_id:
            area = self.env['sales.area'].browse(int(area_id))
            area_reps = self.env['sales.area'].search([('id', 'child_of', area.id)]).mapped('sales_rep_ids')
            reps = reps & area_reps
        return reps

    @api.model
    def get_filters(self):
        reps = self.env['sales.representative'].search([('company_id', 'in', self.env.companies.ids)], order='name')
        return {
            'reps': [{'id': r.id, 'name': r.name} for r in reps],
            'supervisors': [{'id': r.id, 'name': r.name} for r in reps.filtered('is_supervisor')],
            'areas': [{'id': a.id, 'name': a.complete_name} for a in self.env['sales.area'].search([], order='complete_name')],
        }

    @api.model
    def get_data(self, date_from=None, date_to=None, rep_ids=None, supervisor_id=None, area_id=None):
        today = fields.Date.context_today(self)
        date_from = fields.Date.to_date(date_from) if date_from else today.replace(day=1)
        date_to = fields.Date.to_date(date_to) if date_to else today
        reps = self._scope_reps(rep_ids, supervisor_id, area_id)
        currency = self.env.company.currency_id
        if not reps:
            return {'empty': True, 'currency': currency.symbol, 'date_from': date_from, 'date_to': date_to}

        su = reps.sudo()
        k = su._kpi_compute(date_from, date_to)
        length = (date_to - date_from).days + 1
        prev = su._kpi_compute(date_from - timedelta(days=length), date_from - timedelta(days=1))
        env = self.env
        rid = reps.ids

        def trend(key):
            a, b = k.get(key) or 0.0, prev.get(key) or 0.0
            return round(100.0 * (a - b) / b, 1) if b else None

        inv_domain = [('sales_rep_id', 'in', rid), ('move_type', '=', 'out_invoice'), ('state', '=', 'posted'),
                      ('invoice_date', '>=', date_from), ('invoice_date', '<=', date_to)]
        invoices = env['account.move'].sudo().search(inv_domain)
        unpaid = invoices.filtered(lambda m: m.payment_state in ('not_paid', 'partial'))

        pick_domain = [('sale_id.sales_rep_id', 'in', rid), ('picking_type_code', '=', 'outgoing')]
        Picking = env['stock.picking'].sudo()
        to_deliver = Picking.search_count(pick_domain + [('state', 'in', ('confirmed', 'waiting', 'assigned'))])
        late = Picking.search_count(pick_domain + [('state', 'in', ('confirmed', 'waiting', 'assigned')),
                                                   ('scheduled_date', '<', fields.Datetime.now())])
        delivered = Picking.search_count(pick_domain + [('state', '=', 'done'), ('date_done', '>=', fields.Datetime.to_datetime(date_from)),
                                                        ('date_done', '<', fields.Datetime.to_datetime(date_to + timedelta(days=1)))])

        Task = env['sales.rep.task'].sudo()
        task_open = Task.search_count([('sales_rep_id', 'in', rid), ('state', 'in', ('new', 'in_progress'))])
        task_overdue = Task.search_count([('sales_rep_id', 'in', rid), ('state', 'in', ('new', 'in_progress')),
                                          ('date_deadline', '<', today)])

        targets = env['sales.rep.target'].sudo().search([
            ('sales_rep_id', 'in', rid), ('state', 'in', ('approved', 'closed')),
            ('date_from', '<=', date_to), ('date_to', '>=', date_from)])
        target_score = round(sum(targets.mapped('score')) / len(targets), 1) if targets else None

        sheets = env['sales.rep.commission.sheet'].sudo().search([
            ('sales_rep_id', 'in', rid), ('state', 'in', ('confirmed', 'paid')),
            ('date_from', '<=', date_to), ('date_to', '>=', date_from)])

        days = env['sales.rep.workday'].sudo().search([('sales_rep_id', 'in', rid), ('date', '>=', date_from), ('date', '<=', date_to)])
        worked = days.filtered('duration_hours')
        working_now = env['sales.rep.workday'].sudo().search_count([
            ('sales_rep_id', 'in', rid), ('date', '=', today), ('start_time', '!=', False), ('end_time', '=', False)])

        alerts = env['sales.rep.security.alert'].sudo().search_count([
            ('sales_rep_id', 'in', rid), ('detected_at', '>=', fields.Datetime.to_datetime(date_from))])
        complaints = env['sales.rep.dept.request'].sudo().search_count([
            ('sales_rep_id', 'in', rid), ('category', '=', 'customer'), ('state', 'in', ('submitted', 'info'))])

        sections = [
            {'key': 'visits', 'title': _('Visits'), 'icon': 'fa-map-marker', 'color': '#4F46E5',
             'value': f"{k['visited']}/{k['planned_visits']}", 'sub': _('%s%% journey compliance', k['compliance']),
             'trend': trend('visited'), 'model': 'sales.rep.visit',
             'domain': [('sales_rep_id', 'in', rid), ('planned_time', '>=', fields.Datetime.to_string(fields.Datetime.to_datetime(date_from))),
                        ('planned_time', '<', fields.Datetime.to_string(fields.Datetime.to_datetime(date_to + timedelta(days=1))))],
             'extra': [(_('Strike rate'), f"{k['strike_rate']}%"), (_('Coverage'), f"{k['coverage']}%")]},
            {'key': 'sales', 'title': _('Sales'), 'icon': 'fa-shopping-cart', 'color': '#7C3AED',
             'value': k['sales_value'], 'money': True, 'sub': _('%s orders', k['orders']), 'trend': trend('sales_value'),
             'model': 'sale.order', 'domain': [('sales_rep_id', 'in', rid), ('state', '=', 'sale'),
                                               ('date_order', '>=', fields.Datetime.to_string(fields.Datetime.to_datetime(date_from))),
                                               ('date_order', '<', fields.Datetime.to_string(fields.Datetime.to_datetime(date_to + timedelta(days=1))))],
             'extra': [(_('Drop size'), k['drop_size']), (_('Lines / order'), f"{k['lines_per_call']}")]},
            {'key': 'invoices', 'title': _('Invoices'), 'icon': 'fa-file-text-o', 'color': '#D97706',
             'value': sum(invoices.mapped('amount_total')), 'money': True, 'sub': _('%s invoices, %s unpaid', len(invoices), len(unpaid)),
             'model': 'account.move', 'domain': inv_domain,
             'extra': [(_('Unpaid'), sum(unpaid.mapped('amount_residual'))), (_('Returns'), f"{k['returns_pct']}%")]},
            {'key': 'delivery', 'title': _('Shipping & Delivery'), 'icon': 'fa-truck', 'color': '#0891B2',
             'value': to_deliver, 'sub': _('%s late · %s delivered in period', late, delivered),
             'model': 'stock.picking', 'domain': pick_domain + [('state', 'in', ('confirmed', 'waiting', 'assigned'))],
             'alert': bool(late)},
            {'key': 'collections', 'title': _('Collections'), 'icon': 'fa-money', 'color': '#059669',
             'value': k['collection_value'], 'money': True, 'trend': trend('collection_value'),
             'sub': _('Overdue debts: %s', currency.format(k['overdue_amount'])),
             'model': 'account.payment', 'domain': [('sales_rep_id', 'in', rid), ('payment_type', '=', 'inbound'),
                                                    ('date', '>=', date_from), ('date', '<=', date_to)],
             'extra': [(_('Overdue ratio'), f"{k['overdue_ratio']}%")]},
            {'key': 'tasks', 'title': _('Tasks'), 'icon': 'fa-check-square-o', 'color': '#16A34A',
             'value': task_open, 'sub': _('%s overdue · %s%% done on time', task_overdue, k['task_completion']),
             'model': 'sales.rep.task', 'domain': [('sales_rep_id', 'in', rid), ('state', 'in', ('new', 'in_progress'))],
             'alert': bool(task_overdue)},
            {'key': 'targets', 'title': _('Targets'), 'icon': 'fa-bullseye', 'color': '#DB2777',
             'value': f"{target_score}%" if target_score is not None else '—', 'sub': _('%s approved targets', len(targets)),
             'model': 'sales.rep.target', 'domain': [('id', 'in', targets.ids)]},
            {'key': 'commissions', 'title': _('Commissions'), 'icon': 'fa-percent', 'color': '#9333EA',
             'value': sum(sheets.mapped('total_amount')), 'money': True, 'sub': _('%s confirmed / paid sheets', len(sheets)),
             'model': 'sales.rep.commission.sheet', 'domain': [('id', 'in', sheets.ids)]},
            {'key': 'workdays', 'title': _('Working Days'), 'icon': 'fa-clock-o', 'color': '#0EA5E9',
             'value': f"{round(sum(worked.mapped('duration_hours')) / len(worked), 1)} h" if worked else '—',
             'sub': _('%s working now', working_now), 'model': 'sales.rep.workday', 'domain': [('id', 'in', days.ids)]},
            {'key': 'risk', 'title': _('Alerts & Complaints'), 'icon': 'fa-shield', 'color': '#DC2626',
             'value': alerts + complaints, 'sub': _('%s location alerts · %s open complaints', alerts, complaints),
             'model': 'sales.rep.security.alert', 'domain': [('sales_rep_id', 'in', rid),
                                                             ('detected_at', '>=', fields.Datetime.to_string(fields.Datetime.to_datetime(date_from)))],
             'alert': bool(alerts or complaints)},
        ]

        # leaderboard and areas (one KPI run per rep / area)
        leaderboard = []
        for rep in su:
            rk = rep._kpi_compute(date_from, date_to)
            leaderboard.append({'id': rep.id, 'name': rep.name, 'sales': rk['sales_value'], 'collections': rk['collection_value'],
                                'compliance': rk['compliance'], 'strike_rate': rk['strike_rate']})
        leaderboard.sort(key=lambda r: r['sales'], reverse=True)
        top = leaderboard[0]['sales'] if leaderboard and leaderboard[0]['sales'] else 1.0
        for r in leaderboard:
            r['bar'] = round(100.0 * r['sales'] / top, 1)

        areas = []
        for area in env['sales.area'].search([('parent_id', '=', False)]):
            area_reps = su & env['sales.area'].search([('id', 'child_of', area.id)]).mapped('sales_rep_ids')
            if area_reps:
                ak = area_reps._kpi_compute(date_from, date_to)
                areas.append({'id': area.id, 'name': area.name, 'reps': len(area_reps),
                              'sales': ak['sales_value'], 'collections': ak['collection_value'], 'compliance': ak['compliance']})
        areas.sort(key=lambda a: a['sales'], reverse=True)

        return {
            'empty': False, 'currency': currency.symbol, 'currency_position': currency.position,
            'date_from': date_from, 'date_to': date_to, 'rep_count': len(reps),
            'sections': sections, 'leaderboard': leaderboard[:15], 'areas': areas,
        }
