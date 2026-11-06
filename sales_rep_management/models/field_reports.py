# -*- coding: utf-8 -*-
"""Field analytics: one SQL report per business flow of the representatives.

Every report shares the same analysis axes so the office can slice any flow
the same way: date, representative, supervisor, sales area (customer area,
else the route's area), customer, route and company. Each row links back to
its source document, and the measures are additive so pivot / graph totals
are always right (no double counting from joins: amounts are aggregated in
CTEs first, then joined).

    sales.rep.report.visit       planned route stops + unplanned visits
    sales.rep.report.sale        sales order lines (ordered / delivered / invoiced)
    sales.rep.report.invoice     posted customer invoices and credit notes, with aging
    sales.rep.report.collection  customer payments
    sales.rep.report.delivery    deliveries, customer returns and van loads
    sales.rep.report.task        representative tasks

Visibility follows the operational rules: a representative sees his own
rows, a supervisor his team, a manager everything (security.xml).
"""
from odoo import api, fields, models, tools

AGING = [
    ('not_due', 'Not Due'),
    ('1_30', '1-30 Days'),
    ('31_60', '31-60 Days'),
    ('61_90', '61-90 Days'),
    ('90_plus', '+90 Days'),
]


class SalesRepFieldReportMixin(models.AbstractModel):
    _name = 'sales.rep.field.report.mixin'
    _description = 'Field Report Axes'

    date = fields.Date(string='Date', readonly=True)
    sales_rep_id = fields.Many2one('sales.representative', string='Representative', readonly=True)
    supervisor_id = fields.Many2one('sales.representative', string='Supervisor', readonly=True)
    sales_area_id = fields.Many2one('sales.area', string='Area', readonly=True)
    partner_id = fields.Many2one('res.partner', string='Customer', readonly=True)
    route_id = fields.Many2one('sales.rep.route', string='Route', readonly=True)
    company_id = fields.Many2one('res.company', string='Company', readonly=True)

    def _create_view(self, query):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute(f"CREATE OR REPLACE VIEW {self._table} AS ({query})")

    @api.model
    def _area_child_domain(self, area_ids):
        return [('sales_area_id', 'child_of', area_ids)] if area_ids else []


