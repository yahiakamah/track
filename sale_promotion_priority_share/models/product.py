from odoo import models, fields, api, _
import logging

_logger = logging.getLogger(__name__)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    discount_account_id = fields.Many2one(
        'account.account', 
        string='Discount Account',
        company_dependent=True,
        help="Account used for discounts for this product."
    )
    bonus_product_account_id = fields.Many2one(
        'account.account', 
        string='Bonus Product Account',
        company_dependent=True,
        help="Account used for bonus products for this product."
    )

class ProductCategory(models.Model):
    _inherit = 'product.category'

    discount_account_id = fields.Many2one(
        'account.account', 
        string='Discount Account',
        company_dependent=True,
        help="Account used for discounts for this category."
    )
    bonus_product_account_id = fields.Many2one(
        'account.account', 
        string='Bonus Product Account',
        company_dependent=True,
        help="Account used for bonus products for this category."
    )


