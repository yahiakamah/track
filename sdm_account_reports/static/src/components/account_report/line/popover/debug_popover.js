/** @odoo-module */

import { Component } from "@odoo/owl";

export class AccountReportDebugPopover extends Component {
    static template = "sdm_account_reports.AccountReportDebugPopover";
    static props = {
        close: Function,
        expressionsDetail: Array,
        onClose: Function,
    };
}
