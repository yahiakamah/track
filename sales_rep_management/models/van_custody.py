# -*- coding: utf-8 -*-
"""
Van custody chain (phase 2).

* Load: warehouse -> transit (storekeeper) -> van (rep accepts with counted
  quantities). Shortages go to a load-variance location, damaged goods to a
  quarantine location, so the rep only signs for what he really received.
* Day close: van count vs system, cash count vs the rep's cash box, then the
  supervisor approves: stock differences are posted as inventory adjustments,
  the cash is handed over to treasury in one journal entry (differences to the
  cash journal's loss/profit account) and the day's routes are closed.
"""
import logging

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero

_logger = logging.getLogger(__name__)


class SalesRepresentative(models.Model):
    _inherit = 'sales.representative'

    vehicle_id = fields.Many2one('fleet.vehicle', string='Vehicle', tracking=True,
                                 help="Vehicle used as this representative's mobile warehouse.")
    transit_location_id = fields.Many2one(
        'stock.location', string='Load Transit Location', check_company=True,
        domain="[('usage', 'in', ('internal', 'transit'))]",
        help="When set, stock requests are delivered warehouse -> this location, and the representative "
             "must accept the load (counted quantities) before it enters the van.")
    load_variance_location_id = fields.Many2one(
        'stock.location', string='Load Shortage Location', check_company=True,
        help="Quantities missing at load acceptance are moved here (usually an inventory-loss location).")
    quarantine_location_id = fields.Many2one(
        'stock.location', string='Quarantine Location', check_company=True,
        help="Damaged goods found at load acceptance are moved here.")
    cash_journal_id = fields.Many2one(
        'account.journal', string='Rep Cash Box', check_company=True, domain="[('type', '=', 'cash')]",
        help="Cash journal holding the cash collected by this representative until it is handed over.")
    treasury_journal_id = fields.Many2one(
        'account.journal', string='Treasury Journal', check_company=True, domain="[('type', 'in', ('cash', 'bank'))]",
        help="Journal receiving the cash at day close.")
    require_day_close = fields.Boolean(
        string='Require Day Close', default=False,
        help="The app warns the representative and blocks a new route until the previous working day is closed.")
    day_close_ids = fields.One2many('sales.rep.day.close', 'sales_rep_id', string='Day Closes')
    pending_day_close_date = fields.Date(
        string='Day To Close', compute='_compute_pending_day_close_date',
        help="Latest past working day of this representative without a submitted or approved day close.")

    def _get_load_destination(self):
        """Destination of a stock request: the transit location when the
        acceptance step is enabled, otherwise directly the van."""
        self.ensure_one()
        return self.transit_location_id or self.default_location_id

    def _compute_pending_day_close_date(self):
        today = fields.Date.context_today(self)
        Route = self.env['sales.rep.route'].sudo()
        Close = self.env['sales.rep.day.close'].sudo()
        for rep in self:
            last = Route.search([
                ('sales_rep_id', '=', rep.id),
                ('state', 'in', ('in_progress', 'completed')),
                ('date', '<', today),
            ], order='date desc', limit=1)
            if last and not Close.search_count([
                    ('sales_rep_id', '=', rep.id), ('date', '>=', last.date),
                    ('state', 'in', ('submitted', 'approved'))]):
                rep.pending_day_close_date = last.date
            else:
                rep.pending_day_close_date = False


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    load_source_picking_id = fields.Many2one(
        'stock.picking', string='Load Issued By', readonly=True, copy=False, index=True,
        help="Warehouse -> transit transfer this load acceptance comes from.")
    load_acceptance_ids = fields.One2many('stock.picking', 'load_source_picking_id', string='Acceptance Transfers')
    load_acceptance_state = fields.Selection([
        ('pending', 'Waiting for rep acceptance'),
        ('accepted', 'Accepted'),
        ('disputed', 'Accepted with differences'),
    ], string='Load Acceptance', readonly=True, copy=False, tracking=True)
    load_accepted_by = fields.Many2one('res.users', string='Accepted By', readonly=True, copy=False)
    load_accepted_date = fields.Datetime(string='Accepted On', readonly=True, copy=False)
    load_acceptance_note = fields.Text(string='Acceptance Note', readonly=True, copy=False)
    load_variance_picking_ids = fields.Many2many(
        'stock.picking', 'stock_picking_load_variance_rel', 'acceptance_id', 'variance_id',
        string='Shortage / Damage Transfers', readonly=True, copy=False)

    def _action_done(self):
        res = super()._action_done()
        self._create_rep_load_acceptance()
        return res

    def _create_rep_load_acceptance(self):
        """After the warehouse issues a load into a rep's transit location,
        create the transit -> van transfer the rep has to accept."""
        for picking in self.sudo():
            rep = picking.sales_rep_id
            if (not rep or picking.state != 'done' or picking.load_source_picking_id
                    or not rep.transit_location_id or picking.location_dest_id != rep.transit_location_id
                    or picking.load_acceptance_ids or not rep.default_location_id):
                continue
            moves = picking.move_ids.filtered(lambda m: m.state == 'done' and m.quantity)
            if not moves:
                continue
            acceptance = self.env['stock.picking'].sudo().create({
                'picking_type_id': picking.picking_type_id.id,
                'location_id': rep.transit_location_id.id,
                'location_dest_id': rep.default_location_id.id,
                'origin': picking.name,
                'company_id': picking.company_id.id,
                'sales_rep_id': rep.id,
                'load_source_picking_id': picking.id,
                'load_acceptance_state': 'pending',
                'move_ids': [(0, 0, {
                    'name': _("Load acceptance: %s", move.product_id.display_name),
                    'product_id': move.product_id.id,
                    'product_uom_qty': move.quantity,
                    'product_uom': move.product_uom.id,
                    'location_id': rep.transit_location_id.id,
                    'location_dest_id': rep.default_location_id.id,
                    'company_id': picking.company_id.id,
                }) for move in moves],
            })
            acceptance.action_confirm()
            acceptance.action_assign()
            try:
                from .sse_dispatch import schedule_reps
                schedule_reps(self.env, rep.ids, 'load_ready')
            except Exception:  # pragma: no cover - notification is best effort
                pass

    def _rep_move_to(self, location, quantities, origin_note):
        """Move {product: (qty, uom)} from this picking's source to `location`."""
        self.ensure_one()
        lines = [(p, q, u) for p, (q, u) in quantities.items() if q > 0]
        if not lines or not location:
            return self.env['stock.picking']
        variance = self.env['stock.picking'].sudo().create({
            'picking_type_id': self.picking_type_id.id,
            'location_id': self.location_id.id,
            'location_dest_id': location.id,
            'origin': f"{self.name} - {origin_note}",
            'company_id': self.company_id.id,
            'sales_rep_id': self.sales_rep_id.id,
            'move_ids': [(0, 0, {
                'name': f"{origin_note}: {product.display_name}",
                'product_id': product.id,
                'product_uom_qty': qty,
                'product_uom': uom.id,
                'location_id': self.location_id.id,
                'location_dest_id': location.id,
                'company_id': self.company_id.id,
            }) for product, qty, uom in lines],
        })
        variance.action_confirm()
        variance.action_assign()
        for move in variance.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        variance.with_context(skip_backorder=True, picking_ids_not_to_backorder=variance.ids,
                              mail_notrack=True).button_validate()
        return variance

    def action_rep_accept_load(self, lines, note=False):
        """Rep accepts a pending load.

        :param lines: list of {'product_id', 'received_qty', 'damaged_qty'}
                      quantities in the move's unit of measure.
        """
        self.ensure_one()
        picking = self.sudo()
        if picking.load_acceptance_state != 'pending' or picking.state in ('done', 'cancel'):
            raise UserError(_("This load is not waiting for acceptance."))
        by_product = {}
        for line in lines or []:
            pid = int(line.get('product_id') or 0)
            if pid:
                by_product[pid] = (float(line.get('received_qty') or 0.0), float(line.get('damaged_qty') or 0.0))

        shortages, damaged = {}, {}
        any_received = False
        for move in picking.move_ids.filtered(lambda m: m.state not in ('done', 'cancel')):
            demand = move.product_uom_qty
            received, broken = by_product.get(move.product_id.id, (demand, 0.0))
            rounding = move.product_uom.rounding
            received = max(0.0, min(received, demand))
            broken = max(0.0, min(broken, demand - received))
            missing = demand - received - broken
            if not float_is_zero(broken, precision_rounding=rounding):
                damaged[move.product_id] = (broken, move.product_uom)
            if not float_is_zero(missing, precision_rounding=rounding):
                shortages[move.product_id] = (missing, move.product_uom)
            move.quantity = received
            move.picked = True
            any_received = any_received or not float_is_zero(received, precision_rounding=rounding)

        rep = picking.sales_rep_id
        # Differences leave transit first, so the van receives exactly what was counted.
        variances = self.env['stock.picking']
        if damaged:
            variances |= picking._rep_move_to(rep.quarantine_location_id or rep.load_variance_location_id,
                                              damaged, _("Damaged at load"))
        if shortages:
            variances |= picking._rep_move_to(rep.load_variance_location_id, shortages, _("Short at load"))

        if any_received:
            picking.with_context(skip_backorder=True, picking_ids_not_to_backorder=picking.ids,
                                 mail_notrack=True).button_validate()
        else:
            picking.action_cancel()

        picking.write({
            'load_acceptance_state': 'disputed' if (shortages or damaged) else 'accepted',
            'load_accepted_by': self.env.user.id,
            'load_accepted_date': fields.Datetime.now(),
            'load_acceptance_note': note or False,
            'load_variance_picking_ids': [(6, 0, variances.ids)],
        })
        if shortages or damaged:
            picking.message_post(body=_("Load accepted with differences. Short: %(s)s. Damaged: %(d)s. Note: %(n)s",
                                        s=", ".join(f"{p.display_name} {q:g}" for p, (q, _u) in shortages.items()) or "-",
                                        d=", ".join(f"{p.display_name} {q:g}" for p, (q, _u) in damaged.items()) or "-",
                                        n=note or "-"))
        return True


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    day_close_id = fields.Many2one('sales.rep.day.close', string='Handed Over In', readonly=True, copy=False, index=True)


