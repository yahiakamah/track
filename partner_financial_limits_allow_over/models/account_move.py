# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    # ═══════════════════════════════════════════════════════════════════════════
    # FIELDS
    # ═══════════════════════════════════════════════════════════════════════════

    partner_financial_limit = fields.Monetary(
        string='Partner Financial Limit',
        compute='_compute_partner_financial_info',
        help='حد الاستحقاق للشريك',
    )

    partner_financial_used = fields.Monetary(
        string='Partner Amount Used',
        compute='_compute_partner_financial_info',
        help='المستحقات الحالية للشريك',
    )

    partner_financial_available = fields.Monetary(
        string='Partner Amount Available',
        compute='_compute_partner_financial_info',
        help='الرصيد المتاح للشريك',
    )

    financial_limit_warning = fields.Text(
        string='Financial Limit Warning',
        compute='_compute_financial_limit_warning',
        help='تحذير تجاوز حد الاستحقاق',
    )

    show_financial_warning = fields.Boolean(
        string='Show Financial Warning',
        compute='_compute_financial_limit_warning',
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # COMPUTE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    @api.depends('partner_id', 'amount_total', 'move_type')
    def _compute_partner_financial_info(self):
        """Compute partner financial information based on move type."""
        for move in self:
            move.partner_financial_limit = 0
            move.partner_financial_used = 0
            move.partner_financial_available = 0
            
            if not move.partner_id:
                continue
            
            commercial_partner = move.partner_id.commercial_partner_id
            
            # Customer Invoice / Refund
            if move.move_type in ['out_invoice', 'out_refund']:
                move.partner_financial_limit = commercial_partner.sale_credit_limit
                move.partner_financial_used = commercial_partner._get_outstanding_receivable(move.company_id)
                if commercial_partner.sale_credit_limit:
                    move.partner_financial_available = (
                        commercial_partner.sale_credit_limit - move.partner_financial_used
                    )
            
            # Vendor Bill / Refund
            elif move.move_type in ['in_invoice', 'in_refund']:
                move.partner_financial_limit = commercial_partner.purchase_debit_limit
                move.partner_financial_used = commercial_partner._get_outstanding_payable(move.company_id)
                if commercial_partner.purchase_debit_limit:
                    move.partner_financial_available = (
                        commercial_partner.purchase_debit_limit - move.partner_financial_used
                    )

    @api.depends('partner_id', 'amount_total', 'state', 'move_type')
    def _compute_financial_limit_warning(self):
        """Compute warning message for draft invoices/bills."""
        for move in self:
            move.financial_limit_warning = False
            move.show_financial_warning = False
            
            if move.state != 'draft' or not move.partner_id or not move.amount_total:
                continue
            
            # Customer Invoice
            if move.move_type == 'out_invoice':
                warning = move.partner_id.get_sale_limit_warning_message(move.amount_total)
                if warning:
                    move.financial_limit_warning = warning
                    move.show_financial_warning = True
            
            # Vendor Bill
            elif move.move_type == 'in_invoice':
                warning = move.partner_id.get_purchase_limit_warning_message(move.amount_total)
                if warning:
                    move.financial_limit_warning = warning
                    move.show_financial_warning = True

    # ═══════════════════════════════════════════════════════════════════════════
    # OVERRIDE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    def action_post(self):
        """
        Override action_post to check financial limits before posting.
        """
        for move in self:
            if not move.partner_id:
                continue
            
            # Customer Invoice - Check Sale Credit Limit
            if move.move_type == 'out_invoice' and move.amount_total:
                move.partner_id.check_sale_credit_limit(
                    move.amount_total,
                    company=move.company_id
                )
            
            # Vendor Bill - Check Purchase Debit Limit
            elif move.move_type == 'in_invoice' and move.amount_total:
                move.partner_id.check_purchase_debit_limit(
                    move.amount_total,
                    company=move.company_id
                )
            
            # Note: Credit Notes (out_refund, in_refund) are not blocked
            # as they reduce the outstanding amount
        
        return super(AccountMove, self).action_post()

    # ═══════════════════════════════════════════════════════════════════════════
    # ONCHANGE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    @api.onchange('partner_id')
    def _onchange_partner_financial_warning(self):
        """Show warning when selecting a partner that is at or near their limit."""
        if not self.partner_id:
            return
        
        commercial_partner = self.partner_id.commercial_partner_id
        
        # Customer Invoice
        if self.move_type in ['out_invoice', 'out_refund']:
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
                                'هذا العميل تجاوز حد الاستحقاق المحدد!\n'
                                'This customer has exceeded their credit limit!\n\n'
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
                                'هذا العميل قريب من حد الاستحقاق المحدد.\n'
                                'This customer is approaching their credit limit.\n\n'
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
        
        # Vendor Bill
        elif self.move_type in ['in_invoice', 'in_refund']:
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
                                'هذا المورد تجاوز حد الاستحقاق المحدد!\n'
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
                                'هذا المورد قريب من حد الاستحقاق المحدد.\n'
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