# ---------------------------------------------------------------------- visits
class SalesRepReportVisit(models.Model):
    _name = 'sales.rep.report.visit'
    _inherit = 'sales.rep.field.report.mixin'
    _description = 'Visit Report'
    _auto = False
    _order = 'date desc, sales_rep_id'

    route_customer_id = fields.Many2one('sales.route.customer', string='Route Stop', readonly=True)
    visit_id = fields.Many2one('sales.rep.visit', string='Visit', readonly=True)
    origin = fields.Selection([('planned', 'Planned'), ('unplanned', 'Unplanned')], readonly=True)
    status = fields.Selection([
        ('visited', 'Visited'), ('in_progress', 'In Progress'), ('missed', 'Missed'),
        ('upcoming', 'Upcoming'), ('cancelled', 'Cancelled'),
    ], string='Status', readonly=True)
    visit_result = fields.Selection([
        ('successful', 'Successful'), ('customer_unavailable', 'Customer Unavailable'),
        ('refused', 'Refused'), ('closed', 'Location Closed'), ('rescheduled', 'Rescheduled'), ('other', 'Other'),
    ], readonly=True)
    visit_type_id = fields.Many2one('sales.rep.visit.types', string='Visit Type', readonly=True)
    start_time = fields.Datetime(readonly=True)
    end_time = fields.Datetime(readonly=True)

    nbr_planned = fields.Integer('# Planned', readonly=True)
    nbr_visited = fields.Integer('# Visited', readonly=True)
    nbr_plan_done = fields.Integer('# Planned & Visited', readonly=True)
    nbr_missed = fields.Integer('# Missed', readonly=True)
    nbr_unplanned = fields.Integer('# Unplanned', readonly=True)
    nbr_productive = fields.Integer('# Productive (visited with order)', readonly=True)
    nbr_successful = fields.Integer('# Successful', readonly=True)
    duration_hours = fields.Float('Duration (h)', readonly=True, aggregator='sum')
    order_count = fields.Integer('# Orders', readonly=True)
    sale_amount = fields.Float('Sales', readonly=True)
    collected_amount = fields.Float('Collected', readonly=True)

    def init(self):
        self._create_view("""
            WITH so_rc AS (
                SELECT route_customer_id AS key, COUNT(*) AS n, SUM(amount_untaxed / NULLIF(currency_rate, 0)) AS amount
                FROM sale_order WHERE state = 'sale' AND route_customer_id IS NOT NULL GROUP BY route_customer_id
            ), so_v AS (
                SELECT visit_id AS key, COUNT(*) AS n, SUM(amount_untaxed / NULLIF(currency_rate, 0)) AS amount
                FROM sale_order WHERE state = 'sale' AND visit_id IS NOT NULL AND route_customer_id IS NULL GROUP BY visit_id
            ), pay_rc AS (
                SELECT route_customer_id AS key, SUM(amount_company_currency_signed) AS amount
                FROM account_payment WHERE state IN ('in_process', 'paid') AND payment_type = 'inbound'
                  AND route_customer_id IS NOT NULL GROUP BY route_customer_id
            ), pay_v AS (
                SELECT visit_id AS key, SUM(amount_company_currency_signed) AS amount
                FROM account_payment WHERE state IN ('in_process', 'paid') AND payment_type = 'inbound'
                  AND visit_id IS NOT NULL AND route_customer_id IS NULL GROUP BY visit_id
            ), planned AS (
                SELECT
                    rc.id * 2 AS id,
                    r.date AS date,
                    r.sales_rep_id,
                    rep.supervisor_id,
                    COALESCE(p.sales_area_id, r.sales_area_id) AS sales_area_id,
                    rc.partner_id,
                    r.id AS route_id,
                    COALESCE(rc.company_id, r.company_id) AS company_id,
                    rc.id AS route_customer_id,
                    rc.visit_id,
                    'planned' AS origin,
                    CASE
                        WHEN rc.state = 'visited' THEN 'visited'
                        WHEN rc.state = 'in_progress' THEN 'in_progress'
                        WHEN rc.state = 'cancelled' THEN 'cancelled'
                        WHEN r.date < CURRENT_DATE THEN 'missed'
                        ELSE 'upcoming'
                    END AS status,
                    rc.visit_result,
                    rc.visit_type_id,
                    rc.visit_start_time AS start_time,
                    rc.visit_end_time AS end_time,
                    1 AS nbr_planned,
                    CASE WHEN rc.state = 'visited' THEN 1 ELSE 0 END AS nbr_visited,
                    CASE WHEN rc.state = 'visited' THEN 1 ELSE 0 END AS nbr_plan_done,
                    CASE WHEN rc.state = 'planned' AND r.date < CURRENT_DATE THEN 1 ELSE 0 END AS nbr_missed,
                    0 AS nbr_unplanned,
                    CASE WHEN rc.state IN ('visited', 'in_progress') AND COALESCE(so_rc.n, 0) + COALESCE(so_v.n, 0) > 0
                         THEN 1 ELSE 0 END AS nbr_productive,
                    CASE WHEN rc.visit_result = 'successful' THEN 1 ELSE 0 END AS nbr_successful,
                    CASE WHEN rc.visit_start_time IS NOT NULL AND rc.visit_end_time IS NOT NULL
                         THEN EXTRACT(EPOCH FROM (rc.visit_end_time - rc.visit_start_time)) / 3600.0 ELSE 0 END AS duration_hours,
                    COALESCE(so_rc.n, 0) + COALESCE(so_v.n, 0) AS order_count,
                    COALESCE(so_rc.amount, 0) + COALESCE(so_v.amount, 0) AS sale_amount,
                    COALESCE(pay_rc.amount, 0) + COALESCE(pay_v.amount, 0) AS collected_amount
                FROM sales_route_customer rc
                JOIN sales_rep_route r ON r.id = rc.route_id
                LEFT JOIN sales_representative rep ON rep.id = r.sales_rep_id
                LEFT JOIN res_partner p ON p.id = rc.partner_id
                LEFT JOIN so_rc ON so_rc.key = rc.id
                LEFT JOIN so_v ON so_v.key = rc.visit_id
                LEFT JOIN pay_rc ON pay_rc.key = rc.id
                LEFT JOIN pay_v ON pay_v.key = rc.visit_id
            ), unplanned AS (
                SELECT
                    v.id * 2 + 1 AS id,
                    (v.planned_time AT TIME ZONE 'UTC')::date AS date,
                    COALESCE(v.sales_rep_id, r.sales_rep_id) AS sales_rep_id,
                    rep.supervisor_id,
                    COALESCE(p.sales_area_id, r.sales_area_id) AS sales_area_id,
                    v.partner_id,
                    v.route_id,
                    v.company_id,
                    NULL::integer AS route_customer_id,
                    v.id AS visit_id,
                    'unplanned' AS origin,
                    CASE WHEN v.state = 'completed' THEN 'visited'
                         WHEN v.state = 'in_progress' THEN 'in_progress'
                         WHEN v.state = 'cancelled' THEN 'cancelled'
                         ELSE 'upcoming' END AS status,
                    v.visit_result,
                    NULL::integer AS visit_type_id,
                    v.planned_time AS start_time,
                    v.visit_time AS end_time,
                    0, CASE WHEN v.state = 'completed' THEN 1 ELSE 0 END, 0, 0, 1,
                    CASE WHEN v.state IN ('completed', 'in_progress') AND COALESCE(so_v.n, 0) > 0 THEN 1 ELSE 0 END,
                    CASE WHEN v.visit_result = 'successful' THEN 1 ELSE 0 END,
                    COALESCE(v.duration, 0),
                    COALESCE(so_v.n, 0),
                    COALESCE(so_v.amount, 0),
                    COALESCE(pay_v.amount, 0)
                FROM sales_rep_visit v
                LEFT JOIN sales_rep_route r ON r.id = v.route_id
                LEFT JOIN sales_representative rep ON rep.id = COALESCE(v.sales_rep_id, r.sales_rep_id)
                LEFT JOIN res_partner p ON p.id = v.partner_id
                LEFT JOIN so_v ON so_v.key = v.id
                LEFT JOIN pay_v ON pay_v.key = v.id
                WHERE v.route_customer_id IS NULL
                  AND NOT EXISTS (SELECT 1 FROM sales_route_customer x WHERE x.visit_id = v.id)
            )
            SELECT * FROM planned UNION ALL SELECT * FROM unplanned
        """)


