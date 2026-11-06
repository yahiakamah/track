# -*- coding: utf-8 -*-
"""
Phase 4: configurable mobile home grid and department requests.

* Home tiles: the supervisor chooses which icons a representative sees on the
  app's home screen and in which order (sales.rep.home.tile).
* Department requests: advance, mission, vehicle maintenance, route change,
  customer data change... sent from the app, approved in Odoo, with an inbox
  of requests that need the representative's answer.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError

HOME_TILES = [
    ('visit', 'Visit'),
    ('collect', 'Collect'),
    ('invoice', 'Invoice'),
    ('stock', 'Stock'),
    ('orders', 'Orders'),
    ('customers', 'Customers'),
    ('requests', 'Requests'),
    ('tasks', 'Tasks'),
    ('van_loads', 'Van Loads'),
    ('day_close', 'Day Close'),
    ('kpis', 'My KPIs'),
    ('returns', 'Returns'),
    ('reports', 'Reports'),
    ('offers', 'Offers'),
    ('route_map', 'Route Map'),
    ('team', 'My Team'),
]
DEFAULT_TILES = ['visit', 'collect', 'invoice', 'stock', 'orders', 'customers',
                 'requests', 'tasks', 'offers', 'day_close', 'kpis']
# Tiles hidden when the matching access right is off.
TILE_ACCESS = {
    'collect': 'access_visit_payment',
    'invoice': 'access_visit_order',
    'stock': 'access_storage',
    'returns': 'access_general_return',
    'team': 'is_supervisor',
}


class SalesRepHomeTile(models.Model):
    _name = 'sales.rep.home.tile'
    _description = 'Mobile Home Tile'
    _order = 'sequence, id'

    sales_rep_id = fields.Many2one('sales.representative', required=True, ondelete='cascade', index=True)
    tile = fields.Selection(HOME_TILES, required=True)
    sequence = fields.Integer(default=10)

    _sql_constraints = [('rep_tile_uniq', 'unique(sales_rep_id, tile)', 'Each tile only once per representative.')]


class SalesRepresentative(models.Model):
    _inherit = 'sales.representative'

    home_tile_ids = fields.One2many('sales.rep.home.tile', 'sales_rep_id', string='Mobile Home Tiles')
    dept_request_ids = fields.One2many('sales.rep.dept.request', 'sales_rep_id', string='Department Requests')

    def _get_home_tiles(self):
        """Ordered tile keys for the app, filtered by the rep's access rights."""
        self.ensure_one()
        keys = self.home_tile_ids.sorted('sequence').mapped('tile') or list(DEFAULT_TILES)
        if self.is_supervisor and 'team' not in keys:
            keys = ['team'] + keys  # supervisors always get their team first
        return [k for k in keys if not TILE_ACCESS.get(k) or self[TILE_ACCESS[k]]]

    def action_reset_home_tiles(self):
        for rep in self:
            rep.home_tile_ids.unlink()
            rep.home_tile_ids = [(0, 0, {'tile': key, 'sequence': i}) for i, key in enumerate(DEFAULT_TILES)]
        return True


class SalesRepDeptRequestType(models.Model):
    _name = 'sales.rep.dept.request.type'
    _description = 'Department Request Type'
    _order = 'sequence, name'

    name = fields.Char(required=True, translate=True)
    code = fields.Selection([
        ('advance', 'Advance (سلفة)'),
        ('mission', 'Mission (مأمورية)'),
        ('maintenance', 'Vehicle maintenance'),
        ('route_change', 'Route change'),
        ('customer_change', 'Customer data change'),
        ('customer_complaint', 'Customer complaint'),
        ('customer_request', 'Customer request'),
        ('other', 'Other'),
    ], required=True, default='other')
    category = fields.Selection([
        ('internal', 'Rep request to a department'),
        ('customer', 'On behalf of a customer'),
    ], default='internal', required=True,
        help="Customer complaints and requests are registered by the rep for a customer and need the customer.")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    department_id = fields.Many2one('hr.department', string='Department')
    approver_id = fields.Many2one('res.users', string='Approver',
                                  help="Empty: the representative's supervisor approves.")
    needs_amount = fields.Boolean(string='Amount Required')
    needs_dates = fields.Boolean(string='Dates Required')
    needs_vehicle = fields.Boolean(string='Vehicle Required')
    needs_customer = fields.Boolean(string='Customer Required')
    icon = fields.Char(default='document-text-outline', help="Ionicons name shown in the app.")

    @api.model
    def _fix_categories(self):
        """Upgrade helper: types created before 'category' existed."""
        self.search([('code', 'in', ('customer_change', 'customer_complaint', 'customer_request')),
                     ('category', '!=', 'customer')]).write({'category': 'customer'})
    service_type_id = fields.Many2one('fleet.service.type', string='Fleet Service Type',
                                      help="Maintenance requests: service type of the fleet service created on approval.")


