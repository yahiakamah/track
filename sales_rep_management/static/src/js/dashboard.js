/** @odoo-module **/

import { Component, onWillStart, onMounted, onWillUnmount, useState, useRef, useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { loadJS } from "@web/core/assets";

function getLocalDateString(d = new Date()) {
    const yyyy = d.getFullYear();
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    return `${yyyy}-${mm}-${dd}`;
}

function formatToDatetime(val, isEnd = false) {
    if (!val) return false;
    let s = val.replace('T', ' ');
    if (s.length === 10) {
        s += isEnd ? ' 23:59:59' : ' 00:00:00';
    } else if (s.length === 16) {
        s += isEnd ? ':59' : ':00';
    }
    return s;
}

function formatToDate(val) {
    if (!val) return false;
    return val.split('T')[0].split(' ')[0];
}

class SalesRepDashboard extends Component {
    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        
        // Refs for charts
        this.visitChartRef = useRef("visitChart");
        this.efficiencyGaugeRef = useRef("efficiencyGauge");

        const todayStr = getLocalDateString();

        // UI State
        this.state = useState({
            date_from: `${todayStr}T00:00`,
            date_to: `${todayStr}T23:59`,
            sales_rep_id: "",
            reps: [],
            data: {
                kpis: {
                    total_routes: 0,
                    completed_routes: 0,
                    total_visits: 0,
                    completed_visits: 0,
                    total_sales: 0,
                    total_payments: 0,
                    route_completion: 0,
                    visit_completion: 0,
                    efficiency: 0,
                },
                route_status_data: [],
                daily_visit_data: [],
                security_alerts: [],
            },
        });

        // Lifecycle
        onWillStart(async () => {
            await loadJS("/web/static/lib/Chart/Chart.js");
            await this.loadInitialData();
            await this.refreshData();
        });

        onMounted(() => {
            this.renderCharts();
        });

        onWillUnmount(() => {
            this.destroyCharts();
        });

        // Reactivity for charts
        useEffect(() => {
            this.renderCharts();
        }, () => [this.state.data]);
    }

    async loadInitialData() {
        // Fetch sales representatives for the filter dropdown
        const reps = await this.orm.searchRead(
            "sales.representative",
            [],
            ["id", "name"]
        );
        this.state.reps = reps;
    }

    async refreshData() {
        if (!this.state.date_from || !this.state.date_to) {
            return;
        }
        const result = await this.orm.call(
            "sales.rep.dashboard",
            "get_dashboard_stats",
            [],
            {
                date_from: formatToDatetime(this.state.date_from, false),
                date_to: formatToDatetime(this.state.date_to, true),
                sales_rep_id: this.state.sales_rep_id ? parseInt(this.state.sales_rep_id) : false,
            }
        );
        this.state.data = result;
    }

    renderCharts() {
        this.destroyCharts();



        // 2. Visit Completion Stacked Bar Chart
        if (this.visitChartRef.el) {
            const ctx = this.visitChartRef.el.getContext("2d");
            const data = this.state.data.daily_visit_data;
            this.visitChart = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: data.map(d => d.date),
                    datasets: [
                        {
                            label: 'Completed',
                            data: data.map(d => d.completed),
                            backgroundColor: '#1e3a5f',
                        },
                        {
                            label: 'Pending',
                            data: data.map(d => d.pending),
                            backgroundColor: '#2d7a8d',
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: { stacked: true, grid: { display: false } },
                        y: { stacked: true, beginAtZero: true }
                    },
                    plugins: {
                        legend: { position: 'bottom', labels: { boxWidth: 12 } }
                    }
                }
            });
        }

        // 3. Efficiency Gauge (Semi-circle)
        if (this.efficiencyGaugeRef.el) {
            const ctx = this.efficiencyGaugeRef.el.getContext("2d");
            const val = this.state.data.kpis.efficiency;
            this.efficiencyGauge = new Chart(ctx, {
                type: 'doughnut',
                data: {
                    datasets: [{
                        data: [val, 100 - val],
                        backgroundColor: ['#28a745', '#e2e8f0'],
                        borderWidth: 0,
                    }]
                },
                options: {
                    rotation: 270,
                    circumference: 180,
                    cutout: '80%',
                    plugins: { legend: { display: false }, tooltip: { enabled: false } },
                    maintainAspectRatio: false,
                }
            });
        }
    }

    destroyCharts() {
        if (this.visitChart) this.visitChart.destroy();
        if (this.efficiencyGauge) this.efficiencyGauge.destroy();
    }

    formatCurrency(value) {
        return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    formatAlertDate(value) {
        if (!value) return "";
        const date = new Date(value.replace(' ', 'T') + 'Z');
        if (Number.isNaN(date.getTime())) return value;
        return date.toLocaleString();
    }

    async openSecurityAlerts() {
        let domain = [
            ["active", "=", true],
        ];
        if (this.state.sales_rep_id) {
            domain.push(["sales_rep_id", "=", parseInt(this.state.sales_rep_id)]);
        }
        await this.action.doAction({
            type: 'ir.actions.act_window',
            name: 'Security Alerts',
            res_model: 'sales.rep.security.alert',
            view_mode: 'list',
            views: [[false, 'list']],
            domain: domain,
            target: 'current',
        });
    }

    async openAction(resModel) {
        let name = resModel.split('.').pop().charAt(0).toUpperCase() + resModel.split('.').pop().slice(1) + 's';
        let dateField = 'date';
        let useDatetime = false;
        
        if (resModel === 'sales.rep.collection' || resModel === 'account.payment') {
            name = 'Payments';
            dateField = (resModel === 'sales.rep.collection') ? 'collection_date' : 'date';
        } else if (resModel === 'sales.rep.visit') {
            name = 'Visits';
            dateField = 'planned_time';
            useDatetime = true;
        } else if (resModel === 'sale.order') {
            name = 'Sales Orders';
            dateField = 'date_order';
            useDatetime = true;
        } else if (resModel === 'sales.rep.route') {
            name = 'Routes';
            dateField = 'date';
            useDatetime = false;
        }

        let fromVal = useDatetime ? formatToDatetime(this.state.date_from, false) : formatToDate(this.state.date_from);
        let toVal = useDatetime ? formatToDatetime(this.state.date_to, true) : formatToDate(this.state.date_to);

        let domain = [[dateField, '>=', fromVal], [dateField, '<=', toVal]];
        
        if (resModel === 'sale.order' || resModel === 'account.payment') {
            domain.push(['visit_id', '!=', false]);
        }

        if (this.state.sales_rep_id) {
            domain.push(['sales_rep_id', '=', parseInt(this.state.sales_rep_id)]);
        }

        await this.action.doAction({
            type: 'ir.actions.act_window',
            name: name,
            res_model: resModel,
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: domain,
            target: 'current',
        });
    }

    async openOnlineReps() {
        // Query server for IDs of currently online reps
        const onlineRepIds = await this.orm.call(
            'sales.rep.dashboard',
            'get_online_rep_ids',
            [],
            {
                sales_rep_id: this.state.sales_rep_id ? parseInt(this.state.sales_rep_id) : false,
            }
        );

        await this.action.doAction({
            type: 'ir.actions.act_window',
            name: 'Online Sales Representatives',
            res_model: 'sales.representative',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: [['id', 'in', onlineRepIds]],
            target: 'current',
        });
    }

    async openRoutes() {
        return this.openAction('sales.rep.route');
    }

    async openVisits() {
        return this.openAction('sales.rep.visit');
    }

    async openSalesOrders() {
        const saleOrderIds = await this.orm.call(
            'sales.rep.dashboard',
            'get_visit_related_sale_order_ids',
            [],
            {
                date_from: formatToDatetime(this.state.date_from, false),
                date_to: formatToDatetime(this.state.date_to, true),
                sales_rep_id: this.state.sales_rep_id ? parseInt(this.state.sales_rep_id) : false,
            }
        );

        await this.action.doAction({
            type: 'ir.actions.act_window',
            name: 'Sales Orders',
            res_model: 'sale.order',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: [['id', 'in', saleOrderIds]],
            target: 'current',
        });
    }

    async openPayments() {
        const paymentIds = await this.orm.call(
            'sales.rep.dashboard',
            'get_visit_related_payment_ids',
            [],
            {
                date_from: formatToDatetime(this.state.date_from, false),
                date_to: formatToDatetime(this.state.date_to, true),
                sales_rep_id: this.state.sales_rep_id ? parseInt(this.state.sales_rep_id) : false,
            }
        );

        await this.action.doAction({
            type: 'ir.actions.act_window',
            name: 'Payments',
            res_model: 'account.payment',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: [['id', 'in', paymentIds]],
            target: 'current',
        });
    }

    async openCompletedVisits() {
        let domain = [
            ['state', '=', 'completed'],
            ['planned_time', '>=', formatToDatetime(this.state.date_from, false)],
            ['planned_time', '<=', formatToDatetime(this.state.date_to, true)],
        ];

        if (this.state.sales_rep_id) {
            domain.push(['sales_rep_id', '=', parseInt(this.state.sales_rep_id)]);
        }

        await this.action.doAction({
            type: 'ir.actions.act_window',
            name: 'Completed Visits',
            res_model: 'sales.rep.visit',
            view_mode: 'list,form',
            views: [[false, 'list'], [false, 'form']],
            domain: domain,
            target: 'current',
        });
    }
}

SalesRepDashboard.template = "sales_rep_dashboard_template";

registry.category("actions").add("sales_rep_dashboard", SalesRepDashboard);