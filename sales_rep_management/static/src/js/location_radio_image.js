/** @odoo-module */

import { registry } from "@web/core/registry";
import { RadioField, radioField } from "@web/views/fields/radio/radio_field";

class LocationRadioImageField extends RadioField {
    static template = "sales_rep_management.LocationRadioImageField";
}

registry.category("fields").add("location_radio_image", {
    ...radioField,
    component: LocationRadioImageField,
});
