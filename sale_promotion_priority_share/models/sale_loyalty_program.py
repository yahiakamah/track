from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import logging

_logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  TIER MODEL — defines the From/To ranges for a tiered promotion
# ══════════════════════════════════════════════════════════════════════════════

class LoyaltyProgramTier(models.Model):
    _name = 'loyalty.program.tier'
    _description = 'Loyalty Program Tier'
    _rec_name = 'name'
    _order = 'sequence, id'

    sequence = fields.Integer(default=10)

    program_id = fields.Many2one(
        'loyalty.program',
        string='Program',
        required=True,
        ondelete='cascade',
    )
    name = fields.Char(string='Tier Name', required=True, index=True)

    # ── Range ─────────────────────────────────────────────────────────────
    minimum_amount = fields.Float(string='From', required=True)
    maximum_amount = fields.Float(string='To', required=True)

    # ── Inherited from parent program (read-only, enforces single type) ──
    tiers_type = fields.Selection(
        related='program_id.tiers_type',
        string='Tiers Type',
        readonly=True,
        store=True,
    )

    # ── What the From/To values measure ──────────────────────────────────
    trigger_type = fields.Selection([
        ('amount', 'Amount'),
        ('quantity', 'Quantity'),
    ], string='Trigger By', default='amount', required=True, compute='_compute_trigger_type', store=True, readonly=True)
    

    @api.depends('tiers_type')
    def _compute_trigger_type(self):
        for tier in self:
            if tier.tiers_type == 'order_total':
                tier.trigger_type = 'amount'
            elif tier.tiers_type == 'order_line':
                tier.trigger_type = 'quantity'
            else:
                tier.trigger_type = 'amount'

    # ── Rules (Optional, for Order Line tiers) ───────────────────────────
    rule_product_id = fields.Many2one('product.product', string='Product Rule')
    rule_uom_id = fields.Many2one('uom.uom', string='UoM Rule')

    # ── Reward ────────────────────────────────────────────────────────────
    reward_type = fields.Selection([
        ('discount', 'Discount %'),
        ('bonus', 'Bonus'),
    ], string='Reward Type', required=True, default='discount')

    reward_amount = fields.Float(
        string='Discount %',
        help="Percentage discount to apply (e.g. 3.0 = 3%).",
    )

    reward_product_id = fields.Many2one('product.product', string='Reward Product')
    qty = fields.Float(string='Bonus Qty', default=1.0)
    uom_id = fields.Many2one('uom.uom', string='Unit of Measure')

    # ── Technical Fields for Domains ─────────────────────────────────────
    rule_product_uom_category_id = fields.Many2one(
        related='rule_product_id.uom_id.category_id',
        string='Rule Product UoM Category',
    )
    reward_product_uom_category_id = fields.Many2one(
        related='reward_product_id.uom_id.category_id',
        string='Reward Product UoM Category',
    )

    # ── Onchanges ────────────────────────────────────────────────────────

    @api.onchange('rule_product_id')
    def _onchange_rule_product_id(self):
        if self.rule_product_id:
            self.rule_uom_id = self.rule_product_id.uom_id

    @api.onchange('reward_product_id')
    def _onchange_reward_product_id(self):
        if self.reward_product_id:
            self.uom_id = self.reward_product_id.uom_id


    # ── Validation ────────────────────────────────────────────────────────

    @api.constrains('minimum_amount', 'maximum_amount')
    def _check_range(self):
        for tier in self:
            if tier.maximum_amount <= tier.minimum_amount:
                raise ValidationError(
                    _('Tier "%s": "To" value must be greater than "From" value.')
                    % tier.name
                )

    @api.constrains('reward_type', 'reward_amount', 'reward_product_id')
    def _check_reward_config(self):
        for tier in self:
            if tier.reward_type == 'discount' and tier.reward_amount <= 0:
                raise ValidationError(
                    _('Tier "%s": Discount %% must be greater than zero.') % tier.name
                )
            if tier.reward_type == 'bonus' and not tier.reward_product_id:
                raise ValidationError(
                    _('Tier "%s": Bonus reward requires a product.') % tier.name
                )


# ══════════════════════════════════════════════════════════════════════════════
#  LOYALTY PROGRAM — extends the standard loyalty.program with priority,
#  sharing, customer targeting, and tier support.
# ══════════════════════════════════════════════════════════════════════════════