# ----------------------------------------------------------------------- sales
class SalesRepReportSale(models.Model):
    _name = 'sales.rep.report.sale'
    _inherit = 'sales.rep.field.report.mixin'
    _description = 'Sales Report (Lines)'
    _auto = False
    _order = 'date desc'

    order_id = fields.Many2one('sale.order', string='Order', readonly=True)
    order_count = fields.Integer('# Orders', readonly=True, aggregator='count_distinct')
    product_id = fields.Many2one('product.product', string='Product', readonly=True)
    categ_id = fields.Many2one('product.category', string='Product Category', readonly=True)
    uom_id = fields.Many2one('uom.uom', string='Unit', readonly=True)
    state = fields.Selection([('draft', 'Quotation'), ('sent', 'Quotation Sent'), ('sale', 'Sales Order'),
                              ('cancel', 'Cancelled')], readonly=True)
    invoice_status = fields.Selection([('upselling', 'Upselling Opportunity'), ('invoiced', 'Fully Invoiced'),
                                       ('to invoice', 'To Invoice'), ('no', 'Nothing to Invoice')], readonly=True)
    is_cash = fields.Boolean('Cash Order', readonly=True)
    is_free = fields.Boolean('Free / Promotion Line', readonly=True)

    qty_ordered = fields.Float('Qty Ordered', readonly=True)
    qty_delivered = fields.Float('Qty Delivered', readonly=True)
    qty_to_deliver = fields.Float('Qty To Deliver', readonly=True)
    qty_invoiced = fields.Float('Qty Invoiced', readonly=True)
    qty_free = fields.Float('Free Qty', readonly=True)
    gross_amount = fields.Float('Gross (before discount)', readonly=True)
    discount_amount = fields.Float('Discount', readonly=True)
    amount_untaxed = fields.Float('Untaxed Amount', readonly=True)
    amount_total = fields.Float('Total', readonly=True)

    def init(self):
        self._create_view("""
            SELECT
                l.id,
                (so.date_order AT TIME ZONE 'UTC')::date AS date,
                so.sales_rep_id,
                rep.supervisor_id,
                COALESCE(p.sales_area_id, r.sales_area_id) AS sales_area_id,
                so.partner_id,
                so.route_id,
                so.company_id,
                so.id AS order_id,
                so.id AS order_count,
                l.product_id,
                t.categ_id,
                l.product_uom AS uom_id,
                so.state,
                so.invoice_status,
                COALESCE(so.is_cash, FALSE) AS is_cash,
                (l.reward_id IS NOT NULL OR l.price_unit = 0) AS is_free,
                l.product_uom_qty AS qty_ordered,
                l.qty_delivered,
                GREATEST(l.product_uom_qty - l.qty_delivered, 0) AS qty_to_deliver,
                l.qty_invoiced,
                CASE WHEN l.reward_id IS NOT NULL OR l.price_unit = 0 THEN l.product_uom_qty ELSE 0 END AS qty_free,
                l.price_unit * l.product_uom_qty / NULLIF(so.currency_rate, 0) AS gross_amount,
                (l.price_unit * l.product_uom_qty - l.price_subtotal) / NULLIF(so.currency_rate, 0) AS discount_amount,
                l.price_subtotal / NULLIF(so.currency_rate, 0) AS amount_untaxed,
                l.price_total / NULLIF(so.currency_rate, 0) AS amount_total
            FROM sale_order_line l
            JOIN sale_order so ON so.id = l.order_id
            LEFT JOIN product_product pp ON pp.id = l.product_id
            LEFT JOIN product_template t ON t.id = pp.product_tmpl_id
            LEFT JOIN res_partner p ON p.id = so.partner_id
            LEFT JOIN sales_rep_route r ON r.id = so.route_id
            LEFT JOIN sales_representative rep ON rep.id = so.sales_rep_id
            WHERE l.display_type IS NULL AND so.sales_rep_id IS NOT NULL
        """)


