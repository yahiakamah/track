from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError
from odoo.osv import expression
import logging

_logger = logging.getLogger(__name__)


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # ══════════════════════════════════════════════════════════════════════
    #  COMPATIBILITY FIELDS (gift_bonus_engine)
    # ══════════════════════════════════════════════════════════════════════
    gift_approval_state = fields.Selection([
        ('draft', 'Not Required'),
        ('pending', 'Waiting for Approval'),
        ('approved', 'Approved'),
    ], string='Gift Approval State', default='draft', readonly=True)
    gift_approval_needed = fields.Boolean(
        string='Gift Approval Needed', compute='_compute_gift_approval_needed',
    )
    gift_location_id = fields.Many2one(
        'stock.location', string='Van/Car Location', readonly=True,
    )
    is_competitor_war = fields.Boolean(
        string='Competitor Price Pressure', readonly=True,
    )
    delivery_gps_lat = fields.Float(string='Delivery Latitude', digits=(10, 7))
    delivery_gps_lng = fields.Float(string='Delivery Longitude', digits=(10, 7))
    partner_return_rate = fields.Float(
        string='Return Rate %', compute='_compute_partner_stats_fallback',
    )
    has_return_risk = fields.Boolean(
        string='High Return Risk', compute='_compute_partner_stats_fallback',
    )

    def _compute_gift_approval_needed(self):
        for order in self:
            order.gift_approval_needed = False

    def _compute_partner_stats_fallback(self):
        for order in self:
            order.partner_return_rate = 0.0
            order.has_return_risk = False

    # ══════════════════════════════════════════════════════════════════════
    #  OUR FIELDS
    # ══════════════════════════════════════════════════════════════════════
    is_cash = fields.Boolean(
        string='Is Cash Order',
        compute='_compute_is_cash',
        store=True,
        readonly=False,
    )

    @api.depends('partner_id')
    def _compute_is_cash(self):
        for order in self:
            # Check if is_cash field exists on partner (added by sales_rep_management)
            if order.partner_id and 'is_cash' in order.partner_id._fields:
                order.is_cash = order.partner_id.is_cash
            else:
                # Default to current value or False if not found
                order.is_cash = order.is_cash or False


    applied_program_ids = fields.Many2many(
        'loyalty.program',
        string='Applied Promotions',
        compute='_compute_applied_programs',
        store=False,
    )
    applied_tier_ids = fields.Many2many(
        'loyalty.program.tier',
        string='Applied Tiers',
        compute='_compute_applied_programs',
        store=False,
    )
    applied_program_count = fields.Integer(
        string='Promotion Count',
        compute='_compute_applied_programs',
    )

    def _compute_applied_programs(self):
        """Compute applied promotions and tiers from reward lines."""
        for order in self:
            try:
                programs = self.env['loyalty.program']
                tiers = self.env['loyalty.program.tier']
                for line in order.order_line:
                    if not line.is_reward_line and not getattr(line, 'is_tiered_reward', False):
                        continue
                        
                    prog = False
                    tier = False
                    if 'coupon_id' in line._fields and line.coupon_id:
                        prog = line.coupon_id.program_id
                    elif 'reward_id' in line._fields and line.reward_id:
                        prog = line.reward_id.program_id
                    elif 'program_id' in line._fields and line.program_id:
                        prog = line.program_id
                        
                    if 'tier_id' in line._fields and line.tier_id:
                        tier = line.tier_id
                    
                    if prog:
                        programs |= prog
                    if tier:
                        tiers |= tier
                        
                order.applied_program_ids = programs
                order.applied_tier_ids = tiers
                order.applied_program_count = len(programs)
            except Exception as e:
                _logger.warning("Error computing applied promotions for order %s: %s", order.name, e)
                order.applied_program_ids = False
                order.applied_tier_ids = False
                order.applied_program_count = 0

    def action_view_promotions(self):
        """Smart button action to view applied promotions."""
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': 'Applied Promotions',
            'view_mode': 'list,form',
            'res_model': 'loyalty.program',
            'domain': [('id', 'in', self.applied_program_ids.ids)],
            'context': {'create': False, 'edit': False},
        }

    # ══════════════════════════════════════════════════════════════════════
    #  CUSTOMER TARGETING — shared constraint checks
    # ══════════════════════════════════════════════════════════════════════

    def _check_program_constraints(self, program):
        """
        Return True if the program's customer-targeting constraints are met
        for this order.  Return False (with a log message) if not.
        """
        self.ensure_one()

        # Cash / Non-cash mutual exclusion (two-way gate):
        # Cash promotions (is_cash=True) can ONLY apply to cash orders.
        # Non-cash promotions (is_cash=False) can ONLY apply to non-cash orders.
        if program.is_cash and not self.is_cash:
            _logger.info("Program %s: Skipped — cash-only promotion on non-cash order (Order is_cash=%s).",
                         program.name, self.is_cash)
            return False
        if not program.is_cash and self.is_cash:
            _logger.info("Program %s: Skipped — non-cash promotion on cash order (Order is_cash=%s).",
                         program.name, self.is_cash)
            return False

        # Specific customers
        if program.limit_partner_ids and self.partner_id not in program.limit_partner_ids:
            _logger.info("Program %s: Skipped — partner %s not in allowed list.",
                         program.name, self.partner_id.name)
            return False

        # Customer tags
        if program.limit_partner_category_ids:
            if not (self.partner_id.category_id & program.limit_partner_category_ids):
                _logger.info("Program %s: Skipped — partner category mismatch.", program.name)
                return False

        # Country
        if program.limit_country_ids and self.partner_id.country_id not in program.limit_country_ids:
            _logger.info("Program %s: Skipped — country not allowed.", program.name)
            return False

        # State
        if program.limit_state_ids and self.partner_id.state_id not in program.limit_state_ids:
            _logger.info("Program %s: Skipped — state not allowed.", program.name)
            return False

        return True

    # ══════════════════════════════════════════════════════════════════════
    #  PRIORITY ENGINE — two independent tracks
    # ══════════════════════════════════════════════════════════════════════

    def _get_filtered_programs(self):
        """
        Evaluate all active auto-apply programs against this order's
        constraints. Returns a tuple:

            (standard_programs, tier_programs)

        Standard and Tier programs now share a single priority track.
        If a program (of either type) is non-shareable, it blocks all
        subsequent programs of lower priority.
        """
        self.ensure_one()

        # Promotions only apply to draft/sent orders
        if self.state not in ['draft', 'sent']:
            _logger.info("Priority Engine: Order %s is confirmed (%s) — skipping promotion evaluation.", self.name, self.state)
            return self.env['loyalty.program'], self.env['loyalty.program']

        all_programs = self.env['loyalty.program'].search([
            ('active', '=', True),
            '|',
            ('is_auto', '=', True),
            ('program_type', '=', 'tier'),
        ], order='priority asc')
        _logger.info("Priority Engine: Evaluating %d programs for order %s", len(all_programs), self.name)
        for p in all_programs:
            _logger.info("  -> Program: %s (ID: %s, Type: %s, Priority: %s, Tiers: %d)", 
                         p.name, p.id, p.program_type, p.priority, len(p.program_tier_ids))

        standard = self.env['loyalty.program']
        tiers = self.env['loyalty.program']
        stop_sharing = False

        for program in all_programs:
            # ── Customer targeting (applies to ALL program types) ──────
            if not self._check_program_constraints(program):
                continue

            if stop_sharing:
                _logger.info(
                    "Program %s: Blocked — previous program is non-shareable.",
                    program.name,
                )
                continue

            # ── TIER programs ───────
            if program.program_type == 'tier':
                if self._check_tier_eligibility(program):
                    tiers |= program
                    _logger.info("Program %s (tier): ELIGIBLE (shareable=%s)",
                                 program.name, program.can_be_shared)

                    if not program.can_be_shared:
                        stop_sharing = True
                else:
                    _logger.info("Program %s (tier): No matching tier.", program.name)
                continue

            # ── STANDARD programs ────────────────
            if not self._check_standard_eligibility(program):
                _logger.info("Program %s: Not eligible (rules not met).", program.name)
                continue

            standard |= program
            _logger.info("Program %s: APPLICABLE (shareable=%s)",
                         program.name, program.can_be_shared)

            if not program.can_be_shared and program.program_type not in ['gift_card', 'ewallet']:
                stop_sharing = True

        return standard, tiers

    def _check_standard_eligibility(self, program):
        """
        Check if a standard (non-tier) program is actually eligible for this order
        by verifying its rules and potential points.
        """
        self.ensure_one()
        try:
            points_results = self._program_check_compute_points(program)
            res = points_results.get(program, {})
            _logger.info("Order %s: Checking standard eligibility for %s. Results: %s", self.name, program.name, res)
            if isinstance(res, dict):
                # If there's an error (e.g. minimum amount not met), it's not eligible
                if res.get('error'):
                    return False
                # If it's a promotion/coupon, it should grant at least some points
                points = res.get('points', 0)
                if isinstance(points, (list, tuple)):
                    return any(p > 0 for p in points if isinstance(p, (int, float)))
                return isinstance(points, (int, float)) and points > 0
            
            if isinstance(res, (list, tuple)):
                return any(p > 0 for p in res if isinstance(p, (int, float)))
            return isinstance(res, (int, float)) and res > 0
        except Exception as e:
            _logger.debug("Error checking standard eligibility for %s: %s", program.name, e)
            return False

    def _check_tier_eligibility(self, program):
        """
        Return True if at least one tier in *program* is satisfied by the
        current order.  Does NOT apply anything — purely a check.
        """
        self.ensure_one()
        relevant_lines = self.order_line.filtered(
            lambda l: not l.is_tiered_reward and not l.is_reward_line
        )
        _logger.info("Order %s: relevant_lines: %d, total_lines: %d", self.name, len(relevant_lines), len(self.order_line))
        if not relevant_lines:
            return False

        order_total_amount = sum(relevant_lines.mapped('price_subtotal'))
        order_total_qty = sum(relevant_lines.mapped('product_uom_qty'))
        _logger.info("Order %s: Total Amount: %f, Total Qty: %f", self.name, order_total_amount, order_total_qty)

        for tier in program.program_tier_ids:
            if program.tiers_type == 'order_total':
                # order_total mode: From/To always refers to the untaxed amount
                val = order_total_amount
                
                _logger.info("  Tier %s: Val %f (amount), Range [%f, %f] (Order Total mode)", 
                             tier.name, val, tier.minimum_amount, tier.maximum_amount)
                if tier.minimum_amount <= val <= tier.maximum_amount:
                    return True

            elif program.tiers_type == 'order_line':
                for line in relevant_lines:
                    if tier.rule_product_id and line.product_id != tier.rule_product_id:
                        _logger.debug("  Tier '%s': Line product %s (ID %s) != Rule product %s (ID %s)", 
                                     tier.name, line.product_id.display_name, line.product_id.id, 
                                     tier.rule_product_id.display_name, tier.rule_product_id.id)
                        continue
                    
                    val = line.price_subtotal if tier.trigger_type == 'amount' else line.product_uom_qty
                    if tier.trigger_type == 'quantity' and tier.rule_uom_id:
                        val = line.product_uom._compute_quantity(line.product_uom_qty, tier.rule_uom_id)

                    _logger.debug("  Tier '%s': Checking line %s. Val: %f, Range: [%f, %f]", 
                                 tier.name, line.name, val, tier.minimum_amount, tier.maximum_amount)

                    if tier.minimum_amount <= val <= tier.maximum_amount:
                        return True

        return False

    # ══════════════════════════════════════════════════════════════════════
    #  STANDARD PROGRAM INTEGRATION (Odoo loyalty engine)
    # ══════════════════════════════════════════════════════════════════════

    def _get_program_domain(self):
        """
        Override to exclude from the standard Odoo loyalty engine:
          1. Tier programs (handled by _apply_tiered_promotions)
          2. Cash-only programs when the order is non-cash
          3. ALL programs if the order is still a draft/quotation, 
             unless specifically applying at confirmation.
        """
        # Block promotions during drafting phase (quotation) unless confirmation is in progress
        if self.state in ['draft', 'sent'] and not self.env.context.get('applying_at_confirmation'):
            return [('id', '=', 0)]

        # Also block for confirmed orders (Sale/Done) to keep them locked
        if self.state not in ['draft', 'sent']:
            return [('id', '=', 0)]

        domain = super()._get_program_domain()
        extra_filters = [('program_type', '!=', 'tier')]

        # Two-way gate: Cash orders only get cash promotions,
        # non-cash orders only get non-cash promotions.
        if self.is_cash:
            extra_filters.append(('is_cash', '=', True))
        else:
            extra_filters.append(('is_cash', '=', False))

        return expression.AND([domain, extra_filters])

    def _program_check_compute_points(self, programs):
        """
        Override to inject our is_cash constraint into Odoo's standard
        program validation.

        The standard engine calls this to validate programs and compute
        points. By marking cash-only programs as errored for non-cash
        orders, we prevent them from being applied at STEP 2 (existing
        programs), STEP 3 (reward lines), and STEP 4 (auto-apply).

        This is a belt-and-suspenders approach alongside the domain filter
        in _get_program_domain.
        """
        self.ensure_one()
        result = super()._program_check_compute_points(programs)

        # Post-filter: enforce two-way gate on is_cash
        for program in programs:
            if program in result:
                if program.is_cash and not self.is_cash:
                    result[program] = {
                        'error': _('This is a cash-only promotion and the order is not a cash order.')
                    }
                    _logger.info(
                        "Program %s: Blocked by _program_check_compute_points — "
                        "cash-only promotion on non-cash order.",
                        program.name,
                    )
                elif not program.is_cash and self.is_cash:
                    result[program] = {
                        'error': _('This is a non-cash promotion and the order is a cash order.')
                    }
                    _logger.info(
                        "Program %s: Blocked by _program_check_compute_points — "
                        "non-cash promotion on cash order.",
                        program.name,
                    )

        return result

    def _get_applicable_program_points(self, domain=None):
        """
        Override Odoo's standard method to inject our priority filter.
        Only standard (non-tier) programs reach the loyalty engine.
        """
        self.ensure_one()
        standard_programs, _tiers = self._get_filtered_programs()

        priority_domain = [
            ('id', 'in', standard_programs.ids),
            ('program_type', '!=', 'tier'),
        ]
        if domain:
            domain = expression.AND([domain, priority_domain])
        else:
            domain = priority_domain

        return super()._get_applicable_program_points(domain=domain)

    def _update_programs_and_rewards(self):
        """
        Hook into Odoo's loyalty update cycle to apply our tiered promotions
        whenever standard rewards are recalculated (e.g. Save, Reward button).

        The standard engine is prevented from processing tier programs by
        our _get_program_domain() override. After the standard engine runs,
        we apply our custom tier logic.

        A re-entrancy guard prevents duplicate execution when called from
        action_confirm → _apply_standard_promotions.
        """
        res = super()._update_programs_and_rewards()
        # Custom tiered promotions are now handled exclusively during action_confirm
        # to fulfill the requirement of 'only Upon Confirmation'.
        return res

    def _apply_standard_promotions(self):
        """
        Apply standard (non-tier) promotions respecting priority and sharing.
        Uses Odoo's native _get_claimable_rewards + _apply_program_reward.
        """
        self.ensure_one()
        try:
            self._update_programs_and_rewards()

            claimable = self._get_claimable_rewards()
            if not claimable:
                _logger.info("Order %s: No claimable standard rewards.", self.name)
                return

            standard_programs, _tiers = self._get_filtered_programs()
            allowed_ids = set(standard_programs.ids)

            # Build candidates: (priority, program, coupon, reward)
            candidates = []
            for coupon, rewards in claimable.items():
                prog = coupon.program_id
                if prog.id not in allowed_ids:
                    continue
                pri = prog.priority or 999
                for reward in rewards:
                    candidates.append((pri, prog, coupon, reward))

            candidates.sort(key=lambda x: x[0])
            if not candidates:
                return

            applied = set()
            for pri, prog, coupon, reward in candidates:
                result = self._apply_program_reward(reward, coupon)
                if not result.get('error'):
                    applied.add(prog.name)
                    _logger.info("Order %s: Applied standard reward from %s",
                                 self.name, prog.name)
                    if not prog.can_be_shared:
                        _logger.info("Program %s is non-shareable — stopping.", prog.name)
                        break

            if applied:
                self.message_post(
                    body=_("Promotions applied: %s") % ', '.join(applied),
                    subtype_xmlid='mail.mt_note',
                )
        except Exception as e:
            _logger.warning("Error applying standard promotions on order %s: %s",
                            self.name, e)

    # ══════════════════════════════════════════════════════════════════════
    #  TIER ENGINE — our custom reward application
    # ══════════════════════════════════════════════════════════════════════

    def _apply_tiered_promotions(self):
        """
        Evaluate and apply tier-type promotions for this order.

        Logic:
        1.  Re-entrancy guard: skip if already running.
        2.  Remove any old tier reward lines (always try unlink first).
        3.  Get eligible tier programs from the priority engine.
        4.  For each program, find the BEST matching tier and apply its reward.
        """
        # Re-entrancy guard
        if self.env.context.get('in_tiered_promo_engine'):
            return

        self = self.with_context(in_tiered_promo_engine=True)

        for order in self:
            try:
                # 1. Clear any existing tier reward lines
                old_lines = order.order_line.filtered(lambda l: l.is_tiered_reward)
                if old_lines:
                    _logger.info("Order %s: Removing %d old tier reward lines.", order.name, len(old_lines))
                    try:
                        old_lines.sudo().unlink()
                    except Exception as unlink_err:
                        _logger.warning(
                            "Order %s: Could not unlink tier lines (%s), zeroing instead.",
                            order.name, unlink_err,
                        )
                        old_lines.sudo().write({'product_uom_qty': 0, 'price_unit': 0})

                # Also clean up any ghost lines ($0 tier rewards from previous failed runs)
                ghost_lines = order.order_line.filtered(
                    lambda l: l.is_tiered_reward and l.product_uom_qty == 0 and l.price_unit == 0
                )
                if ghost_lines:
                    try:
                        ghost_lines.sudo().unlink()
                    except Exception:
                        pass  # Best effort cleanup

                # 2. Get eligible tier programs
                _standard, tier_programs = order._get_filtered_programs()
                if not tier_programs:
                    _logger.info("Order %s: No eligible tier programs.", order.name)
                    continue

                # 3. Base totals for matching (products only)
                product_lines = order.order_line.filtered(
                    lambda l: not l.is_tiered_reward and not l.is_reward_line
                )
                if not product_lines:
                    continue

                order_total_amount = sum(product_lines.mapped('price_subtotal'))
                order_total_qty = sum(product_lines.mapped('product_uom_qty'))

                # 4. Evaluate each program
                for program in tier_programs:
                    # For 'order_total' rewards, the discount should be calculated based on the 
                    # current net untaxed amount (including standard Odoo rewards and 
                    # any tiered rewards added in previous iterations of this loop).
                    current_untaxed_amount = sum(order.order_line.mapped('price_subtotal'))

                    _logger.info("Order %s: Evaluating tier program '%s' (type=%s). Net Amount: %f",
                                 order.name, program.name, program.tiers_type, current_untaxed_amount)

                    if program.tiers_type == 'order_total':
                        order._apply_tier_order_total(
                            program, product_lines,
                            order_total_amount, order_total_qty,
                            reward_base_amount=current_untaxed_amount
                        )
                    elif program.tiers_type == 'order_line':
                        order._apply_tier_order_line(
                            program, product_lines,
                        )

                applied_tier_names = [p.name for p in tier_programs]
                if applied_tier_names:
                    order.message_post(
                        body=_("Tier promotions evaluated: %s") % ', '.join(applied_tier_names),
                        subtype_xmlid='mail.mt_note',
                    )

            except Exception as e:
                _logger.error("Error applying tiered promotions on order %s: %s",
                              order.name, e, exc_info=True)

    def _apply_tier_order_total(self, program, product_lines, total_amount, total_qty, reward_base_amount=None):
        """
        For 'order_total' tiers: evaluate thresholds against the whole order
        (or a specific rule product's subtotal).
        """
        self.ensure_one()
        if reward_base_amount is None:
            reward_base_amount = total_amount

        for tier in program.program_tier_ids:
            # order_total mode: From/To always refers to the untaxed amount
            val = total_amount

            if tier.minimum_amount <= val <= tier.maximum_amount:
                _logger.info(
                    "  Tier '%s' matched: %s %f in [%f, %f]",
                    tier.name, tier.trigger_type, val,
                    tier.minimum_amount, tier.maximum_amount,
                )
                # For 'order_total' programs, the discount always applies to the provided base amount
                # (which is the current net untaxed amount).
                self._create_tier_reward_line(tier, base_amount=reward_base_amount)
                return  # Only the first (best) match applies
        _logger.info("  No tier matched for order total (amount=%f, qty=%f)",
                     total_amount, total_qty)

    def _apply_tier_order_line(self, program, relevant_lines):
        """
        For 'order_line' tiers: evaluate each order line independently.
        Each line gets at most one tier reward.
        """
        self.ensure_one()
        for line in relevant_lines:
            _logger.info("  Checking line '%s' (Product: %s, Qty: %f)", line.name, line.product_id.display_name, line.product_uom_qty)
            for tier in program.program_tier_ids:
                if tier.rule_product_id and line.product_id != tier.rule_product_id:
                    _logger.debug("    Tier '%s': Product mismatch (%s vs %s)",
                                  tier.name, line.product_id.display_name, tier.rule_product_id.display_name)
                    continue

                val = (line.price_subtotal
                       if tier.trigger_type == 'amount'
                       else line.product_uom_qty)
                
                if tier.trigger_type == 'quantity' and tier.rule_uom_id:
                    val = line.product_uom._compute_quantity(line.product_uom_qty, tier.rule_uom_id)

                _logic_info = "Trigger: %s, Val: %f, Range: [%f, %f]" % (
                    tier.trigger_type, val, tier.minimum_amount, tier.maximum_amount
                )
                _logger.info("    Evaluating Tier '%s': %s", tier.name, _logic_info)
                
                if tier.minimum_amount <= val <= tier.maximum_amount:
                    _logger.info("    -> MATCH! Creating reward line for '%s'", tier.name)
                    self._create_tier_reward_line(
                        tier,
                        base_amount=line.price_subtotal,
                        related_line=line,
                    )
                    break 
                else:
                    _logger.info("    -> No match for '%s'", tier.name)

    def _get_reward_line_values(self, reward, coupon, **kwargs):
        """
        Override to use the program's discount_product_id if set for discount rewards.
        """
        res = super()._get_reward_line_values(reward, coupon, **kwargs)
        if reward.reward_type == 'discount' and reward.program_id.discount_product_id:
            for vals in res:
                vals['product_id'] = reward.program_id.discount_product_id.id
        return res

    def _create_tier_reward_line(self, tier, base_amount=0.0, related_line=None):
        """
        Create a sale.order.line representing the tier reward.

        For 'discount': negative-price line using Odoo's reward product.
        For 'bonus': free product line at price 0.
        """
        self.ensure_one()
        vals = {
            'order_id': self.id,
            'is_tiered_reward': True,
            'program_id': tier.program_id.id,
            'tier_id': tier.id,
            'sequence': 999,
        }

        if tier.reward_type == 'discount':
            discount = (base_amount * tier.reward_amount) / 100.0

            # Use the discounted product as the line's product (required by Odoo).
            # Setting is_reward_line=True hides the product picker in the UI,
            # so only the description name is shown — matching standard loyalty display.
            reward_product = tier.program_id.discount_product_id or tier.reward_product_id
            if not reward_product and related_line:
                reward_product = related_line.product_id

            if not reward_product:
                _logger.error(
                    "Order %s: Cannot create discount for tier '%s' — no product available.",
                    self.name, tier.name,
                )
                return

            line_name = _('Discount: %s (-%s%%)') % (tier.name, tier.reward_amount)
            if related_line:
                line_name = _('Discount on %s: %s (-%s%%)') % (
                    related_line.product_id.display_name, tier.name, tier.reward_amount,
                )

            vals.update({
                'name': line_name,
                'product_id': reward_product.id,
                'is_reward_line': True,
                'product_uom_qty': 1.0,
                'price_unit': -discount,
            })

        elif tier.reward_type == 'bonus' and tier.reward_product_id:
            vals.update({
                'name': _('Bonus: %s (%s)') % (tier.reward_product_id.display_name, tier.name),
                'product_id': tier.reward_product_id.id,
                'is_reward_line': True,
                'product_uom_qty': tier.qty or 1.0,
                'product_uom': tier.uom_id.id or tier.reward_product_id.uom_id.id,
                'price_unit': 0.0,
            })
        else:
            _logger.warning("Order %s: Tier '%s' matched but skipped. Reward Type: %s, Reward Product: %s", 
                            self.name, tier.name, tier.reward_type, tier.reward_product_id.display_name if tier.reward_product_id else "None")
            return

        self.env['sale.order.line'].sudo().create(vals)
        _logger.info("  → Created reward line: %s", vals.get('name'))

    # ══════════════════════════════════════════════════════════════════════
    #  ORDER CONFIRMATION — the single entry point
    # ══════════════════════════════════════════════════════════════════════

    def action_confirm(self):
        """
        Override to apply BOTH standard and tier promotions on confirmation.

        Promotions are applied BEFORE super().action_confirm() so the order
        is still in 'draft' state, allowing clean unlink of old reward lines
        and avoiding ghost $0 lines.

        A context flag 'skip_tier_in_update' prevents the _update_programs_and_rewards
        hook (triggered by the standard loyalty engine inside super()) from
        re-running tier promotions.
        """
        # Skip if already confirmed
        already_confirmed = self.filtered(lambda o: o.state in ('sale', 'done'))
        to_confirm = self - already_confirmed

        # Apply promotions BEFORE confirmation (order is still draft → safe to unlink lines)
        for order in to_confirm:
            _logger.info("═══ Order %s: Running promotion engine (Upon Confirmation) ═══", order.name)
            # Use applying_at_confirmation context to bypass the draft-phase lock
            order_ctx = order.with_context(applying_at_confirmation=True, skip_tier_in_update=True)
            
            # Apply standard promotions first (respecting priority)
            order_ctx._apply_standard_promotions()
            # Then apply tiered promotions on the remaining balance
            order_ctx.with_context(skip_tier_in_update=False)._apply_tiered_promotions()

        # Confirm with context flag to prevent _update_programs_and_rewards
        # from re-triggering tier promotions during the standard loyalty engine run.
        result = super(SaleOrder, self.with_context(
            skip_tier_in_update=True,
            applying_at_confirmation=True,
        )).action_confirm()

        return result
