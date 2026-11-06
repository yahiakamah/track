# -*- coding: utf-8 -*-
from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError, ValidationError, AccessError

MANAGER_GROUP = 'customer_return_request.group_customer_return_manager'


class CustomerReturnRequest(models.Model):
    """Commercial customer return that does not originate from a Sale Order,
    Delivery Order or Customer Invoice.

    On approval it generates standard Odoo documents (an incoming picking and,
    optionally, a customer credit note) so inventory valuation and accounting
    stay consistent with the platform. No manual stock moves or journal
    entries are created.
    """

    _name = 'customer.return.request'
    _description = 'Customer Return Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'return_date desc, id desc'
    _check_company_auto = True

    # ------------------------------------------------------------------
    # Header fields
    # ------------------------------------------------------------------
    name = fields.Char(
        string='Request Number', required=True, copy=False, readonly=True,
        index=True, default=lambda self: _('New'),
    )
    partner_id = fields.Many2one(
        'res.partner', string='Customer', required=True,
        tracking=True, check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    location_dest_id = fields.Many2one(
        'stock.location', string='Destination Location', required=True,
        check_company=True, domain="[('usage', '=', 'internal')]",
        help="Destination of the returned goods.",
    )
    return_date = fields.Date(
        string='Return Date', required=True, tracking=True,
        default=fields.Date.context_today,
    )
    reference = fields.Char(string='Reference', tracking=True)
    reason_id = fields.Many2one(
        'customer.return.reason', string='Return Reason', required=True,
        tracking=True, ondelete='restrict',
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
    )
    requested_by = fields.Many2one(
        'res.users', string='Requested By', readonly=True, tracking=True,
        default=lambda self: self.env.user,
    )
    approved_by = fields.Many2one(
        'res.users', string='Approved By', readonly=True, copy=False, tracking=True,
    )
    approval_date = fields.Datetime(
        string='Approval Date', readonly=True, copy=False, tracking=True,
    )
    manager_notes = fields.Text(string='Manager Notes', tracking=True)

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        default=lambda self: self.env.company,
    )
    currency_id = fields.Many2one(
        'res.currency', string='Currency',
        compute='_compute_currency_id', store=True, readonly=True,
        help="Currency used for the credit note (customer / pricelist currency).",
    )
    company_currency_id = fields.Many2one(
        'res.currency', related='company_id.currency_id', string='Company Currency',
        help="Currency used for the inventory valuation (always the company "
             "currency).",
    )
    state = fields.Selection(
        selection=[
            ('draft', 'Draft'),
            ('submitted', 'Submitted'),
            ('waiting_approval', 'Waiting Approval'),
            ('approved', 'Approved'),
            ('rejected', 'Rejected'),
            ('done', 'Done'),
            ('cancelled', 'Cancelled'),
        ],
        string='State', default='draft', required=True, copy=False,
        tracking=True, index=True,
    )

    line_ids = fields.One2many(
        'customer.return.request.line', 'request_id', string='Return Lines',
        copy=True,
    )
    create_credit_note = fields.Boolean(
        string='Create Credit Note', default=False, tracking=True,
        help="If set, approving the request also creates a draft customer "
             "credit note using the Credit Note Unit Price of each line.",
    )

    # ------------------------------------------------------------------
    # Linked documents
    # ------------------------------------------------------------------
    picking_ids = fields.One2many(
        'stock.picking', 'customer_return_request_id', string='Pickings',
    )
    credit_note_ids = fields.One2many(
        'account.move', 'customer_return_request_id', string='Credit Notes',
    )

    picking_count = fields.Integer(compute='_compute_counts')
    credit_note_count = fields.Integer(compute='_compute_counts')
    valuation_count = fields.Integer(compute='_compute_counts')
    journal_entry_count = fields.Integer(compute='_compute_counts')
    attachment_count = fields.Integer(compute='_compute_attachment_count')

    # ------------------------------------------------------------------
    # Totals
    # ------------------------------------------------------------------
    total_qty = fields.Float(
        string='Total Quantity', compute='_compute_totals', store=True,
    )
    total_inventory_value = fields.Monetary(
        string='Total Inventory Value', compute='_compute_totals', store=True,
        currency_field='company_currency_id',
    )
    total_credit_amount = fields.Monetary(
        string='Total Credit Amount', compute='_compute_totals', store=True,
        currency_field='currency_id',
    )

    # ==================================================================
    # Compute methods
    # ==================================================================
    @api.depends('company_id', 'partner_id')
    def _compute_currency_id(self):
        for rec in self:
            pricelist = rec.partner_id.property_product_pricelist
            rec.currency_id = (
                pricelist.currency_id
                or rec.company_id.currency_id
                or self.env.company.currency_id
            )

    @api.depends('line_ids.quantity', 'line_ids.inventory_value',
                 'line_ids.price_subtotal')
    def _compute_totals(self):
        for rec in self:
            rec.total_qty = sum(rec.line_ids.mapped('quantity'))
            rec.total_inventory_value = sum(rec.line_ids.mapped('inventory_value'))
            rec.total_credit_amount = sum(rec.line_ids.mapped('price_subtotal'))

    @api.depends('picking_ids', 'credit_note_ids')
    def _compute_counts(self):
        SVL = self.env['stock.valuation.layer']
        for rec in self:
            rec.picking_count = len(rec.picking_ids)
            rec.credit_note_count = len(rec.credit_note_ids)
            svls = SVL.search([('customer_return_request_id', '=', rec.id)])
            rec.valuation_count = len(svls)
            journal_entries = rec.credit_note_ids | svls.account_move_id
            rec.journal_entry_count = len(journal_entries)

    def _compute_attachment_count(self):
        Attachment = self.env['ir.attachment']
        for rec in self:
            rec.attachment_count = Attachment.search_count([
                ('res_model', '=', self._name),
                ('res_id', '=', rec.id),
            ])

    # ==================================================================
    # CRUD
    # ==================================================================
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name') or vals['name'] == _('New'):
                company_id = vals.get('company_id') or self.env.company.id
                seq = self.env['ir.sequence'].with_company(company_id)
                vals['name'] = seq.next_by_code('customer.return.request') or _('New')
        return super().create(vals_list)

    def write(self, vals):
        """Enforce the "edit only Draft" rule for non-managers.

        Workflow transitions run in ``sudo`` (``self.env.su`` is True) so they
        are not blocked. Managers can always edit.
        """
        if not self.env.su and not self.env.user.has_group(MANAGER_GROUP):
            for rec in self:
                if rec.state != 'draft':
                    raise UserError(_(
                        "You can only edit a return request while it is in "
                        "the Draft state."
                    ))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        for rec in self:
            if rec.state not in ('draft', 'cancelled', 'rejected'):
                raise UserError(_(
                    "You can only delete return requests that are Draft, "
                    "Rejected or Cancelled."
                ))

    # ==================================================================
    # Helpers
    # ==================================================================
    def _ensure_manager(self):
        if not self.env.su and not self.env.user.has_group(MANAGER_GROUP):
            raise AccessError(_(
                "Only a Customer Return Manager can perform this action."
            ))

    def _get_customer_location(self):
        self.ensure_one()
        partner = self.partner_id.with_company(self.company_id)
        location = partner.property_stock_customer
        if not location:
            location = self.env.ref('stock.stock_location_customers',
                                    raise_if_not_found=False)
        if not location:
            raise UserError(_("No customer location could be determined."))
        return location

    def _check_approval_constraints(self):
        """Validate every line before the request can be approved."""
        self.ensure_one()
        if not self.line_ids:
            raise UserError(_("Add at least one return line before approval."))
        errors = []
        for line in self.line_ids:
            label = line.product_id.display_name or _("Undefined product")
            if not line.product_id:
                errors.append(_("A line has no product."))
                continue
            if line.quantity <= 0:
                errors.append(_("%s: quantity must be greater than zero.", label))
            if line.inventory_unit_cost <= 0:
                errors.append(_("%s: inventory unit cost must be greater than "
                                "zero.", label))
            if (self.create_credit_note
                    and line.credit_note_unit_price <= 0):
                errors.append(_("%s: credit note unit price must be greater "
                                "than zero.", label))
            if (line.product_uom_id
                    and line.product_id.uom_id.category_id
                    != line.product_uom_id.category_id):
                errors.append(_("%s: the unit of measure does not belong to "
                                "the product UoM category.", label))
            if (line.product_id.tracking != 'none' and not line.lot_id):
                errors.append(_("%s: a lot/serial number is required for "
                                "tracked products.", label))
        if errors:
            raise UserError(_("The request cannot be approved:\n\n- %s")
                            % "\n- ".join(errors))

    # ==================================================================
    # Workflow actions
    # ==================================================================
    def action_submit(self):
        for rec in self:
            if rec.state != 'draft':
                raise UserError(_("Only Draft requests can be submitted."))
            if not rec.line_ids:
                raise UserError(_("Add at least one return line before "
                                  "submitting."))
        self.sudo().write({'state': 'submitted'})
        for rec in self:
            rec.message_post(body=_("Request submitted."))
        return True

    def action_request_approval(self):
        for rec in self:
            if rec.state != 'submitted':
                raise UserError(_("Only Submitted requests can be sent for "
                                  "approval."))
        self.sudo().write({'state': 'waiting_approval'})
        for rec in self:
            rec.message_post(body=_("Request sent for approval."))
        return True

    def action_approve(self):
        self._ensure_manager()
        for rec in self:
            if rec.state != 'waiting_approval':
                raise UserError(_("Only requests in 'Waiting Approval' can be "
                                  "approved."))
            rec._check_approval_constraints()
        self.sudo().write({
            'state': 'approved',
            'approved_by': self.env.user.id,
            'approval_date': fields.Datetime.now(),
        })
        for rec in self:
            rec._create_return_picking()
            if rec.create_credit_note:
                rec._create_customer_credit_note()
            rec.message_post(body=_("Request approved by %s.",
                                    self.env.user.name))
        return True

    def action_open_reject_wizard(self):
        self._ensure_manager()
        self.ensure_one()
        if self.state not in ('submitted', 'waiting_approval'):
            raise UserError(_("Only Submitted or Waiting Approval requests can "
                              "be rejected."))
        return {
            'type': 'ir.actions.act_window',
            'name': _("Reject Return Request"),
            'res_model': 'customer.return.reject.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_request_id': self.id},
        }

    def action_reject(self, reason=False):
        self._ensure_manager()
        for rec in self:
            if rec.state not in ('submitted', 'waiting_approval'):
                raise UserError(_("This request cannot be rejected in its "
                                  "current state."))
        self.sudo().write({'state': 'rejected'})
        for rec in self:
            body = _("Request rejected by %s.", self.env.user.name)
            if reason:
                body += _("<br/>Reason: %s", reason)
            rec.message_post(body=body)
        return True

    def action_done(self):
        self._ensure_manager()
        for rec in self:
            if rec.state != 'approved':
                raise UserError(_("Only Approved requests can be marked Done."))
        self.sudo().write({'state': 'done'})
        for rec in self:
            rec.message_post(body=_("Request marked as Done."))
        return True

    def _on_picking_done(self):
        """Called from stock.picking when the incoming transfer is validated."""
        to_done = self.filtered(lambda r: r.state == 'approved')
        if to_done:
            to_done.sudo().write({'state': 'done'})
            for rec in to_done:
                rec.message_post(body=_("Incoming transfer validated - "
                                        "request automatically set to Done."))

    def action_cancel(self):
        self._ensure_manager()
        for rec in self:
            if rec.state in ('done', 'cancelled'):
                raise UserError(_("This request can no longer be cancelled."))
            # Cancel the linked pickings that are not done yet.
            pickings = rec.picking_ids.filtered(
                lambda p: p.state not in ('done', 'cancel'))
            if pickings:
                pickings.action_cancel()
        self.sudo().write({'state': 'cancelled'})
        for rec in self:
            rec.message_post(body=_("Request cancelled by %s.",
                                    self.env.user.name))
        return True

    def action_reset_to_draft(self):
        self._ensure_manager()
        for rec in self:
            if rec.state not in ('rejected', 'cancelled'):
                raise UserError(_("Only Rejected or Cancelled requests can be "
                                  "reset to Draft."))
        self.sudo().write({
            'state': 'draft',
            'approved_by': False,
            'approval_date': False,
        })
        for rec in self:
            rec.message_post(body=_("Request reset to Draft."))
        return True

    # ==================================================================
    # Document generation (standard Odoo flows only)
    # ==================================================================
    def _create_return_picking(self):
        """Create a standard incoming picking Customer -> Warehouse input.

        No custom stock moves: we rely on the standard picking workflow. The
        inventory cost is injected through ``stock.move._get_price_unit`` (see
        the ``stock_move`` model).
        """
        self.ensure_one()
        picking_type = self.location_dest_id.warehouse_id.in_type_id
        if not picking_type:
            picking_type = self.env['stock.picking.type'].search([
                ('company_id', '=', self.company_id.id),
                ('code', '=', 'incoming'),
            ], limit=1)
        if not picking_type:
            raise UserError(_("No incoming operation type could be found."))
        src_location = self._get_customer_location()
        dest_location = self.location_dest_id

        move_cmds = [
            Command.create(line._prepare_stock_move_vals(
                picking_type, src_location, dest_location))
            for line in self.line_ids
        ]
        picking = self.env['stock.picking'].create({
            'partner_id': self.partner_id.id,
            'picking_type_id': picking_type.id,
            'location_id': src_location.id,
            'location_dest_id': dest_location.id,
            'origin': self.name,
            'company_id': self.company_id.id,
            'customer_return_request_id': self.id,
            'move_ids': move_cmds,
        })
        picking.action_confirm()
        picking.action_assign()
        # Propagate the chosen lot/serial to the reserved move lines.
        for move in picking.move_ids:
            crl = move.customer_return_line_id
            if crl and crl.lot_id and move.move_line_ids:
                move.move_line_ids[:1].lot_id = crl.lot_id.id
        return picking

    def _prepare_credit_note_vals(self):
        self.ensure_one()
        return {
            'move_type': 'out_refund',
            'partner_id': self.partner_id.id,
            'currency_id': self.currency_id.id,
            'invoice_date': fields.Date.context_today(self),
            'invoice_origin': self.name,
            'company_id': self.company_id.id,
            'customer_return_request_id': self.id,
            'invoice_line_ids': [
                Command.create(line._prepare_credit_note_line_vals())
                for line in self.line_ids
            ],
        }

    def _create_customer_credit_note(self):
        """Create a standard customer credit note (draft).

        Taxes, accounts and receivable lines are computed by the standard
        ``account.move`` workflow - we never post manual receivable entries.
        """
        self.ensure_one()
        move = self.env['account.move'].create(self._prepare_credit_note_vals())
        self.message_post(body=_("Customer credit note %s created.",
                                 move.name or move.display_name))
        return move

    # ==================================================================
    # Smart buttons
    # ==================================================================
    def action_view_pickings(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'stock.action_picking_tree_all')
        pickings = self.picking_ids
        if len(pickings) == 1:
            action.update({'view_mode': 'form', 'res_id': pickings.id,
                           'views': [(False, 'form')]})
        else:
            action['domain'] = [('id', 'in', pickings.ids)]
        action['context'] = {'default_customer_return_request_id': self.id}
        return action

    def action_view_credit_notes(self):
        self.ensure_one()
        action = self.env['ir.actions.actions']._for_xml_id(
            'account.action_move_out_refund_type')
        moves = self.credit_note_ids
        if len(moves) == 1:
            action.update({'view_mode': 'form', 'res_id': moves.id,
                           'views': [(False, 'form')]})
        else:
            action['domain'] = [('id', 'in', moves.ids)]
        return action

    def action_view_valuation(self):
        self.ensure_one()
        svls = self.env['stock.valuation.layer'].search([
            ('customer_return_request_id', '=', self.id)])
        return {
            'type': 'ir.actions.act_window',
            'name': _("Stock Valuation"),
            'res_model': 'stock.valuation.layer',
            'view_mode': 'list,form',
            'domain': [('id', 'in', svls.ids)],
        }

    def action_view_journal_entries(self):
        self.ensure_one()
        svls = self.env['stock.valuation.layer'].search([
            ('customer_return_request_id', '=', self.id)])
        moves = self.credit_note_ids | svls.account_move_id
        return {
            'type': 'ir.actions.act_window',
            'name': _("Journal Entries"),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', moves.ids)],
        }

    def action_view_attachments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Attachments"),
            'res_model': 'ir.attachment',
            'view_mode': 'kanban,list,form',
            'domain': [('res_model', '=', self._name), ('res_id', '=', self.id)],
            'context': {
                'default_res_model': self._name,
                'default_res_id': self.id,
            },
        }
