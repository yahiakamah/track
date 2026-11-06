/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
/*global L*/

import { renderToString } from "@web/core/utils/render";
import { delay } from "@web/core/utils/concurrency";

import {
    Component,
    onWillUnmount,
    onWillUpdateProps,
    useEffect,
    useRef,
    useState,
} from "@odoo/owl";

const apiTilesRouteWithToken =
    "https://api.mapbox.com/styles/v1/{id}/tiles/{z}/{x}/{y}?access_token={accessToken}";
const apiTilesRouteWithoutToken = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png";

const colors = [
    "#F06050",
    "#6CC1ED",
    "#F7CD1F",
    "#814968",
    "#30C381",
    "#D6145F",
    "#475577",
    "#F4A460",
    "#EB7E7F",
    "#2C8397",
];

const mapTileAttribution = `
    © <a href="https://www.mapbox.com/about/maps/">Mapbox</a>
    © <a href="http://www.openstreetmap.org/copyright">OpenStreetMap</a>
    <strong>
        <a href="https://www.mapbox.com/map-feedback/" target="_blank">
            Improve this map
        </a>
    </strong>`;

export class MapRenderer extends Component {
    setup() {
        this.leafletMap = null;
        this.markers = [];
        this.polylines = [];
        this.mapContainerRef = useRef("mapContainer");
        this.state = useState({
            closedGroupIds: [],
            expendedPinList: false,
        });
        this.nextId = 1;

        useEffect(
            () => {
                this.leafletMap = L.map(this.mapContainerRef.el, {
                    maxBounds: [L.latLng(180, -180), L.latLng(-180, 180)],
                });
                const useMapBox = this.props.model.data.useMapBoxAPI;
                const tileOptions = {
                    attribution: mapTileAttribution,
                    minZoom: 2,
                    maxZoom: 19,
                };
                if (useMapBox) {
                    Object.assign(tileOptions, {
                        tileSize: 512,
                        zoomOffset: -1,
                        id: "mapbox/streets-v11",
                        accessToken: this.props.model.metaData.mapBoxToken,
                    });
                }
                L.tileLayer(this.apiTilesRoute, tileOptions).addTo(this.leafletMap);
            },
            () => []
        );
        useEffect(() => {
            this.updateMap();
        });

        onWillUpdateProps(this.onWillUpdateProps);
        onWillUnmount(this.onWillUnmount);
    }
    /**
     * Update group opened/closed state.
     */
    async onWillUpdateProps(nextProps) {
        if (this.props.model.data.groupByKey !== nextProps.model.data.groupByKey) {
            this.state.closedGroupIds = [];
        }
    }
    /**
     * Remove map and the listeners on its markers and routes.
     */
    onWillUnmount() {
        this.removeMarkers();
        this.removeRoutes();
        if (this.leafletMap) {
            this.leafletMap.remove();
        }
    }

    /**
     * Return the route to the tiles api with or without access token.
     *
     * @returns {string}
     */
    get apiTilesRoute() {
        return this.props.model.data.useMapBoxAPI
            ? apiTilesRouteWithToken
            : apiTilesRouteWithoutToken;
    }