class SalesRepDeptRequest(models.Model):
    _name = 'sales.rep.dept.request'
    _description = 'Department Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc, id desc'
    _check_company_auto = True

    name = fields.Char(required=True, readonly=True, copy=False, default=lambda self: _('New'))
    type_id = fields.Many2one('sales.rep.dept.request.type', string='Type', required=True, tracking=True)
    type_code = fields.Selection(related='type_id.code', store=True)
    category = fields.Selection(related='type_id.category', store=True, string='Kind')
    sales_rep_id = fields.Many2one('sales.representative', required=True, tracking=True, check_company=True, index=True)
    company_id = fields.Many2one('res.company', related='sales_rep_id.company_id', store=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    department_id = fields.Many2one(related='type_id.department_id', store=True)
    approver_id = fields.Many2one('res.users', string='Approver', compute='_compute_approver', store=True, readonly=False)
    description = fields.Text(required=True)
    amount = fields.Monetary(currency_field='currency_id', tracking=True)
    date_from = fields.Date()
    date_to = fields.Date()
    vehicle_id = fields.Many2one('fleet.vehicle', string='Vehicle')
    partner_id = fields.Many2one('res.partner', string='Customer')
    route_id = fields.Many2one('sales.rep.route', string='Route')
    attachment = fields.Image(max_width=1600, max_height=1600)
    state = fields.Selection([
        ('submitted', 'Submitted'),
        ('info', 'More info needed'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
    ], default='submitted', required=True, tracking=True, index=True)
    response = fields.Text(string='Answer to Representative', tracking=True)
    decided_by = fields.Many2one('res.users', readonly=True, copy=False)
    decided_date = fields.Datetime(readonly=True, copy=False)
    service_log_id = fields.Many2one('fleet.vehicle.log.services', string='Maintenance Service', readonly=True, copy=False)
    mobile_local_id = fields.Char(index=True, copy=False)

    @api.depends('type_id.approver_id', 'sales_rep_id.supervisor_id.user_id')
    def _compute_approver(self):
        for req in self:
            req.approver_id = req.type_id.approver_id or req.sales_rep_id.supervisor_id.user_id or False

    @api.constrains('type_id', 'amount', 'date_from', 'date_to', 'vehicle_id', 'partner_id')
    def _check_required(self):
        for req in self:
            t = req.type_id
            if t.needs_amount and req.amount <= 0:
                raise ValidationError(_("%s needs an amount.", t.name))
            if t.needs_dates and not (req.date_from and req.date_to):
                raise ValidationError(_("%s needs a start and end date.", t.name))
            if req.date_from and req.date_to and req.date_to < req.date_from:
                raise ValidationError(_("The end date must be after the start date."))
            if t.needs_vehicle and not req.vehicle_id:
                raise ValidationError(_("%s needs a vehicle.", t.name))
            if t.needs_customer and not req.partner_id:
                raise ValidationError(_("%s needs a customer.", t.name))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('sales.rep.dept.request') or _('New')
            if not vals.get('vehicle_id') and vals.get('sales_rep_id'):
                rep = self.env['sales.representative'].browse(vals['sales_rep_id'])
                if rep.vehicle_id:
                    vals['vehicle_id'] = rep.vehicle_id.id
        requests = super().create(vals_list)
        for req in requests:
            if req.approver_id:
                req.activity_schedule('mail.mail_activity_data_todo', user_id=req.approver_id.id,
                                      summary=_("%(type)s request from %(rep)s", type=req.type_id.name, rep=req.sales_rep_id.name))
        return requests

    def _notify_rep(self):
        try:
            from .sse_dispatch import schedule_reps
            schedule_reps(self.env, self.mapped('sales_rep_id').ids, 'request_answered')
        except Exception:
            pass

    def _decide(self, state):
        for req in self:
            if req.state not in ('submitted', 'info'):
                raise UserError(_("Request %s is already decided.", req.name))
        self.write({'state': state, 'decided_by': self.env.user.id, 'decided_date': fields.Datetime.now()})
        self.activity_ids.action_done()
        self._notify_rep()

    def action_approve(self):
        self._decide('approved')
        ServiceType = self.env['fleet.service.type'].sudo()
        for req in self.filtered(lambda r: r.type_code == 'maintenance' and r.vehicle_id and not r.service_log_id):
            stype = req.type_id.service_type_id or ServiceType.search([('category', '=', 'service')], limit=1) \
                or ServiceType.create({'name': _('Maintenance'), 'category': 'service'})
            req.service_log_id = self.env['fleet.vehicle.log.services'].sudo().create({
                'service_type_id': stype.id,
                'vehicle_id': req.vehicle_id.id,
                'description': f"{req.name}: {req.description}",
                'amount': req.amount,
                'date': req.date_from or fields.Date.context_today(req),
            })
        return True

    def action_reject(self):
        if any(not r.response for r in self):
            raise UserError(_("Write the answer to the representative before rejecting."))
        self._decide('rejected')
        return True

    def action_request_info(self):
        if any(not r.response for r in self):
            raise UserError(_("Write what information you need in 'Answer to Representative'."))
        self.write({'state': 'info'})
        self._notify_rep()
        return True

    def action_done(self):
        self.filtered(lambda r: r.state == 'approved').write({'state': 'done'})
        return True

    def action_cancel(self):
        self.filtered(lambda r: r.state in ('submitted', 'info')).write({'state': 'cancelled'})
        return True

    def rep_reply(self, text, attachment=None):
        """Representative answers an 'info' request: back to the approver."""
        self.ensure_one()
        if self.state != 'info':
            raise UserError(_("This request is not waiting for your answer."))
        vals = {'state': 'submitted', 'description': (self.description or '') + "\n\n" + _("Rep answer: %s", text)}
        if attachment:
            vals['attachment'] = attachment
        self.write(vals)
        self.message_post(body=_("Representative answered: %s", text))
        if self.approver_id:
            self.activity_schedule('mail.mail_activity_data_todo', user_id=self.approver_id.id,
                                   summary=_("Answer received on %s", self.name))
        return True
