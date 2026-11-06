from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import base64


class SalesRepVisit(models.Model):
    _name = 'sales.rep.visit'
    _description = 'Sales Representative Visit'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'sequence, visit_time desc'

    name = fields.Char(
        string='Visit Reference',
        required=True,
        copy=False,
        index=True
    )
    route_id = fields.Many2one(
        'sales.rep.route',
        string='Route',
        required=True,
        ondelete='cascade',
        check_company=True
    )
    sales_rep_id = fields.Many2one(
        'sales.representative',
        string='Sales Representative',
        related='route_id.sales_rep_id',
        store=True,
        readonly=True
    )
    company_id = fields.Many2one(
        'res.company',
        string='Company',
        related='route_id.company_id',
        store=True,
        index=True,
        readonly=True
    )
    partner_id = fields.Many2one(
        'res.partner',
        string='Customer',
        required=True,
        # domain="[('customer_rank', '>', 0)]"
    )
    route_customer_id = fields.Many2one(
        'sales.route.customer',
        string='Route Customer',
        ondelete='set null'
    )
    sequence = fields.Integer(
        string='Sequence',
        default=10,
        help='Order of visit in the route'
    )

    # Visit Details
    visit_type = fields.Selection([
        ('sales', 'Sales Visit'),
        ('collection', 'Collection'),
        ('follow_up', 'Follow Up'),
        ('new_customer', 'New Customer'),
        ('service', 'Service Visit')
    ], string='Visit Type', required=True, default='sales')

    state = fields.Selection([
        ('planned', 'Planned'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('rescheduled', 'Rescheduled')
    ], string='Status', default='planned', tracking=True)

    # Location
    visit_location_lat = fields.Char(string='Visit Location Latitude', readonly=True)
    visit_location_long = fields.Char(string='Visit Location Longitude', readonly=True)

    # Fields required for spd_leaflet_map
    latitude = fields.Char(string='Latitude', related='visit_location_lat', store=True)
    longitude = fields.Char(string='Longitude', related='visit_location_long', store=True)

    # Timing
    planned_time = fields.Datetime(
        string='Start Time',
        required=True
    )
    visit_time = fields.Datetime(
        string='End Time'
    )
    duration = fields.Float(
        string='Duration (Hours)',
        compute='_compute_duration',
        store=True,
        aggregator='sum',
        help='Actual visit duration in hours'
    )

    # Visit Results
    visit_result = fields.Selection([
        ('successful', 'Successful'),
        ('customer_unavailable', 'Customer Unavailable'),
        ('refused', 'Refused'),
        ('closed', 'Location Closed'),
        ('rescheduled', 'Rescheduled'),
        ('other', 'Other')
    ], string='Visit Result')

    notes = fields.Text(
        string='Visit Notes'
    )
    internal_notes = fields.Text(
        string='Internal Notes'
    )

    # Sales Information
    sale_order_ids = fields.One2many(
        'sale.order',
        'visit_id',
        string='Related Sale Orders'
    )
    sale_amount = fields.Float(
        string='Sale Amount',
        compute='_compute_sale_amount',
        store=True,
        aggregator='sum'
    )
    sale_order_id = fields.Many2one(
        'sale.order',
        string='Related Sale Order'
    )
    product_ids = fields.Many2many(
        'product.product',
        string='Products Discussed'
    )
    mobile_local_id = fields.Char(string='Mobile Local ID', index=True)


    # Related Records
    # pos_order_ids = fields.One2many(
    #     'pos.order',
    #     'visit_id',
    #     string='POS Orders'
    # )
    payment_ids = fields.One2many(
        'account.payment',
        'visit_id',
        string='Payments'
    )

    # Collections
    # collection_ids = fields.One2many(
    #     'sales.rep.collection',
    #     'visit_id',
    #     string='Collections'
    # )
    total_collected = fields.Float(
        string='Total Collected',
        compute='_compute_total_collected',
        store=True,
        aggregator='sum'
    )

    # Count fields for button box
    # pos_order_count = fields.Integer(
    #     string='POS Orders Count',
    #     compute='_compute_pos_order_count'
    # )
    payment_count = fields.Integer(
        string='Payments Count',
        compute='_compute_payment_count'
    )

    # Attachments
    attachment_ids = fields.One2many(
        'ir.attachment',
        'res_id',
        string='Attachments',
        domain=[('res_model', '=', 'sales.rep.visit')]
    )
    image_ids = fields.One2many(
        'sales.visit.image',
        'visit_id',
        string='Visit Images'
    )

    # Customer Info at time of visit
    customer_name = fields.Char(
        string='Customer Name',
        related='partner_id.name',
        store=True
    )
    customer_phone = fields.Char(
        string='Customer Phone',
        related='partner_id.phone',
        store=True
    )
    customer_email = fields.Char(
        string='Customer Email',
        related='partner_id.email',
        store=True
    )

    customer_latitude = fields.Float(
        string='Customer Latitude',
        related='partner_id.visit_latitude',
        store=False
    )
    customer_longitude = fields.Float(
        string='Customer Longitude',
        related='partner_id.visit_longitude',
        store=False
    )

    # Next Action
    next_visit_date = fields.Date(
        string='Next Visit Date'
    )
    follow_up_required = fields.Boolean(
        string='Follow-up Required'
    )
    follow_up_notes = fields.Text(
        string='Follow-up Notes'
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get('name'):
                vals['name'] = self.env['ir.sequence'].next_by_code('sales.rep.visit') or _('New')
        return super().create(vals_list)

    @api.depends('sale_order_id', 'sale_order_id.amount_total', 'sale_order_id.state',
                 'sale_order_ids', 'sale_order_ids.amount_total', 'sale_order_ids.state')
                #  'pos_order_ids', 'pos_order_ids.amount_total', 'pos_order_ids.state')
    def _compute_sale_amount(self):
        for visit in self:
            all_sales = visit.sale_order_ids | visit.sale_order_id
            active_sales = all_sales.filtered(lambda o: o.state != 'cancel')
            # active_pos = visit.pos_order_ids.filtered(lambda o: o.state != 'cancel')
            # visit.sale_amount = sum(active_sales.mapped('amount_total')) + sum(active_pos.mapped('amount_total'))
            visit.sale_amount = sum(active_sales.mapped('amount_total'))

    @api.depends('payment_ids', 'payment_ids.amount', 'payment_ids.state')
    def _compute_total_collected(self):
        for visit in self:
            # We count posted or in_payment payments linked to this visit
            valid_payments = visit.payment_ids.filtered(lambda p: p.state in ('posted', 'in_payment'))
            visit.total_collected = sum(valid_payments.mapped('amount'))

    # @api.depends('pos_order_ids')
    # def _compute_pos_order_count(self):
    #     for visit in self:
    #         visit.pos_order_count = len(visit.pos_order_ids)

    @api.depends('payment_ids')
    def _compute_payment_count(self):
        for visit in self:
            visit.payment_count = len(visit.payment_ids)

    @api.depends('planned_time', 'visit_time')
    def _compute_duration(self):
        for visit in self:
            if visit.planned_time and visit.visit_time:
                diff = visit.visit_time - visit.planned_time
                visit.duration = abs(diff.total_seconds()) / 3600
            else:
                visit.duration = 0.0

    def action_start_visit(self):
        self.state = 'in_progress'
        self.visit_time = fields.Datetime.now()
        return True

    def action_complete_visit(self):
        if not self.visit_result:
            raise ValidationError(_("Please select a visit result before completing the visit."))

        self.state = 'completed'
        if not self.visit_time:
            self.visit_time = fields.Datetime.now()
        return True

    def action_cancel_visit(self):
        self.state = 'cancelled'
        return True

    def action_reschedule_visit(self):
        self.state = 'rescheduled'
        return True

    def action_create_sale_order(self):
        """Create a sale order from this visit"""
        if self.sale_order_id:
            raise ValidationError(_("A sale order already exists for this visit."))

        sale_order = self.env['sale.order'].create({
            'partner_id': self.partner_id.id,
            'date_order': self.visit_time or fields.Datetime.now(),
            'origin': f"Created from visit: {self.name}\nVisit Notes: {self.notes or ''}",
            'route_id': self.route_id.id,
            'route_customer_id': self.route_customer_id.id,
            'visit_id': self.id,
        })

        self.sale_order_id = sale_order.id
        self.sale_amount = sale_order.amount_total

        # Auto-complete the visit
        self.state = 'completed'
        self.visit_result = 'successful'
        self.visit_time = fields.Datetime.now()

        return {
            'type': 'ir.actions.act_window',
            'name': _('Sale Order'),
            'res_model': 'sale.order',
            'res_id': sale_order.id,
            'view_mode': 'form',
            'target': 'current'
        }

    def action_view_sale_order(self):
        """View related sale order"""
        if not self.sale_order_id:
            return False

        return {
            'type': 'ir.actions.act_window',
            'name': _('Sale Order'),
            'res_model': 'sale.order',
            'res_id': self.sale_order_id.id,
            'view_mode': 'form',
            'target': 'current'
        }

    # def action_view_pos_orders(self):
    #     """View POS orders for this visit"""
    #     return {
    #         'type': 'ir.actions.act_window',
    #         'name': _('POS Orders - %s') % self.partner_id.name,
    #         'res_model': 'pos.order',
    #         'view_mode': 'list,form',
    #         'domain': [('visit_id', '=', self.id)],
    #         'context': {'create': False}
    #     }

    def action_view_payments(self):
        """View payments for this visit"""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payments - %s') % self.partner_id.name,
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('visit_id', '=', self.id)],
            'context': {'create': False}
        }

    def action_view_collections(self):
        """View payments (collections) for this visit"""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payments - %s') % self.partner_id.name,
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('visit_id', '=', self.id)],
            'context': {'default_visit_id': self.id}
        }
    # 
    # def action_create_collection(self):
    #     """Create a new collection for this visit"""
    #     collection = self.env['sales.rep.collection'].create({
    #         'visit_id': self.id,
    #         'collection_date': fields.Datetime.now(),
    #         'payment_method': 'cash'
    #     })
    # 
    #     return {
    #         'type': 'ir.actions.act_window',
    #         'name': _('New Collection'),
    #         'res_model': 'sales.rep.collection',
    #         'res_id': collection.id,
    #         'view_mode': 'form',
    #         'target': 'new'
    #     }

    @api.onchange('visit_result')
    def _onchange_visit_result(self):
        if self.visit_result == 'rescheduled' and not self.next_visit_date:
            self.next_visit_date = fields.Date.context_today(self)
        if self.visit_result in ['customer_unavailable', 'refused', 'closed']:
            self.follow_up_required = True

    def action_open_google_maps(self):
        self.ensure_one()
        if self.visit_location_lat and self.visit_location_long:
            url = f"https://www.google.com/maps/search/?api=1&query={self.visit_location_lat},{self.visit_location_long}"
            return {
                'type': 'ir.actions.act_url',
                'url': url,
                'target': 'new',
            }
        return False


class SalesVisitImage(models.Model):
    _name = 'sales.visit.image'
    _description = 'Sales Visit Image'
    _order = 'sequence, id'

    name = fields.Char(
        string='Description',
        required=True
    )
    visit_id = fields.Many2one(
        'sales.rep.visit',
        string='Visit',
        required=True,
        ondelete='cascade'
    )
    image = fields.Binary(
        string='Image',
        required=True
    )
    image_filename = fields.Char(
        string='Filename'
    )
    sequence = fields.Integer(
        string='Sequence',
        default=10
    )
    image_type = fields.Selection([
        ('receipt', 'Receipt'),
        ('invoice', 'Invoice'),
        ('product', 'Product'),
        ('store', 'Store Front'),
        ('signature', 'Signature'),
        ('other', 'Other')
    ], string='Image Type', default='other')

    notes = fields.Text(
        string='Notes'
    )

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('image') and not vals.get('image_filename'):
                vals['image_filename'] = f"visit_image_{fields.Datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        return super().create(vals_list)