# -*- coding: utf-8 -*-
"""Mobile access policy on the tenant: token lifetime and device binding.

* Every login binds the rep to ONE phone (device id sent by the app) and gives
  the token a lifetime (Settings > Mobile access days, default 30, 0 = no limit).
* The lifetime slides: while the rep keeps using the app the expiry is pushed
  forward (at most one write per hour), so an active rep is never thrown out,
  and a lost or unused phone stops working on its own.
* A request with a valid token but from another device id is refused.
* "Revoke Mobile Access" on the rep cuts the phone off immediately.
"""
from datetime import timedelta

from odoo import _, fields, models

PARAM_DAYS = 'sales_rep_management.mobile_token_days'
PARAM_BIND = 'sales_rep_management.mobile_device_binding'


def token_days(env):
    try:
        return int(env['ir.config_parameter'].sudo().get_param(PARAM_DAYS, '30') or 0)
    except ValueError:
        return 30


def binding_enabled(env):
    return env['ir.config_parameter'].sudo().get_param(PARAM_BIND, 'True') not in ('False', '0', '')


# Rep settings that change which products the phone must hold.
CATALOG_FIELDS = {'product_category_ids', 'default_location_id', 'default_location_ids', 'discount_product_id',
                  'company_id', 'return_location_id', 'return_location_ids'}


class SalesRepresentativeMobileAccess(models.Model):
    _inherit = 'sales.representative'

    catalog_changed_at = fields.Datetime(copy=False, readonly=True,
        help="Last change of a setting that decides the rep's product catalog (forces a full catalog download).")

    def write(self, vals):
        if CATALOG_FIELDS & set(vals) and 'catalog_changed_at' not in vals:
            vals = dict(vals, catalog_changed_at=fields.Datetime.now())
        return super().write(vals)

    def _mobile_bind(self, token_hash, device_id=None, device_model=None, master_expiry=None):
        """Store a new login: token hash, device and expiry."""
        self.ensure_one()
        days = token_days(self.env)
        expiry = fields.Datetime.now() + timedelta(days=days) if days else False
        if master_expiry and (not expiry or master_expiry < expiry):
            expiry = master_expiry  # never outlive what the master server granted
        self.sudo().with_context(tracking_disable=True).write({
            'mobile_access_token': token_hash,
            'mobile_device_id': device_id or False,
            'mobile_device_model': device_model or False,
            'mobile_token_expiry': expiry,
            'mobile_last_seen': fields.Datetime.now(),
        })
        return expiry

    def _mobile_check(self, device_id=None):
        """None when the request may go on, else the refusal code."""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.mobile_token_expiry and self.mobile_token_expiry < now:
            return 'token_expired'
        if binding_enabled(self.env) and self.mobile_device_id and device_id and device_id != self.mobile_device_id:
            return 'other_device'
        vals = {}
        if not self.mobile_last_seen or (now - self.mobile_last_seen) > timedelta(hours=1):
            vals['mobile_last_seen'] = now
            days = token_days(self.env)
            if days and self.mobile_token_expiry:
                vals['mobile_token_expiry'] = max(self.mobile_token_expiry, now + timedelta(days=days))
        if vals:
            self.sudo().with_context(tracking_disable=True).write(vals)
        return None

    def action_revoke_mobile_access(self):
        for rep in self:
            rep.sudo().with_context(tracking_disable=True).write({
                'mobile_access_token': False, 'mobile_device_id': False, 'mobile_token_expiry': False})
            rep.message_post(body=_("Mobile access revoked by %s.", self.env.user.name))
            try:
                from odoo.addons.sales_rep_management.models.sse_dispatch import schedule_reps
                schedule_reps(self.env, [rep.id], 'access_revoked', event_type='force_logout')
            except Exception:
                pass
        return True
