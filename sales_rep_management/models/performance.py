# -*- coding: utf-8 -*-
"""
Phase 3: areas, rep tasks, targets and the KPI engine.

KPIs are computed from planned route customers, confirmed orders, posted
collections, refunds and tasks, for one representative or a whole team, so
the same numbers feed the targets, the daily snapshots, the back-office
dashboards and the mobile "My KPIs" screen.
"""
import logging
import pytz
from datetime import datetime, time, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)

KPI_TYPES = [
    ('sales_value', 'Sales (untaxed)'),
    ('collection_value', 'Collections'),
    ('compliance', 'Journey-plan compliance %'),
    ('strike_rate', 'Strike rate %'),
    ('drop_size', 'Drop size (sales per order)'),
    ('lines_per_call', 'Lines per order'),
    ('new_customers', 'New customers'),
    ('coverage', 'Coverage %'),
    ('category_sales', 'Category sales'),
    ('task_completion', 'Task completion %'),
    ('returns_pct', 'Returns % (lower is better)'),
    ('overdue_ratio', 'Overdue ratio % (lower is better)'),
]
LOWER_IS_BETTER = {'returns_pct', 'overdue_ratio'}
PERCENT_KPIS = {'compliance', 'strike_rate', 'coverage', 'task_completion', 'returns_pct', 'overdue_ratio'}


# ---------------------------------------------------------------------------
# Areas
# ---------------------------------------------------------------------------
class SalesArea(models.Model):
    _name = 'sales.area'
    _description = 'Sales Area'
    _parent_store = True
    _parent_name = 'parent_id'
    _order = 'complete_name'
    _rec_name = 'complete_name'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(index=True)
    complete_name = fields.Char(compute='_compute_complete_name', store=True, recursive=True)
    parent_id = fields.Many2one('sales.area', string='Parent Area', index=True, ondelete='restrict')
    parent_path = fields.Char(index=True)
    child_ids = fields.One2many('sales.area', 'parent_id', string='Sub-areas')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)
    active = fields.Boolean(default=True)
    sales_rep_ids = fields.Many2many('sales.representative', 'sales_area_rep_rel', 'area_id', 'rep_id',
                                     string='Representatives')
    partner_ids = fields.One2many('res.partner', 'sales_area_id', string='Customers')
    partner_count = fields.Integer(compute='_compute_partner_count')
    latitude = fields.Float(digits=(16, 7))
    longitude = fields.Float(digits=(16, 7))
    note = fields.Text()

    _sql_constraints = [('code_company_uniq', 'unique(code, company_id)', 'The area code must be unique per company.')]

    @api.depends('name', 'parent_id.complete_name')
    def _compute_complete_name(self):
        for area in self:
            area.complete_name = f"{area.parent_id.complete_name} / {area.name}" if area.parent_id else area.name

    @api.constrains('parent_id')
    def _check_parent(self):
        if self._has_cycle():
            raise ValidationError(_("An area cannot be its own parent."))

    def _compute_partner_count(self):
        Partner = self.env['res.partner']
        for area in self:
            area.partner_count = Partner.search_count([('sales_area_id', 'child_of', area.id)]) if area.id else 0


class ResPartner(models.Model):
    _inherit = 'res.partner'

    sales_area_id = fields.Many2one('sales.area', string='Sales Area', index=True, tracking=True)


class SalesRepRoute(models.Model):
    _inherit = 'sales.rep.route'

    sales_area_id = fields.Many2one('sales.area', string='Area', index=True)


