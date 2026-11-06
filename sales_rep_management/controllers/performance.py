# -*- coding: utf-8 -*-
"""Mobile endpoints for rep tasks and the 'My KPIs' screen."""
import json
import logging
from datetime import timedelta

from odoo import fields, http, _
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from .utils import SalesRepUtils
from . import mobile_ops
from ..models.performance import KPI_TYPES, PERCENT_KPIS, LOWER_IS_BETTER

_logger = logging.getLogger(__name__)
KPI_LABELS = dict(KPI_TYPES)


class PerformanceController(http.Controller, SalesRepUtils):

    def _json(self, data, status=200):
        return request.make_response(json.dumps(data, default=str),
                                     headers={'Content-Type': 'application/json'}, status=status)

    def _body(self):
        try:
            return json.loads(request.httprequest.data or '{}')
        except ValueError:
            return {}

    _task_payload = staticmethod(mobile_ops.task_payload)

    # ------------------------------------------------------------------
    @http.route('/api/mobile/tasks', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def tasks(self, **kw):
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        tasks = mobile_ops.rep_tasks(request.env, rep)
        return self._json({'success': True, 'tasks': [self._task_payload(t) for t in tasks]})

    @http.route('/api/mobile/tasks/<int:task_id>/update', type='http', auth='public', methods=['POST'], cors='*', csrf=False)
    def task_update(self, task_id, **kw):
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        try:
            with request.env.cr.savepoint():
                task = mobile_ops.task_update(request.env, rep, task_id, self._body())
        except (UserError, ValidationError) as e:
            return self._json({'success': False, 'message': str(e)})
        return self._json({'success': True, 'task': task})

    # ------------------------------------------------------------------
    @http.route('/api/mobile/kpi/summary', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def kpi_summary(self, **kw):
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        rep = rep.sudo()
        today = fields.Date.context_today(rep.user_id)
        month_start = today.replace(day=1)
        month = rep._kpi_compute(month_start, today)
        day = rep._kpi_compute(today, today)

        targets = request.env['sales.rep.target'].sudo().search([
            ('sales_rep_id', '=', rep.id), ('scope', '=', 'rep'), ('state', '=', 'approved'),
            ('date_from', '<=', today), ('date_to', '>=', today)])
        target_rows = []
        for t in targets:
            for line in t.line_ids:
                target_rows.append({
                    'target': t.name,
                    'kpi_type': line.kpi_type,
                    'label': KPI_LABELS.get(line.kpi_type, line.kpi_type) + (
                        f" - {line.product_category_id.name}" if line.product_category_id else ''),
                    'is_percent': line.kpi_type in PERCENT_KPIS,
                    'lower_is_better': line.kpi_type in LOWER_IS_BETTER,
                    'target_value': line.target_value,
                    'actual_value': line.actual_value,
                    'achievement': round(line.achievement, 1),
                    'weight': line.weight,
                })
        score = round(sum(t.score for t in targets) / len(targets), 1) if targets else None

        # Rank among the reps of the same supervisor by month sales (no target needed).
        rank = team_size = None
        if rep.supervisor_id:
            peers = request.env['sales.representative'].sudo().search([('supervisor_id', '=', rep.supervisor_id.id)])
            values = sorted(((p._kpi_compute(month_start, today)['sales_value'], p.id) for p in peers), reverse=True)
            team_size = len(values)
            rank = next((i + 1 for i, (_v, pid) in enumerate(values) if pid == rep.id), None)

        return self._json({
            'success': True,
            'currency_symbol': rep.company_id.currency_id.symbol,
            'period': {'from': month_start, 'to': today},
            'month': month,
            'today': day,
            'targets': target_rows,
            'score': score,
            'rank': rank,
            'team_size': team_size,
            'open_tasks': rep.open_task_count,
            # commission earned so far this month (estimate; the confirmed sheet is what is paid)
            'commission_estimate': request.env['sales.rep.commission.sheet'].sudo().estimate_for(rep, month_start, today),
        })

    # ------------------------------------------------------------------
    @http.route('/api/mobile/team/summary', type='http', auth='public', methods=['GET'], cors='*', csrf=False)
    def team_summary(self, **kw):
        """Supervisor mode: every rep under the caller, today and this month."""
        rep, _user = self._authenticate_request()
        if not rep:
            return self._json({'success': False, 'message': 'Unauthorized'}, 401)
        rep = rep.sudo()
        team = rep._team_reps() - rep
        if not rep.is_supervisor and not team:
            return self._json({'success': False, 'message': _('Only supervisors can see a team.')}, 403)
        today = fields.Date.context_today(rep.user_id)
        month_start = today.replace(day=1)
        Partner = request.env['res.partner']
        Workday = request.env['sales.rep.workday'].sudo()
        members = []
        for member in team.sorted('name'):
            month = member._kpi_compute(month_start, today)
            day = member._kpi_compute(today, today)
            # debts of the customers the member works with (route customers + sold to)
            route_partners = request.env['sales.route.customer'].sudo().search([
                ('route_id.sales_rep_id', '=', member.id), ('route_id.state', '!=', 'cancelled')]).mapped('partner_id').ids
            sold_to = request.env['sale.order'].sudo().search([
                ('user_id', '=', member.user_id.id), ('state', '=', 'sale')]).mapped('partner_id').ids if member.user_id else []
            due = Partner._sales_rep_due_map(list(set(route_partners + sold_to)), member.company_id.id)
            wd = Workday.search([('sales_rep_id', '=', member.id), ('date', '=', today)], limit=1)
            members.append({
                'id': member.id,
                'name': member.name,
                'today': {k: day[k] for k in ('sales_value', 'collection_value', 'orders', 'planned_visits', 'visited', 'compliance')},
                'month': {k: month[k] for k in ('sales_value', 'collection_value', 'orders', 'compliance', 'strike_rate', 'overdue_amount')},
                'debt_total': round(sum(due.values()), 2),
                'debtors': len([v for v in due.values() if v > 0]),
                'workday': wd.payload() if wd else {'started': False, 'ended': False},
                'open_tasks': member.open_task_count,
            })
        totals = {k: round(sum(m['month'][k] for m in members), 2) for k in ('sales_value', 'collection_value', 'orders')}
        totals['debt_total'] = round(sum(m['debt_total'] for m in members), 2)
        totals['today_sales'] = round(sum(m['today']['sales_value'] for m in members), 2)
        totals['today_collections'] = round(sum(m['today']['collection_value'] for m in members), 2)
        totals['working_now'] = len([m for m in members if m['workday'].get('started') and not m['workday'].get('ended')])
        return self._json({'success': True, 'currency_symbol': rep.company_id.currency_id.symbol,
                           'period': {'from': month_start, 'to': today}, 'totals': totals, 'members': members})
