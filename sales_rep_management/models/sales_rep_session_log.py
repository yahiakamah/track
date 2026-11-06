# -*- coding: utf-8 -*-
from odoo import models, fields, api

class SalesRepresentativeSessionLog(models.Model):
    _name = 'sales.representative.session.log'
    _description = 'Sales Representative Session Log'
    _order = 'timestamp desc'

    sales_rep_id = fields.Many2one(
        'sales.representative',
        string='Sales Representative',
        required=True,
        ondelete='cascade',
        readonly=True,
        index=True
    )
    action_type = fields.Selection([
        ('login', 'Login'),
        ('logout', 'Logout'),
        ('forced_logout', 'Forced Log Out')
    ], string='Action Type', required=True, readonly=True)
    timestamp = fields.Datetime(
        string='Time',
        required=True,
        default=fields.Datetime.now,
        readonly=True
    )
    latitude = fields.Float(
        string='Latitude',
        digits=(10, 6),
        readonly=True
    )
    longitude = fields.Float(
        string='Longitude',
        digits=(10, 6),
        readonly=True
    )
    device_model = fields.Char(
        string='Device Model',
        readonly=True
    )
    mac_address = fields.Char(
        string='MAC Address',
        readonly=True
    )
    session_identifier = fields.Char(
        string='Session Identifier',
        readonly=True
    )
    session_sequence = fields.Char(
        string='Session Reference',
        compute='_compute_session_sequence'
    )
    device_identifier = fields.Char(
        string='Device Identifier',
        readonly=True
    )

    @api.depends('sales_rep_id', 'session_identifier', 'timestamp')
    def _compute_session_sequence(self):
        # Group records by sales_rep_id to batch fetch and compute
        reps = self.mapped('sales_rep_id')
        
        # Batch search all logs for the involved sales reps
        all_logs_by_rep = {}
        if reps:
            logs = self.env['sales.representative.session.log'].search([
                ('sales_rep_id', 'in', reps.ids),
                ('session_identifier', '!=', False)
            ], order='timestamp asc')
            for l in logs:
                all_logs_by_rep.setdefault(l.sales_rep_id.id, []).append(l.session_identifier)
        
        # Build mapping of session_identifier -> sequence number for each rep
        session_map_by_rep = {}
        for rep_id, session_list in all_logs_by_rep.items():
            unique_sessions = []
            for s_id in session_list:
                if s_id not in unique_sessions:
                    unique_sessions.append(s_id)
            session_map_by_rep[rep_id] = {s_id: idx + 1 for idx, s_id in enumerate(unique_sessions)}
            
        for log in self:
            if log.sales_rep_id and log.session_identifier:
                rep_map = session_map_by_rep.get(log.sales_rep_id.id, {})
                seq = rep_map.get(log.session_identifier)
                if seq:
                    log.session_sequence = f"Session #{seq}"
                else:
                    log.session_sequence = "New Session"
            else:
                log.session_sequence = ""


    def view_on_map(self):
        self.ensure_one()
        if self.latitude and self.longitude:
            url = f"https://www.google.com/maps/search/?api=1&query={self.latitude},{self.longitude}"
            return {
                'type': 'ir.actions.act_url',
                'url': url,
                'target': 'new',
            }
        return False
