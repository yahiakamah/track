# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # ═══════════════════════════════════════════════════════════════════════════
    # FIELDS
    # ═══════════════════════════════════════════════════════════════════════════

    sale_credit_limit = fields.Monetary(
        string='Sale Credit Limit',
        help='حد الاستحقاق في المبيعات - Maximum allowed outstanding receivable amount for this customer. '
             'Leave empty or zero for no limit.',
        currency_field='currency_id',
        tracking=True,
    )

    purchase_debit_limit = fields.Monetary(
        string='Purchase Debit Limit',
        help='حد الاستحقاق في المشتريات - Maximum allowed outstanding payable amount for this vendor. '
             'Leave empty or zero for no limit.',
        currency_field='currency_id',
        tracking=True,
    )

    allow_over_sale_credit = fields.Boolean(
        string='Allow Over Sale Credit',
        help='السماح بتجاوز حد الاستحقاق في المبيعات - If checked, this partner can exceed the sale credit limit.',
        tracking=True,
        default=False,
    )

    allow_over_purchase_debit = fields.Boolean(
        string='Allow Over Purchase Debit',
        help='السماح بتجاوز حد الاستحقاق في المشتريات - If checked, this partner can exceed the purchase debit limit.',
        tracking=True,
        default=False,
    )

    # Computed fields for display
    sale_credit_used = fields.Monetary(
        string='Sale Credit Used',
        compute='_compute_credit_debit_used',
        currency_field='currency_id',
        help='إجمالي المستحقات الحالية - Total outstanding receivables from posted unpaid invoices.',
    )

    sale_credit_available = fields.Monetary(
        string='Sale Credit Available',
        compute='_compute_credit_debit_used',
        currency_field='currency_id',
        help='الحد المتاح للمبيعات - Remaining credit available for this customer.',
    )

    sale_credit_usage_percent = fields.Float(
        string='Sale Credit Usage %',
        compute='_compute_credit_debit_used',
        help='نسبة استخدام حد المبيعات',
    )

    purchase_debit_used = fields.Monetary(
        string='Purchase Debit Used',
        compute='_compute_credit_debit_used',
        currency_field='currency_id',
        help='إجمالي المستحقات الحالية - Total outstanding payables from posted unpaid bills.',
    )

    purchase_debit_available = fields.Monetary(
        string='Purchase Debit Available',
        compute='_compute_credit_debit_used',
        currency_field='currency_id',
        help='الحد المتاح للمشتريات - Remaining debit available for this vendor.',
    )

    purchase_debit_usage_percent = fields.Float(
        string='Purchase Debit Usage %',
        compute='_compute_credit_debit_used',
        help='نسبة استخدام حد المشتريات',
    )

    sale_limit_warning = fields.Selection(
        selection=[
            ('ok', 'OK'),
            ('warning', 'Warning'),
            ('exceeded', 'Exceeded'),
        ],
        string='Sale Limit Status',
        compute='_compute_credit_debit_used',
    )

    purchase_limit_warning = fields.Selection(
        selection=[
            ('ok', 'OK'),
            ('warning', 'Warning'),
            ('exceeded', 'Exceeded'),
        ],
        string='Purchase Limit Status',
        compute='_compute_credit_debit_used',
    )

    # ═══════════════════════════════════════════════════════════════════════════
    # COMPUTE METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    @api.depends('credit', 'debit', 'sale_credit_limit', 'purchase_debit_limit')
    def _compute_credit_debit_used(self):
        """
        Compute the outstanding amounts and available limits for each partner.
        Uses account.move.line for accurate outstanding calculation.
        """
        for partner in self:
            # Get outstanding receivables (customer invoices)
            partner.sale_credit_used = partner._get_outstanding_receivable()
            
            # Get outstanding payables (vendor bills)
            partner.purchase_debit_used = partner._get_outstanding_payable()
            
            # Calculate available amounts
            if partner.sale_credit_limit:
                partner.sale_credit_available = partner.sale_credit_limit - partner.sale_credit_used
                partner.sale_credit_usage_percent = (
                    (partner.sale_credit_used / partner.sale_credit_limit) * 100
                    if partner.sale_credit_limit else 0
                )
                # Set warning status
                if partner.sale_credit_used >= partner.sale_credit_limit:
                    partner.sale_limit_warning = 'exceeded'
                elif partner.sale_credit_usage_percent >= 80:
                    partner.sale_limit_warning = 'warning'
                else:
                    partner.sale_limit_warning = 'ok'
            else:
                partner.sale_credit_available = 0
                partner.sale_credit_usage_percent = 0
                partner.sale_limit_warning = 'ok'
            
            if partner.purchase_debit_limit:
                partner.purchase_debit_available = partner.purchase_debit_limit - partner.purchase_debit_used
                partner.purchase_debit_usage_percent = (
                    (partner.purchase_debit_used / partner.purchase_debit_limit) * 100
                    if partner.purchase_debit_limit else 0
                )
                # Set warning status
                if partner.purchase_debit_used >= partner.purchase_debit_limit:
                    partner.purchase_limit_warning = 'exceeded'
                elif partner.purchase_debit_usage_percent >= 80:
                    partner.purchase_limit_warning = 'warning'
                else:
                    partner.purchase_limit_warning = 'ok'
            else:
                partner.purchase_debit_available = 0
                partner.purchase_debit_usage_percent = 0
                partner.purchase_limit_warning = 'ok'

    # ═══════════════════════════════════════════════════════════════════════════
    # BUSINESS METHODS
    # ═══════════════════════════════════════════════════════════════════════════

    def _get_outstanding_receivable(self, company=None):
        """
        Calculate total outstanding receivable amount for customer invoices.
        Only considers posted invoices with remaining amount > 0.
        
        :param company: Optional company record for multi-company filtering
        :return: Total outstanding receivable amount
        """
        self.ensure_one()
        if not self.id or isinstance(self.id, models.NewId):
            return 0.0
        company = company or self.env.company
        
        # Get all posted customer invoices with outstanding amount
        domain = [
            ('partner_id', 'child_of', self.commercial_partner_id.id),
            ('move_type', 'in', ['out_invoice', 'out_refund']),
            ('state', '=', 'posted'),
            ('payment_state', 'not in', ['paid', 'reversed']),
            ('company_id', '=', company.id),
        ]
        
        invoices = self.env['account.move'].sudo().search(domain)
        
        # Sum the residual amounts (considering refunds as negative)
        total = 0.0
        for invoice in invoices:
            if invoice.move_type == 'out_invoice':
                total += invoice.amount_residual
            elif invoice.move_type == 'out_refund':
                total -= invoice.amount_residual
        
        return max(total, 0.0)

    def _get_outstanding_payable(self, company=None):
        """
        Calculate total outstanding payable amount for vendor bills.
        Only considers posted bills with remaining amount > 0.
        
        :param company: Optional company record for multi-company filtering
        :return: Total outstanding payable amount
        """
        self.ensure_one()
        if not self.id or isinstance(self.id, models.NewId):
            return 0.0
        company = company or self.env.company
        
        # Get all posted vendor bills with outstanding amount
        domain = [
            ('partner_id', 'child_of', self.commercial_partner_id.id),
            ('move_type', 'in', ['in_invoice', 'in_refund']),
            ('state', '=', 'posted'),
            ('payment_state', 'not in', ['paid', 'reversed']),
            ('company_id', '=', company.id),
        ]
        
        bills = self.env['account.move'].sudo().search(domain)
        
        # Sum the residual amounts (considering refunds as negative)
        total = 0.0
        for bill in bills:
            if bill.move_type == 'in_invoice':
                total += bill.amount_residual
            elif bill.move_type == 'in_refund':
                total -= bill.amount_residual
        
        return max(total, 0.0)

    def check_sale_credit_limit(self, amount, company=None):
        """
        Check if adding the given amount would exceed the sale credit limit.
        
        :param amount: Amount to be added (new invoice/order amount)
        :param company: Optional company record
        :return: True if within limit, raises UserError if exceeded
        """
        self.ensure_one()
        
        commercial_partner = self.commercial_partner_id
        
        # Skip check if no limit is set
        if not commercial_partner.sale_credit_limit:
            return True
        
        # Skip check if partner has "Allow Over Sale Credit" enabled
        if commercial_partner.allow_over_sale_credit:
            return True
        
        # Check if user has override permission
        if self.env.user.has_group('partner_financial_limits_allow_over.group_exceed_partner_limit'):
            return True
        
        outstanding = commercial_partner._get_outstanding_receivable(company)
        
        if outstanding + amount > commercial_partner.sale_credit_limit:
            raise UserError(_(
                'لا يمكن إتمام العملية، تم تجاوز الحد الائتماني المحدد لهذا الشريك.\n\n'
                'Cannot complete the operation. The credit limit for this partner has been exceeded.\n\n'
                'تفاصيل / Details:\n'
                '• الشريك / Partner: %(partner)s\n'
                '• حد الاستحقاق / Credit Limit: %(limit)s\n'
                '• المستحقات الحالية / Current Outstanding: %(outstanding)s\n'
                '• المبلغ المطلوب / Requested Amount: %(amount)s\n'
                '• الإجمالي / Total: %(total)s\n'
                '• تجاوز بمقدار / Exceeded by: %(exceeded)s'
            ) % {
                'partner': commercial_partner.display_name,
                'limit': self._format_amount(commercial_partner.sale_credit_limit),
                'outstanding': self._format_amount(outstanding),
                'amount': self._format_amount(amount),
                'total': self._format_amount(outstanding + amount),
                'exceeded': self._format_amount((outstanding + amount) - commercial_partner.sale_credit_limit),
            })
        
        return True

    def check_purchase_debit_limit(self, amount, company=None):
        """
        Check if adding the given amount would exceed the purchase debit limit.
        
        :param amount: Amount to be added (new bill/order amount)
        :param company: Optional company record
        :return: True if within limit, raises UserError if exceeded
        """
        self.ensure_one()
        
        commercial_partner = self.commercial_partner_id
        
        # Skip check if no limit is set
        if not commercial_partner.purchase_debit_limit:
            return True
        
        # Skip check if partner has "Allow Over Purchase Debit" enabled
        if commercial_partner.allow_over_purchase_debit:
            return True
        
        # Check if user has override permission
        if self.env.user.has_group('partner_financial_limits_allow_over.group_exceed_partner_limit'):
            return True
        
        outstanding = commercial_partner._get_outstanding_payable(company)
        
        if outstanding + amount > commercial_partner.purchase_debit_limit:
            raise UserError(_(
                'لا يمكن إتمام العملية، تم تجاوز حد الاستحقاق في المشتريات المحدد لهذا الشريك.\n\n'
                'Cannot complete the operation. The debit limit for this vendor has been exceeded.\n\n'
                'تفاصيل / Details:\n'
                '• الشريك / Partner: %(partner)s\n'
                '• حد الاستحقاق / Debit Limit: %(limit)s\n'
                '• المستحقات الحالية / Current Outstanding: %(outstanding)s\n'
                '• المبلغ المطلوب / Requested Amount: %(amount)s\n'
                '• الإجمالي / Total: %(total)s\n'
                '• تجاوز بمقدار / Exceeded by: %(exceeded)s'
            ) % {
                'partner': commercial_partner.display_name,
                'limit': self._format_amount(commercial_partner.purchase_debit_limit),
                'outstanding': self._format_amount(outstanding),
                'amount': self._format_amount(amount),
                'total': self._format_amount(outstanding + amount),
                'exceeded': self._format_amount((outstanding + amount) - commercial_partner.purchase_debit_limit),
            })
        
        return True

    def _format_amount(self, amount):
        """Format amount with currency symbol."""
        return "{:,.2f} {}".format(amount, self.currency_id.symbol or '')

    def get_sale_limit_warning_message(self, amount):
        """
        Get a warning message for draft documents if approaching limit.
        Returns None if no warning needed.
        """
        self.ensure_one()
        
        commercial_partner = self.commercial_partner_id
        
        if not commercial_partner.sale_credit_limit:
            return None
        
        # No warning if partner has override enabled
        if commercial_partner.allow_over_sale_credit:
            return None
        
        outstanding = commercial_partner._get_outstanding_receivable()
        total = outstanding + amount
        usage_percent = (total / commercial_partner.sale_credit_limit) * 100
        
        if usage_percent >= 100:
            return _(
                '⚠️ تحذير: سيتم تجاوز حد الاستحقاق عند تأكيد هذه العملية!\n'
                'Warning: Credit limit will be exceeded upon confirmation!\n'
                'الاستخدام المتوقع / Expected Usage: %.1f%%'
            ) % usage_percent
        elif usage_percent >= 80:
            return _(
                '⚡ تنبيه: اقتربت من حد الاستحقاق.\n'
                'Notice: Approaching credit limit.\n'
                'الاستخدام المتوقع / Expected Usage: %.1f%%'
            ) % usage_percent
        
        return None

    def get_purchase_limit_warning_message(self, amount):
        """
        Get a warning message for draft documents if approaching limit.
        Returns None if no warning needed.
        """
        self.ensure_one()
        
        commercial_partner = self.commercial_partner_id
        
        if not commercial_partner.purchase_debit_limit:
            return None
        
        # No warning if partner has override enabled
        if commercial_partner.allow_over_purchase_debit:
            return None
        
        outstanding = commercial_partner._get_outstanding_payable()
        total = outstanding + amount
        usage_percent = (total / commercial_partner.purchase_debit_limit) * 100
        
        if usage_percent >= 100:
            return _(
                '⚠️ تحذير: سيتم تجاوز حد الاستحقاق عند تأكيد هذه العملية!\n'
                'Warning: Debit limit will be exceeded upon confirmation!\n'
                'الاستخدام المتوقع / Expected Usage: %.1f%%'
            ) % usage_percent
        elif usage_percent >= 80:
            return _(
                '⚡ تنبيه: اقتربت من حد الاستحقاق.\n'
                'Notice: Approaching debit limit.\n'
                'الاستخدام المتوقع / Expected Usage: %.1f%%'
            ) % usage_percent
        
        return None