class SalesRepDayClose(models.Model):
    _name = 'sales.rep.day.close'
    _description = 'Sales Rep Day Close'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc, id desc'
    _check_company_auto = True

    name = fields.Char(string='Reference', required=True, readonly=True, copy=False, default=lambda self: _('New'))
    sales_rep_id = fields.Many2one('sales.representative', string='Sales Representative', required=True,
                                   tracking=True, check_company=True, index=True)
    company_id = fields.Many2one('res.company', related='sales_rep_id.company_id', store=True, readonly=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', readonly=True)
    date = fields.Date(string='Day', required=True, default=fields.Date.context_today, tracking=True)
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('cancelled', 'Cancelled'),
    ], string='Status', default='draft', required=True, tracking=True, copy=False)
    van_location_id = fields.Many2one('stock.location', string='Van', readonly=True)
    line_ids = fields.One2many('sales.rep.day.close.line', 'day_close_id', string='Van Count')
    cash_journal_id = fields.Many2one('account.journal', string='Cash Box', readonly=True)
    payment_ids = fields.Many2many('account.payment', string='Collections Handed Over', readonly=True)
    system_cash = fields.Monetary(string='Cash Expected', readonly=True)
    counted_cash = fields.Monetary(string='Cash Counted', tracking=True)
    cash_difference = fields.Monetary(string='Cash Difference', compute='_compute_cash_difference', store=True)
    stock_difference_count = fields.Integer(string='Items With Difference', compute='_compute_stock_difference')
    handover_move_id = fields.Many2one('account.move', string='Cash Handover Entry', readonly=True, copy=False)
    route_ids = fields.Many2many('sales.rep.route', string='Routes Closed', readonly=True)
    submitted_date = fields.Datetime(readonly=True, copy=False)
    approved_by = fields.Many2one('res.users', readonly=True, copy=False)
    approved_date = fields.Datetime(readonly=True, copy=False)
    note = fields.Text(string='Notes')
    mobile_local_id = fields.Char(index=True, copy=False)

    _sql_constraints = [
        ('rep_date_unique', 'unique(sales_rep_id, date)', 'A representative can only have one day close per day.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('sales.rep.day.close') or _('New')
        return super().create(vals_list)

    @api.depends('counted_cash', 'system_cash')
    def _compute_cash_difference(self):
        for rec in self:
            rec.cash_difference = rec.counted_cash - rec.system_cash

    @api.depends('line_ids.difference_qty')
    def _compute_stock_difference(self):
        for rec in self:
            rec.stock_difference_count = len(rec.line_ids.filtered(
                lambda l: not float_is_zero(l.difference_qty, precision_rounding=l.product_uom_id.rounding or 0.01)))

    # ------------------------------------------------------------------
    # Expected figures
    # ------------------------------------------------------------------
    def _open_payments(self):
        self.ensure_one()
        rep = self.sales_rep_id
        if not rep.cash_journal_id:
            return self.env['account.payment']
        return self.env['account.payment'].sudo().search([
            ('journal_id', '=', rep.cash_journal_id.id),
            ('sales_rep_id', '=', rep.id),
            ('state', 'in', ('in_process', 'paid')),
            ('day_close_id', '=', False),
            ('date', '<=', self.date),
        ])

    def action_compute(self):
        """(Re)load the expected van stock and cash; keeps counted values."""
        for rec in self:
            if rec.state not in ('draft', 'submitted'):
                raise UserError(_("Only draft or submitted day closes can be recomputed."))
            rep = rec.sales_rep_id
            van = rep.default_location_id
            if not van:
                raise UserError(_("Set the representative's default (van) location first."))
            counted = {l.product_id.id: l.counted_qty for l in rec.line_ids if l.counted_set}
            quants = self.env['stock.quant'].sudo().read_group(
                [('location_id', 'child_of', van.id), ('product_id.is_storable', '=', True)],
                ['product_id', 'quantity:sum'], ['product_id'])
            commands = [(5, 0, 0)]
            for q in quants:
                product = self.env['product.product'].browse(q['product_id'][0])
                system_qty = q['quantity']
                if float_is_zero(system_qty, precision_rounding=product.uom_id.rounding) and product.id not in counted:
                    continue
                commands.append((0, 0, {
                    'product_id': product.id,
                    'product_uom_id': product.uom_id.id,
                    'system_qty': system_qty,
                    'counted_qty': counted.get(product.id, system_qty),
                    'counted_set': product.id in counted,
                }))
            payments = rec._open_payments()
            system_cash = sum(p.amount for p in payments if p.payment_type == 'inbound') - \
                sum(p.amount for p in payments if p.payment_type == 'outbound')
            rec.write({
                'van_location_id': van.id,
                'line_ids': commands,
                'cash_journal_id': rep.cash_journal_id.id,
                'payment_ids': [(6, 0, payments.ids)],
                'system_cash': system_cash,
            })
        return True

    def set_counts(self, counts, counted_cash=None, note=None):
        """Store the rep's counts: counts = [{'product_id', 'counted_qty'}]."""
        self.ensure_one()
        if self.state != 'draft':
            raise UserError(_("The day close is already submitted."))
        by_product = {int(c['product_id']): float(c.get('counted_qty') or 0.0) for c in counts or [] if c.get('product_id')}
        for line in self.line_ids:
            if line.product_id.id in by_product:
                line.write({'counted_qty': by_product.pop(line.product_id.id), 'counted_set': True})
        for product_id, qty in by_product.items():  # found in the van but not in the system
            product = self.env['product.product'].browse(product_id).exists()
            if product:
                self.env['sales.rep.day.close.line'].create({
                    'day_close_id': self.id, 'product_id': product.id, 'product_uom_id': product.uom_id.id,
                    'system_qty': 0.0, 'counted_qty': qty, 'counted_set': True})
        vals = {}
        if counted_cash is not None:
            vals['counted_cash'] = float(counted_cash)
        if note:
            vals['note'] = note
        if vals:
            self.write(vals)
        return True

    def action_submit(self):
        for rec in self:
            if rec.state != 'draft':
                continue
            if not rec.line_ids and not rec.payment_ids and not rec.system_cash:
                rec.action_compute()
            rec.write({'state': 'submitted', 'submitted_date': fields.Datetime.now()})
            rec.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=(rec.sales_rep_id.supervisor_id.user_id or self.env.user).id,
                summary=_("Approve day close of %s", rec.sales_rep_id.name))
        return True

    def action_reset_draft(self):
        self.filtered(lambda r: r.state in ('submitted', 'cancelled')).write({'state': 'draft'})
        return True

    def action_cancel(self):
        if any(r.state == 'approved' for r in self):
            raise UserError(_("An approved day close cannot be cancelled."))
        self.write({'state': 'cancelled'})
        return True

    # ------------------------------------------------------------------
    # Approval: post stock and cash
    # ------------------------------------------------------------------
    def action_approve(self):
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_("Only submitted day closes can be approved."))
            rec._apply_stock_differences()
            rec._post_cash_handover()
            routes = self.env['sales.rep.route'].sudo().search([
                ('sales_rep_id', '=', rec.sales_rep_id.id),
                ('state', 'in', ('approved', 'in_progress')),
                ('date', '<=', rec.date),
            ])
            routes.write({'state': 'completed', 'end_time': fields.Datetime.now()})
            rec.activity_ids.action_done()
            rec.write({
                'state': 'approved',
                'approved_by': self.env.user.id,
                'approved_date': fields.Datetime.now(),
                'route_ids': [(6, 0, routes.ids)],
            })
        return True

    def _apply_stock_differences(self):
        self.ensure_one()
        Quant = self.env['stock.quant'].sudo().with_context(inventory_mode=True)
        van = self.van_location_id or self.sales_rep_id.default_location_id
        to_apply = Quant
        for line in self.line_ids:
            if float_is_zero(line.difference_qty, precision_rounding=line.product_uom_id.rounding or 0.01):
                continue
            if line.product_id.tracking != 'none':
                raise UserError(_("%s is tracked by lot/serial: adjust it from Inventory.", line.product_id.display_name))
            quant = Quant.search([('product_id', '=', line.product_id.id), ('location_id', '=', van.id),
                                  ('lot_id', '=', False), ('package_id', '=', False), ('owner_id', '=', False)], limit=1)
            others = sum(Quant.search([('product_id', '=', line.product_id.id), ('location_id', 'child_of', van.id),
                                       ('id', '!=', quant.id)]).mapped('quantity'))
            target = line.counted_qty - others  # counted covers the whole van incl. sub-locations
            if quant:
                quant.inventory_quantity = target
            else:
                quant = Quant.create({'product_id': line.product_id.id, 'location_id': van.id,
                                      'inventory_quantity': target})
            to_apply |= quant
        if to_apply:
            to_apply.with_context(inventory_name=self.name).action_apply_inventory()

    def _post_cash_handover(self):
        self.ensure_one()
        rep = self.sales_rep_id
        if not self.payment_ids and float_is_zero(self.counted_cash, precision_rounding=self.currency_id.rounding):
            return
        treasury = rep.treasury_journal_id
        cash_box = self.cash_journal_id or rep.cash_journal_id
        if not treasury or not cash_box:
            raise UserError(_("Set the Rep Cash Box and Treasury Journal on %s.", rep.name))
        if not treasury.default_account_id:
            raise UserError(_("The treasury journal has no default account."))

        # Credit the account each collection currently sits on.
        per_account = {}
        for pay in self.payment_ids:
            account = pay.outstanding_account_id or cash_box.default_account_id
            sign = 1 if pay.payment_type == 'inbound' else -1
            per_account[account] = per_account.get(account, 0.0) + sign * pay.amount

        lines = []
        for account, amount in per_account.items():
            if float_is_zero(amount, precision_rounding=self.currency_id.rounding):
                continue
            lines.append((0, 0, {
                'name': _("%s handover", self.name), 'account_id': account.id,
                'debit': -amount if amount < 0 else 0.0, 'credit': amount if amount > 0 else 0.0,
            }))
        if self.counted_cash:
            lines.append((0, 0, {'name': _("%s cash received", self.name),
                                 'account_id': treasury.default_account_id.id,
                                 'debit': self.counted_cash if self.counted_cash > 0 else 0.0,
                                 'credit': -self.counted_cash if self.counted_cash < 0 else 0.0}))
        diff = self.cash_difference
        if not float_is_zero(diff, precision_rounding=self.currency_id.rounding):
            if diff < 0:
                account = cash_box.loss_account_id or self.company_id.default_cash_difference_expense_account_id
                label = _("%s cash shortage (%s)", self.name, rep.name)
                debit, credit = -diff, 0.0
            else:
                account = cash_box.profit_account_id or self.company_id.default_cash_difference_income_account_id
                label = _("%s cash overage (%s)", self.name, rep.name)
                debit, credit = 0.0, diff
            if not account:
                raise UserError(_("Set the loss/profit accounts on journal %s.", cash_box.name))
            lines.append((0, 0, {'name': label, 'account_id': account.id, 'debit': debit, 'credit': credit}))
        if not lines:
            return
        move = self.env['account.move'].sudo().create({
            'move_type': 'entry',
            'journal_id': treasury.id,
            'date': self.date,
            'ref': _("Day close %s - %s", self.name, rep.name),
            'line_ids': lines,
        })
        move.action_post()
        # Match each collection with the handover so the payments become paid.
        for account in per_account:
            if not account.reconcile:
                continue
            to_rec = (self.payment_ids.move_id.line_ids | move.line_ids).filtered(
                lambda l: l.account_id == account and not l.reconciled)
            if len(to_rec) > 1:
                to_rec.reconcile()
        self.payment_ids.sudo().write({'day_close_id': self.id})
        self.handover_move_id = move.id


class SalesRepDayCloseLine(models.Model):
    _name = 'sales.rep.day.close.line'
    _description = 'Sales Rep Day Close Line'
    _order = 'product_id'

    day_close_id = fields.Many2one('sales.rep.day.close', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='day_close_id.company_id', store=True)
    product_id = fields.Many2one('product.product', string='Product', required=True)
    product_uom_id = fields.Many2one('uom.uom', string='Unit')
    system_qty = fields.Float(string='System Qty', digits='Product Unit of Measure', readonly=True)
    counted_qty = fields.Float(string='Counted Qty', digits='Product Unit of Measure')
    counted_set = fields.Boolean(string='Counted', help="The representative entered a count for this line.")
    difference_qty = fields.Float(string='Difference', digits='Product Unit of Measure',
                                  compute='_compute_difference', store=True)

    @api.depends('system_qty', 'counted_qty')
    def _compute_difference(self):
        for line in self:
            line.difference_qty = line.counted_qty - line.system_qty
