# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    # ═══════════════════════════════════════════════════════════════════════════
    # FIELDS
    # ═══════════════════════════════════════════════════════════════════════════

    partner_debit_limit = fields.Monetary(
        string='Partner Debit Limit',
        related='partner_id.commercial_partner_id.purchase_debit_limit',
        readonly=True,
        help='حد الاستحقاق في المشتريات للشريك',
    )

    partner_debit_used = fields.Monetary(
        string='Partner Debit Used',
        compute='_compute_partner_debit_info',
        help='المستحقات الحالية للشريك',
    )

    partner_debit_available = fields.Monetary(
        string='Partner Debit Available',
        compute='_compute_partner_debit_info',
        help='الرصيد المتاح للشريك',
    )

    debit_limit_warning = fields.Text(
        string='Debit Limit Warning',
        compute='_compute_debit_limit_warning',
        help='تحذير تجاوز حد الاستحقاق',
    )

    show_debit_warning = fields.Boolean(
        string='Show Debit Warning',
        compute='_compute_debit_limit_warning',
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # COMPUTE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    @api.depends('partner_id', 'amount_total')
    def _compute_partner_debit_info(self):
        """Compute partner debit information for display."""
        for order in self:
            if order.partner_id:
                commercial_partner = order.partner_id.commercial_partner_id
                order.partner_debit_used = commercial_partner._get_outstanding_payable(order.company_id)
                if commercial_partner.purchase_debit_limit:
                    order.partner_debit_available = (
                        commercial_partner.purchase_debit_limit - order.partner_debit_used
                    )
                else:
                    order.partner_debit_available = 0
            else:
                order.partner_debit_used = 0
                order.partner_debit_available = 0

    @api.depends('partner_id', 'amount_total', 'state')
    def _compute_debit_limit_warning(self):
        """Compute warning message for draft orders."""
        for order in self:
            order.debit_limit_warning = False
            order.show_debit_warning = False
            
            if order.state in ['draft', 'sent'] and order.partner_id and order.amount_total:
                warning = order.partner_id.get_purchase_limit_warning_message(order.amount_total)
                if warning:
                    order.debit_limit_warning = warning
                    order.show_debit_warning = True

    # ═══════════════════════════════════════════════════════════════════════════
    # OVERRIDE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    def button_confirm(self):
        """
        Override button_confirm to check debit limit before confirming the order.
        """
        for order in self:
            if order.partner_id and order.amount_total:
                order.partner_id.check_purchase_debit_limit(
                    order.amount_total,
                    company=order.company_id
                )
        
        return super(PurchaseOrder, self).button_confirm()

    # ═══════════════════════════════════════════════════════════════════════════
    # ONCHANGE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    @api.onchange('partner_id')
    def _onchange_partner_debit_warning(self):
        """Show warning when selecting a partner that is at or near their limit."""
        if self.partner_id:
            commercial_partner = self.partner_id.commercial_partner_id
            
            # Skip warning if partner has override enabled
            if commercial_partner.allow_over_purchase_debit:
                return
            
            if commercial_partner.purchase_debit_limit:
                outstanding = commercial_partner._get_outstanding_payable()
                usage_percent = (outstanding / commercial_partner.purchase_debit_limit) * 100
                
                if usage_percent >= 100:
                    return {
                        'warning': {
                            'title': _('تحذير حد الاستحقاق / Debit Limit Warning'),
                            'message': _(
                                'هذا الشريك تجاوز حد الاستحقاق المحدد!\n'
                                'This vendor has exceeded their debit limit!\n\n'
                                'المستحقات الحالية / Current Outstanding: %s\n'
                                'حد الاستحقاق / Debit Limit: %s\n'
                                'نسبة الاستخدام / Usage: %.1f%%'
                            ) % (
                                commercial_partner._format_amount(outstanding),
                                commercial_partner._format_amount(commercial_partner.purchase_debit_limit),
                                usage_percent
                            )
                        }
                    }
                elif usage_percent >= 80:
                    return {
                        'warning': {
                            'title': _('تنبيه حد الاستحقاق / Debit Limit Notice'),
                            'message': _(
                                'هذا الشريك قريب من حد الاستحقاق المحدد.\n'
                                'This vendor is approaching their debit limit.\n\n'
                                'المستحقات الحالية / Current Outstanding: %s\n'
                                'حد الاستحقاق / Debit Limit: %s\n'
                                'نسبة الاستخدام / Usage: %.1f%%'
                            ) % (
                                commercial_partner._format_amount(outstanding),
                                commercial_partner._format_amount(commercial_partner.purchase_debit_limit),
                                usage_percent
                            )
                        }
                    }
