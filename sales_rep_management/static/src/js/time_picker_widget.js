/** @odoo-module **/

import { registry } from "@web/core/registry";
import { standardFieldProps } from "@web/views/fields/standard_field_props";
import { Component, useState, onWillStart, onWillUpdateProps, useRef, onMounted, onWillUnmount } from "@odoo/owl";

export class RecurrentTimePicker extends Component {
    static template = "sales_rep_management.RecurrentTimePicker";
    static props = {
        ...standardFieldProps,
    };

    setup() {
        this.root = useRef("root");
        this.state = useState({
            isOpen: false,
            hour12: 8,
            isPM: false,
            displayTime: "08:00 AM",
        });

        onWillStart(() => this._updateFromProps(this.props));
        onWillUpdateProps((nextProps) => this._updateFromProps(nextProps));

        this._onWindowClick = (ev) => {
            if (this.state.isOpen && this.root.el && !this.root.el.contains(ev.target)) {
                this.state.isOpen = false;
            }
        };

        onMounted(() => {
            window.addEventListener("click", this._onWindowClick);
        });

        onWillUnmount(() => {
            window.removeEventListener("click", this._onWindowClick);
        });
    }

    _updateFromProps(props) {
        const value = parseFloat(props.record.data[props.name] || 8.0);
        let h = Math.floor(value);
        const isPM = h >= 12;
        
        let h12 = h % 12;
        if (h12 === 0) h12 = 12;

        this.state.hour12 = h12;
        this.state.isPM = isPM;
        const ampm = isPM ? 'PM' : 'AM';
        this.state.displayTime = `${h12.toString().padStart(2, '0')}:00 ${ampm}`;
    }

    get hours() {
        return [12, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11];
    }

    _getRotation(hour) {
        return (hour % 12) * 30;
    }

    _onHourClick(hour) {
        this.state.hour12 = hour;
        this._updateValue();
    }

    _toggleAMPM(target) {
        this.state.isPM = (target === 'PM');
        this._updateValue();
    }

    _togglePicker() {
        this.state.isOpen = !this.state.isOpen;
    }

    _updateValue() {
        let hour24 = this.state.hour12 % 12;
        if (this.state.isPM) {
            hour24 += 12;
        }
        const valStr = hour24.toFixed(1);
        this.props.record.update({ [this.props.name]: valStr });
        
        // Update display immediately for feedback
        const ampm = this.state.isPM ? 'PM' : 'AM';
        this.state.displayTime = `${this.state.hour12.toString().padStart(2, '0')}:00 ${ampm}`;
    }

    _onConfirm() {
        this.state.isOpen = false;
        this.props.record.save();
    }
}

registry.category("fields").add("recurrent_time_picker", {
    component: RecurrentTimePicker,
    supportedTypes: ["selection"],
});
