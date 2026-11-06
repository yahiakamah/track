# -*- coding: utf-8 -*-

import logging
from odoo import models, fields, api, _

_logger = logging.getLogger(__name__)

from odoo.exceptions import ValidationError

class SalesRepPaymentMethod(models.Model):
    """Sales Rep Payment Method
    
    Defines available payment methods for the sales representative app.
    Each method links to an accounting journal and can be
    configured for different payment types.
    """
    _name = 'sales.rep.payment.method'
    _description = 'Sales Rep Payment Method'
    _order = 'sequence, name'
    _check_company_auto = True

    name = fields.Char(
        string='Method Name',
        required=True,
        translate=True,
        help="Display name for this payment method"
    )
    sequence = fields.Integer(
        string='Sequence',
        default=10,
        help="Order in which payment methods are displayed"
    )
    active = fields.Boolean(
        default=True,
        help="Set to False to hide this payment method"
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        required=True,
        default=lambda self: self.env.company
    )
    
    # Payment Type
    payment_type = fields.Selection([
        ('cash', 'Cash'),
        ('bank', 'Bank'),
        ('online', 'Online Payment'),
    ], string='Type', required=True, default='cash',
       help="Type of payment method")
    
    # Accounting - NO domain, use check_company
    journal_id = fields.Many2one(
        'account.journal',
        string='Journal',
        required=True,
        check_company=True,
        help="Journal where payments will be recorded"
    )
    
    # Online Payment Settings
    payment_provider_id = fields.Many2one(
        'payment.provider',
        string='Payment Provider',
        domain="[('state', '!=', 'disabled')]",
        help="Payment provider for online payments (Stripe, Paymob, etc.)"
    )
    
    # Display
    icon = fields.Char(
        string='Icon Class',
        default='fa-money',
        help="Font Awesome icon class (e.g., fa-credit-card, fa-money)"
    )
    color = fields.Char(
        string='Color',
        default='#875A7B',
        help="Display color in hex format"
    )
    
    # Settings
    split_transactions = fields.Boolean(
        string='Split Transactions',
        default=False,
        help="If enabled, each payment creates a separate accounting entry"
    )

    @api.constrains('payment_type', 'payment_provider_id')
    def _check_online_provider(self):
        """Ensure online payments have a provider configured"""
        for method in self:
            if method.payment_type == 'online' and not method.payment_provider_id:
                raise ValidationError(_(
                    "Online payment methods require a payment provider."
                ))

    @api.constrains('payment_type', 'journal_id')
    def _check_journal_type(self):
        """Ensure journal type matches payment type"""
        for method in self:
            if method.payment_type == 'cash' and method.journal_id.type != 'cash':
                raise ValidationError(_(
                    "Cash payment methods require a cash journal."
                ))
            elif method.payment_type == 'bank' and method.journal_id.type != 'bank':
                raise ValidationError(_(
                    "Bank payment methods require a bank journal."
                ))

    def get_method_data(self):
        """Get payment method data for frontend
        
        Returns:
            dict with payment method information
        """
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'type': self.payment_type,
            'icon': self.icon,
            'color': self.color,
            'is_online': self.payment_type == 'online',
            'provider_id': self.payment_provider_id.id if self.payment_provider_id else None,
        }

    @api.model_create_multi
    def create(self, vals_list):
        return super().create(vals_list)

    def write(self, vals):
        return super().write(vals)

    def unlink(self):
        return super().unlink()