# -------------------------------------------------------------------- invoices
class SalesRepReportInvoice(models.Model):
    _name = 'sales.rep.report.invoice'
    _inherit = 'sales.rep.field.report.mixin'
    _description = 'Invoice Report'
    _auto = False
    _order = 'date desc'

    move_id = fields.Many2one('account.move', string='Invoice', readonly=True)
    move_type = fields.Selection([('out_invoice', 'Invoice'), ('out_refund', 'Credit Note')], string='Type', readonly=True)
    invoice_date_due = fields.Date('Due Date', readonly=True)
    payment_state = fields.Selection([
        ('not_paid', 'Not Paid'), ('in_payment', 'In Payment'), ('paid', 'Paid'), ('partial', 'Partially Paid'),
        ('reversed', 'Reversed'), ('blocked', 'Blocked'), ('invoicing_legacy', 'Invoicing App Legacy'),
    ], readonly=True)
    aging = fields.Selection(AGING, string='Aging', readonly=True)
    days_overdue = fields.Integer('Days Overdue', readonly=True, aggregator='max')
    invoice_count = fields.Integer('# Invoices', readonly=True)
    nbr_overdue = fields.Integer('# Overdue', readonly=True)
    amount_untaxed = fields.Float('Untaxed', readonly=True)
    amount_tax = fields.Float('Tax', readonly=True)
    amount_total = fields.Float('Total', readonly=True)
    amount_paid = fields.Float('Paid', readonly=True)
    amount_residual = fields.Float('Due', readonly=True)
    amount_overdue = fields.Float('Overdue Amount', readonly=True)

    def init(self):
        self._create_view("""
            SELECT
                m.id,
                m.invoice_date AS date,
                m.sales_rep_id,
                rep.supervisor_id,
                p.sales_area_id,
                m.commercial_partner_id AS partner_id,
                so.route_id,
                m.company_id,
                m.id AS move_id,
                m.move_type,
                m.invoice_date_due,
                m.payment_state,
                CASE
                    WHEN m.amount_residual = 0 OR m.invoice_date_due IS NULL OR m.invoice_date_due >= CURRENT_DATE THEN 'not_due'
                    WHEN CURRENT_DATE - m.invoice_date_due <= 30 THEN '1_30'
                    WHEN CURRENT_DATE - m.invoice_date_due <= 60 THEN '31_60'
                    WHEN CURRENT_DATE - m.invoice_date_due <= 90 THEN '61_90'
                    ELSE '90_plus'
                END AS aging,
                CASE WHEN m.amount_residual > 0 AND m.invoice_date_due < CURRENT_DATE
                     THEN CURRENT_DATE - m.invoice_date_due ELSE 0 END AS days_overdue,
                CASE WHEN m.move_type = 'out_invoice' THEN 1 ELSE 0 END AS invoice_count,
                CASE WHEN m.move_type = 'out_invoice' AND m.amount_residual > 0 AND m.invoice_date_due < CURRENT_DATE
                     THEN 1 ELSE 0 END AS nbr_overdue,
                m.amount_untaxed_signed AS amount_untaxed,
                m.amount_tax_signed AS amount_tax,
                m.amount_total_signed AS amount_total,
                m.amount_total_signed - m.amount_residual_signed AS amount_paid,
                m.amount_residual_signed AS amount_residual,
                CASE WHEN m.amount_residual > 0 AND m.invoice_date_due < CURRENT_DATE
                     THEN m.amount_residual_signed ELSE 0 END AS amount_overdue
            FROM account_move m
            LEFT JOIN res_partner p ON p.id = m.commercial_partner_id
            LEFT JOIN sales_representative rep ON rep.id = m.sales_rep_id
            LEFT JOIN LATERAL (
                SELECT o.route_id FROM sale_order o
                WHERE o.name = m.invoice_origin AND o.company_id = m.company_id AND o.route_id IS NOT NULL LIMIT 1
            ) so ON TRUE
            WHERE m.move_type IN ('out_invoice', 'out_refund') AND m.state = 'posted' AND m.sales_rep_id IS NOT NULL
        """)


