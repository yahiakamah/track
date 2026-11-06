# -*- coding: utf-8 -*-
"""Manager mobile app: sessions, scope and the representatives' last position.

* ``sales.rep.manager.session`` — one row per signed-in manager phone
  (token kept as a SHA-256 hash, device binding and expiry like the
  representative app).
* Scope: a Sales Manager sees every representative of his companies; a
  supervisor sees his whole team (any depth, ``_team_reps``).
* ``sales.representative.last_*`` — last known position, refreshed by every
  sync / working-day event of the representative app (the live map of the
  manager app reads it; Traccar stays optional).
"""
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from .mobile_access import binding_enabled, token_days


class SalesRepManagerSession(models.Model):
    _name = 'sales.rep.manager.session'
    _description = 'Manager App Session'
    _order = 'last_seen desc, id desc'

    user_id = fields.Many2one('res.users', required=True, ondelete='cascade', index=True)
    token_hash = fields.Char(required=True, index=True, copy=False, groups='base.group_system')
    device_id = fields.Char(index=True)
    device_model = fields.Char()
    expiry = fields.Datetime()
    last_seen = fields.Datetime()
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', related='user_id.company_id', store=True)

    @api.model
    def _bind(self, user, token_hash, device_id=None, device_model=None, master_expiry=None):
        """New login: the previous sessions of this user are closed (one phone at a time)."""
        days = token_days(self.env)
        expiry = fields.Datetime.now() + timedelta(days=days) if days else False
        if master_expiry and (not expiry or master_expiry < expiry):
            expiry = master_expiry
        Session = self.sudo()
        Session.search([('user_id', '=', user.id)]).write({'active': False})
        Session.create({'user_id': user.id, 'token_hash': token_hash, 'device_id': device_id or False,
                        'device_model': device_model or False, 'expiry': expiry, 'last_seen': fields.Datetime.now()})
        return expiry

    def _check(self, device_id=None):
        """None when the request may go on, else the refusal code."""
        self.ensure_one()
        now = fields.Datetime.now()
        if self.expiry and self.expiry < now:
            return 'token_expired'
        if binding_enabled(self.env) and self.device_id and device_id and device_id != self.device_id:
            return 'other_device'
        if not self.last_seen or now - self.last_seen > timedelta(hours=1):
            vals = {'last_seen': now}
            days = token_days(self.env)
            if days and self.expiry:
                vals['expiry'] = max(self.expiry, now + timedelta(days=days))
            self.sudo().write(vals)
        if not self.user_id._srm_is_manager_app_user():
            return 'not_manager'
        return None

    def action_revoke(self):
        self.sudo().write({'active': False})
        return True


class ResUsers(models.Model):
    _inherit = 'res.users'

    def _srm_is_manager_app_user(self):
        self.ensure_one()
        return self.has_group('sales_rep_management.group_sales_manager') or \
            self.has_group('sales_rep_management.group_sales_supervisor')

    def _srm_scope_reps(self):
        """Representatives this user may follow in the manager app."""
        self.ensure_one()
        Rep = self.env['sales.representative'].sudo()
        companies = self.company_ids.ids or [self.company_id.id]
        if self.has_group('sales_rep_management.group_sales_manager'):
            return Rep.search([('company_id', 'in', companies)])
        own = Rep.search([('user_id', '=', self.id)], limit=1)
        if own and self.has_group('sales_rep_management.group_sales_supervisor'):
            return own._team_reps() - own
        return Rep

    def _srm_check_rep(self, rep_id):
        rep = self._srm_scope_reps().filtered(lambda r: r.id == int(rep_id or 0))
        if not rep:
            raise AccessError(_('This representative is not in your team.'))
        return rep


class SalesRepresentativePosition(models.Model):
    _inherit = 'sales.representative'

    last_latitude = fields.Float(digits=(10, 7), readonly=True, copy=False)
    last_longitude = fields.Float(digits=(10, 7), readonly=True, copy=False)
    last_location_at = fields.Datetime(readonly=True, copy=False)
    last_location_source = fields.Char(readonly=True, copy=False)

    def _set_position(self, lat, lng, when=None, source='app'):
        """Keep the newest known position (an old offline point never wins)."""
        try:
            lat, lng = float(lat or 0.0), float(lng or 0.0)
        except (TypeError, ValueError):
            return
        if not lat and not lng:
            return
        when = when or fields.Datetime.now()
        for rep in self:
            if rep.last_location_at and rep.last_location_at > when:
                continue
            rep.sudo().with_context(tracking_disable=True, skip_srm_sse=True).write({
                'last_latitude': lat, 'last_longitude': lng,
                'last_location_at': when, 'last_location_source': source,
            })
