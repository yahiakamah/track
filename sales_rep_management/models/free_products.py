# -*- coding: utf-8 -*-
"""One shared "free" product per base product for free-product promotions.

Odoo's loyalty engine creates a brand-new service product for EVERY reward
(``loyalty.reward.discount_line_product_id``). With free-product promotions
re-created every season, the catalog fills with "Free Product - X" copies.

Here every free-product reward points to one twin per base product, created
the first time and reused afterwards. The twin copies what matters from the
base product (name, category, unit, taxes, image, code, income/bonus account)
and is a non-saleable service like Odoo's own placeholder. Stock is not
affected: the free line on the order still uses the real product at a 100 %
discount, as standard Odoo does.

Shared twins are never renamed or archived through one reward, so changing
one promotion cannot affect another.
"""
from odoo import _, api, fields, models


class ProductProduct(models.Model):
    _inherit = 'product.product'

    free_of_product_id = fields.Many2one('product.product', string='Free Version Of', index=True,
                                         copy=False, readonly=True, ondelete='cascade')
    free_variant_ids = fields.One2many('product.product', 'free_of_product_id', string='Free Version')

    _sql_constraints = [
        ('free_of_product_uniq', 'unique(free_of_product_id)', 'A product can only have one free version.'),
    ]

    def write(self, vals):
        if self.env.context.get('protect_shared_free_product') and ({'name', 'active'} & set(vals)):
            twins = self.filtered('free_of_product_id')
            if twins:
                kept = {k: v for k, v in vals.items() if k not in ('name', 'active')}
                res = super(ProductProduct, self - twins).write(vals) if self - twins else True
                if kept:
                    super(ProductProduct, twins).write(kept)
                return res
        return super().write(vals)

    def _get_free_variant(self):
        """The shared free twin of this product (created once)."""
        self.ensure_one()
        if self.free_of_product_id:
            return self  # already a free twin
        twin = self.with_context(active_test=False).free_variant_ids[:1]
        if twin:
            if not twin.active:
                twin.active = True
            return twin
        base = self.sudo()
        vals = {
            'name': _('%s (Free)', base.name),
            'type': 'service',
            'sale_ok': False,
            'purchase_ok': False,
            'list_price': 0.0,
            'categ_id': base.categ_id.id,
            'uom_id': base.uom_id.id,
            'uom_po_id': base.uom_po_id.id,
            'taxes_id': [(6, 0, base.taxes_id.ids)],
            'default_code': f'{base.default_code}-FREE' if base.default_code else False,
            'image_1920': base.image_1920,
            'description_sale': base.description_sale,
            'company_id': base.company_id.id,
            'free_of_product_id': base.id,
        }
        income = getattr(base.product_tmpl_id, 'bonus_product_account_id', False) or base.property_account_income_id
        if income:
            vals['property_account_income_id'] = income.id
        return self.env['product.product'].sudo().create(vals)


class LoyaltyRewardFreeProduct(models.Model):
    _inherit = 'loyalty.reward'

    def _shared_free_product(self):
        self.ensure_one()
        if self.reward_type != 'product':
            return False
        base = self.reward_product_id or self.reward_product_ids[:1]
        return base._get_free_variant() if base else False

    def _create_missing_discount_line_products(self):
        for reward in self.filtered(lambda r: not r.discount_line_product_id and r.reward_type == 'product'):
            twin = reward._shared_free_product()
            if twin:
                reward.discount_line_product_id = twin
        return super()._create_missing_discount_line_products()

    def write(self, vals):
        # Odoo renames / archives the reward's product with the reward; a shared
        # twin must not follow one promotion (see ProductProduct.write).
        return super(LoyaltyRewardFreeProduct, self.with_context(protect_shared_free_product=True)).write(vals)

    @api.model
    def _reuse_free_products(self):
        """Upgrade helper: point existing free-product rewards at the shared twin and
        archive the per-reward placeholders nobody uses any more."""
        rewards = self.with_context(active_test=False).search([('reward_type', '=', 'product')])
        old = self.env['product.product']
        for reward in rewards:
            twin = reward._shared_free_product()
            if not twin or reward.discount_line_product_id == twin:
                continue
            old |= reward.discount_line_product_id
            reward.discount_line_product_id = twin
        Line = self.env['sale.order.line'].sudo()
        still_used = self.with_context(active_test=False).search([('discount_line_product_id', 'in', old.ids)]).mapped(
            'discount_line_product_id')
        for product in old - still_used:
            if not Line.search_count([('product_id', '=', product.id)]):
                product.sudo().active = False
        return True