# ----------------------------------------------------------------- collections
class SalesRepReportCollection(models.Model):
    _name = 'sales.rep.report.collection'
    _inherit = 'sales.rep.field.report.mixin'
    _description = 'Collection Report'
    _auto = False
    _order = 'date desc'

    payment_id = fields.Many2one('account.payment', string='Payment', readonly=True)
    journal_id = fields.Many2one('account.journal', string='Journal', readonly=True)
    payment_method_line_id = fields.Many2one('account.payment.method.line', string='Payment Method', readonly=True)
    visit_id = fields.Many2one('sales.rep.visit', string='Visit', readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('in_process', 'In Process'), ('paid', 'Paid'),
                              ('canceled', 'Canceled'), ('rejected', 'Rejected')], readonly=True)
    on_route = fields.Boolean('During a Route', readonly=True)
    handed_over = fields.Boolean('Handed Over (Day Close)', readonly=True)
    payment_count = fields.Integer('# Payments', readonly=True)
    amount = fields.Float('Amount', readonly=True)
    amount_cash = fields.Float('Cash', readonly=True)
    amount_bank = fields.Float('Bank / Other', readonly=True)
    amount_not_handed = fields.Float('Not Handed Over', readonly=True)

    def init(self):
        self._create_view("""
            SELECT
                ap.id,
                ap.date,
                ap.sales_rep_id,
                rep.supervisor_id,
                COALESCE(p.sales_area_id, r.sales_area_id) AS sales_area_id,
                ap.partner_id,
                ap.route_id,
                ap.company_id,
                ap.id AS payment_id,
                ap.journal_id,
                ap.payment_method_line_id,
                ap.visit_id,
                ap.state,
                (ap.route_id IS NOT NULL OR ap.visit_id IS NOT NULL) AS on_route,
                (ap.day_close_id IS NOT NULL) AS handed_over,
                1 AS payment_count,
                ap.amount_company_currency_signed AS amount,
                CASE WHEN j.type = 'cash' THEN ap.amount_company_currency_signed ELSE 0 END AS amount_cash,
                CASE WHEN j.type <> 'cash' THEN ap.amount_company_currency_signed ELSE 0 END AS amount_bank,
                CASE WHEN j.type = 'cash' AND ap.day_close_id IS NULL THEN ap.amount_company_currency_signed ELSE 0 END
                    AS amount_not_handed
            FROM account_payment ap
            JOIN account_journal j ON j.id = ap.journal_id
            LEFT JOIN res_partner p ON p.id = ap.partner_id
            LEFT JOIN sales_rep_route r ON r.id = ap.route_id
            LEFT JOIN sales_representative rep ON rep.id = ap.sales_rep_id
            WHERE ap.payment_type = 'inbound' AND ap.partner_type = 'customer' AND ap.sales_rep_id IS NOT NULL
              AND ap.state NOT IN ('draft', 'canceled', 'rejected')
        """)


