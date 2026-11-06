/** @odoo-module **/

import { registry } from "@web/core/registry";
import { loadJS, loadCSS } from "@web/core/assets";
import { Component, onMounted, onWillStart, onWillUnmount, useRef, useState } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { standardFieldProps } from "@web/views/fields/standard_field_props";

class ContactMapWidget extends Component {
    static template = "sales_rep_management.ContactMapWidget";
    static props = { ...standardFieldProps };

    setup() {
        this.mapContainerRef = useRef("mapContainer");
        this.orm = useService("orm");
        this.state = useState({ 
            mapReady: false,
            mapError: false 
        });

        this.map = null;
        this.marker = null;
        this.circle = null;
        this.pollingInterval = null;
        this.resizeObserver = null;
        this.currentData = { lat: null, lng: null, radius: null, location_type: null };

        onWillStart(async () => {
            try {
                console.log("Contact Map: Preparing Leaflet assets...");
                await this._ensureLeaflet();
            } catch (error) {
                console.error("Contact Map: Failed to load Leaflet assets", error);
                this.state.mapError = "Failed to load mapping library. Please check spd_leaflet_map installation.";
            }
        });

        onMounted(() => {
            console.log("Contact Map: Component mounted");
            if (this.state.mapError) return;

            // Wait until container has dimensions to avoid Leaflet initialization errors
            this.resizeObserver = new ResizeObserver(() => {
                const container = this.mapContainerRef.el;
                if (container && container.offsetWidth > 0 && container.offsetHeight > 0) {
                    if (!this.map) {
                        console.log("Contact Map: Container visible, initializing map");
                        this._initMap();
                    } else {
                        console.log("Contact Map: Container resized, invalidating size");
                        this.map.invalidateSize();
                    }
                }
            });

            if (this.mapContainerRef.el) {
                this.resizeObserver.observe(this.mapContainerRef.el);
            }

            this.pollingInterval = setInterval(() => this._fetchAndUpdate(), 5000);
        });

        onWillUnmount(() => {
            if (this.pollingInterval) clearInterval(this.pollingInterval);
            if (this.resizeObserver) this.resizeObserver.disconnect();
            if (this.map) {
                this.map.remove();
                this.map = null;
            }
        });
    }

    async _ensureLeaflet() {
        if (window.L) return;
        // Ensure module name matches exactly for proper static serving
        await Promise.all([
            loadJS("/spd_leaflet_map/static/lib/leaflet/leaflet.js"),
            loadCSS("/spd_leaflet_map/static/lib/leaflet/leaflet.css"),
        ]);
        if (!window.L) throw new Error("Leaflet 'L' object not found in window");
    }

    _initMap() {
        const container = this.mapContainerRef.el;
        if (!container || !window.L || this.map) return;

        const record = this.props.record;
        const lat = parseFloat(record.data.visit_latitude) || 0;
        const lng = parseFloat(record.data.visit_longitude) || 0;
        const hasCoords = Math.abs(lat) > 0.0001 || Math.abs(lng) > 0.0001;

        console.log(`Contact Map: Initializing on ref element with Lat:${lat}, Lng:${lng}`);

        try {
            this.map = L.map(container, {
                center: hasCoords ? [lat, lng] : [30, 31],
                zoom: hasCoords ? 15 : 3,
                zoomControl: true,
            });

            L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
                attribution: "&copy; OpenStreetMap contributors",
                minZoom: 2,
                maxZoom: 19,
            }).addTo(this.map);

            if (hasCoords) {
                this._updateMarker(lat, lng, record.data.name || "");
                this._updateCircle(lat, lng, parseFloat(record.data.location_radius) || 0);
            }

            this.currentData = {
                lat: record.data.visit_latitude,
                lng: record.data.visit_longitude,
                radius: record.data.location_radius,
            };

            // Signal OWL that the map is ready (this will hide the overlay but not clear the map)
            this.state.mapReady = true;

