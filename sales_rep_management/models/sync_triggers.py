# -*- coding: utf-8 -*-
import logging
from odoo import models, api

from .sse_dispatch import (
    schedule_all, schedule_reps,
    reps_for_partners, reps_for_journals,
)

_logger = logging.getLogger(__name__)

def notify_sse_all_postcommit(env, reason):
    """Broadcast to all reps once per transaction (master data only)."""
    schedule_all(env, reason)

class ProductTemplate(models.Model):
    _inherit = 'product.template'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'product_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'product_updated')
        return res

    def unlink(self):
        res = super().unlink()
        notify_sse_all_postcommit(self.env, 'product_updated')
        return res

class ProductPricelist(models.Model):
    _inherit = 'product.pricelist'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'pricelist_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'pricelist_updated')
        return res

    def unlink(self):
        res = super().unlink()
        notify_sse_all_postcommit(self.env, 'pricelist_updated')
        return res

class ProductPricelistItem(models.Model):
    _inherit = 'product.pricelist.item'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'pricelist_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'pricelist_updated')
        return res

    def unlink(self):
        res = super().unlink()
        notify_sse_all_postcommit(self.env, 'pricelist_updated')
        return res

class LoyaltyProgram(models.Model):
    _inherit = 'loyalty.program'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

    def unlink(self):
        res = super().unlink()
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

class LoyaltyRule(models.Model):
    _inherit = 'loyalty.rule'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

    def unlink(self):
        res = super().unlink()
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

class LoyaltyReward(models.Model):
    _inherit = 'loyalty.reward'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

    def unlink(self):
        res = super().unlink()
        notify_sse_all_postcommit(self.env, 'promotion_updated')
        return res

class AccountPaymentTerm(models.Model):
    _inherit = 'account.payment.term'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'payment_term_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'payment_term_updated')
        return res

class ResPartner(models.Model):
    _inherit = 'res.partner'

    # Fields the mobile app actually shows; other writes do not trigger a sync.
    _SSE_PARTNER_FIELDS = {
        'name', 'street', 'street2', 'city', 'state_id', 'country_id', 'area', 'phone',
        'mobile', 'email', 'is_cash', 'visit_latitude', 'visit_longitude',
        'property_product_pricelist', 'property_payment_term_id', 'credit_limit',
        'enable_location', 'location_radius', 'category_id', 'active',
        'sale_credit_limit', 'allow_over_sale_credit',
    }

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        schedule_reps(self.env, reps_for_partners(self.env, res.ids), 'partner_created')
        return res

    def write(self, vals):
        res = super().write(vals)
        if self._SSE_PARTNER_FIELDS.intersection(vals):
            schedule_reps(self.env, reps_for_partners(self.env, self.ids), 'partner_updated')
        return res

# Written by the mobile access policy on every login / hour of use: no SSE, no catalog reset.
TECHNICAL_REP_FIELDS = {'mobile_access_token', 'mobile_token_expiry', 'mobile_device_id', 'mobile_device_model', 'mobile_last_seen', 'catalog_changed_at'}


class SalesRepresentative(models.Model):
    _inherit = 'sales.representative'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        schedule_reps(self.env, res.ids, 'sales_rep_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        # Token rotation / technical writes must not wake the device up.
        if set(vals) - TECHNICAL_REP_FIELDS:
            schedule_reps(self.env, self.ids, 'sales_rep_updated')
        return res

    def _register_hook(self):
        res = super()._register_hook()
        if 'customer.return.reason' in self.env:
            model_class = self.env['customer.return.reason'].__class__
            if not hasattr(model_class, '_patched_for_sync_triggers'):
                original_create = model_class.create
                original_write = model_class.write
                original_unlink = model_class.unlink
                
                @api.model_create_multi
                def new_create(cls_self, vals_list):
                    records = original_create(cls_self, vals_list)
                    notify_sse_all_postcommit(cls_self.env, 'return_reason_updated')
                    return records
                
                def new_write(cls_self, vals):
                    records_written = original_write(cls_self, vals)
                    notify_sse_all_postcommit(cls_self.env, 'return_reason_updated')
                    return records_written
                
                def new_unlink(cls_self):
                    records_unlinked = original_unlink(cls_self)
                    notify_sse_all_postcommit(cls_self.env, 'return_reason_updated')
                    return records_unlinked
                
                model_class.create = new_create
                model_class.write = new_write
                model_class.unlink = new_unlink
                model_class._patched_for_sync_triggers = True
        return res

class SaleOrderSyncTrigger(models.Model):
    _inherit = 'sale.order'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        schedule_reps(self.env, res.mapped('sales_rep_id').ids, 'sale_order_updated')
        return res

    def write(self, vals):
        reps_before = self.mapped('sales_rep_id').ids
        res = super().write(vals)
        schedule_reps(self.env, reps_before + self.mapped('sales_rep_id').ids, 'sale_order_updated')
        return res

class SalesRepPaymentMethodInherit(models.Model):
    _inherit = 'sales.rep.payment.method'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'payment_method_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'payment_method_updated')
        return res

    def unlink(self):
        res = super().unlink()
        notify_sse_all_postcommit(self.env, 'payment_method_updated')
        return res

class SalesRepReturnReasonInherit(models.Model):
    _inherit = 'sales.rep.return.reason'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        notify_sse_all_postcommit(self.env, 'return_reason_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        notify_sse_all_postcommit(self.env, 'return_reason_updated')
        return res

# --- Balance Sync Triggers ---

def _reps_for_moves(env, moves):
    rep_ids = set(moves.mapped('sales_rep_id').ids)
    rep_ids |= set(reps_for_journals(env, moves.mapped('journal_id').ids))
    customer_moves = moves.filtered(lambda m: m.move_type in ('out_invoice', 'out_refund', 'out_receipt', 'entry'))
    rep_ids |= set(reps_for_partners(env, customer_moves.mapped('partner_id').ids))
    return list(rep_ids)


class AccountMoveBalanceTrigger(models.Model):
    _inherit = 'account.move'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        schedule_reps(self.env, _reps_for_moves(self.env, res), 'move_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in ['state', 'amount_total', 'journal_id', 'date', 'payment_state', 'user_id']):
            schedule_reps(self.env, _reps_for_moves(self.env, self), 'move_updated')
        return res

    def unlink(self):
        rep_ids = _reps_for_moves(self.env, self)
        res = super().unlink()
        schedule_reps(self.env, rep_ids, 'move_deleted')
        return res


def _reps_for_payments(env, payments):
    rep_ids = set(payments.mapped('sales_rep_id').ids)
    rep_ids |= set(reps_for_journals(env, payments.mapped('journal_id').ids))
    rep_ids |= set(reps_for_partners(env, payments.mapped('partner_id').ids))
    return list(rep_ids)


class AccountPaymentBalanceTrigger(models.Model):
    _inherit = 'account.payment'

    @api.model_create_multi
    def create(self, vals_list):
        res = super().create(vals_list)
        schedule_reps(self.env, _reps_for_payments(self.env, res), 'payment_updated')
        return res

    def write(self, vals):
        res = super().write(vals)
        if any(f in vals for f in ['state', 'amount', 'journal_id', 'date', 'partner_id']):
            schedule_reps(self.env, _reps_for_payments(self.env, self), 'payment_updated')
        return res

    def unlink(self):
        rep_ids = _reps_for_payments(self.env, self)
        res = super().unlink()
        schedule_reps(self.env, rep_ids, 'payment_deleted')
        return res
