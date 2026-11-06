/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { Model } from "@web/model/model";
import { session } from "@web/session";
import { browser } from "@web/core/browser/browser";
import { formatDateTime, parseDate, parseDateTime } from "@web/core/l10n/dates";
import { KeepLast } from "@web/core/utils/concurrency";

const DATE_GROUP_FORMATS = {
    year: "yyyy",
    quarter: "'Q'q yyyy",
    month: "MMMM yyyy",
    week: "'W'WW yyyy",
    day: "dd MMM yyyy",
};

export class MapModel extends Model {
    setup(params, { notification, http }) {
        this.notification = notification;
        this.http = http;

        this.metaData = {
            ...params,
            mapBoxToken: session.map_box_token || "",
        };

        this.data = {
            count: 0,
            fetchingCoordinates: false,
            groupByKey: false,
            isGrouped: false,
            numberOfLocatedRecords: 0,
            partnerIds: [],
            partners: [],
            latitude: [],
            longitude: [],
            partnerToCache: [],
            recordGroups: [],
            records: [],
            routes: [],
            routingError: null,
            shouldUpdatePosition: true,
            useMapBoxAPI: !!this.metaData.mapBoxToken,
        };

        this.coordinateFetchingTimeoutHandle = undefined;
        this.shouldFetchCoordinates = false;
        this.keepLast = new KeepLast();
    }
    /**
     * @param {any} params
     * @returns {Promise<void>}
     */
    async load(params) {
        if (this.coordinateFetchingTimeoutHandle !== undefined) {
            this.stopFetchingCoordinates();
        }
        const metaData = {
            ...this.metaData,
            ...params,
        };

        // remove the properties fields from the group by
        metaData.groupBy = (metaData.groupBy || []).filter((groupBy) => {
            // properties fields are in the form `[propert_field_name].[property_entry_key]`
            const [fieldName] = groupBy.split(".");
            const field = metaData.fields[fieldName];
            return field?.type !== "properties";
        });

        this.data = await this._fetchData(metaData);
        this.metaData = metaData;

        this.notify();
    }
    /**
     * Tells the model to stop fetching coordinates.
     * In OSM mode, the model starts to fetch coordinates once every second after the
     * model has loaded.
     * This fetching has to be done every second if we don't want to be banned from OSM.
     * There are typically two cases when we need to stop fetching:
     * - when component is about to be unmounted because the request is bound to
     *   the component and it will crash if we do so.
     * - when calling the `load` method as it will start fetching new coordinates.
     */
    stopFetchingCoordinates() {
        browser.clearTimeout(this.coordinateFetchingTimeoutHandle);
        this.coordinateFetchingTimeoutHandle = undefined;
        this.shouldFetchCoordinates = false;
    }




    /**
     * Handles the case of an empty map.
     * Handles the case where the model is res_partner.
     * Fetches the records according to the model given in the arch.
     * If the records has no partner_id field it is sliced from the array.
     *
     * @protected
     * @params {any} metaData
     * @return {Promise<any>}
     */

    async _fetchData(metaData) {
        const data = {
            count: 0,
            fetchingCoordinates: false,
            groupByKey: metaData.groupBy.length ? metaData.groupBy[0] : false,
            isGrouped: metaData.groupBy.length > 0,
            numberOfLocatedRecords: 0,
            partnerIds: [],
            partners: [],
            partnerToCache: [],
            recordGroups: [],
            records: [],
            routes: [],
            routingError: null,
            shouldUpdatePosition: true,
            useMapBoxAPI: !!metaData.mapBoxToken,
        };
        const results = await this.keepLast.add(this._fetchRecordData(metaData, data));

        const datetimeFields = metaData.fieldNames.filter(
            (name) => metaData.fields[name].type == "datetime"
        );
        for (const record of results.records) {
            // convert date fields from UTC to local timezone
            for (const field of datetimeFields) {
                if (record[field]) {
                    const dateUTC = luxon.DateTime.fromFormat(
                        record[field],
                        "yyyy-MM-dd HH:mm:ss",
                        { zone: "UTC" }
                    );
                    record[field] = formatDateTime(dateUTC, { format: "yyyy-MM-dd HH:mm:ss" });
                }
            }
        }

        data.records = results.records;
        data.count = results.length;
        if (data.isGrouped) {
            data.recordGroups = await this._getRecordGroups(metaData, data);
        } else {
            data.recordGroups = [];
        }

        return data;
    }

    /**
     * Fetch the records for a given model.
     *
     * @protected
     * @returns {Promise}
     */
    _fetchRecordData(metaData, data) {
        const fieldNames = data.groupByKey
            ? metaData.fieldNames.concat(data.groupByKey.split(":")[0])
            : metaData.fieldNames;
        const specification = {};
        for (const fieldName of fieldNames) {
            specification[fieldName] = {};
            if (["many2one", "one2many", "many2many"].includes(metaData.fields[fieldName].type)) {
                specification[fieldName].fields = { display_name: {} };
            }
        }
        const orderBy = [];
        if (metaData.defaultOrder) {
            orderBy.push(metaData.defaultOrder.name);
            if (metaData.defaultOrder.asc) {
                orderBy.push("ASC");
            }
        }
        return this.orm.webSearchRead(metaData.resModel, metaData.domain, {
            specification,
            limit: metaData.limit,
            offset: metaData.offset,
            order: orderBy.join(" "),
            context: metaData.context,
        });
    }

    _getEmptyGroupLabel(fieldName) {
        return _t("None");
    }

    /**
     * @protected
     * @returns {Object} the fetched records grouped by the groupBy field.
     */
    async _getRecordGroups(metaData, data) {
        const [fieldName, subGroup] = data.groupByKey.split(":");
        const fieldType = metaData.fields[fieldName].type;
        const groups = {};
        function addToGroup(id, name, record) {
            if (!groups[id]) {
                groups[id] = {
                    name,
                    records: [],
                };
            }
            groups[id].records.push(record);
        }
        for (const record of data.records) {
            const value = record[fieldName];
            let id, name;
            if (["one2many", "many2many"].includes(fieldType)) {
                if (value.length) {
                    for (const r of value) {
                        addToGroup(r.id, r.display_name, record);
                    }
                } else {
                    id = name = this._getEmptyGroupLabel(fieldName);
                    addToGroup(id, name, record);
                }
            } else {
                if (["date", "datetime"].includes(fieldType) && value) {
                    const date = fieldType === "date" ? parseDate(value) : parseDateTime(value);
                    id = name = date.toFormat(DATE_GROUP_FORMATS[subGroup]);
                } else if (fieldType === "boolean") {
                    id = name = value ? _t("Yes") : _t("No");
                } else if (fieldType === "selection") {
                    const selected = metaData.fields[fieldName].selection.find((o) => o[0] === value);
                    id = name = selected ? selected[1] : value;
                } else if (fieldType === "many2one" && value) {
                    id = value.id;
                    name = value.display_name;
                } else {
                    id = value;
                    name = value;
                }
                if (!id && !name) {
                    id = name = this._getEmptyGroupLabel(fieldName);
                }
                addToGroup(id, name, record);
            }
        }
        return groups;
    }
}

MapModel.services = ["notification", "http"];
MapModel.COORDINATE_FETCH_DELAY = 1000;