# ------------------------------------------------------------------ deliveries
class SalesRepReportDelivery(models.Model):
    _name = 'sales.rep.report.delivery'
    _inherit = 'sales.rep.field.report.mixin'
    _description = 'Shipping & Delivery Report'
    _auto = False
    _order = 'date desc'

    picking_id = fields.Many2one('stock.picking', string='Transfer', readonly=True)
    sale_id = fields.Many2one('sale.order', string='Sales Order', readonly=True)
    kind = fields.Selection([('delivery', 'Delivery'), ('return', 'Customer Return'), ('van_load', 'Van Load')],
                            readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('waiting', 'Waiting Another Operation'), ('confirmed', 'Waiting'),
                              ('assigned', 'Ready'), ('done', 'Done'), ('cancel', 'Cancelled')], readonly=True)
    load_acceptance_state = fields.Selection([('pending', 'Pending'), ('accepted', 'Accepted'),
                                              ('disputed', 'Accepted with Differences')], string='Load Acceptance',
                                             readonly=True)
    return_reason_id = fields.Many2one('sales.rep.return.reason', string='Return Reason', readonly=True)
    scheduled_date = fields.Datetime(readonly=True)
    date_done = fields.Datetime('Done On', readonly=True)
    is_late = fields.Boolean('Late', readonly=True)
    is_backorder = fields.Boolean('Backorder', readonly=True)

    picking_count = fields.Integer('# Transfers', readonly=True)
    nbr_done = fields.Integer('# Done', readonly=True)
    nbr_open = fields.Integer('# Open', readonly=True)
    nbr_late = fields.Integer('# Late', readonly=True)
    nbr_on_time = fields.Integer('# Done On Time', readonly=True)
    qty_demand = fields.Float('Qty Demanded', readonly=True)
    qty_done = fields.Float('Qty Done', readonly=True)
    delay_hours = fields.Float('Delay (h)', readonly=True, aggregator='avg')
    lead_time_hours = fields.Float('Lead Time (h)', readonly=True, aggregator='avg')

    def init(self):
        self._create_view("""
            WITH mv AS (
                SELECT picking_id, SUM(product_uom_qty) AS demand,
                       SUM(CASE WHEN state = 'done' THEN quantity ELSE 0 END) AS done
                FROM stock_move WHERE state <> 'cancel' GROUP BY picking_id
            ), base AS (
                SELECT
                    sp.*,
                    COALESCE(sp.sales_rep_id, so.sales_rep_id, sp.created_sales_rep_id) AS rep,
                    so.route_id AS so_route_id,
                    pt.code AS type_code,
                    CASE
                        WHEN sp.load_acceptance_state IS NOT NULL THEN 'van_load'
                        WHEN pt.code = 'incoming' AND sp.return_id IS NOT NULL THEN 'return'
                        WHEN pt.code = 'outgoing' THEN 'delivery'
                    END AS kind_
                FROM stock_picking sp
                JOIN stock_picking_type pt ON pt.id = sp.picking_type_id
                LEFT JOIN sale_order so ON so.id = sp.sale_id
            )
            SELECT
                b.id,
                (COALESCE(b.date_done, b.scheduled_date) AT TIME ZONE 'UTC')::date AS date,
                b.rep AS sales_rep_id,
                rep.supervisor_id,
                COALESCE(p.sales_area_id, r.sales_area_id) AS sales_area_id,
                b.partner_id,
                b.so_route_id AS route_id,
                b.company_id,
                b.id AS picking_id,
                b.sale_id,
                b.kind_ AS kind,
                b.state,
                b.load_acceptance_state,
                b.return_reason_id,
                b.scheduled_date,
                b.date_done,
                (CASE WHEN b.state = 'done' THEN b.date_done > b.scheduled_date + interval '1 day'
                      WHEN b.state = 'cancel' THEN FALSE
                      ELSE b.scheduled_date < (now() AT TIME ZONE 'UTC') - interval '1 day' END) AS is_late,
                (b.backorder_id IS NOT NULL) AS is_backorder,
                1 AS picking_count,
                CASE WHEN b.state = 'done' THEN 1 ELSE 0 END AS nbr_done,
                CASE WHEN b.state NOT IN ('done', 'cancel') THEN 1 ELSE 0 END AS nbr_open,
                CASE WHEN (b.state = 'done' AND b.date_done > b.scheduled_date + interval '1 day')
                       OR (b.state NOT IN ('done', 'cancel') AND b.scheduled_date < (now() AT TIME ZONE 'UTC') - interval '1 day')
                     THEN 1 ELSE 0 END AS nbr_late,
                CASE WHEN b.state = 'done' AND b.date_done <= b.scheduled_date + interval '1 day' THEN 1 ELSE 0 END AS nbr_on_time,
                COALESCE(mv.demand, 0) AS qty_demand,
                COALESCE(mv.done, 0) AS qty_done,
                CASE WHEN b.state = 'done' AND b.date_done > b.scheduled_date
                     THEN EXTRACT(EPOCH FROM (b.date_done - b.scheduled_date)) / 3600.0 ELSE 0 END AS delay_hours,
                CASE WHEN b.state = 'done' THEN EXTRACT(EPOCH FROM (b.date_done - b.create_date)) / 3600.0 END AS lead_time_hours
            FROM base b
            LEFT JOIN mv ON mv.picking_id = b.id
            LEFT JOIN res_partner p ON p.id = b.partner_id
            LEFT JOIN sales_rep_route r ON r.id = b.so_route_id
            LEFT JOIN sales_representative rep ON rep.id = b.rep
            WHERE b.kind_ IS NOT NULL AND b.rep IS NOT NULL
        """)


