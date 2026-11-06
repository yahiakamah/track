# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    # ═══════════════════════════════════════════════════════════════════════════
    # FIELDS
    # ═══════════════════════════════════════════════════════════════════════════

    partner_credit_limit = fields.Monetary(
        string='Partner Credit Limit',
        related='partner_id.commercial_partner_id.sale_credit_limit',
        readonly=True,
        help='حد الاستحقاق في المبيعات للشريك',
    )

    partner_credit_used = fields.Monetary(
        string='Partner Credit Used',
        compute='_compute_partner_credit_info',
        help='المستحقات الحالية للشريك',
    )

    partner_credit_available = fields.Monetary(
        string='Partner Credit Available',
        compute='_compute_partner_credit_info',
        help='الرصيد المتاح للشريك',
    )

    credit_limit_warning = fields.Text(
        string='Credit Limit Warning',
        compute='_compute_credit_limit_warning',
        help='تحذير تجاوز حد الاستحقاق',
    )

    show_credit_warning = fields.Boolean(
        string='Show Credit Warning',
        compute='_compute_credit_limit_warning',
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # COMPUTE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    @api.depends('partner_id', 'amount_total')
    def _compute_partner_credit_info(self):
        """Compute partner credit information for display."""
        for order in self:
            if order.partner_id:
                commercial_partner = order.partner_id.commercial_partner_id
                order.partner_credit_used = commercial_partner._get_outstanding_receivable(order.company_id)
                if commercial_partner.sale_credit_limit:
                    order.partner_credit_available = (
                        commercial_partner.sale_credit_limit - order.partner_credit_used
                    )
                else:
                    order.partner_credit_available = 0
            else:
                order.partner_credit_used = 0
                order.partner_credit_available = 0

    @api.depends('partner_id', 'amount_total', 'state')
    def _compute_credit_limit_warning(self):
        """Compute warning message for draft orders."""
        for order in self:
            order.credit_limit_warning = False
            order.show_credit_warning = False
            
            if order.state in ['draft', 'sent'] and order.partner_id and order.amount_total:
                warning = order.partner_id.get_sale_limit_warning_message(order.amount_total)
                if warning:
                    order.credit_limit_warning = warning
                    order.show_credit_warning = True

    # ═══════════════════════════════════════════════════════════════════════════
    # OVERRIDE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    def action_confirm(self):
        """
        Override action_confirm to check credit limit before confirming the order.
        """
        for order in self:
            if order.partner_id and order.amount_total:
                order.partner_id.check_sale_credit_limit(
                    order.amount_total,
                    company=order.company_id
                )
        
        return super(SaleOrder, self).action_confirm()

    # ═══════════════════════════════════════════════════════════════════════════
    # ONCHANGE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    @api.onchange('partner_id')
    def _onchange_partner_credit_warning(self):
        """Show warning when selecting a partner that is at or near their limit."""
        if self.partner_id:
            commercial_partner = self.partner_id.commercial_partner_id
            
            # Skip warning if partner has override enabled
            if commercial_partner.allow_over_sale_credit:
                return
            
            if commercial_partner.sale_credit_limit:
                outstanding = commercial_partner._get_outstanding_receivable()
                usage_percent = (outstanding / commercial_partner.sale_credit_limit) * 100
                
                if usage_percent >= 100:
                    return {
                        'warning': {
                            'title': _('تحذير حد الاستحقاق / Credit Limit Warning'),
                            'message': _(
                                'هذا الشريك تجاوز حد الاستحقاق المحدد!\n'
                                'This partner has exceeded their credit limit!\n\n'
                                'المستحقات الحالية / Current Outstanding: %s\n'
                                'حد الاستحقاق / Credit Limit: %s\n'
                                'نسبة الاستخدام / Usage: %.1f%%'
                            ) % (
                                commercial_partner._format_amount(outstanding),
                                commercial_partner._format_amount(commercial_partner.sale_credit_limit),
                                usage_percent
                            )
                        }
                    }
                elif usage_percent >= 80:
                    return {
                        'warning': {
                            'title': _('تنبيه حد الاستحقاق / Credit Limit Notice'),
                            'message': _(
                                'هذا الشريك قريب من حد الاستحقاق المحدد.\n'
                                'This partner is approaching their credit limit.\n\n'
                                'المستحقات الحالية / Current Outstanding: %s\n'
                                'حد الاستحقاق / Credit Limit: %s\n'
                                'نسبة الاستخدام / Usage: %.1f%%'
                            ) % (
                                commercial_partner._format_amount(outstanding),
                                commercial_partner._format_amount(commercial_partner.sale_credit_limit),
                                usage_percent
                            )
                        }
                    }