class LoyaltyProgram(models.Model):
    _inherit = 'loyalty.program'

    # ── Priority & Sharing ────────────────────────────────────────────────
    priority = fields.Integer(
        string='Priority',
        required=True,
        index=True,
        default=lambda self: self._get_default_priority(),
        help="Lower value = higher priority. Must be unique.",
    )
    can_be_shared = fields.Boolean(
        string='Can Be Shared',
        default=False,
        help="If checked, this program can stack with other shareable programs.",
    )
    is_cash = fields.Boolean(
        string='Cash Promotion',
        default=False,
        help="If checked, this promotion only applies to cash orders.",
    )
    is_auto = fields.Boolean(
        string='Auto Apply',
        default=True,
        help="If checked, this promotion is automatically evaluated.",
    )

    _sql_constraints = [
        ('unique_priority', 'unique (priority)',
         'Priority must be unique. Another promotion program already uses this priority.'),
    ]

    # ── Tier Configuration ────────────────────────────────────────────────
    program_type = fields.Selection(
        selection_add=[('tier', 'Tiers')],
        ondelete={'tier': 'set default'},
    )
    tiers_type = fields.Selection([
        ('order_total', 'Total Order'),
        ('order_line', 'Order Line'),
    ], string='Tiers Type')
    discount_product_id = fields.Many2one(
        'product.product', 
        domain=[('type', '=', 'service')], 
        string='Discount Product'
    )

    program_tier_ids = fields.One2many(
        'loyalty.program.tier',
        'program_id',
        string='Tiers',
    )

    # ── Customer Targeting ────────────────────────────────────────────────
    limit_partner_ids = fields.Many2many(
        'res.partner', 'loyalty_program_partner_rel', 'program_id', 'partner_id',
        string='Specific Customers',
        help="If set, this program only applies to these customers.",
    )
    limit_partner_category_ids = fields.Many2many(
        'res.partner.category', string='Customer Tags',
        help="If set, this program only applies to customers with these tags.",
    )
    limit_country_ids = fields.Many2many(
        'res.country', string='Countries',
        help="If set, this program only applies to customers in these countries.",
    )
    limit_state_ids = fields.Many2many(
        'res.country.state', string='States',
        help="If set, this program only applies to customers in these states.",
    )
    limit_area_ids = fields.Many2many(
        'loyalty.area', string='Areas',
        help="If set, this program only applies to customers in these areas.",
    )

    # ── Overrides ─────────────────────────────────────────────────────────

    def _program_items_name(self):
        """Register the custom 'tier' type in the loyalty items dictionary."""
        res = super()._program_items_name()
        res.update({'tier': _('Tiers')})
        return res

    @api.depends('program_type', 'coupon_count')
    def _compute_coupon_count_display(self):
        """Override to handle the 'tier' program type and avoid KeyError."""
        program_items_name = {
            'coupons': _('Coupons'),
            'promo_code': _('Promo Codes'),
            'loyalty': _('Points'),
            'gift_card': _('Gift Cards'),
            'ewallet': _('eWallet'),
            'tier': _('Tiers'),
        }
        for program in self:
            if program.program_type not in program_items_name:
                super(LoyaltyProgram, program)._compute_coupon_count_display()
                continue
            program.coupon_count_display = "%i %s" % (
                program.coupon_count or 0,
                program_items_name[program.program_type] or '',
            )

    @api.model
    def _get_default_priority(self):
        """Get next available priority number."""
        try:
            program_count = self.search_count([])
            if program_count == 0:
                return 1
            highest = self.search([], order='priority desc', limit=1)
            if highest:
                return highest.priority + 1
            return 1
        except Exception as e:
            _logger.warning("Error getting default priority: %s", e)
            return 1

    @api.constrains('priority')
    def _check_priority_unique(self):
        """Additional Python-level validation to ensure unique priority."""
        for program in self:
            if program.priority:
                duplicate = self.search([
                    ('priority', '=', program.priority),
                    ('id', '!=', program.id),
                ], limit=1)
                if duplicate:
                    raise ValidationError(
                        _('Priority must be unique. Program "%s" already uses priority %s.')
                        % (duplicate.name, program.priority)
                    )


# ══════════════════════════════════════════════════════════════════════════════
#  LOYALTY AREA — simple lookup table for geographic targeting
# ══════════════════════════════════════════════════════════════════════════════

class LoyaltyArea(models.Model):
    _name = 'loyalty.area'
    _description = 'Loyalty Promotion Area'

    name = fields.Char(string='Area Name', required=True, index=True)

    _sql_constraints = [
        ('name_unique', 'unique(name)', 'Area name must be unique!'),
    ]