    /**
     * If there's located records, adds the corresponding marker on the map.
     * Binds events to the created markers.
     */
    addMarkers() {
        this.removeMarkers();

        const markersInfo = {};
        let records = this.props.model.data.records;
        if (this.props.model.data.isGrouped) {
            records = Object.entries(this.props.model.data.recordGroups)
                .filter(([key]) => !this.state.closedGroupIds.includes(key))
                .flatMap(([groupId, value]) => value.records.map((elem) => ({ ...elem, groupId })));
        }

        const pinInSamePlace = {};
        for (const record of records) {
            let latitude = Number(record.latitude)
            let longitude = Number(record.longitude)
            if (record && latitude && longitude) {
                const lat_long = `${latitude}-${longitude}`;
                const group = this.props.model.data.recordGroups ? `-${record.groupId}` : "";
                const key = `${lat_long}${group}`;
                if (key in markersInfo) {
                    markersInfo[key].record = record;
                    markersInfo[key].ids.push(record.id);
                } else {
                    pinInSamePlace[lat_long] = ++pinInSamePlace[lat_long] || 0;
                    markersInfo[key] = {
                        record: record,
                        ids: [record.id],
                        pinInSamePlace: pinInSamePlace[lat_long],
                    };
                }
            }
        }

        for (const markerInfo of Object.values(markersInfo)) {
            const params = {
                count: markerInfo.ids.length,
                isMulti: markerInfo.ids.length > 1,
                number: this.props.model.data.records.indexOf(markerInfo.record) + 1,
                numbering: this.props.model.metaData.numbering,
            };

            if (this.props.model.data.isGrouped) {
                const groupId = markerInfo.record.groupId;
                params.color = this.getGroupColor(groupId);
                params.number =
                    this.props.model.data.recordGroups[groupId].records.findIndex((record) => {
                        return record.id === markerInfo.record.id;
                    }) + 1;
            }

            // Icon creation
            const iconInfo = {
                className: "o-map-renderer--marker",
                html: renderToString("spd_leaflet_map.marker", params),
            };

            const offset = markerInfo.pinInSamePlace * 0.000025;
            // Attach marker with icon and popup
            let latitude = Number(markerInfo.record.latitude)
            let longitude = Number(markerInfo.record.longitude)
               const marker = L.marker(
                [
                    latitude + offset,
                    longitude - offset,
                ],
                { icon: L.divIcon(iconInfo) }
            );
            marker.addTo(this.leafletMap);
            marker.on("click", () => {
                this.createMarkerPopup(markerInfo, offset);
            });
            this.markers.push(marker);
        }
    }
    /**
     * If there are computed routes, create polylines and add them to the map.
     * each element of this.props.routeInfo[0].legs array represent the route between
     * two waypoints thus each of these must be a polyline.
     */
    addRoutes() {
        this.removeRoutes();
        if (!this.props.model.data.useMapBoxAPI || !this.props.model.data.routes.length) {
            return;
        }

        for (const leg of this.props.model.data.routes[0].legs) {
            const latLngs = [];
            for (const step of leg.steps) {
                for (const coordinate of step.geometry.coordinates) {
                    latLngs.push(L.latLng(coordinate[1], coordinate[0]));
                }
            }

            const polyline = L.polyline(latLngs, {
                color: "blue",
                weight: 5,
                opacity: 0.3,
            }).addTo(this.leafletMap);

            const polylines = this.polylines;
            polyline.on("click", function () {
                for (const polyline of polylines) {
                    polyline.setStyle({ color: "blue", opacity: 0.3 });
                }
                this.setStyle({ color: "darkblue", opacity: 1.0 });
            });
            this.polylines.push(polyline);
        }
    }
    /**
     * Create a popup for the specified marker.
     *
     * @param {Object} markerInfo
     * @param {Number} latLongOffset
     */
    createMarkerPopup(markerInfo, latLongOffset = 0) {
        const popupFields = this.getMarkerPopupFields(markerInfo);
        const popupHtml = renderToString("spd_leaflet_map.markerPopup", {
            fields: popupFields,
            hasFormView: this.props.model.metaData.hasFormView,
        });
        let latitude  = Number(markerInfo.record.latitude)
        let longitude  = Number(markerInfo.record.longitude)
        const popup = L.popup({ offset: [0, -30] })
            .setLatLng([
                latitude + latLongOffset,
                longitude - latLongOffset,
            ])
            .setContent(popupHtml)
            .openOn(this.leafletMap);

        const openBtn = popup
            .getElement()
            .querySelector("button.o-map-renderer--popup-buttons-open");
        if (openBtn) {
            openBtn.onclick = () => {
                this.props.onMarkerClick(markerInfo.ids);
            };
        }
        return popup;
    }
    /**
     * @param {Number} groupId
     */
    getGroupColor(groupId) {
        const index = Object.keys(this.props.model.data.recordGroups).indexOf(groupId);
        return colors[index % colors.length];
    }
    /**
     * Creates an array of latLng objects if there is located records.
     *
     * @returns {latLngBounds|boolean} objects containing the coordinates that
     *          allows all the records to be shown on the map or returns false
     *          if the records does not contain any located record.
     */
    getLatLng() {
        const tabLatLng = [];
        for (const record of this.props.model.data.records) {
            let latitude = Number(record.latitude)
            let longitude = Number(record.longitude)
//            }
            if (record && latitude && longitude) {
                tabLatLng.push(L.latLng(latitude, longitude));
            }
        }
        if (!tabLatLng.length) {
            return false;
        }
        return L.latLngBounds(tabLatLng);
    }
    /**
     * Get the fields' name and value to display in the popup.
     *
     * @param {Object} markerInfo
     * @returns {Object} value contains the value of the field and string
     *                   contains the value of the xml's string attribute
     */
    getMarkerPopupFields(markerInfo) {
        const record = markerInfo.record;
        const fieldsView = [];
        if (!this.props.model.metaData.hideName) {
            fieldsView.push({
                id: this.nextId++,
                value: record.display_name,
                string: _t("Name"),
            });
        }
        const fields = this.props.model.metaData.fields;

        return fieldsView;
    }
    /**
     * @returns {string}
     */
    get googleMapUrl() {
        let url = "https://www.google.com/maps/dir/?api=1";
        if (this.props.model.data.records.length) {

            const allCoordinates = this.props.model.data.records.filter(
                ({ latitude,longitude }) => latitude && longitude &&  Number(latitude) && Number(longitude)
            );

            const uniqueCoordinates = allCoordinates.reduce((coords, { latitude,longitude }) => {
                const coord = Number(latitude) + "," + Number(longitude);
                if (!coords.includes(coord)) {
                    coords.push(coord);
                }
                return coords;
            }, []);
            if (uniqueCoordinates.length && this.props.model.metaData.routing) {
                // When routing is enabled, make last record the destination
                url += `&destination=${uniqueCoordinates.pop()}`;
            }
            if (uniqueCoordinates.length) {
                url += `&waypoints=${uniqueCoordinates.join("|")}`;
            }
        }
        return url;
    }
    /**
     * Remove the markers from the map and empty the markers array.
     */
    removeMarkers() {
        for (const marker of this.markers) {
            marker.off("click");
            this.leafletMap.removeLayer(marker);
        }
        this.markers = [];
    }
    /**
     * Remove the routes from the map and empty the the polyline array.
     */
    removeRoutes() {
        for (const polyline of this.polylines) {
            polyline.off("click");
            this.leafletMap.removeLayer(polyline);
        }
        this.polylines = [];
    }
    /**
     * Update position in the map, markers and routes.
     */
    updateMap() {
        if (this.props.model.data.shouldUpdatePosition) {
            const initialCoord = this.getLatLng();
            if (initialCoord) {
                this.leafletMap.flyToBounds(initialCoord, { animate: false });
            } else {
                this.leafletMap.fitWorld();
            }
            this.leafletMap.closePopup();
        }
        this.addMarkers();
        this.addRoutes();
    }

