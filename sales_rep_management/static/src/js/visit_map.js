/** @odoo-module **/

import { loadJS, loadCSS } from "@web/core/assets";

let map = null;
let visitMarker = null;
let customerMarker = null;
let isInitialized = false;

async function ensureLeaflet() {
    if (window.L) return;
    await Promise.all([
        loadJS("/spd_leaflet_map/static/lib/leaflet/leaflet.js"),
        loadCSS("/spd_leaflet_map/static/lib/leaflet/leaflet.css"),
    ]);
}

async function initializeMap() {
    const mapContainer = document.getElementById('visit_map');
    const loadingOverlay = document.getElementById('visit_map_loading');
    const errorOverlay = document.getElementById('visit_map_error');
    const errorText = document.getElementById('visit_map_error_text');

    if (!mapContainer || isInitialized) return;

    // Helper to hide loading and show error
    const showError = (msg) => {
        if (loadingOverlay) loadingOverlay.classList.add('d-none');
        if (errorOverlay) {
            errorOverlay.classList.remove('d-none');
            errorOverlay.classList.add('d-flex');
        }
        if (errorText) errorText.innerText = msg;
    };

    await ensureLeaflet();

    // Comprehensive ID extraction for Odoo 18/17 and legacy
    let resId = null;

    // 1. Try URL Path (Odoo 18 style: /odoo/action-XXX/ID or /odoo/model/ID)
    const pathMatch = window.location.pathname.match(/\/odoo\/(?:action-|[^\/]+\/)?([^\/]+)\/(\d+)$/);
    if (pathMatch) {
        resId = pathMatch[2];
    } else {
        // Simple path check: /odoo/action-XXX/ID
        const simplePathMatch = window.location.pathname.match(/\/odoo\/[^\/]+\/(\d+)/);
        if (simplePathMatch) resId = simplePathMatch[1];
    }

    // 2. Try URL Search Params
    if (!resId) {
        const urlParams = new URLSearchParams(window.location.search);
        resId = urlParams.get('id');
    }

    // 3. Try URL Hash (Legacy)
    if (!resId) {
        const hashParams = new URLSearchParams(window.location.hash.substring(1).replace(/^#?\//, ''));
        resId = hashParams.get('id');
    }

    if (!resId || isNaN(parseInt(resId))) {
        console.warn('Visit Map: No valid record ID found in URL paths or params. Path:', window.location.pathname, 'Hash:', window.location.hash);
        showError('No record ID found.');
        return;
    }

    console.info('Visit Map: Initializing for visit ID:', resId);

    // Fetch coordinates for this visit
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/web/dataset/call_kw', true);
    xhr.setRequestHeader('Content-Type', 'application/json');

    const data = {
        jsonrpc: "2.0",
        method: "call",
        params: {
            model: "sales.rep.visit",
            method: "search_read",
            args: [[['id', '=', parseInt(resId)]]],
            kwargs: {
                fields: ["visit_location_lat", "visit_location_long", "name", "customer_latitude", "customer_longitude"],
                limit: 1
            }
        },
        id: Math.floor(Math.random() * 1000000)
    };

    xhr.onreadystatechange = function() {
        if (xhr.readyState === 4) {
            if (xhr.status === 200) {
                try {
                    const response = JSON.parse(xhr.responseText);
                    if (response.result && response.result.length > 0) {
                        const visit = response.result[0];
                        renderMap(visit);
                        // Hide loading overlay on success
                        if (loadingOverlay) loadingOverlay.classList.add('d-none');
                    } else {
                        showError('Visit record not found.');
                    }
                } catch (e) {
                    console.error('Visit Map: Error parsing response', e);
                    showError('Failed to parse server response.');
                }
            } else {
                showError('Failed to connect to server.');
            }
        }
    };

    xhr.send(JSON.stringify(data));
    isInitialized = true;
}

function renderMap(visit) {
    if (!window.L) {
        console.warn('Visit Map: Leaflet not loaded yet.');
        return;
    }

    const visitLat = parseFloat(visit.visit_location_lat);
    const visitLng = parseFloat(visit.visit_location_long);
    const custLat = parseFloat(visit.customer_latitude);
    const custLng = parseFloat(visit.customer_longitude);

    const hasVisit = !isNaN(visitLat) && !isNaN(visitLng) && visitLat !== 0;
    const hasCust = !isNaN(custLat) && !isNaN(custLng) && custLat !== 0;

    if (!hasVisit && !hasCust) {
        const errorOverlay = document.getElementById('visit_map_error');
        const errorText = document.getElementById('visit_map_error_text');
        if (errorOverlay) {
            errorOverlay.classList.remove('d-none');
            errorOverlay.classList.add('d-flex');
        }
        if (errorText) errorText.innerText = 'No valid coordinates found for this visit.';
        return;
    }

    map = L.map('visit_map');

    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; OpenStreetMap contributors',
        minZoom: 2,
        maxZoom: 19,
    }).addTo(map);

    const visitIcon = L.divIcon({
        className: 'bg-transparent',
        html: '<div class="text-primary text-center" style="font-size: 3em; filter: drop-shadow(3px 3px 2px rgba(0,0,0,0.3));"><i class="fa fa-map-pin"></i></div>',
        iconSize: [40, 48],
        iconAnchor: [20, 42],
        popupAnchor: [1, -45]
    });

    const customerIcon = L.divIcon({
        className: 'bg-transparent',
        html: '<div class="text-danger text-center" style="font-size: 2.5em; filter: drop-shadow(3px 3px 2px rgba(0,0,0,0.3));"><i class="fa fa-user"></i></div>',
        iconSize: [40, 40],
        iconAnchor: [20, 20],
        popupAnchor: [1, -25]
    });

    const bounds = [];

    if (hasVisit) {
        visitMarker = L.marker([visitLat, visitLng], {icon: visitIcon}).addTo(map)
            .bindPopup(`<b>${visit.name || 'Visit'}</b><br>Lat: ${visitLat}<br>Long: ${visitLng}<br><span class="badge badge-primary">Visit Location</span>`)
            .openPopup();
        bounds.push([visitLat, visitLng]);
    }

    if (hasCust) {
        customerMarker = L.marker([custLat, custLng], {icon: customerIcon}).addTo(map)
            .bindPopup(`<b>Customer Location</b><br>Lat: ${custLat}<br>Long: ${custLng}<br><span class="badge badge-danger">Customer Location</span>`);
        bounds.push([custLat, custLng]);
    }

    if (bounds.length > 0) {
        if (bounds.length === 1) {
            map.setView(bounds[0], 15);
        } else {
            map.fitBounds(bounds, { padding: [50, 50] });
        }
    }

    // Force map resize with staggered delays to handle Odoo tab lag
    setTimeout(() => { if (map) map.invalidateSize(); }, 100);
    setTimeout(() => { if (map) map.invalidateSize(); }, 500);
    setTimeout(() => { if (map) map.invalidateSize(); }, 2000);
}

// Check for map container periodically
setInterval(() => {
    const mapContainer = document.getElementById('visit_map');
    if (mapContainer && mapContainer.offsetParent !== null && !isInitialized) {
        initializeMap();
    } else if (!mapContainer) {
        isInitialized = false;
        if (map) { map.remove(); map = null; }
    }
}, 1000);
