from odoo import models, fields, api, _
from odoo.exceptions import ValidationError, UserError


class SalesRepCollection(models.Model):
    _name = 'sales.rep.collection'
    _description = 'Sales Representative Collection'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'collection_date desc, name'

    name = fields.Char(
        string='Collection Reference',
        required=True,
        copy=False,
        index=True
    )
    visit_id = fields.Many2one(
        'sales.rep.visit',
        string='Visit',
        required=True,
        ondelete='cascade',
        check_company=True
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        related='visit_id.partner_id',
        store=True
    )
    sales_rep_id = fields.Many2one(
        'sales.representative',
        string='Sales Representative',
        related='visit_id.route_id.sales_rep_id',
        store=True
    )
    route_id = fields.Many2one(
        'sales.rep.route',
        string='Route',
        related='visit_id.route_id',
        store=True
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        compute='_compute_company_id',
        store=True,
        index=True,
        readonly=False,
        default=lambda self: self.env.company
    )

    @api.depends('route_id.company_id')
    def _compute_company_id(self):
        for collection in self:
            collection.company_id = collection.route_id.company_id
    route_customer_id = fields.Many2one(
        'sales.route.customer',
        string='Route Customer',
        related='visit_id.route_customer_id',
        store=True
    )

    # Collection Details
    collection_date = fields.Datetime(
        string='Collection Date',
        required=True,
        default=fields.Datetime.now
    )
    amount = fields.Float(
        string='Amount Collected',
        required=True,
        default=1.0,  # Set default amount to avoid validation error
        tracking=True
    )
    currency_id = fields.Many2one(
        'res.currency',
        string='Currency',
        default=lambda self: self.env.company.currency_id,
        required=True
    )

    # Payment Information
    payment_method = fields.Selection([
        ('cash', 'Cash'),
        ('check', 'Check'),
        ('bank_transfer', 'Bank Transfer'),
        ('card', 'Credit/Debit Card'),
        ('other', 'Other')
    ], string='Payment Method', required=True, default='cash')
    payment_method_id = fields.Many2one(
        'sales.rep.payment.method',
        string='Payment Method (Mobile)',
        help="Linked mobile payment method"
    )
    mobile_local_id = fields.Char(string='Mobile Local ID', index=True)


    payment_reference = fields.Char(
        string='Payment Reference',
        help='Check number, transaction ID, etc.'
    )

    # Receipt Information
    receipt_number = fields.Char(
        string='Receipt Number'
    )
    receipt_image = fields.Binary(
        string='Receipt Image'
    )
    receipt_filename = fields.Char(
        string='Receipt Filename'
    )

    # Invoice Information
    invoice_ids = fields.Many2many(
        'account.move',
        string='Related Invoices',
        domain="[('partner_id', '=', partner_id), ('move_type', '=', 'out_invoice'), ('state', '=', 'posted')]"
    )
    is_partial_payment = fields.Boolean(
        string='Partial Payment',
        default=False
    )

    # Status and Approval
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('confirmed', 'Confirmed'),
        ('reconciled', 'Reconciled'),
        ('cancelled', 'Cancelled')
    ], string='Status', default='draft', tracking=True)

    # Approval workflow
    submitted_by = fields.Many2one(
        'res.users',
        string='Submitted By'
    )
    submitted_date = fields.Datetime(
        string='Submitted Date'
    )
    confirmed_by = fields.Many2one(
        'res.users',
        string='Confirmed By'
    )
    confirmed_date = fields.Datetime(
        string='Confirmed Date'
    )

    # Accounting Integration
    payment_id = fields.Many2one(
        'account.payment',
        string='Accounting Payment',
        readonly=True
    )
    bank_statement_line_id = fields.Many2one(
        'account.bank.statement.line',
        string='Bank Statement Line'
    )

    # Notes and Comments
    notes = fields.Text(
        string='Collection Notes'
    )
    rejection_reason = fields.Text(
        string='Rejection Reason'
    )

    # Computed Fields
    outstanding_amount = fields.Float(
        string='Outstanding Amount',
        compute='_compute_outstanding_amount'
    )
    collection_efficiency = fields.Float(
        string='Collection Efficiency (%)',
        compute='_compute_collection_efficiency'
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name'):
                vals['name'] = self.env['ir.sequence'].next_by_code('sales.rep.collection') or _('New')
            if not vals.get('receipt_number'):
                vals['receipt_number'] = self.env['ir.sequence'].next_by_code('sales.rep.receipt') or _('New')
        records = super().create(vals_list)
        records._sse_notify('collection_created')
        return records

    def _sse_notify(self, reason):
        """Only the collector and the reps serving these customers need to re-sync."""
        from .sse_dispatch import schedule_reps, reps_for_partners
        rep_ids = set(self.mapped('sales_rep_id').ids)
        rep_ids |= set(reps_for_partners(self.env, self.mapped('partner_id').ids))
        schedule_reps(self.env, list(rep_ids), reason)

    def write(self, vals):
        res = super().write(vals)
        self._sse_notify('collection_updated')
        return res

    def unlink(self):
        self._sse_notify('collection_deleted')
        return super().unlink()

    @api.depends('partner_id', 'amount')
    def _compute_outstanding_amount(self):
        for collection in self:
            if collection.partner_id:
                # Calculate total receivable amount for the customer
                domain = [
                    ('partner_id', '=', collection.partner_id.id),
                    ('account_id.account_type', '=', 'asset_receivable'),
                    ('reconciled', '=', False)
                ]
                receivable_lines = self.env['account.move.line'].search(domain)
                collection.outstanding_amount = sum(receivable_lines.mapped('amount_residual'))
            else:
                collection.outstanding_amount = 0.0

    @api.depends('amount', 'outstanding_amount')
    def _compute_collection_efficiency(self):
        for collection in self:
            if collection.outstanding_amount > 0:
                collection.collection_efficiency = (collection.amount / collection.outstanding_amount)
            else:
                collection.collection_efficiency = 0.0

    def action_submit(self):
        """Submit collection for approval"""
        if self.amount <= 0:
            raise ValidationError(_("Collection amount must be greater than zero."))

        self.state = 'submitted'
        self.submitted_by = self.env.user.id
        self.submitted_date = fields.Datetime.now()

        # Send notification to supervisor
        if self.sales_rep_id.supervisor_id and self.sales_rep_id.supervisor_id.user_id:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=self.sales_rep_id.supervisor_id.user_id.id,
                summary=_('Collection Approval Required'),
                note=_('Collection of %s from %s requires approval.') % (
                    self.amount, self.partner_id.name
                )
            )
        return True

    def action_confirm(self):
        """Confirm collection (supervisor/manager action)"""
        if not self.receipt_image and self.payment_method != 'bank_transfer':
            raise ValidationError(_("Receipt image is required for this payment method."))

        self.state = 'confirmed'
        self.confirmed_by = self.env.user.id
        self.confirmed_date = fields.Datetime.now()

        # Create accounting payment if needed
        if not self.payment_id:
            self._create_accounting_payment()

        return True

    def action_reject(self):
        """Reject collection"""
        if not self.rejection_reason:
            raise ValidationError(_("Please provide a rejection reason."))

        self.state = 'draft'
        return True

    def action_cancel(self):
        """Cancel collection"""
        if self.state == 'reconciled':
            raise ValidationError(_("Cannot cancel a reconciled collection."))

        self.state = 'cancelled'

        # Cancel related payment if exists
        if self.payment_id and self.payment_id.state == 'draft':
            self.payment_id.action_cancel()

        return True

    def action_reconcile(self):
        """Mark collection as reconciled with bank statement"""
        if self.state != 'confirmed':
            raise ValidationError(_("Only confirmed collections can be reconciled."))

        self.state = 'reconciled'
        return True

    def _create_accounting_payment(self):
        """Create accounting payment record"""
        if self.payment_id:
            return self.payment_id

        payment_vals = {
            'payment_type': 'inbound',
            'partner_type': 'customer',
            'partner_id': self.partner_id.id,
            'amount': self.amount,
            'currency_id': self.currency_id.id,
            'date': self.collection_date.date(),
            'memo': self.name,
            'journal_id': self._get_payment_journal().id,
        }

        # Link to invoices if specified
        if self.invoice_ids:
            payment_vals['reconciled_invoice_ids'] = [(6, 0, self.invoice_ids.ids)]

        payment = self.env['account.payment'].create(payment_vals)
        self.payment_id = payment.id

        return payment

    def _get_payment_journal(self):
        """Get appropriate journal based on payment method"""
        journal_code_map = {
            'cash': 'CSH1',
            'bank_transfer': 'BNK1',
            'check': 'BNK1',
            'card': 'BNK1',
        }

        journal_code = journal_code_map.get(self.payment_method, 'CSH1')
        journal = self.env['account.journal'].search([
            ('code', '=', journal_code),
            ('company_id', '=', self.env.company.id)
        ], limit=1)

        if not journal:
            # Fallback to first available journal of appropriate type
            journal_type = 'cash' if self.payment_method == 'cash' else 'bank'
            journal = self.env['account.journal'].search([
                ('type', '=', journal_type),
                ('company_id', '=', self.env.company.id)
            ], limit=1)

        if not journal:
            raise UserError(_("No appropriate journal found for payment method: %s") % self.payment_method)

        return journal

    @api.constrains('amount')
    def _check_amount(self):
        for record in self:
            if record.amount <= 0:
                raise ValidationError(_("Collection amount must be greater than zero."))
            if record.amount > record.outstanding_amount:
                raise ValidationError(_("Payment amount cannot exceed the amount due on customer."))

    @api.onchange('payment_method')
    def _onchange_payment_method(self):
        if self.payment_method == 'bank_transfer':
            self.payment_reference = ''
        elif self.payment_method == 'check':
            if not self.payment_reference:
                self.payment_reference = 'Check #'

    def open_related_invoices(self):
        """Open related invoices"""
        if not self.invoice_ids:
            return False

        return {
            'type': 'ir.actions.act_window',
            'name': _('Related Invoices'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.invoice_ids.ids)],
            'context': {'default_partner_id': self.partner_id.id}
        }

    def action_view_payment(self):
        """View related accounting payment"""
        if not self.payment_id:
            return False

        return {
            'type': 'ir.actions.act_window',
            'name': _('Payment'),
            'res_model': 'account.payment',
            'res_id': self.payment_id.id,
            'view_mode': 'form',
            'target': 'current'
        }


