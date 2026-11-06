# -*- coding: utf-8 -*-
"""Sales commissions for representatives (Odoo Community has none).

Plan  -> rules (rate % per product / category / everything, most specific wins)
Sheet -> one per rep and period, computed from the rep's posted customer
         invoices (refunds deducted), on one of two bases:
           * invoiced:  untaxed amount of the invoice lines;
           * collected: the same amount times the share of the invoice that is
                        paid when the sheet is computed (commission follows cash).
         Optional target gate: no commission below a score on the rep's
         approved target for the period, and a fixed bonus at 100 %.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class SalesRepCommissionPlan(models.Model):
    _name = 'sales.rep.commission.plan'
    _description = 'Sales Rep Commission Plan'
    _inherit = ['mail.thread']

    name = fields.Char(required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    base = fields.Selection([
        ('invoiced', 'Invoiced sales'),
        ('collected', 'Collected sales (paid share of invoices)'),
    ], default='collected', required=True, tracking=True)
    rule_ids = fields.One2many('sales.rep.commission.rule', 'plan_id', string='Rules', copy=True)
    target_gate = fields.Float(string='Minimum Target Score %', tracking=True,
                               help='No commission when the rep\'s approved target score for the period is below '
                                    'this value. 0 = no gate.')
    target_bonus = fields.Monetary(string='Bonus at 100 % Target', currency_field='currency_id', tracking=True)
    sales_rep_ids = fields.One2many('sales.representative', 'commission_plan_id', string='Representatives')

    def action_generate_sheets(self):
        """Draft sheets for this month for every rep on the plan (skips existing)."""
        today = fields.Date.context_today(self)
        date_from = today.replace(day=1)
        Sheet = self.env['sales.rep.commission.sheet']
        created = Sheet
        for plan in self:
            for rep in plan.sales_rep_ids:
                if Sheet.search_count([('sales_rep_id', '=', rep.id), ('state', '!=', 'cancelled'),
                                       ('date_from', '<=', today), ('date_to', '>=', date_from)]):
                    continue
                created |= Sheet.create({'sales_rep_id': rep.id, 'plan_id': plan.id,
                                         'date_from': date_from, 'date_to': today})
        created.action_compute()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'sales.rep.commission.sheet',
            'name': _('Commission Sheets'), 'view_mode': 'list,form',
            'domain': [('id', 'in', created.ids)] if created else [('plan_id', 'in', self.ids)],
        }

    def _rule_for(self, product):
        """Most specific rule: product > category (deepest first) > catch-all."""
        self.ensure_one()
        rules = self.rule_ids
        by_product = rules.filtered(lambda r: r.product_id == product)
        if by_product:
            return by_product[0]
        categ = product.categ_id
        while categ:
            by_categ = rules.filtered(lambda r: not r.product_id and r.categ_id == categ)
            if by_categ:
                return by_categ[0]
            categ = categ.parent_id
        return rules.filtered(lambda r: not r.product_id and not r.categ_id)[:1]


class SalesRepCommissionRule(models.Model):
    _name = 'sales.rep.commission.rule'
    _description = 'Sales Rep Commission Rule'
    _order = 'plan_id, sequence, id'

    plan_id = fields.Many2one('sales.rep.commission.plan', required=True, ondelete='cascade')
    sequence = fields.Integer(default=10)
    categ_id = fields.Many2one('product.category', string='Product Category')
    product_id = fields.Many2one('product.product', string='Product')
    rate = fields.Float(string='Rate %', required=True, digits=(16, 2))

    @api.constrains('rate')
    def _check_rate(self):
        for rule in self:
            if rule.rate < 0 or rule.rate > 100:
                raise ValidationError(_('A commission rate must be between 0 and 100 %.'))

    @api.constrains('plan_id', 'categ_id', 'product_id')
    def _check_unique_scope(self):
        for rule in self:
            dup = self.search_count([('id', '!=', rule.id), ('plan_id', '=', rule.plan_id.id),
                                     ('categ_id', '=', rule.categ_id.id), ('product_id', '=', rule.product_id.id)])
            if dup:
                raise ValidationError(_('Plan %s already has a rule for this scope.', rule.plan_id.name))


class SalesRepresentativeCommission(models.Model):
    _inherit = 'sales.representative'

    commission_plan_id = fields.Many2one('sales.rep.commission.plan', string='Commission Plan', tracking=True)


class SalesRepCommissionSheet(models.Model):
    _name = 'sales.rep.commission.sheet'
    _description = 'Sales Rep Commission Sheet'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_from desc, id desc'

    name = fields.Char(compute='_compute_name', store=True)
    sales_rep_id = fields.Many2one('sales.representative', required=True, index=True, tracking=True)
    plan_id = fields.Many2one('sales.rep.commission.plan', required=True, tracking=True)
    company_id = fields.Many2one(related='plan_id.company_id', store=True)
    currency_id = fields.Many2one(related='plan_id.currency_id')
    date_from = fields.Date(required=True, default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(required=True, default=fields.Date.context_today)
    state = fields.Selection([('draft', 'Draft'), ('confirmed', 'Confirmed'), ('paid', 'Paid'), ('cancelled', 'Cancelled')],
                             default='draft', required=True, tracking=True)
    line_ids = fields.One2many('sales.rep.commission.line', 'sheet_id', string='Lines', copy=False)
    base_amount = fields.Monetary(compute='_compute_totals', store=True, currency_field='currency_id')
    commission_amount = fields.Monetary(compute='_compute_totals', store=True, currency_field='currency_id')
    target_score = fields.Float(string='Target Score %', readonly=True, digits=(16, 1))
    bonus_amount = fields.Monetary(readonly=True, currency_field='currency_id')
    gate_blocked = fields.Boolean(readonly=True)
    total_amount = fields.Monetary(string='Total Due', compute='_compute_totals', store=True, currency_field='currency_id')
    note = fields.Text()

    _sql_constraints = [('dates', 'CHECK(date_from <= date_to)', 'The period start must be before its end.')]

    @api.depends('sales_rep_id', 'date_from', 'date_to')
    def _compute_name(self):
        for s in self:
            s.name = f"{s.sales_rep_id.name or ''} {s.date_from or ''} → {s.date_to or ''}"

    @api.depends('line_ids.base_amount', 'line_ids.commission_amount', 'bonus_amount', 'gate_blocked')
    def _compute_totals(self):
        for s in self:
            s.base_amount = sum(s.line_ids.mapped('base_amount'))
            s.commission_amount = 0.0 if s.gate_blocked else sum(s.line_ids.mapped('commission_amount'))
            s.total_amount = s.commission_amount + (0.0 if s.gate_blocked else s.bonus_amount)

    @api.onchange('sales_rep_id')
    def _onchange_rep(self):
        if self.sales_rep_id.commission_plan_id:
            self.plan_id = self.sales_rep_id.commission_plan_id

    @api.constrains('sales_rep_id', 'date_from', 'date_to', 'state')
    def _check_overlap(self):
        for s in self.filtered(lambda x: x.state != 'cancelled'):
            if self.search_count([('id', '!=', s.id), ('sales_rep_id', '=', s.sales_rep_id.id),
                                  ('state', '!=', 'cancelled'),
                                  ('date_from', '<=', s.date_to), ('date_to', '>=', s.date_from)]):
                raise ValidationError(_('%s already has a commission sheet overlapping this period.', s.sales_rep_id.name))

    # ------------------------------------------------------------------
    def _invoice_lines(self):
        self.ensure_one()
        return self.env['account.move.line'].sudo().search([
            ('move_id.sales_rep_id', '=', self.sales_rep_id.id),
            ('move_id.move_type', 'in', ('out_invoice', 'out_refund')),
            ('move_id.state', '=', 'posted'),
            ('move_id.invoice_date', '>=', self.date_from),
            ('move_id.invoice_date', '<=', self.date_to),
            ('move_id.company_id', '=', self.company_id.id),
            ('display_type', '=', 'product'),
            ('product_id', '!=', False),
        ])

    @staticmethod
    def _paid_share(move):
        if not move.amount_total:
            return 0.0
        return max(0.0, min(1.0, 1.0 - (move.amount_residual / move.amount_total)))

    def action_compute(self):
        for sheet in self:
            if sheet.state != 'draft':
                raise UserError(_('Only draft sheets can be recomputed.'))
            plan = sheet.plan_id
            groups = {}
            for line in sheet._invoice_lines():
                rule = plan._rule_for(line.product_id)
                if not rule:
                    continue
                sign = -1.0 if line.move_id.move_type == 'out_refund' else 1.0
                amount = line.price_subtotal * sign
                if plan.base == 'collected':
                    amount *= sheet._paid_share(line.move_id)
                key = rule.id
                g = groups.setdefault(key, {'rule': rule, 'base': 0.0, 'moves': set()})
                g['base'] += amount
                g['moves'].add(line.move_id.id)
            sheet.line_ids.unlink()
            sheet.write({'line_ids': [(0, 0, {
                'rule_id': g['rule'].id,
                'base_amount': g['base'],
                'rate': g['rule'].rate,
                'invoice_count': len(g['moves']),
            }) for g in groups.values()]})
            sheet._apply_target_gate()
        return True

    def _apply_target_gate(self):
        for sheet in self:
            plan = sheet.plan_id
            targets = self.env['sales.rep.target'].sudo().search([
                ('sales_rep_id', '=', sheet.sales_rep_id.id), ('scope', '=', 'rep'),
                ('state', 'in', ('approved', 'closed')),
                ('date_from', '<=', sheet.date_to), ('date_to', '>=', sheet.date_from)])
            score = sum(targets.mapped('score')) / len(targets) if targets else 0.0
            blocked = bool(plan.target_gate) and (not targets or score < plan.target_gate)
            bonus = plan.target_bonus if targets and score >= 100.0 else 0.0
            sheet.write({'target_score': score, 'gate_blocked': blocked, 'bonus_amount': bonus})

    def action_confirm(self):
        for sheet in self:
            if sheet.state != 'draft':
                continue
            if not sheet.line_ids:
                sheet.action_compute()
            sheet.state = 'confirmed'
        return True

    def action_mark_paid(self):
        self.filtered(lambda s: s.state == 'confirmed').write({'state': 'paid'})
        return True

    def action_cancel(self):
        self.filtered(lambda s: s.state != 'paid').write({'state': 'cancelled'})
        return True

    def action_reset_draft(self):
        self.filtered(lambda s: s.state in ('confirmed', 'cancelled')).write({'state': 'draft'})
        return True

    @api.model
    def estimate_for(self, rep, date_from, date_to):
        """Unsaved estimate for the mobile 'My KPIs' screen."""
        if not rep.commission_plan_id:
            return None
        sheet = self.new({'sales_rep_id': rep.id, 'plan_id': rep.commission_plan_id.id,
                          'date_from': date_from, 'date_to': date_to})
        plan = sheet.plan_id
        total = 0.0
        for line in sheet._invoice_lines():
            rule = plan._rule_for(line.product_id)
            if not rule:
                continue
            sign = -1.0 if line.move_id.move_type == 'out_refund' else 1.0
            amount = line.price_subtotal * sign
            if plan.base == 'collected':
                amount *= self._paid_share(line.move_id)
            total += amount * rule.rate / 100.0
        return round(total, 2)


class SalesRepCommissionLine(models.Model):
    _name = 'sales.rep.commission.line'
    _description = 'Sales Rep Commission Line'

    sheet_id = fields.Many2one('sales.rep.commission.sheet', required=True, ondelete='cascade')
    currency_id = fields.Many2one(related='sheet_id.currency_id')
    rule_id = fields.Many2one('sales.rep.commission.rule', ondelete='set null')
    scope = fields.Char(compute='_compute_scope')
    base_amount = fields.Monetary(currency_field='currency_id')
    rate = fields.Float(string='Rate %', digits=(16, 2))
    commission_amount = fields.Monetary(compute='_compute_commission', store=True, currency_field='currency_id')
    invoice_count = fields.Integer(string='Invoices')

    @api.depends('rule_id')
    def _compute_scope(self):
        for line in self:
            r = line.rule_id
            line.scope = r.product_id.display_name or r.categ_id.display_name or _('All products')

    @api.depends('base_amount', 'rate')
    def _compute_commission(self):
        for line in self:
            line.commission_amount = line.base_amount * line.rate / 100.0
