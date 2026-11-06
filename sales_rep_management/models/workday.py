# -*- coding: utf-8 -*-
"""Rep working day: explicit Start / End from the app, first / last visit as fallback."""
from datetime import datetime, time, timedelta

import pytz

from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SalesRepWorkday(models.Model):
    _name = 'sales.rep.workday'
    _description = 'Sales Rep Working Day'
    _order = 'date desc, sales_rep_id'
    _rec_name = 'date'

    sales_rep_id = fields.Many2one('sales.representative', required=True, index=True, ondelete='cascade')
    company_id = fields.Many2one(related='sales_rep_id.company_id', store=True)
    supervisor_id = fields.Many2one(related='sales_rep_id.supervisor_id', store=True)
    date = fields.Date(required=True, index=True)
    start_time = fields.Datetime()
    end_time = fields.Datetime()
    start_latitude = fields.Float(digits=(10, 6))
    start_longitude = fields.Float(digits=(10, 6))
    end_latitude = fields.Float(digits=(10, 6))
    end_longitude = fields.Float(digits=(10, 6))
    start_source = fields.Selection([('app', 'Start button'), ('visit', 'First visit')], readonly=True)
    end_source = fields.Selection([('app', 'End button'), ('visit', 'Last visit'), ('day_close', 'Day close')], readonly=True)
    duration_hours = fields.Float(compute='_compute_duration', store=True, digits=(16, 2))
    visit_count = fields.Integer(compute='_compute_visits')

    _sql_constraints = [('rep_date_uniq', 'unique(sales_rep_id, date)', 'One working day per representative and date.')]

    @api.depends('start_time', 'end_time')
    def _compute_duration(self):
        for d in self:
            d.duration_hours = ((d.end_time - d.start_time).total_seconds() / 3600.0
                                if d.start_time and d.end_time and d.end_time > d.start_time else 0.0)

    def _visits(self):
        self.ensure_one()
        start, end = self._day_bounds(self.sales_rep_id, self.date)
        return self.env['sales.rep.visit'].sudo().search([
            ('sales_rep_id', '=', self.sales_rep_id.id), ('visit_time', '>=', start), ('visit_time', '<', end)])

    def _compute_visits(self):
        for d in self:
            d.visit_count = len(d._visits()) if d.sales_rep_id and d.date else 0

    @api.model
    def _day_bounds(self, rep, day):
        tz = pytz.timezone(rep.user_id.tz or rep.company_id.partner_id.tz or 'UTC')
        start = tz.localize(datetime.combine(day, time.min)).astimezone(pytz.utc).replace(tzinfo=None)
        return start, start + timedelta(days=1)

    @api.model
    def _today(self, rep):
        return fields.Date.context_today(rep.user_id or self.env.user)

    @api.model
    def get_or_create(self, rep, day=None):
        day = day or self._today(rep)
        wd = self.search([('sales_rep_id', '=', rep.id), ('date', '=', day)], limit=1)
        return wd or self.create({'sales_rep_id': rep.id, 'date': day})

    # ------------------------------------------------------------------ app
    def action_start(self, when=None, lat=0.0, lng=0.0):
        self.ensure_one()
        if self.start_time and self.start_source == 'app':
            return self  # replay / second tap: keep the first start
        self.write({'start_time': when or fields.Datetime.now(), 'start_latitude': lat or 0.0,
                    'start_longitude': lng or 0.0, 'start_source': 'app'})
        return self

    def action_end(self, when=None, lat=0.0, lng=0.0, source='app'):
        self.ensure_one()
        if not self.start_time:
            raise UserError(_('Start your working day first.'))
        if self.end_time and self.end_source == 'app':
            return self
        when = when or fields.Datetime.now()
        if when < self.start_time:
            raise UserError(_('The end of the day cannot be before its start.'))
        self.write({'end_time': when, 'end_latitude': lat or 0.0, 'end_longitude': lng or 0.0, 'end_source': source})
        return self

    def payload(self):
        self.ensure_one()
        return {
            'date': self.date, 'start_time': self.start_time, 'end_time': self.end_time,
            'start_source': self.start_source or False, 'end_source': self.end_source or False,
            'duration_hours': round(self.duration_hours, 2), 'started': bool(self.start_time),
            'ended': bool(self.end_time),
        }

    # ------------------------------------------------------------------ fallback
    @api.model
    def _cron_fill_from_visits(self, days=2):
        """Days without Start / End buttons: first and last visit of the day."""
        today = fields.Date.context_today(self)
        Visit = self.env['sales.rep.visit'].sudo()
        for rep in self.env['sales.representative'].sudo().search([('active', '=', True)]):
            for offset in range(1, days + 1):
                day = today - timedelta(days=offset)
                start, end = self._day_bounds(rep, day)
                visits = Visit.search([('sales_rep_id', '=', rep.id), ('visit_time', '>=', start),
                                       ('visit_time', '<', end)], order='visit_time')
                wd = self.search([('sales_rep_id', '=', rep.id), ('date', '=', day)], limit=1)
                if not visits and not wd:
                    continue
                wd = wd or self.create({'sales_rep_id': rep.id, 'date': day})
                vals = {}
                if visits and not wd.start_time:
                    first = visits[0]
                    vals.update(start_time=first.planned_time or first.visit_time, start_source='visit')
                if visits and not wd.end_time:
                    vals.update(end_time=visits[-1].visit_time, end_source='visit')
                if vals:
                    wd.write(vals)