class SalesRepresentative(models.Model):
    _inherit = 'sales.representative'

    sales_area_ids = fields.Many2many('sales.area', 'sales_area_rep_rel', 'rep_id', 'area_id', string='Areas')
    task_ids = fields.One2many('sales.rep.task', 'sales_rep_id', string='Tasks')
    open_task_count = fields.Integer(compute='_compute_open_task_count')

    def _compute_open_task_count(self):
        data = self.env['sales.rep.task']._read_group(
            [('sales_rep_id', 'in', self.ids), ('state', 'in', ('new', 'in_progress'))], ['sales_rep_id'], ['__count'])
        counts = {rep.id: n for rep, n in data}
        for rep in self:
            rep.open_task_count = counts.get(rep.id, 0)

    def _team_reps(self):
        """The representative plus everyone under him (any depth)."""
        reps = self
        frontier = self
        while frontier:
            frontier = self.search([('supervisor_id', 'in', frontier.ids)]) - reps
            reps |= frontier
        return reps

    # ------------------------------------------------------------------
    # KPI engine
    # ------------------------------------------------------------------
    def _kpi_compute(self, date_from, date_to, category=None):
        """KPIs aggregated over the reps in `self` for [date_from, date_to].

        Ratios are computed from aggregated numerators/denominators, so a team
        figure is the real team ratio, not an average of averages.
        """
        reps = self
        env = self.env
        tz_user = reps[:1].user_id or env.user
        tz = pytz.timezone(tz_user.tz or 'UTC')
        # local day bounds of the representative, in UTC (datetime fields are UTC)
        start = tz.localize(datetime.combine(date_from, time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
        end = tz.localize(datetime.combine(date_to, time.max)).astimezone(pytz.UTC).replace(tzinfo=None)
        rep_ids = reps.ids
        su = env['sale.order'].sudo()

        orders = su.search([('sales_rep_id', 'in', rep_ids), ('state', '=', 'sale'),
                            ('date_order', '>=', start), ('date_order', '<=', end)])
        discount_products = reps.mapped('discount_product_id')
        lines = orders.order_line.filtered(lambda l: not l.display_type and l.product_id not in discount_products
                                           and not getattr(l, 'is_reward_line', False))
        sales_value = sum(orders.mapped('amount_untaxed'))
        category_sales = 0.0
        if category:
            category_sales = sum(lines.filtered(lambda l: l.product_id.categ_id.id in
                                                env['product.category'].search([('id', 'child_of', category.id)]).ids)
                                 .mapped('price_subtotal'))

        payments = env['account.payment'].sudo().search([
            ('sales_rep_id', 'in', rep_ids), ('payment_type', '=', 'inbound'),
            ('state', 'in', ('in_process', 'paid')), ('date', '>=', date_from), ('date', '<=', date_to)])
        collection_value = sum(payments.mapped('amount'))

        rcs = env['sales.route.customer'].sudo().search([
            ('route_id.sales_rep_id', 'in', rep_ids), ('route_id.state', '!=', 'cancelled'),
            ('route_id.date', '>=', date_from), ('route_id.date', '<=', date_to), ('state', '!=', 'cancelled')])
        planned = len(rcs)
        visited = len(rcs.filtered(lambda r: r.state == 'visited'))

        visits = env['sales.rep.visit'].sudo().search([
            ('sales_rep_id', 'in', rep_ids), ('state', '=', 'completed'),
            ('planned_time', '>=', start), ('planned_time', '<=', end)])
        visits_with_order = len(visits.filtered(lambda v: v.sale_order_id or v.sale_order_ids))

        partners_planned = rcs.mapped('partner_id')
        buying_partners = orders.mapped('partner_id')
        new_customers = env['res.partner'].sudo().search_count([
            ('created_sales_rep_id', 'in', rep_ids), ('create_date', '>=', start), ('create_date', '<=', end),
            ('id', 'in', buying_partners.ids)]) if buying_partners else 0

        refunds = env['account.move'].sudo().search([
            ('sales_rep_id', 'in', rep_ids), ('move_type', '=', 'out_refund'), ('state', '=', 'posted'),
            ('invoice_date', '>=', date_from), ('invoice_date', '<=', date_to)])
        returns_value = sum(refunds.mapped('amount_untaxed'))

        tasks = env['sales.rep.task'].sudo().search([
            ('sales_rep_id', 'in', rep_ids), ('state', '!=', 'cancelled'),
            ('date_deadline', '>=', date_from), ('date_deadline', '<=', date_to)])
        tasks_on_time = len(tasks.filtered(lambda t: t.state == 'done' and t.done_date
                                           and t.done_date.date() <= t.date_deadline))

        portfolio = partners_planned | buying_partners
        overdue = total_open = 0.0
        if portfolio:
            amls = env['account.move.line'].sudo().search([
                ('partner_id', 'in', portfolio.commercial_partner_id.ids), ('parent_state', '=', 'posted'),
                ('account_id.account_type', '=', 'asset_receivable'), ('reconciled', '=', False)])
            total_open = sum(a.amount_residual for a in amls if a.amount_residual > 0)
            overdue = sum(a.amount_residual for a in amls if a.amount_residual > 0
                          and a.date_maturity and a.date_maturity < fields.Date.context_today(tz_user))

        def pct(a, b):
            return round(100.0 * a / b, 2) if b else 0.0

        n_orders = len(orders)
        return {
            'sales_value': round(sales_value, 2),
            'collection_value': round(collection_value, 2),
            'orders': n_orders,
            'planned_visits': planned,
            'visited': visited,
            'compliance': pct(visited, planned),
            'visits': len(visits),
            'strike_rate': pct(visits_with_order, len(visits)),
            'drop_size': round(sales_value / n_orders, 2) if n_orders else 0.0,
            'lines_per_call': round(len(lines) / n_orders, 2) if n_orders else 0.0,
            'new_customers': new_customers,
            'coverage': pct(len(buying_partners & partners_planned), len(partners_planned)),
            'category_sales': round(category_sales, 2),
            'tasks_due': len(tasks),
            'task_completion': pct(tasks_on_time, len(tasks)),
            'returns_value': round(returns_value, 2),
            'returns_pct': pct(returns_value, sales_value),
            'overdue_amount': round(overdue, 2),
            'overdue_ratio': pct(overdue, total_open),
        }


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------
class SalesRepTask(models.Model):
    _name = 'sales.rep.task'
    _description = 'Sales Rep Task'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_deadline, priority desc, id'
    _check_company_auto = True

    name = fields.Char(required=True, tracking=True)
    task_type = fields.Selection([
        ('collect', 'Collect overdue'),
        ('deliver', 'Deliver'),
        ('merchandising', 'Merchandising / shelf'),
        ('photo_audit', 'Photo audit'),
        ('survey', 'Survey'),
        ('onboarding', 'New customer onboarding'),
        ('other', 'Other'),
    ], required=True, default='other', tracking=True)
    sales_rep_id = fields.Many2one('sales.representative', string='Representative', required=True,
                                   tracking=True, check_company=True, index=True)
    company_id = fields.Many2one('res.company', related='sales_rep_id.company_id', store=True)
    partner_id = fields.Many2one('res.partner', string='Customer', index=True)
    route_id = fields.Many2one('sales.rep.route', string='Route', check_company=True)
    date_deadline = fields.Date(string='Due Date', required=True, default=fields.Date.context_today, tracking=True)
    priority = fields.Selection([('0', 'Normal'), ('1', 'High'), ('2', 'Urgent')], default='0')
    description = fields.Text()
    proof_type = fields.Selection([
        ('none', 'None'), ('note', 'Note'), ('photo', 'Photo'), ('gps', 'GPS check-in'),
    ], string='Required Proof', default='none', required=True)
    state = fields.Selection([
        ('new', 'New'), ('in_progress', 'In Progress'), ('done', 'Done'),
        ('failed', 'Failed'), ('cancelled', 'Cancelled'),
    ], default='new', required=True, tracking=True, index=True)
    result_note = fields.Text(string='Result')
    proof_image = fields.Image(string='Photo Proof', max_width=1280, max_height=1280)
    latitude = fields.Float(digits=(16, 7))
    longitude = fields.Float(digits=(16, 7))
    done_date = fields.Datetime(readonly=True, copy=False)
    assigned_by = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    source = fields.Selection([('manual', 'Manual'), ('auto_overdue', 'Overdue rule')], default='manual', readonly=True)
    amount_due = fields.Monetary(string='Amount Overdue', currency_field='currency_id', readonly=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    is_overdue = fields.Boolean(compute='_compute_is_overdue')

    def _compute_is_overdue(self):
        today = fields.Date.context_today(self)
        for t in self:
            t.is_overdue = t.state in ('new', 'in_progress') and t.date_deadline < today

    def _check_proof(self, vals):
        for task in self:
            proof = task.proof_type
            if proof == 'note' and not (vals.get('result_note') or task.result_note):
                raise ValidationError(_("Task '%s' needs a result note.", task.name))
            if proof == 'photo' and not (vals.get('proof_image') or task.proof_image):
                raise ValidationError(_("Task '%s' needs a photo.", task.name))
            if proof == 'gps' and not (vals.get('latitude') or task.latitude):
                raise ValidationError(_("Task '%s' needs a GPS check-in.", task.name))

    def action_start(self):
        self.filtered(lambda t: t.state == 'new').write({'state': 'in_progress'})

    def action_done(self, vals=None):
        vals = dict(vals or {})
        self._check_proof(vals)
        vals.update({'state': 'done', 'done_date': fields.Datetime.now()})
        self.write(vals)
        return True

    def action_fail(self, vals=None):
        vals = dict(vals or {})
        if not (vals.get('result_note') or all(self.mapped('result_note'))):
            raise ValidationError(_("Explain why the task failed."))
        vals.update({'state': 'failed', 'done_date': fields.Datetime.now()})
        self.write(vals)
        return True

    def action_cancel(self):
        self.write({'state': 'cancelled'})

    def action_reset(self):
        self.write({'state': 'new', 'done_date': False})

    @api.model_create_multi
    def create(self, vals_list):
        tasks = super().create(vals_list)
        try:
            from .sse_dispatch import schedule_reps
            schedule_reps(self.env, tasks.mapped('sales_rep_id').ids, 'task_assigned')
        except Exception:
            pass
        return tasks

    @api.model
    def _cron_create_overdue_collection_tasks(self):
        """One 'collect overdue' task per customer on today's routes whose
        oldest unpaid invoice is past due by more than N days."""
        days = int(self.env['ir.config_parameter'].sudo().get_param('sales_rep_management.overdue_task_days', 30) or 30)
        today = fields.Date.context_today(self)
        limit = today - timedelta(days=days)
        rcs = self.env['sales.route.customer'].sudo().search([
            ('route_id.date', '=', today), ('route_id.state', 'in', ('approved', 'in_progress'))])
        created = self.browse()
        for rc in rcs:
            partner = rc.partner_id.commercial_partner_id
            amls = self.env['account.move.line'].sudo().search([
                ('partner_id', '=', partner.id), ('parent_state', '=', 'posted'),
                ('account_id.account_type', '=', 'asset_receivable'), ('reconciled', '=', False),
                ('amount_residual', '>', 0), ('date_maturity', '<', limit)])
            if not amls:
                continue
            if self.search_count([('partner_id', '=', partner.id), ('task_type', '=', 'collect'),
                                  ('state', 'in', ('new', 'in_progress'))]):
                continue
            created |= self.sudo().create({
                'name': _("Collect overdue: %s", partner.name),
                'task_type': 'collect',
                'sales_rep_id': rc.route_id.sales_rep_id.id,
                'partner_id': partner.id,
                'route_id': rc.route_id.id,
                'date_deadline': today,
                'priority': '1',
                'proof_type': 'note',
                'source': 'auto_overdue',
                'amount_due': sum(amls.mapped('amount_residual')),
            })
        return created


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------
class SalesRepTarget(models.Model):
    _name = 'sales.rep.target'
    _description = 'Sales Rep Target'
    _inherit = ['mail.thread']
    _order = 'date_from desc, id desc'

    name = fields.Char(required=True, tracking=True)
    scope = fields.Selection([('rep', 'Representative'), ('team', 'Team (supervisor and all below)')],
                             default='rep', required=True)
    sales_rep_id = fields.Many2one('sales.representative', string='Representative / Supervisor', required=True, index=True)
    company_id = fields.Many2one('res.company', related='sales_rep_id.company_id', store=True)
    date_from = fields.Date(required=True, default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(required=True)
    state = fields.Selection([('draft', 'Draft'), ('approved', 'Approved'), ('closed', 'Closed')],
                             default='draft', tracking=True)
    line_ids = fields.One2many('sales.rep.target.line', 'target_id', string='Targets', copy=True)
    score = fields.Float(string='Score %', compute='_compute_score', digits=(16, 1))
    achievement_cap = fields.Float(string='Achievement Cap %', default=120.0,
                                   help="Achievement above this percentage does not add to the score.")

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for t in self:
            if t.date_to < t.date_from:
                raise ValidationError(_("The end date must be after the start date."))

    def _reps(self):
        self.ensure_one()
        return self.sales_rep_id._team_reps() if self.scope == 'team' else self.sales_rep_id

    @api.depends('line_ids.achievement', 'line_ids.weight', 'achievement_cap')
    def _compute_score(self):
        for t in self:
            weight = sum(t.line_ids.mapped('weight'))
            t.score = (sum(min(l.achievement, t.achievement_cap) * l.weight for l in t.line_ids) / weight) if weight else 0.0

    def action_approve(self):
        self.write({'state': 'approved'})

    def action_close(self):
        self.write({'state': 'closed'})

    def action_draft(self):
        self.write({'state': 'draft'})


class SalesRepTargetLine(models.Model):
    _name = 'sales.rep.target.line'
    _description = 'Sales Rep Target Line'

    target_id = fields.Many2one('sales.rep.target', required=True, ondelete='cascade', index=True)
    kpi_type = fields.Selection(KPI_TYPES, required=True)
    product_category_id = fields.Many2one('product.category', string='Product Category',
                                          help="Required for 'Category sales'.")
    target_value = fields.Float(required=True)
    weight = fields.Float(default=1.0)
    actual_value = fields.Float(compute='_compute_actual', digits=(16, 2))
    achievement = fields.Float(string='Achievement %', compute='_compute_actual', digits=(16, 1))

    @api.constrains('kpi_type', 'product_category_id')
    def _check_category(self):
        for line in self:
            if line.kpi_type == 'category_sales' and not line.product_category_id:
                raise ValidationError(_("Choose the product category for a category sales target."))

    @api.depends('kpi_type', 'target_value', 'product_category_id', 'target_id.date_from', 'target_id.date_to',
                 'target_id.sales_rep_id', 'target_id.scope')
    def _compute_actual(self):
        cache = {}
        for line in self:
            target = line.target_id
            if not target.sales_rep_id or not target.date_from or not target.date_to:
                line.actual_value = line.achievement = 0.0
                continue
            key = (target.id, line.product_category_id.id)
            if key not in cache:
                cache[key] = target._reps()._kpi_compute(target.date_from, target.date_to, line.product_category_id or None)
            actual = cache[key].get(line.kpi_type, 0.0)
            line.actual_value = actual
            if not line.target_value:
                line.achievement = 0.0
            elif line.kpi_type in LOWER_IS_BETTER:
                line.achievement = 100.0 * line.target_value / actual if actual else 100.0 * 1.2
            else:
                line.achievement = 100.0 * actual / line.target_value


# ---------------------------------------------------------------------------
# Daily snapshot (history for dashboards; avoids recomputing months of data)
# ---------------------------------------------------------------------------
class SalesRepKpiSnapshot(models.Model):
    _name = 'sales.rep.kpi.snapshot'
    _description = 'Sales Rep Daily KPI Snapshot'
    _order = 'date desc, sales_rep_id'

    date = fields.Date(required=True, index=True)
    sales_rep_id = fields.Many2one('sales.representative', required=True, index=True, ondelete='cascade')
    supervisor_id = fields.Many2one('sales.representative', related='sales_rep_id.supervisor_id', store=True)
    company_id = fields.Many2one('res.company', related='sales_rep_id.company_id', store=True)
    sales_value = fields.Float(aggregator='sum')
    collection_value = fields.Float(aggregator='sum')
    orders = fields.Integer(aggregator='sum')
    planned_visits = fields.Integer(aggregator='sum')
    visited = fields.Integer(aggregator='sum')
    compliance = fields.Float(string='Compliance %', aggregator='avg')
    visits = fields.Integer(aggregator='sum')
    strike_rate = fields.Float(string='Strike Rate %', aggregator='avg')
    drop_size = fields.Float(aggregator='avg')
    lines_per_call = fields.Float(aggregator='avg')
    new_customers = fields.Integer(aggregator='sum')
    tasks_due = fields.Integer(aggregator='sum')
    task_completion = fields.Float(string='Task Completion %', aggregator='avg')
    returns_value = fields.Float(aggregator='sum')
    overdue_amount = fields.Float(aggregator='avg')

    _sql_constraints = [('rep_date_uniq', 'unique(sales_rep_id, date)', 'One snapshot per representative and day.')]

    @api.model
    def _snapshot_fields(self):
        return [f for f in ('sales_value', 'collection_value', 'orders', 'planned_visits', 'visited', 'compliance',
                            'visits', 'strike_rate', 'drop_size', 'lines_per_call', 'new_customers', 'tasks_due',
                            'task_completion', 'returns_value', 'overdue_amount')]

    @api.model
    def _cron_snapshot(self, day=None):
        day = day or (fields.Date.context_today(self) - timedelta(days=1))
        names = self._snapshot_fields()
        for rep in self.env['sales.representative'].sudo().search([]):
            k = rep._kpi_compute(day, day)
            vals = {n: k.get(n, 0) for n in names}
            existing = self.search([('sales_rep_id', '=', rep.id), ('date', '=', day)])
            if existing:
                existing.write(vals)
            else:
                self.create(dict(vals, sales_rep_id=rep.id, date=day))
        return True