            // Give Leaflet a moment to compute initial tiles
            setTimeout(() => {
                if (this.map) {
                    this.map.invalidateSize();
                    console.log("Contact Map: Initial size invalidation complete");
                }
            }, 250);
        } catch (err) {
            console.error("Contact Map: Error during Leaflet initialization", err);
            this.state.mapError = "Error initializing map engine: " + err.message;
        }
    }

    _updateMarker(lat, lng, name) {
        if (!this.map) return;

        const locationType = this.props.record.data.location_type || 'customer';
        let faIcon = 'fa-user';
        let colorClass = 'text-primary';
        if (locationType === 'house') {
            faIcon = 'fa-home';
            colorClass = 'text-warning';
        } else if (locationType === 'company') {
            faIcon = 'fa-building';
            colorClass = 'text-success';
        } else if (locationType === 'other') {
            faIcon = 'fa-map-marker';
            colorClass = 'text-secondary';
        }

        const customerIcon = L.divIcon({
            className: "bg-transparent",
            html: `<div class="${colorClass} text-center" style="font-size: 2.5em; filter: drop-shadow(3px 3px 2px rgba(0,0,0,0.3));"><i class="fa ${faIcon}"></i></div>`,
            iconSize: [40, 40],
            iconAnchor: [20, 35],
            popupAnchor: [1, -30],
        });

        if (this.marker) {
            this.marker.setLatLng([lat, lng]);
            this.marker.setIcon(customerIcon);
        } else {
            this.marker = L.marker([lat, lng], { icon: customerIcon, draggable: true }).addTo(this.map);
            this.marker.on("dragend", () => {
                const pos = this.marker.getLatLng();
                this._saveCoordinates(pos.lat, pos.lng);
            });
        }
        this.marker.bindPopup(`<b>${name || 'Customer'}</b><br/>Lat: ${lat}<br/>Long: ${lng}`).openPopup();
    }

    _updateCircle(lat, lng, radius) {
        if (!this.map) return;
        if (this.circle) {
            this.map.removeLayer(this.circle);
            this.circle = null;
        }
        if (radius > 0) {
            this.circle = L.circle([lat, lng], {
                color: "#ff4d4d",
                fillColor: "#ff4d4d",
                fillOpacity: 0.15,
                radius: radius,
                weight: 2
            }).addTo(this.map);
        }
    }

    async _fetchAndUpdate() {
        if (!this.map || !this.state.mapReady) return;
        const resId = this.props.record.resId;
        if (!resId) return;

        try {
            const results = await this.orm.read(
                "res.partner",
                [resId],
                ["visit_latitude", "visit_longitude", "name", "location_radius", "location_type"]
            );
            
            if (results.length > 0) {
                const partner = results[0];
                if (
                    this.currentData.lat === partner.visit_latitude &&
                    this.currentData.lng === partner.visit_longitude &&
                    this.currentData.radius === partner.location_radius &&
                    this.currentData.location_type === partner.location_type
                ) {
                    return;
                }
                
                console.log("Contact Map: Data updated from server", partner);
                this.currentData = {
                    lat: partner.visit_latitude,
                    lng: partner.visit_longitude,
                    radius: partner.location_radius,
                    location_type: partner.location_type,
                };

                const lat = parseFloat(partner.visit_latitude) || 0;
                const lng = parseFloat(partner.visit_longitude) || 0;
                
                if (Math.abs(lat) > 0.0001 || Math.abs(lng) > 0.0001) {
                    this._updateMarker(lat, lng, partner.name || "");
                    this._updateCircle(lat, lng, parseFloat(partner.location_radius) || 0);
                    // panTo if the marker moved significantly or wasn't centered
                    if (!this.map.getBounds().contains([lat, lng])) {
                        this.map.panTo([lat, lng]);
                    }
                }
            }
        } catch (e) {
            console.error("Contact Map: fetch error", e);
        }
    }

    async _saveCoordinates(lat, lng) {
        const resId = this.props.record.resId;
        if (!resId) return;

        this.currentData.lat = lat;
        this.currentData.lng = lng;

        try {
            await this.orm.write("res.partner", [resId], {
                visit_latitude: lat,
                visit_longitude: lng,
            });
            console.log(`Contact Map: Saved coordinates [${lat}, ${lng}]`);
        } catch (e) {
            console.error("Contact Map: save error", e);
        }
    }
}

registry.category("fields").add("contact_map_widget", {
    component: ContactMapWidget,
    supportedTypes: ["float"],
});
