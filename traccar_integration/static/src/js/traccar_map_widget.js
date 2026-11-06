///** @odoo-module **/
//
//import { Component, onMounted, useRef, onWillUnmount } from "@odoo/owl";
//import { registry } from "@web/core/registry";
//import { standardFieldProps } from "@web/views/fields/standard_field_props";
//
//export class TraccarMapWidget extends Component {
//    static props = { ...standardFieldProps };
//
//    setup() {
//        this.mapRef = useRef("map");
//        this.map = null;
//        this.deviceMarkers = {};
//
//        onMounted(() => {
//            this.loadLeafletAndInit();
//        });
//
//        onWillUnmount(() => {
//            this.cleanupMap();
//        });
//    }
//
//    cleanupMap() {
//        if (this.map) {
//            this.map.remove();
//            this.map = null;
//        }
//        this.deviceMarkers = {};
//    }
//
//    loadLeafletAndInit() {
//        if (window.L) {
//            this.initMap();
//            return;
//        }
//
//        // Load Leaflet CSS
//        const cssLink = document.createElement('link');
//        cssLink.rel = 'stylesheet';
//        cssLink.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
//        cssLink.integrity = 'sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=';
//        cssLink.crossOrigin = '';
//        document.head.appendChild(cssLink);
//
//        // Load Leaflet JS
//        const script = document.createElement('script');
//        script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
//        script.integrity = 'sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=';
//        script.crossOrigin = '';
//
//        script.onload = () => {
//            this.initMap();
//        };
//
//        script.onerror = () => {
//            console.error('Failed to load Leaflet');
//        };
//
//        document.head.appendChild(script);
//    }
//
//    initMap() {
//        const mapContainer = this.mapRef.el;
//        if (!mapContainer || !window.L) return;
//
//        // Initialize map with default coordinates
//        const lat = this.props.record.data.latitude || 30.0444;
//        const lng = this.props.record.data.longitude || 31.2357;
//
//        this.map = L.map(mapContainer).setView([lat, lng], 13);
//
//        // Add OpenStreetMap tiles
//        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
//            attribution: '© OpenStreetMap contributors',
//            maxZoom: 18
//        }).addTo(this.map);
//
//        // Create custom marker icons for different statuses
//        this.deviceIcon = L.divIcon({
//            className: 'custom-device-marker',
//            html: '<div style="background: #28a745; width: 20px; height: 20px; border-radius: 50%; border: 3px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>',
//            iconSize: [26, 26],
//            iconAnchor: [13, 13]
//        });
//
//        this.offlineIcon = L.divIcon({
//            className: 'custom-device-marker',
//            html: '<div style="background: #dc3545; width: 20px; height: 20px; border-radius: 50%; border: 3px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>',
//            iconSize: [26, 26],
//            iconAnchor: [13, 13]
//        });
//
//        this.unknownIcon = L.divIcon({
//            className: 'custom-device-marker',
//            html: '<div style="background: #ffc107; width: 20px; height: 20px; border-radius: 50%; border: 3px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3);"></div>',
//            iconSize: [26, 26],
//            iconAnchor: [13, 13]
//        });
//
//        // Add marker for current position if available
//        if (this.props.record.data.latitude && this.props.record.data.longitude) {
//            this.addPositionMarker(
//                this.props.record.data.latitude,
//                this.props.record.data.longitude,
//                this.props.record.data
//            );
//        }
//    }
//
//    addPositionMarker(lat, lng, data) {
//        if (!this.map) return;
//
//        const deviceId = data.device_id && data.device_id[0];
//        if (!deviceId) return;
//
//        // Get device status from device_id if available, otherwise use default
//        let status = 'unknown';
//        if (data.device_id && data.device_id.length >= 3) {
//            // device_id is a tuple like [id, name, status_info]
//            // We need to extract status from the device record
//            status = this._getDeviceStatusFromData(data);
//        }
//
//        // Choose appropriate icon based on device status
//        let icon;
//        switch (status) {
//            case 'online':
//                icon = this.deviceIcon;
//                break;
//            case 'offline':
//                icon = this.offlineIcon;
//                break;
//            default:
//                icon = this.unknownIcon;
//        }
//
//        if (this.deviceMarkers[deviceId]) {
//            // Update existing marker
//            this.deviceMarkers[deviceId].setLatLng([lat, lng]);
//            this.deviceMarkers[deviceId].setIcon(icon);
//            this.deviceMarkers[deviceId].setPopupContent(this.createPopupContent(data, status));
//        } else {
//            // Create new marker
//            const marker = L.marker([lat, lng], { icon: icon })
//                .addTo(this.map)
//                .bindPopup(this.createPopupContent(data, status));
//
//            this.deviceMarkers[deviceId] = marker;
//
//            // Add bounce animation for new markers
//            marker.getElement().classList.add('marker-bounce');
//            setTimeout(() => {
//                if (marker.getElement()) {
//                    marker.getElement().classList.remove('marker-bounce');
//                }
//            }, 600);
//        }
//
//        // Center map on the marker
//        this.map.setView([lat, lng], Math.max(this.map.getZoom(), 13));
//    }
//
//    _getDeviceStatusFromData(data) {
//        // Try to extract status from device data
//        // This is a simplified approach - in a real implementation, you might need to fetch device details
//        if (data.device_id && data.device_id.length >= 3) {
//            // device_id tuple may contain status information
//            return 'online'; // Default to online for simplicity
//        }
//        return 'unknown';
//    }
//
//    createPopupContent(data, status) {
//        const deviceTime = data.device_time ? new Date(data.device_time).toLocaleString() : 'Unknown';
//        const deviceName = data.device_id && data.device_id[1] || 'Unknown Device';
//        const latitude = data.latitude || 0;
//        const longitude = data.longitude || 0;
//        const speedKmh = data.speed_kmh || 0;
//        const course = data.course || 0;
//        const altitude = data.altitude || 0;
//
//        return `
//            <div class="user-popup">
//                <div class="user-header">
//                    <div class="status-indicator ${status || 'unknown'}"></div>
//                    <div>
//                        <div class="user-name">${deviceName}</div>
//                        <small class="text-muted">ID: ${data.device_id && data.device_id[0] || 'N/A'}</small>
//                    </div>
//                </div>
//                <div class="user-details">
//                    <div class="detail-row">
//                        <i class="fa fa-map-marker text-primary"></i>
//                        <span>${latitude.toFixed(6)}, ${longitude.toFixed(6)}</span>
//                    </div>
//                    <div class="detail-row">
//                        <i class="fa fa-clock-o"></i>
//                        <span class="timestamp">${deviceTime}</span>
//                    </div>
//                    <div class="detail-row">
//                        <i class="fa fa-tachometer"></i>
//                        <span class="speed">${speedKmh.toFixed(1)} km/h</span>
//                    </div>
//                    <div class="detail-row">
//                        <i class="fa fa-compass"></i>
//                        <span class="heading">${course.toFixed(0)}° heading</span>
//                    </div>
//                    ${altitude ? `
//                    <div class="detail-row">
//                        <i class="fa fa-mountain"></i>
//                        <span class="altitude">${altitude.toFixed(0)}m altitude</span>
//                    </div>
//                    ` : ''}
//                    <div class="detail-row">
//                        <i class="fa fa-status"></i>
//                        <span class="status">Status: ${status || 'unknown'}</span>
//                    </div>
//                </div>
//            </div>
//        `;
//    }
//}
//
//// Use a simple template without any dynamic content that might cause errors
//TraccarMapWidget.template = "traccar_integration.MapWidget";
//
//// Create a simple XML template for the widget
//const __TEMPLATE__ = `
//<div class="o_field_widget traccar_map">
//    <div t-ref="map" class="traccar-map-container"></div>
//</div>`;
//
//// Register the template
//const __TEMPLATE_OBJECT__ = { __TEMPLATE__ };
//TraccarMapWidget.template = __TEMPLATE_OBJECT__;
//
//registry.category("fields").add("traccar_map", TraccarMapWidget);