# ----------------------------------------------------------------------- tasks
class SalesRepReportTask(models.Model):
    _name = 'sales.rep.report.task'
    _inherit = 'sales.rep.field.report.mixin'
    _description = 'Task Report'
    _auto = False
    _order = 'date desc'

    task_id = fields.Many2one('sales.rep.task', string='Task', readonly=True)
    task_type = fields.Selection([
        ('collect', 'Collect overdue'), ('deliver', 'Deliver'), ('merchandising', 'Merchandising / shelf'),
        ('photo_audit', 'Photo audit'), ('survey', 'Survey'), ('onboarding', 'New customer onboarding'), ('other', 'Other'),
    ], string='Type', readonly=True)
    state = fields.Selection([('new', 'New'), ('in_progress', 'In Progress'), ('done', 'Done'),
                              ('failed', 'Failed'), ('cancelled', 'Cancelled')], readonly=True)
    priority = fields.Selection([('0', 'Normal'), ('1', 'High'), ('2', 'Urgent')], readonly=True)
    source = fields.Selection([('manual', 'Manual'), ('auto_overdue', 'Overdue rule')], readonly=True)
    assigned_by = fields.Many2one('res.users', readonly=True)
    done_date = fields.Datetime(readonly=True)

    task_count = fields.Integer('# Tasks', readonly=True)
    nbr_done = fields.Integer('# Done', readonly=True)
    nbr_failed = fields.Integer('# Failed', readonly=True)
    nbr_open = fields.Integer('# Open', readonly=True)
    nbr_overdue = fields.Integer('# Overdue', readonly=True)
    nbr_on_time = fields.Integer('# Done On Time', readonly=True)
    days_late = fields.Integer('Days Late', readonly=True, aggregator='max')
    amount_due = fields.Float('Amount To Collect', readonly=True)

    def init(self):
        self._create_view("""
            SELECT
                t.id,
                t.date_deadline AS date,
                t.sales_rep_id,
                rep.supervisor_id,
                COALESCE(p.sales_area_id, r.sales_area_id) AS sales_area_id,
                t.partner_id,
                t.route_id,
                t.company_id,
                t.id AS task_id,
                t.task_type,
                t.state,
                t.priority,
                t.source,
                t.assigned_by,
                t.done_date,
                1 AS task_count,
                CASE WHEN t.state = 'done' THEN 1 ELSE 0 END AS nbr_done,
                CASE WHEN t.state = 'failed' THEN 1 ELSE 0 END AS nbr_failed,
                CASE WHEN t.state IN ('new', 'in_progress') THEN 1 ELSE 0 END AS nbr_open,
                CASE WHEN t.state IN ('new', 'in_progress') AND t.date_deadline < CURRENT_DATE THEN 1 ELSE 0 END AS nbr_overdue,
                CASE WHEN t.state = 'done' AND (t.done_date AT TIME ZONE 'UTC')::date <= t.date_deadline THEN 1 ELSE 0 END
                    AS nbr_on_time,
                CASE WHEN t.state IN ('new', 'in_progress') AND t.date_deadline < CURRENT_DATE
                         THEN CURRENT_DATE - t.date_deadline
                     WHEN t.state = 'done' AND (t.done_date AT TIME ZONE 'UTC')::date > t.date_deadline
                         THEN (t.done_date AT TIME ZONE 'UTC')::date - t.date_deadline
                     ELSE 0 END AS days_late,
                COALESCE(t.amount_due, 0) AS amount_due
            FROM sales_rep_task t
            LEFT JOIN sales_representative rep ON rep.id = t.sales_rep_id
            LEFT JOIN res_partner p ON p.id = t.partner_id
            LEFT JOIN sales_rep_route r ON r.id = t.route_id
        """)
