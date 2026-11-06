from odoo import models, fields, api, _
from odoo.exceptions import UserError


class SalesRepPerformanceReport(models.Model):
    _name = 'sales.rep.performance.report'
    _description = 'Sales Representative Performance Report'
    _auto = False
    _log_access = False

    sales_rep_id = fields.Many2one('sales.representative', string='Sales Representative')
    route_id = fields.Many2one('sales.rep.route', string='Route')
    visit_id = fields.Many2one('sales.rep.visit', string='Visit')
    company_id = fields.Many2one('res.company', string='Company')

    # Count fields
    sale_order_count = fields.Integer(string='Sales Orders')
    # pos_order_count = fields.Integer(string='POS Orders')
    payment_count = fields.Integer(string='Payments')

    # Amount fields
    sale_order_total = fields.Float(string='Sales Order Total')
    # pos_order_total = fields.Float(string='POS Order Total')
    payment_total = fields.Float(string='Payment Total')
    total_amount = fields.Float(string='Grand Total')

    # Quantity fields
    sale_order_qty = fields.Float(string='Sales Order Qty')
    # pos_order_qty = fields.Float(string='POS Order Qty')
    total_qty = fields.Float(string='Total Quantity')

    # Hierarchy fields
    level = fields.Integer(string='Level')
    parent_id = fields.Integer(string='Parent ID')

    # Display name field
    display_name = fields.Char(string='Name', compute='_compute_display_name')

    @api.depends('sales_rep_id', 'route_id', 'visit_id', 'level')
    def _compute_display_name(self):
        for record in self:
            if record.level == 1 and record.sales_rep_id:
                record.display_name = f"📊 {record.sales_rep_id.name}"
            elif record.level == 2 and record.route_id:
                record.display_name = f"🗺️ {record.route_id.name}"
            elif record.level == 3 and record.visit_id:
                record.display_name = f"👥 {record.visit_id.partner_id.name} - {record.visit_id.visit_type}"
            else:
                record.display_name = "Unknown"

    def init(self):
        """Hierarchical rep > route > visit report.

        Orders, order lines and payments are aggregated in separate CTEs and
        joined afterwards. Joining them first and summing (the previous
        version) multiplied each order total by its number of lines and
        payments, inflating every figure in the report.
        Cancelled orders and draft/cancelled payments are excluded.
        """
        self._cr.execute("""
            DROP VIEW IF EXISTS sales_rep_performance_report;
            CREATE OR REPLACE VIEW sales_rep_performance_report AS (
                WITH so AS (
                    SELECT so.id, so.route_id, so.visit_id, so.amount_total
                    FROM sale_order so
                    WHERE so.state != 'cancel'
                ),
                sol AS (
                    SELECT sol.order_id, SUM(sol.product_uom_qty) AS qty
                    FROM sale_order_line sol
                    WHERE sol.display_type IS NULL
                    GROUP BY sol.order_id
                ),
                so_agg AS (
                    SELECT so.id, so.route_id, so.visit_id, so.amount_total,
                           COALESCE(sol.qty, 0) AS qty
                    FROM so LEFT JOIN sol ON sol.order_id = so.id
                ),
                ap AS (
                    SELECT ap.id, ap.route_id, ap.visit_id, ap.amount
                    FROM account_payment ap
                    WHERE ap.state NOT IN ('draft', 'canceled', 'cancel', 'rejected')
                      AND ap.payment_type = 'inbound'
                ),
                route_orders AS (
                    SELECT route_id, COUNT(*) AS cnt, SUM(amount_total) AS total, SUM(qty) AS qty
                    FROM so_agg WHERE route_id IS NOT NULL GROUP BY route_id
                ),
                route_payments AS (
                    SELECT route_id, COUNT(*) AS cnt, SUM(amount) AS total
                    FROM ap WHERE route_id IS NOT NULL GROUP BY route_id
                ),
                visit_orders AS (
                    SELECT visit_id, COUNT(*) AS cnt, SUM(amount_total) AS total, SUM(qty) AS qty
                    FROM so_agg WHERE visit_id IS NOT NULL GROUP BY visit_id
                ),
                visit_payments AS (
                    SELECT visit_id, COUNT(*) AS cnt, SUM(amount) AS total
                    FROM ap WHERE visit_id IS NOT NULL GROUP BY visit_id
                ),
                route_rows AS (
                    SELECT srr.id AS route_id, srr.sales_rep_id, srr.company_id,
                           COALESCE(ro.cnt, 0) AS so_cnt, COALESCE(ro.total, 0) AS so_total,
                           COALESCE(ro.qty, 0) AS so_qty,
                           COALESCE(rp.cnt, 0) AS pay_cnt, COALESCE(rp.total, 0) AS pay_total
                    FROM sales_rep_route srr
                    LEFT JOIN route_orders ro ON ro.route_id = srr.id
                    LEFT JOIN route_payments rp ON rp.route_id = srr.id
                )
                -- Level 1: Sales Representatives
                SELECT
                    sr.id AS id,
                    sr.id AS sales_rep_id,
                    NULL::integer AS route_id,
                    NULL::integer AS visit_id,
                    1 AS level,
                    0 AS parent_id,
                    COALESCE(SUM(rr.so_cnt), 0)::integer AS sale_order_count,
                    COALESCE(SUM(rr.pay_cnt), 0)::integer AS payment_count,
                    COALESCE(SUM(rr.so_total), 0) AS sale_order_total,
                    COALESCE(SUM(rr.pay_total), 0) AS payment_total,
                    COALESCE(SUM(rr.so_total), 0) + COALESCE(SUM(rr.pay_total), 0) AS total_amount,
                    COALESCE(SUM(rr.so_qty), 0) AS sale_order_qty,
                    COALESCE(SUM(rr.so_qty), 0) AS total_qty,
                    sr.company_id AS company_id
                FROM sales_representative sr
                LEFT JOIN route_rows rr ON rr.sales_rep_id = sr.id
                WHERE sr.active = True
                GROUP BY sr.id, sr.company_id

                UNION ALL

                -- Level 2: Routes
                SELECT
                    (1000000 + rr.route_id) AS id,
                    rr.sales_rep_id,
                    rr.route_id,
                    NULL::integer AS visit_id,
                    2 AS level,
                    rr.sales_rep_id AS parent_id,
                    rr.so_cnt::integer AS sale_order_count,
                    rr.pay_cnt::integer AS payment_count,
                    rr.so_total AS sale_order_total,
                    rr.pay_total AS payment_total,
                    rr.so_total + rr.pay_total AS total_amount,
                    rr.so_qty AS sale_order_qty,
                    rr.so_qty AS total_qty,
                    rr.company_id
                FROM route_rows rr

                UNION ALL

                -- Level 3: Visits
                SELECT
                    (2000000 + srv.id) AS id,
                    srv.sales_rep_id,
                    srv.route_id,
                    srv.id AS visit_id,
                    3 AS level,
                    (1000000 + srv.route_id) AS parent_id,
                    COALESCE(vo.cnt, 0)::integer AS sale_order_count,
                    COALESCE(vp.cnt, 0)::integer AS payment_count,
                    COALESCE(vo.total, 0) AS sale_order_total,
                    COALESCE(vp.total, 0) AS payment_total,
                    COALESCE(vo.total, 0) + COALESCE(vp.total, 0) AS total_amount,
                    COALESCE(vo.qty, 0) AS sale_order_qty,
                    COALESCE(vo.qty, 0) AS total_qty,
                    srv.company_id
                FROM sales_rep_visit srv
                LEFT JOIN visit_orders vo ON vo.visit_id = srv.id
                LEFT JOIN visit_payments vp ON vp.visit_id = srv.id
            )
        """)
