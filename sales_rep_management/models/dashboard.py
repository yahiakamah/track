from odoo import models, fields, api, _
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
import logging

_logger = logging.getLogger(__name__)


class SalesRepDashboard(models.TransientModel):
    _name = 'sales.rep.dashboard'
    _description = 'Sales Representative Dashboard'

    # Date Filters
    date_from = fields.Datetime(
        string='From Date & Time',
        default=fields.Datetime.now
    )
    date_to = fields.Datetime(
        string='To Date & Time',
        default=fields.Datetime.now
    )
    sales_rep_id = fields.Many2one(
        'sales.representative',
        string='Sales Representative'
    )
    
    # KPI Fields
    total_routes = fields.Integer(string='Total Routes')
    completed_routes = fields.Integer(string='Completed Routes')
    total_visits = fields.Integer(string='Total Visits')
    completed_visits = fields.Integer(string='Completed Visits')
    total_sales = fields.Float(string='Total Sales')
    total_collections = fields.Float(string='Total Payments')
    route_completion_rate = fields.Float(string='Route Completion Rate (%)')
    visit_completion_rate = fields.Float(string='Visit Completion Rate (%)')
    collection_efficiency = fields.Float(string='Payment Efficiency (%)')
    
    # Top Performers
    top_sales_rep_ids = fields.Many2many(
        'sales.representative',
        'dashboard_top_sales_rel',
        string='Top Sales Performers'
    )
    top_collection_rep_ids = fields.Many2many(
        'sales.representative',
        'dashboard_top_collection_rel',
        string='Top Collection Performers'
    )

    @api.model
    def _parse_filter_dates(self, date_from=None, date_to=None):
        """Parse date_from and date_to into datetime bounds."""
        from datetime import date as date_type

        def _parse(val, is_end=False):
            if not val:
                return None
            if isinstance(val, datetime):
                return val
            if isinstance(val, date_type):
                default_time = datetime.max.time().replace(microsecond=0) if is_end else datetime.min.time()
                return datetime.combine(val, default_time)
            if isinstance(val, str):
                val = val.strip().replace('T', ' ')
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                    try:
                        dt = datetime.strptime(val, fmt)
                        if fmt == '%Y-%m-%d':
                            default_time = datetime.max.time().replace(microsecond=0) if is_end else datetime.min.time()
                            return datetime.combine(dt.date(), default_time)
                        return dt
                    except ValueError:
                        continue
            return None

        date_from_dt = _parse(date_from, is_end=False)
        date_to_dt = _parse(date_to, is_end=True)

        if not date_from_dt:
            today = fields.Date.context_today(self)
            date_from_dt = datetime.combine(today, datetime.min.time())
        if not date_to_dt:
            today = fields.Date.context_today(self)
            date_to_dt = datetime.combine(today, datetime.max.time().replace(microsecond=0))

        return date_from_dt, date_to_dt
    
    @api.model
    def get_dashboard_stats(self, date_from=None, date_to=None, sales_rep_id=None):
        """Get combined dashboard statistics for the OWL component"""
        date_from_dt, date_to_dt = self._parse_filter_dates(date_from, date_to)
        date_from = date_from_dt.date()
        date_to = date_to_dt.date()

        # Query visits directly based on planned_time
        visit_domain = [
            ('planned_time', '>=', date_from_dt),
            ('planned_time', '<=', date_to_dt),
            '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
        ]
        if sales_rep_id:
            visit_domain.append(('sales_rep_id', '=', sales_rep_id))
            
        visits = self.env['sales.rep.visit'].sudo().search(visit_domain)
        completed_visits = visits.filtered(lambda v: v.state == 'completed')
        
        # Query routes directly based on date and sales_rep_id
        route_domain = [
            ('date', '>=', date_from),
            ('date', '<=', date_to),
            '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
        ]
        if sales_rep_id:
            route_domain.append(('sales_rep_id', '=', sales_rep_id))

        routes = self.env['sales.rep.route'].sudo().search(route_domain)
        completed_routes = routes.filtered(lambda r: r.state == 'completed')
        
        _logger.info("Dashboard Stats: Found %s routes (%s completed) and %s visits (%s completed) for date range %s to %s", 
                     len(routes), len(completed_routes), len(visits), len(completed_visits), date_from, date_to)

        # Get financial data (Sales and Payments) from the selected visits
        # 1. Total Sales from the selected visits
        visit_orders = self.env['sale.order'].sudo().search([('visit_id', 'in', visits.ids)])
        total_sales = sum(visits.mapped('sale_amount'))
        
        # 2. Total Payments:
        # a) Direct payments linked to visits
        direct_payments = visits.mapped('payment_ids').filtered(
            lambda p: p.state in ['posted', 'in_process', 'paid'] and p.date and date_from <= p.date <= date_to
        )
        
        # b) Payments related to sales orders that are linked to visits
        visit_invoices = visit_orders.mapped('invoice_ids').filtered(lambda i: i.state == 'posted')
        reconciled_inv_payments = self.env['account.payment'].sudo().search([
            ('reconciled_invoice_ids', 'in', visit_invoices.ids),
            ('state', 'in', ['posted', 'in_process', 'paid']),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
        ])
        
        matched_debit_payments = visit_invoices.mapped('line_ids.matched_debit_ids.debit_move_id.payment_id')
        matched_credit_payments = visit_invoices.mapped('line_ids.matched_credit_ids.credit_move_id.payment_id')
        reconciled_line_payments = (matched_debit_payments | matched_credit_payments).filtered(
            lambda p: p and p.state in ['posted', 'in_process', 'paid'] and p.date and date_from <= p.date <= date_to
        )
        
        payments = direct_payments | reconciled_inv_payments | reconciled_line_payments
        total_payments = sum(payments.mapped('amount'))
        
        _logger.info("Dashboard Stats: Total Sales Order: %s", total_sales)
        _logger.info("Dashboard Stats: Total Payments: %s", total_payments)

        # Calculate KPIs
        total_routes = len(routes)
        done_routes = len(completed_routes)
        # Journey-plan compliance: planned customers of the routes in range vs
        # customers actually visited. Counting only visits that were started
        # (previous behaviour) made "visit completion" ~100% by construction.
        planned_customers = routes.mapped('route_customer_ids').filtered(lambda rc: rc.state != 'cancelled')
        total_visits = len(planned_customers) or len(visits)
        done_visits = (len(planned_customers.filtered(lambda rc: rc.state == 'visited'))
                       if planned_customers else len(completed_visits))
        
        route_completion = (done_routes / total_routes * 100) if total_routes else 0
        visit_completion = (done_visits / total_visits * 100) if total_visits else 0
        efficiency = (total_payments / total_sales * 100) if total_sales else 0
        
        # Donut Chart Data: Route Status Distribution
        route_status_counts = {}
        for r in routes:
            st = r.state
            route_status_counts[st] = route_status_counts.get(st, 0) + 1
            
        # Stacked Bar Chart Data: Daily Visit Completion
        # Group visits by planned date
        daily_visits = {}
        current_date = date_from
        while current_date <= date_to:
            daily_visits[current_date] = {'total': 0, 'completed': 0}
            current_date += timedelta(days=1)
            
        for visit in visits:
            v_date = visit.planned_time.date() if visit.planned_time else visit.route_id.date
            if v_date in daily_visits:
                daily_visits[v_date]['total'] += 1
                if visit.state == 'completed':
                    daily_visits[v_date]['completed'] += 1
                    
        daily_data = []
        for d in sorted(daily_visits.keys()):
            stats = daily_visits[d]
            daily_data.append({
                'date': d.strftime('%m/%d'),
                'total': stats['total'],
                'completed': stats['completed'],
                'pending': stats['total'] - stats['completed']
            })

        # Security alerts: get all active alerts (unfiltered by date range)
        alert_domain = [
            ('active', '=', True),
            '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
        ]
        if sales_rep_id:
            alert_domain.append(('sales_rep_id', '=', sales_rep_id))

        security_alerts_records = self.env['sales.rep.security.alert'].sudo().search(
            alert_domain, order='detected_at desc'
        )
        selection = dict(
            self.env["sales.rep.security.alert"]._fields["alert_type"].selection
        )

        rep_alert_map = {}
        for alert in security_alerts_records:
            rep = alert.sales_rep_id
            if rep.id not in rep_alert_map:
                rep_alert_map[rep.id] = {
                    'rep_id': rep.id,
                    'rep_name': rep.name,
                    'last_detected_at': fields.Datetime.to_string(alert.detected_at),
                    'latitude': alert.latitude,
                    'longitude': alert.longitude,
                    'alert_type': selection.get(alert.alert_type, alert.alert_type),
                    'alert_count': 1,
                }
            else:
                rep_alert_map[rep.id]['alert_count'] += 1

        security_alerts = list(rep_alert_map.values())
        _logger.info("Security Alerts: %s", security_alerts)

        # Get online status based on Odoo session log history
        online_count = 0
        total_devices = 0
        session_log_model = self.env.get('sales.representative.session.log')
        if session_log_model is not None:
            active_reps = self.env['sales.representative'].search([])
            total_devices = len(active_reps)
            
            if sales_rep_id:
                rep = self.env['sales.representative'].browse(sales_rep_id)
                if rep.active:
                    total_devices = 1
                    latest_log = session_log_model.sudo().search([
                        ('sales_rep_id', '=', rep.id)
                    ], order='timestamp desc', limit=1)
                    if latest_log and latest_log.action_type == 'login':
                        online_count = 1
            else:
                for rep in active_reps:
                    latest_log = session_log_model.sudo().search([
                        ('sales_rep_id', '=', rep.id)
                    ], order='timestamp desc', limit=1)
                    if latest_log and latest_log.action_type == 'login':
                        online_count += 1
            
        # Get session logs according to date range and sales_rep_id filter
        session_logs_data = []
        session_log_model = self.env.get('sales.representative.session.log')
        if session_log_model is not None:
            session_domain = [
                ('timestamp', '>=', date_from_dt),
                ('timestamp', '<=', date_to_dt),
            ]
            if sales_rep_id:
                session_domain.append(('sales_rep_id', '=', sales_rep_id))
            logs = session_log_model.sudo().search(session_domain, order='timestamp desc')
            for log in logs:
                session_logs_data.append({
                    'id': log.id,
                    'rep_name': log.sales_rep_id.name,
                    'action_type': log.action_type,
                    'timestamp': fields.Datetime.to_string(log.timestamp),
                })

        return {
            'kpis': {
                'total_routes': total_routes,
                'completed_routes': done_routes,
                'total_visits': total_visits,
                'completed_visits': done_visits,
                'total_sales': total_sales,
                'total_payments': total_payments,
                'route_completion': round(route_completion, 1),
                'visit_completion': round(visit_completion, 1),
                'efficiency': round(efficiency, 1),
            },
            'route_status_data': [
                {'status': s, 'count': c} for s, c in route_status_counts.items()
            ],
            'daily_visit_data': daily_data,
            'security_alerts': security_alerts,
            'online_count': online_count,
            'total_devices': total_devices,
            'session_logs_data': session_logs_data,
        }

    @api.model
    def get_online_rep_ids(self, sales_rep_id=None):
        """Return list of IDs for currently online sales representatives."""
        online_ids = []
        session_log_model = self.env.get('sales.representative.session.log')
        if session_log_model is None:
            return online_ids

        if sales_rep_id:
            reps = self.env['sales.representative'].browse(sales_rep_id).filtered('active')
        else:
            reps = self.env['sales.representative'].search([])

        for rep in reps:
            latest_log = session_log_model.sudo().search(
                [('sales_rep_id', '=', rep.id)],
                order='timestamp desc', limit=1
            )
            if latest_log and latest_log.action_type == 'login':
                online_ids.append(rep.id)

        return online_ids

    @api.model
    def get_visit_related_payment_ids(self, date_from=None, date_to=None, sales_rep_id=None):
        """Return list of IDs for payments linked directly to visits or to sales orders linked to visits."""
        date_from_dt, date_to_dt = self._parse_filter_dates(date_from, date_to)
        date_from = date_from_dt.date()
        date_to = date_to_dt.date()

        visit_domain = [
            ('planned_time', '>=', date_from_dt),
            ('planned_time', '<=', date_to_dt),
            '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
        ]
        if sales_rep_id:
            visit_domain.append(('sales_rep_id', '=', sales_rep_id))
            
        visits = self.env['sales.rep.visit'].sudo().search(visit_domain)
        
        direct_payments = visits.mapped('payment_ids').filtered(
            lambda p: p.state in ['posted', 'in_process', 'paid'] and p.date and date_from <= p.date <= date_to
        )
        
        visit_orders = self.env['sale.order'].sudo().search([('visit_id', 'in', visits.ids)])
        visit_invoices = visit_orders.mapped('invoice_ids').filtered(lambda i: i.state == 'posted')
        
        reconciled_inv_payments = self.env['account.payment'].sudo().search([
            ('reconciled_invoice_ids', 'in', visit_invoices.ids),
            ('state', 'in', ['posted', 'in_process', 'paid']),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
        ])
        
        matched_debit_payments = visit_invoices.mapped('line_ids.matched_debit_ids.debit_move_id.payment_id')
        matched_credit_payments = visit_invoices.mapped('line_ids.matched_credit_ids.credit_move_id.payment_id')
        reconciled_line_payments = (matched_debit_payments | matched_credit_payments).filtered(
            lambda p: p and p.state in ['posted', 'in_process', 'paid'] and p.date and date_from <= p.date <= date_to
        )
        
        all_payments = direct_payments | reconciled_inv_payments | reconciled_line_payments
        return all_payments.ids

    @api.model
    def get_visit_related_sale_order_ids(self, date_from=None, date_to=None, sales_rep_id=None):
        """Return list of IDs for sales orders linked to visits in the given date range."""
        date_from_dt, date_to_dt = self._parse_filter_dates(date_from, date_to)

        visit_domain = [
            ('planned_time', '>=', date_from_dt),
            ('planned_time', '<=', date_to_dt),
            '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
        ]
        if sales_rep_id:
            visit_domain.append(('sales_rep_id', '=', sales_rep_id))
            
        visits = self.env['sales.rep.visit'].sudo().search(visit_domain)
        visit_orders = self.env['sale.order'].sudo().search([('visit_id', 'in', visits.ids)])
        return visit_orders.ids

    def get_dashboard_data(self):
        """Get dashboard data for current filters (used by Form View)"""
        # Call the model method with self's values
        stats = self.get_dashboard_stats(self.date_from, self.date_to, self.sales_rep_id.id)
        kpis = stats['kpis']
        
        # Update self fields
        self.total_routes = kpis['total_routes']
        self.completed_routes = kpis['completed_routes']
        self.total_visits = kpis['total_visits']
        self.completed_visits = kpis['completed_visits']
        self.total_sales = kpis['total_sales']
        self.total_collections = kpis['total_payments']
        self.route_completion_rate = kpis['route_completion']
        self.visit_completion_rate = kpis['visit_completion']
        self.collection_efficiency = kpis['efficiency']
        
        return True

    def get_top_customers_data(self):
        """Get top customers by sales/collections from visits in range"""
        date_from_dt, date_to_dt = self._parse_filter_dates(self.date_from, self.date_to)
        
        domain = [
            ('planned_time', '>=', date_from_dt),
            ('planned_time', '<=', date_to_dt),
            ('state', '=', 'completed'),
            '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
        ]
        
        if self.sales_rep_id:
            domain.append(('sales_rep_id', '=', self.sales_rep_id.id))
        
        visits = self.env['sales.rep.visit'].search(domain)
        
        # Group by customer and sum sales
        customer_sales = {}
        customer_collections = {}
        
        for visit in visits:
            customer = visit.partner_id
            
            # Sales
            if customer.id in customer_sales:
                customer_sales[customer.id]['amount'] += visit.sale_amount
            else:
                customer_sales[customer.id] = {
                    'customer': customer.name,
                    'amount': visit.sale_amount
                }
            
            # Collections (Payments)
            collections_amount = sum(
                visit.payment_ids.filtered(
                    lambda p: p.state in ['posted', 'in_process', 'paid']
                ).mapped('amount')
            )
            
            if customer.id in customer_collections:
                customer_collections[customer.id]['amount'] += collections_amount
            else:
                customer_collections[customer.id] = {
                    'customer': customer.name,
                    'amount': collections_amount
                }
        
        # Sort and get top 10
        top_sales = sorted(
            customer_sales.values(),
            key=lambda x: x['amount'],
            reverse=True
        )[:10]
        
        top_collections = sorted(
            customer_collections.values(),
            key=lambda x: x['amount'],
            reverse=True
        )[:10]
        
        return {
            'top_sales': top_sales,
            'top_collections': top_collections
        }
    
    def get_rep_performance_data(self):
        """Get performance comparison between sales reps based on visits in range"""
        if self.sales_rep_id:
            # If specific rep selected, compare with team members
            if self.sales_rep_id.supervisor_id:
                reps = self.sales_rep_id.supervisor_id.team_member_ids
            else:
                reps = self.env['sales.representative'].search([
                    '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
                ])
        else:
            # Get all active reps
            reps = self.env['sales.representative'].search([
                ('active', '=', True),
                '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
            ])
        
        performance_data = []
        date_from_dt, date_to_dt = self._parse_filter_dates(self.date_from, self.date_to)
        
        for rep in reps:
            # Get rep's visits for the period
            visits_all = self.env['sales.rep.visit'].search([
                ('sales_rep_id', '=', rep.id),
                ('planned_time', '>=', date_from_dt),
                ('planned_time', '<=', date_to_dt),
                '|', ('company_id', '=', False), ('company_id', 'in', self.env.companies.ids)
            ])
            
            visits = visits_all.filtered(lambda v: v.state == 'completed')
            payments = visits.mapped('payment_ids').filtered(
                lambda p: p.state in ['posted', 'in_process', 'paid']
            )
            
            performance_data.append({
                'rep_name': rep.name,
                'total_visits': len(visits_all),
                'completed_visits': len(visits),
                'total_sales': sum(visits.mapped('sale_amount')),
                'total_collections': sum(payments.mapped('amount')),
                'completion_rate': (
                    len(visits) / len(visits_all)
                    if visits_all else 0
                )
            })
        
        # Sort by total sales
        performance_data.sort(key=lambda x: x['total_sales'], reverse=True)
        
        return performance_data[:10]  # Top 10 performers
    
    def action_open_routes(self):
        """Open routes view with current filters derived from visits"""
        date_from_dt, date_to_dt = self._parse_filter_dates(self.date_from, self.date_to)
        
        visit_domain = [
            ('planned_time', '>=', date_from_dt),
            ('planned_time', '<=', date_to_dt)
        ]
        if self.sales_rep_id:
            visit_domain.append(('sales_rep_id', '=', self.sales_rep_id.id))
        
        visits = self.env['sales.rep.visit'].search(visit_domain)
        route_ids = visits.mapped('route_id').ids
        
        domain = [('id', 'in', route_ids)]
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Routes'),
            'res_model': 'sales.rep.route',
            'view_mode': 'list,form',
            'domain': domain,
            'context': {'default_date': date_to_dt.date()}
        }
    
    def action_open_visits(self):
        """Open visits view with current filters applied directly to visits"""
        date_from_dt, date_to_dt = self._parse_filter_dates(self.date_from, self.date_to)
        
        domain = [
            ('planned_time', '>=', date_from_dt),
            ('planned_time', '<=', date_to_dt)
        ]
        
        if self.sales_rep_id:
            domain.append(('sales_rep_id', '=', self.sales_rep_id.id))
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Visits'),
            'res_model': 'sales.rep.visit',
            'view_mode': 'list,form',
            'domain': domain
        }
    
    def action_open_collections(self):
        """Open collections view with current filters derived from visits"""
        date_from_dt, date_to_dt = self._parse_filter_dates(self.date_from, self.date_to)
        date_from = date_from_dt.date()
        date_to = date_to_dt.date()
        
        visit_domain = [
            ('planned_time', '>=', date_from_dt),
            ('planned_time', '<=', date_to_dt)
        ]
        if self.sales_rep_id:
            visit_domain.append(('sales_rep_id', '=', self.sales_rep_id.id))
        
        visits = self.env['sales.rep.visit'].search(visit_domain)
        payments = visits.mapped('payment_ids').filtered(
            lambda p: p.state in ['posted', 'in_process', 'paid'] and p.date and date_from <= p.date <= date_to
        )
        
        domain = [('id', 'in', payments.ids)]
        
        return {
            'type': 'ir.actions.act_window',
            'name': _('Payments'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': domain
        }