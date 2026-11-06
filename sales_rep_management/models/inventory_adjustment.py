from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

class SalesRepInventoryAdjustment(models.Model):
    _name = 'sales.rep.inventory.adjustment'
    _description = 'Sales Rep Inventory Adjustment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date desc'

    name = fields.Char(string='Reference', required=True, copy=False, readonly=True, default=lambda self: _('New'))
    sales_rep_id = fields.Many2one('sales.representative', string='Sales Representative', required=True, tracking=True, check_company=True)
    allowed_location_ids = fields.Many2many(
        'stock.location',
        string='Allowed Locations',
        compute='_compute_allowed_location_ids'
    )
    location_id = fields.Many2one(
        'stock.location', 
        string='Location', 
        required=True, 
        tracking=True, 
        check_company=True,
        compute='_compute_location_id',
        store=True,
        readonly=False,
        domain="[('id', 'in', allowed_location_ids)]"
    )
    date = fields.Datetime(string='Date', required=True, default=fields.Datetime.now, tracking=True)

    @api.depends('sales_rep_id')
    def _compute_allowed_location_ids(self):
        for record in self:
            if record.sales_rep_id:
                record.allowed_location_ids = record.sales_rep_id.default_location_ids
            else:
                record.allowed_location_ids = self.env['stock.location']

    @api.depends('sales_rep_id')
    def _compute_location_id(self):
        for record in self:
            if record.sales_rep_id:
                record.location_id = record.sales_rep_id.default_location_id
            else:
                record.location_id = False
    company_id = fields.Many2one('res.company', string='Company', related='sales_rep_id.company_id', store=True, readonly=True, default=lambda self: self.env.company)
    line_ids = fields.One2many('sales.rep.inventory.adjustment.line', 'adjustment_id', string='Adjustment Lines')
    mobile_local_id = fields.Char(string='Mobile Local ID', index=True, copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('sales.rep.inventory.adjustment') or _('New')
        return super().create(vals_list)

    def action_print_pdf(self):
        return self.env.ref('sales_rep_management.action_report_inventory_adjustment').report_action(self)

    def action_export_excel(self):
        """ Redirects to a controller that generates a CSV/XLSX file """
        return {
            'type': 'ir.actions.act_url',
            'url': f'/sales_rep/inventory_adjustment/export/{self.id}',
            'target': 'self',
        }

class SalesRepInventoryAdjustmentLine(models.Model):
    _name = 'sales.rep.inventory.adjustment.line'
    _description = 'Sales Rep Inventory Adjustment Line'

    adjustment_id = fields.Many2one('sales.rep.inventory.adjustment', string='Adjustment', ondelete='cascade', required=True, check_company=True)
    company_id = fields.Many2one('res.company', related='adjustment_id.company_id', store=True, readonly=True, default=lambda self: self.env.company)
    product_id = fields.Many2one(
        'product.product', 
        string='Product', 
        required=True, 
        check_company=True,
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]"
    )
    counted_qty = fields.Float(string='Counted Quantity', required=True, default=0.0)
    theoretical_qty = fields.Float(string='On Hand Quantity', compute='_compute_theoretical_qty', store=True)
    difference_qty = fields.Float(string='Difference', compute='_compute_difference_qty', store=True)
    product_uom_id = fields.Many2one('uom.uom', string='Unit of Measure')

    @api.depends('product_id', 'adjustment_id.location_id')
    def _compute_theoretical_qty(self):
        for line in self:
            if line.product_id and line.adjustment_id.location_id:
                # Get on-hand qty in the sales rep's location
                quants = self.env['stock.quant'].search([
                    ('product_id', '=', line.product_id.id),
                    ('location_id', '=', line.adjustment_id.location_id.id)
                ])
                line.theoretical_qty = sum(quants.mapped('quantity'))
            else:
                line.theoretical_qty = 0.0

    @api.depends('counted_qty', 'theoretical_qty')
    def _compute_difference_qty(self):
        for line in self:
            line.difference_qty = line.counted_qty - line.theoretical_qty
