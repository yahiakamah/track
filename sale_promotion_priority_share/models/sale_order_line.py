from odoo import models, fields, api


class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    # ── gift_bonus_engine compatibility fields ──────────────────────────────
    # gift_bonus_engine defines these on sale.order.line. When that module
    # fails to load its Python models, the order_line subview crashes.
    # Declaring them here ensures they always exist.
    is_gift = fields.Boolean(string='Is Gift', default=False, readonly=True)
    gift_status = fields.Selection([
        ('available', 'Available'),
        ('pending', 'Pending Stock'),
    ], string='Gift Status', default='available', readonly=True)
    gift_progress_percent = fields.Float(
        string='Gift Progress %',
        compute='_compute_gift_progress_fallback',
    )

    def _compute_gift_progress_fallback(self):
        for line in self:
            line.gift_progress_percent = 0.0
    # ── end gift_bonus_engine compatibility fields ──────────────────────────
    is_tiered_reward = fields.Boolean(string='Is Tiered Reward', default=False)
    program_id = fields.Many2one('loyalty.program', string='Program', readonly=True)
    tier_id = fields.Many2one('loyalty.program.tier', string='Applied Tier', readonly=True)
