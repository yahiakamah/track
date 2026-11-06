/** @odoo-module **/
import { Component, onWillStart, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const iso = (d) => {
    const z = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
    return z.toISOString().slice(0, 10);
};

export class SalesCommandCenter extends Component {
    static template = "sales_rep_management.CommandCenter";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        const now = new Date();
        this.state = useState({
            loading: true,
            date_from: iso(new Date(now.getFullYear(), now.getMonth(), 1)),
            date_to: iso(now),
            rep_id: "",
            supervisor_id: "",
            area_id: "",
            filters: { reps: [], supervisors: [], areas: [] },
            data: null,
        });
        onWillStart(async () => {
            this.state.filters = await this.orm.call("sales.rep.command.center", "get_filters", []);
            await this.load();
        });
    }

    async load() {
        this.state.loading = true;
        const s = this.state;
        this.state.data = await this.orm.call("sales.rep.command.center", "get_data", [], {
            date_from: s.date_from,
            date_to: s.date_to,
            rep_ids: s.rep_id ? [parseInt(s.rep_id)] : null,
            supervisor_id: s.supervisor_id ? parseInt(s.supervisor_id) : null,
            area_id: s.area_id ? parseInt(s.area_id) : null,
        });
        this.state.loading = false;
    }

    setPeriod(kind) {
        const now = new Date();
        let from = now;
        if (kind === "today") from = now;
        else if (kind === "week") from = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 1) % 7)); // week starts Saturday
        else if (kind === "month") from = new Date(now.getFullYear(), now.getMonth(), 1);
        else if (kind === "quarter") from = new Date(now.getFullYear(), Math.floor(now.getMonth() / 3) * 3, 1);
        else if (kind === "year") from = new Date(now.getFullYear(), 0, 1);
        this.state.date_from = iso(from);
        this.state.date_to = iso(now);
        this.load();
    }

    onFilter(field, ev) {
        this.state[field] = ev.target.value;
        this.load();
    }

    isSel(field, id) {
        return `${id}` === this.state[field];
    }

    abs(v) {
        return Math.abs(v || 0);
    }

    money(v) {
        const d = this.state.data;
        const n = Number(v || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
        if (!d) return n;
        return d.currency_position === "before" ? `${d.currency} ${n}` : `${n} ${d.currency}`;
    }

    display(section) {
        return section.money ? this.money(section.value) : section.value;
    }

    extraValue(v) {
        return typeof v === "number" ? this.money(v) : v;
    }

    open(section) {
        this.action.doAction({
            type: "ir.actions.act_window",
            name: section.title,
            res_model: section.model,
            domain: section.domain,
            views: [[false, "list"], [false, "form"]],
            target: "current",
        });
    }

    openRep(rep) {
        this.state.rep_id = String(rep.id);
        this.state.supervisor_id = "";
        this.state.area_id = "";
        this.load();
    }

    openArea(area) {
        this.state.area_id = String(area.id);
        this.state.rep_id = "";
        this.load();
    }

    complianceStyle() {
        const s = (this.state.data?.sections || []).find((x) => x.key === "visits");
        const pct = s ? parseFloat(String(s.sub)) || 0 : 0;
        return `--pct:${Math.min(100, pct)}`;
    }
}

registry.category("actions").add("sales_command_center", SalesCommandCenter);