    /**
     * Center the map on a certain pin and open the popup linked to it.
     *
     * @param {Object} record
     */
    async centerAndOpenPin(record) {
        this.state.expendedPinList = false;
        await delay(0);
        const popup = this.createMarkerPopup({
            record: record,
            ids: [record.id],
        });

       let latitude = Number(record.latitude)
       let longitude = Number(record.longitude)
        const px = this.leafletMap.project([
            latitude,
            longitude,
        ]);
        const popupHeight = popup.getElement().offsetHeight;
        px.y -= popupHeight / 2;
        const latlng = this.leafletMap.unproject(px);
        this.leafletMap.panTo(latlng, { animate: true });
    }
    /**
     * @param {Number} id
     */
    toggleGroup(id) {
        if (this.state.closedGroupIds.includes(id)) {
            const index = this.state.closedGroupIds.indexOf(id);
            this.state.closedGroupIds.splice(index, 1);
        } else {
            this.state.closedGroupIds.push(id);
        }
    }

    togglePinList() {
        this.state.expendedPinList = !this.state.expendedPinList;
    }

    get expendedPinList() {
        return this.env.isSmall ? this.state.expendedPinList : false;
    }

    get canDisplayPinList() {
        return !this.env.isSmall || this.expendedPinList;
    }
}

MapRenderer.template = "spd_leaflet_map.MapRenderer";
MapRenderer.props = {
    model: Object,
    onMarkerClick: Function,
};