class SalesRepDailyReport(models.Model):
    _name = 'sales.rep.daily.report'
    _description = 'Sales Representative Daily Report'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'report_date desc'

    name = fields.Char(
        string='Report Reference',
        required=True,
        copy=False,
        index=True
    )
    sales_rep_id = fields.Many2one(
        'sales.representative',
        string='Sales Representative',
        required=True
    )
    report_date = fields.Date(
        string='Report Date',
        required=True,
        default=fields.Date.context_today
    )
    route_ids = fields.Many2many(
        'sales.rep.route',
        string='Routes Covered'
    )

    # Summary Statistics
    total_visits_planned = fields.Integer(
        string='Total Visits Planned',
        compute='_compute_summary_stats'
    )
    total_visits_completed = fields.Integer(
        string='Total Visits Completed',
        compute='_compute_summary_stats'
    )
    total_sales_amount = fields.Float(
        string='Total Sales Amount',
        compute='_compute_summary_stats'
    )
    total_collections = fields.Float(
        string='Total Collections',
        compute='_compute_summary_stats'
    )
    completion_rate = fields.Float(
        string='Completion Rate (%)',
        compute='_compute_summary_stats'
    )

    # Status
    state = fields.Selection([
        ('draft', 'Draft'),
        ('submitted', 'Submitted'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected')
    ], string='Status', default='draft', tracking=True)

    # Approval
    submitted_by = fields.Many2one('res.users', string='Submitted By')
    submitted_date = fields.Datetime(string='Submitted Date')
    approved_by = fields.Many2one('res.users', string='Approved By')
    approved_date = fields.Datetime(string='Approved Date')

    # Notes
    summary_notes = fields.Text(string='Summary Notes')
    challenges_faced = fields.Text(string='Challenges Faced')
    next_day_plan = fields.Text(string='Next Day Plan')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name'):
                vals['name'] = self.env['ir.sequence'].next_by_code('sales.rep.daily.report') or _('New')
        return super().create(vals_list)

    @api.depends('route_ids', 'route_ids.visit_ids')
    def _compute_summary_stats(self):
        for report in self:
            all_visits = report.route_ids.mapped('visit_ids')
            completed_visits = all_visits.filtered(lambda v: v.state == 'completed')

            report.total_visits_planned = len(all_visits)
            report.total_visits_completed = len(completed_visits)
            report.total_sales_amount = sum(completed_visits.mapped('sale_amount'))

            # Calculate collections from account.payment records linked to visits
            payments = completed_visits.mapped('payment_ids').filtered(
                lambda p: p.state in ['posted', 'in_payment']
            )
            report.total_collections = sum(payments.mapped('amount'))

            # Calculate completion rate
            if report.total_visits_planned > 0:
                report.completion_rate = (report.total_visits_completed / report.total_visits_planned)
            else:
                report.completion_rate = 0.0

    def action_submit(self):
        """Submit daily report for approval"""
        self.state = 'submitted'
        self.submitted_by = self.env.user.id
        self.submitted_date = fields.Datetime.now()

        # Notify supervisor
        if self.sales_rep_id.supervisor_id and self.sales_rep_id.supervisor_id.user_id:
            self.activity_schedule(
                'mail.mail_activity_data_todo',
                user_id=self.sales_rep_id.supervisor_id.user_id.id,
                summary=_('Daily Report Approval Required'),
                note=_('Daily report from %s for %s requires approval.') % (
                    self.sales_rep_id.name, self.report_date
                )
            )
        return True

    def action_approve(self):
        """Approve daily report"""
        self.state = 'approved'
        self.approved_by = self.env.user.id
        self.approved_date = fields.Datetime.now()
        return True

    def action_reject(self):
        """Reject daily report"""
        self.state = 'rejected'
        return True

    def action_reset_to_draft(self):
        """Reset to draft"""
        self.state = 'draft'
        return True