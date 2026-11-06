/** @odoo-module **/

(function() {
    'use strict';

    // Parse focus_position_id immediately on script load before router strips it
    let initialFocusPositionId = (function() {
        const hash = window.location.hash || '';
        const search = window.location.search || '';
        const match = hash.match(new RegExp('[#&]focus_position_id=([^&]*)')) || 
                      search.match(new RegExp('[?&]focus_position_id=([^&]*)'));
        return match ? decodeURIComponent(match[1]) : null;
    })();

    function showRuntimeError(err) {
        console.error("Replay Error:", err);
        const mapContainer = document.getElementById('traccar_live_map_container') || document.body;
        let errDiv = document.getElementById('traccar_debug_error');
        if (!errDiv) {
            errDiv = document.createElement('div');
            errDiv.id = 'traccar_debug_error';
            errDiv.style = 'position: absolute; top: 70px; left: 16px; right: 16px; z-index: 99999; background: #fee2e2; color: #991b1b; padding: 12px; border-radius: 8px; border: 1px solid #fca5a5; font-family: monospace; font-size: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);';
            mapContainer.appendChild(errDiv);
        }
        errDiv.innerHTML = `<strong>JS Error:</strong> ${err.message}<br/><small>${err.stack || ''}</small>`;
    }

    window.addEventListener('error', (event) => {
        if (event.error) {
            showRuntimeError(event.error);
        } else {
            showRuntimeError({ message: event.message, stack: 'At line ' + event.lineno + ':' + event.colno });
        }
    });

    // Global variables
    let map = null;
    let deviceMarkers = {};
    let allDevices = [];
    let contactMarkers = {};
    let contactCircles = {};
    let allContacts = [];
    let allVisits = [];
    let visitMarkers = {};
    let contactClusterGroup = null;
    let showReps = true;
    let showRepOnline = true;
    let showRepOffline = true;
    let showRepUnknown = true;
    let showCustomers = true;
    let showLocCustomer = true;
    let showLocHouse = true;
    let showLocCompany = true;
    let showLocOther = true;
    let showVisits = true;
    let leafletLoaded = false;
    let isInitialized = false;
    let pollingInterval = null;
    let lastPositionCheck = new Date();
    let browserSyncInterval = 30;
    let hasInitialCentered = false;
    let syncInFlight = false;
    let checkInFlight = false;
    let positionsInFlight = false;
    let syncCooldownUntil = null;
    let focusPositionMarker = null;
    let hasProcessedFocusPosition = false;

    let replayPolyline = null; // This will now be an array or FeatureGroup
    let routePolylineObj = null;
    let replayMarkers = [];
    let waitingPointMarkers = []; // Leaflet markers for waiting points
    let waitingPointCircles = []; // Leaflet circles for waiting points 20m radius
    let isReplayMode = false;
    let selectedDeviceId = null;
    let filterVisitRepId = ""; // Filter visits by sales rep ID
    let currentSortOrder = 'none'; // 'none', 'asc', 'desc'

    // Playback state
    let playbackPositions = [];
    let playbackIndex = 0;
    let playbackTimer = null;
    let playbackMarker = null;
    let isPlaying = false;
    let followPlaybackCar = false;

    // Configuration
    const INITIAL_LOAD_DELAY = 800;
    const POLLING_INTERVAL = 10000; // Poll status every 10 seconds instead of every 1 second
    const REQUEST_TIMEOUT = 15000;
    const SYNC_FAILURE_COOLDOWN = 60000;

    function formatDatetimeLocal(date) {
        const year = date.getFullYear();
        const month = String(date.getMonth() + 1).padStart(2, '0');
        const day = String(date.getDate()).padStart(2, '0');
        const hours = String(date.getHours()).padStart(2, '0');
        const minutes = String(date.getMinutes()).padStart(2, '0');
        return `${year}-${month}-${day}T${hours}:${minutes}`;
    }

    function getUTCDateString(localDateTimeStr) {
        if (!localDateTimeStr) return false;
        const d = new Date(localDateTimeStr);
        if (isNaN(d.getTime())) return false;
        const year = d.getUTCFullYear();
        const month = String(d.getUTCMonth() + 1).padStart(2, '0');
        const day = String(d.getUTCDate()).padStart(2, '0');
        const hours = String(d.getUTCHours()).padStart(2, '0');
        const minutes = String(d.getUTCMinutes()).padStart(2, '0');
        const seconds = String(d.getUTCSeconds()).padStart(2, '0');
        return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
    }

    function updateStatus(message, type) {
        try {
            const statusElement = document.getElementById('header_status_text');
            const spinner = document.getElementById('header_status_spinner');
            const badge = document.getElementById('header_status_badge');
            if (!statusElement) return;
            
            statusElement.textContent = message;
            
            if (spinner) {
                if (message === "Syncing...") {
                    spinner.classList.remove('d-none');
                } else {
                    spinner.classList.add('d-none');
                }
            }

            if (badge) {
                // Reset classes
                badge.classList.remove('bg-info-light', 'bg-danger-light', 'bg-success-light', 'text-info', 'text-danger', 'text-success', 'border-info', 'border-danger', 'border-success');
                
                if (type === 'danger' || message.includes('Error')) {
                    badge.classList.add('bg-danger-light', 'text-danger', 'border-danger');
                } else if (type === 'success' || message === 'Online') {
                    badge.classList.add('bg-success-light', 'text-success', 'border-success');
                } else {
                    badge.classList.add('bg-info-light', 'text-info', 'border-info');
                }
            }
        } catch (error) {
            console.error('Status error:', error);
        }
    }

    function initializeWhenReady() {
        const mapElement = document.getElementById('traccar_live_map');
        if (mapElement && !isInitialized && !window._traccar_initializing) {
            window._traccar_initializing = true;
            console.info('Initializing Premium Dashboard...');
            
            fetchSyncConfiguration().then(() => {
                setTimeout(() => {
                    loadLeafletAndInit();
                    setupEventListeners();
                    triggerBrowserSync();
                    window._traccar_initializing = false;
                }, INITIAL_LOAD_DELAY);
            }).catch(() => {
                window._traccar_initializing = false;
            });
        } 
        setTimeout(initializeWhenReady, 1500);
    }

    function fetchSyncConfiguration() {
        return new Promise((resolve) => {
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/web/dataset/call_kw', true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.timeout = REQUEST_TIMEOUT;

            const data = {
                jsonrpc: "2.0",
                method: "call",
                params: {
                    model: "traccar.config",
                    method: "get_browser_sync_interval",
                    args: [],
                    kwargs: {}
                },
                id: Math.floor(Math.random() * 1000000)
            };

            xhr.onreadystatechange = function() {
                if (xhr.readyState === 4) {
                    if (xhr.status === 200) {
                        try {
                            const response = JSON.parse(xhr.responseText);
                            if (response.result) {
                                browserSyncInterval = parseInt(response.result) || 30;
                            }
                        } catch (e) {}
                    }
                    resolve();
                }
            };
            xhr.send(JSON.stringify(data));
        });
    }

    function setupEventListeners() {
        // Search Functionality
        const searchInput = document.getElementById('device_search');
        if (searchInput) {
            searchInput.addEventListener('input', (e) => {
                const term = e.target.value.toLowerCase();
                filterDevices(term);
            });
        }

        // Category filtering (All, Reps, Customers, Visits)
        const filterAllBtn = document.getElementById('filter_all');
        const filterRepsBtn = document.getElementById('filter_reps');
        const filterRepOnlineBtn = document.getElementById('filter_rep_online');
        const filterRepOfflineBtn = document.getElementById('filter_rep_offline');
        const filterRepUnknownBtn = document.getElementById('filter_rep_unknown');
        const filterCustomersBtn = document.getElementById('filter_customers');
        const filterLocCustomerBtn = document.getElementById('filter_loc_customer');
        const filterLocHouseBtn = document.getElementById('filter_loc_house');
        const filterLocCompanyBtn = document.getElementById('filter_loc_company');
        const filterLocOtherBtn = document.getElementById('filter_loc_other');
        const filterVisitsBtn = document.getElementById('filter_visits');

        const dateFromInput = document.getElementById('visit_date_from');
        const dateToInput = document.getElementById('visit_date_to');
        const dateContainer = document.getElementById('date_filter_container');

        // Set default date range to today's visits
        if (dateFromInput && dateToInput && !dateFromInput.value) {
            const todayStart = new Date();
            todayStart.setHours(0, 0, 0, 0);
            const todayEnd = new Date();
            todayEnd.setHours(23, 59, 59, 999);
            
            dateFromInput.value = formatDatetimeLocal(todayStart);
            dateToInput.value = formatDatetimeLocal(todayEnd);
        }

        function updateFilterTypeActiveState() {
            // Helper: set checkbox icon + active state
            function setCheck(btn, isOn) {
                const icon = btn?.querySelector('.filter-checkbox');
                if (icon) {
                    icon.className = isOn
                        ? 'fa fa-check-square filter-checkbox'
                        : 'fa fa-square-o filter-checkbox';
                }
                if (btn) {
                    if (isOn) btn.classList.add('active');
                    else btn.classList.remove('active');
                }
            }

            setCheck(filterRepsBtn, showReps);
            setCheck(filterRepOnlineBtn, showReps && showRepOnline);
            setCheck(filterRepOfflineBtn, showReps && showRepOffline);
            setCheck(filterRepUnknownBtn, showReps && showRepUnknown);
            setCheck(filterCustomersBtn, showCustomers);
            setCheck(filterLocCustomerBtn, showCustomers && showLocCustomer);
            setCheck(filterLocHouseBtn, showCustomers && showLocHouse);
            setCheck(filterLocCompanyBtn, showCustomers && showLocCompany);
            setCheck(filterLocOtherBtn, showCustomers && showLocOther);
            setCheck(filterVisitsBtn, showVisits);

            const allActive = showReps && showCustomers && showVisits && showLocCustomer && showLocHouse && showLocCompany && showLocOther && showRepOnline && showRepOffline && showRepUnknown;
            setCheck(filterAllBtn, allActive);

            // Dynamic visibility of rep status sub-filters
            const repFilterItems = document.querySelectorAll('.rep-status-filter');
            repFilterItems.forEach(item => {
                item.style.display = showReps ? 'flex' : 'none';
            });

            // Dynamic visibility of contact sub-filters
            const locFilterItems = document.querySelectorAll('.location-type-filter');
            locFilterItems.forEach(item => {
                item.style.display = showCustomers ? 'flex' : 'none';
            });

            // Show/Hide Date Filter Container and Divider
            if (dateContainer) {
                const divider = document.getElementById('date_filter_divider');
                const dateTitle = document.getElementById('date_filter_title');
                if (showVisits) {
                    dateContainer.style.display = 'flex';
                    if (divider) divider.style.display = 'block';
                    if (dateTitle) dateTitle.style.display = 'block';
                } else {
                    dateContainer.style.display = 'none';
                    if (divider) divider.style.display = 'none';
                    if (dateTitle) dateTitle.style.display = 'none';
                }
            }
        }

        // Initialize display state
        if (dateContainer) {
            // Prevent dropdown closure when interacting with dates
            dateContainer.addEventListener('click', (e) => {
                e.stopPropagation();
            });
        }
        updateFilterTypeActiveState();

        const handleDateChange = () => {
            const term = document.getElementById('device_search')?.value.toLowerCase() || '';
            loadVisitsData();
        };

        if (dateFromInput) dateFromInput.addEventListener('change', handleDateChange);
        if (dateToInput) dateToInput.addEventListener('change', handleDateChange);

        if (filterAllBtn) {
            filterAllBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                const allActive = showReps && showCustomers && showVisits && showLocCustomer && showLocHouse && showLocCompany && showLocOther && showRepOnline && showRepOffline && showRepUnknown;
                showReps = !allActive;
                showRepOnline = !allActive;
                showRepOffline = !allActive;
                showRepUnknown = !allActive;
                showCustomers = !allActive;
                showLocCustomer = !allActive;
                showLocHouse = !allActive;
                showLocCompany = !allActive;
                showLocOther = !allActive;
                showVisits = !allActive;
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterRepsBtn) {
            filterRepsBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showReps = !showReps;
                showRepOnline = showReps;
                showRepOffline = showReps;
                showRepUnknown = showReps;
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterRepOnlineBtn) {
            filterRepOnlineBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showRepOnline = !showRepOnline;
                if (showRepOnline || showRepOffline || showRepUnknown) {
                    showReps = true;
                } else {
                    showReps = false;
                }
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterRepOfflineBtn) {
            filterRepOfflineBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showRepOffline = !showRepOffline;
                if (showRepOnline || showRepOffline || showRepUnknown) {
                    showReps = true;
                } else {
                    showReps = false;
                }
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterRepUnknownBtn) {
            filterRepUnknownBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showRepUnknown = !showRepUnknown;
                if (showRepOnline || showRepOffline || showRepUnknown) {
                    showReps = true;
                } else {
                    showReps = false;
                }
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterCustomersBtn) {
            filterCustomersBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showCustomers = !showCustomers;
                showLocCustomer = showCustomers;
                showLocHouse = showCustomers;
                showLocCompany = showCustomers;
                showLocOther = showCustomers;
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterLocCustomerBtn) {
            filterLocCustomerBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showLocCustomer = !showLocCustomer;
                if (showLocCustomer || showLocHouse || showLocCompany || showLocOther) {
                    showCustomers = true;
                } else {
                    showCustomers = false;
                }
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterLocHouseBtn) {
            filterLocHouseBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showLocHouse = !showLocHouse;
                if (showLocCustomer || showLocHouse || showLocCompany || showLocOther) {
                    showCustomers = true;
                } else {
                    showCustomers = false;
                }
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterLocCompanyBtn) {
            filterLocCompanyBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showLocCompany = !showLocCompany;
                if (showLocCustomer || showLocHouse || showLocCompany || showLocOther) {
                    showCustomers = true;
                } else {
                    showCustomers = false;
                }
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterLocOtherBtn) {
            filterLocOtherBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showLocOther = !showLocOther;
                if (showLocCustomer || showLocHouse || showLocCompany || showLocOther) {
                    showCustomers = true;
                } else {
                    showCustomers = false;
                }
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        if (filterVisitsBtn) {
            filterVisitsBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                showVisits = !showVisits;
                updateFilterTypeActiveState();
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }
        const filterMenuBtn = document.getElementById('btn_filter_menu');
        const filterOverlay = document.getElementById('traccar_filter_overlay');
        const closeFilterBtn = document.querySelector('.btn-close-filter');

        if (filterMenuBtn && filterOverlay) {
            if (window.L) {
                L.DomEvent.disableClickPropagation(filterOverlay);
                L.DomEvent.disableScrollPropagation(filterOverlay);
            }
            filterMenuBtn.onclick = (e) => {
                e.preventDefault();
                const isHidden = filterOverlay.classList.toggle('d-none');
                filterMenuBtn.classList.toggle('active', !isHidden);
                adjustOverlaysLayout();
            };
        }

        if (closeFilterBtn && filterOverlay && filterMenuBtn) {
            closeFilterBtn.onclick = (e) => {
                e.preventDefault();
                filterOverlay.classList.add('d-none');
                filterMenuBtn.classList.remove('active');
                adjustOverlaysLayout();
            };
        }


        // Sorting functionality
        const sortBtn = document.getElementById('btn_sort_az');
        const sortIcon = document.getElementById('sort_icon');
        if (sortBtn && sortIcon) {
            sortBtn.onclick = (e) => {
                e.preventDefault();
                if (currentSortOrder === 'none') {
                    currentSortOrder = 'asc';
                    sortIcon.className = 'fa fa-sort-alpha-asc text-primary';
                    sortBtn.title = 'Sort Z-A';
                } else if (currentSortOrder === 'asc') {
                    currentSortOrder = 'desc';
                    sortIcon.className = 'fa fa-sort-alpha-desc text-primary';
                    sortBtn.title = 'Clear Sorting';
                } else {
                    currentSortOrder = 'none';
                    sortIcon.className = 'fa fa-sort-alpha-asc text-muted';
                    sortBtn.title = 'Sort A-Z';
                }
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            };
        }

        // Fullscreen Toggle
        const toggleBtn = document.getElementById('btn_toggle_fullscreen');
        if (toggleBtn) {
            if (window.L) {
                L.DomEvent.disableClickPropagation(toggleBtn);
                L.DomEvent.disableScrollPropagation(toggleBtn);
            }
            toggleBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                toggleMapExpansion();
            };
        }

        // Fit Bounds Button
        const fitBtn = document.getElementById('btn_fit_bounds');
        if (fitBtn) {
            if (window.L) {
                L.DomEvent.disableClickPropagation(fitBtn);
                L.DomEvent.disableScrollPropagation(fitBtn);
            }
            fitBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                autoCenterMap();
            }
        }

        // Manual Refresh
        const refreshBtn = document.querySelector('button[name="sync_all_data"]');
        if (refreshBtn) {
            refreshBtn.onclick = (e) => {
                e.preventDefault();
                loadDevicePositions();
            };
        }
        // Center Car Button
        const centerCarBtn = document.getElementById('btn_center_car');
        if (centerCarBtn) {
            if (window.L) {
                L.DomEvent.disableClickPropagation(centerCarBtn);
                L.DomEvent.disableScrollPropagation(centerCarBtn);
            }
            centerCarBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (playbackMarker) {
                    updateCarTrackingUI(!followPlaybackCar);
                }
            }
        }

        startPositionPolling();
        setupReplayListeners();
    }

    function setupReplayListeners() {
        const confirmBtn = document.getElementById('btn_confirm_replay');
        const closeBtn = document.querySelector('.btn-close-replay');
        
        if (confirmBtn) confirmBtn.onclick = confirmReplay;
        if (closeBtn) closeBtn.onclick = () => toggleReplayDialog(false);

        const collapseReplayBtn = document.getElementById('btn_collapse_replay');
        const replayBody = document.querySelector('.replay-body');
        if (collapseReplayBtn && replayBody) {
            collapseReplayBtn.onclick = (e) => {
                e.preventDefault();
                e.stopPropagation();
                const isCollapsed = replayBody.classList.toggle('d-none');
                const icon = collapseReplayBtn.querySelector('i');
                if (icon) {
                    if (isCollapsed) {
                        icon.className = 'fa fa-chevron-down';
                        collapseReplayBtn.title = 'Expand';
                    } else {
                        icon.className = 'fa fa-chevron-up';
                        collapseReplayBtn.title = 'Collapse';
                    }
                }
            };
        }

        // Playback listeners
        const playPauseBtn = document.getElementById('btn_play_pause');
        const replaySlider = document.getElementById('replay_slider');
        const speedSelect = document.getElementById('playback_speed_mult');

        if (playPauseBtn) playPauseBtn.onclick = togglePlayback;
        if (replaySlider) {
            replaySlider.oninput = (e) => seekTo(parseInt(e.target.value));
        }
        if (speedSelect) {
            speedSelect.onchange = () => {
                if (isPlaying) {
                    pausePlayback();
                    startPlayback();
                }
            };
        }
        
        // Toggle waiting points checkbox visibility on map
        const toggleWaitingBtn = document.getElementById('toggle_waiting_points');
        if (toggleWaitingBtn) {
            toggleWaitingBtn.onchange = (e) => {
                const show = e.target.checked;
                waitingPointMarkers.forEach(marker => {
                    if (show) {
                        marker.addTo(map);
                    } else {
                        map.removeLayer(marker);
                    }
                });
                waitingPointCircles.forEach(circle => {
                    if (show) {
                        circle.addTo(map);
                    } else {
                        map.removeLayer(circle);
                    }
                });
            };
        }

        // Toggle route line checkbox visibility on map
        const toggleRouteBtn = document.getElementById('toggle_route_line');
        if (toggleRouteBtn) {
            toggleRouteBtn.onchange = (e) => {
                const show = e.target.checked;
                if (routePolylineObj) {
                    if (show) {
                        routePolylineObj.addTo(replayPolyline);
                    } else {
                        replayPolyline.removeLayer(routePolylineObj);
                    }
                }
            };
        }

        // Global hook for the sidebar button
        window._traccar_toggle_replay = (id) => {
            toggleReplayDialog(true);
            // Default From to 24h ago, To to Now
            const now = new Date();
            const yesterday = new Date(now.getTime() - 24 * 60 * 60 * 1000);
            
            document.getElementById('replay_from').value = formatDatetimeLocal(yesterday);
            document.getElementById('replay_to').value = formatDatetimeLocal(now);
        };

        // Global hook to remove the focused position marker from map
        window._traccar_remove_focus_position = () => {
            if (focusPositionMarker && map) {
                map.removeLayer(focusPositionMarker);
                focusPositionMarker = null;
            }
        };
    }

    function sortDevicesArray(arr) {
        const sorted = [...arr];
        if (currentSortOrder === 'asc') {
            sorted.sort((a, b) => (a.name || '').localeCompare(b.name || ''));
        } else if (currentSortOrder === 'desc') {
            sorted.sort((a, b) => (b.name || '').localeCompare(a.name || ''));
        }
        return sorted;
    }

    function filterDevices(term) {
        let filteredDevices = [];
        if (showReps) {
            filteredDevices = allDevices.filter(d => {
                // Check status sub-filter
                const status = (d.status || 'unknown').toLowerCase();
                if (status === 'online' && !showRepOnline) return false;
                if (status === 'offline' && !showRepOffline) return false;
                if (status !== 'online' && status !== 'offline' && !showRepUnknown) return false;

                return (d.name && d.name.toLowerCase().includes(term)) || 
                    (d.unique_id && d.unique_id.toString().includes(term));
            });
        }

        let filteredContacts = [];
        if (showCustomers) {
            filteredContacts = allContacts.filter(c => {
                if (c.location_type === 'customer' && !showLocCustomer) return false;
                if (c.location_type === 'house' && !showLocHouse) return false;
                if (c.location_type === 'company' && !showLocCompany) return false;
                if (c.location_type === 'other' && !showLocOther) return false;

                return (c.name && c.name.toLowerCase().includes(term)) ||
                    (c.phone && c.phone.toLowerCase().includes(term)) ||
                    (c.city && c.city.toLowerCase().includes(term)) ||
                    (c.street && c.street.toLowerCase().includes(term));
            });
        }

        let filteredVisits = [];
        if (showVisits) {
            filteredVisits = allVisits.filter(v => {
                return (v.name && v.name.toLowerCase().includes(term)) ||
                    (v.planned_time && v.planned_time.includes(term)) ||
                    (v.visit_result && v.visit_result.toLowerCase().includes(term)) ||
                    (v.state && v.state.toLowerCase().includes(term)) ||
                    (v.notes && v.notes.toLowerCase().includes(term));
            });
        }

        let combined = [...filteredDevices, ...filteredContacts, ...filteredVisits];
        combined = sortDevicesArray(combined);
        renderDeviceList(combined);
        
        // Let's filter map markers, but skip devices if in replay mode (since replay has its own car marker)
        const isReplay = isReplayMode;
        
        // Show/Hide device markers based on search
        for (const id in deviceMarkers) {
            const marker = deviceMarkers[id];
            const isVisible = !isReplay && filteredDevices.some(d => d.id == id);
            if (isVisible) {
                if (!map.hasLayer(marker)) marker.addTo(map);
            } else {
                if (map.hasLayer(marker)) map.removeLayer(marker);
            }
        }

        // Show/Hide contact markers based on search
        for (const id in contactMarkers) {
            const marker = contactMarkers[id];
            const circle = contactCircles[id];
            const isVisible = filteredContacts.some(c => c.id === id);
            if (isVisible) {
                if (contactClusterGroup) {
                    if (!contactClusterGroup.hasLayer(marker)) contactClusterGroup.addLayer(marker);
                } else {
                    if (!map.hasLayer(marker)) marker.addTo(map);
                }
                if (circle && !map.hasLayer(circle)) circle.addTo(map);
            } else {
                if (contactClusterGroup) {
                    if (contactClusterGroup.hasLayer(marker)) contactClusterGroup.removeLayer(marker);
                } else {
                    if (map.hasLayer(marker)) map.removeLayer(marker);
                }
                if (circle && map.hasLayer(circle)) map.removeLayer(circle);
            }
        }

        // Show/Hide visit markers based on search
        for (const id in visitMarkers) {
            const marker = visitMarkers[id];
            const isVisible = filteredVisits.some(v => v.id === id);
            if (isVisible) {
                if (contactClusterGroup) {
                    if (!contactClusterGroup.hasLayer(marker)) contactClusterGroup.addLayer(marker);
                } else {
                    if (!map.hasLayer(marker)) marker.addTo(map);
                }
            } else {
                if (contactClusterGroup) {
                    if (contactClusterGroup.hasLayer(marker)) contactClusterGroup.removeLayer(marker);
                } else {
                    if (map.hasLayer(marker)) map.removeLayer(marker);
                }
            }
        }
        updateCircleVisibility();
    }

    function startPositionPolling() {
        if (pollingInterval) clearInterval(pollingInterval);
        
        // Calculate poll frequency dynamically: use the setting value (seconds) * 1000 milliseconds.
        // Fallback to 10 seconds if not loaded or set to 0.
        const dynamicIntervalMs = (browserSyncInterval && browserSyncInterval > 0) ? (browserSyncInterval * 1000) : 10000;
        
        pollingInterval = setInterval(() => {
            if (!document.getElementById('traccar_live_map')) {
                clearInterval(pollingInterval);
                pollingInterval = null;
                isInitialized = false;
                return;
            }
            checkForNewPositions();
            triggerBrowserSync();
        }, dynamicIntervalMs);
    }

    function triggerBrowserSync() {
        if (syncInFlight) return;
        const now = Date.now();
        if (syncCooldownUntil && now < syncCooldownUntil) return;

        syncInFlight = true;
        updateStatus("Syncing...", "info");

        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/web/dataset/call_kw', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.timeout = REQUEST_TIMEOUT;

        const data = {
            jsonrpc: "2.0",
            method: "call",
            params: {
                model: "traccar.config",
                method: "action_browser_sync",
                args: [],
                kwargs: {}
            },
            id: Math.floor(Math.random() * 1000000)
        };

        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                syncInFlight = false;
                if (xhr.status === 200) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        if (response.result) {
                            setTimeout(() => {
                                checkForNewPositions();
                            }, 1000);
                            updateStatus("Online", "success");
                            syncCooldownUntil = null;
                        } else {
                            updateStatus("Up to date", "success");
                        }
                    } catch (e) {
                        updateStatus("Error", "danger");
                    }
                } else {
                    syncCooldownUntil = Date.now() + SYNC_FAILURE_COOLDOWN;
                    updateStatus("Retry soon", "warning");
                }
            }
        };
        xhr.send(JSON.stringify(data));
    }

    function checkForNewPositions() {
        if (checkInFlight || positionsInFlight) return;
        checkInFlight = true;

        // Use a 5-minute safety overlap to account for clock skew between browser and server
        const safetyMargin = 5 * 60 * 1000;
        const checkTime = new Date(lastPositionCheck.getTime() - safetyMargin);
        
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/web/dataset/call_kw', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.timeout = 5000;

        const data = {
            jsonrpc: "2.0",
            method: "call",
            params: {
                model: "traccar.position",
                method: "search_count",
                args: [],
                kwargs: {
                    domain: [["create_date", ">", checkTime.toISOString()], ["device_id", "!=", false]]
                }
            },
            id: Math.floor(Math.random() * 1000000)
        };

        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                checkInFlight = false;
                if (xhr.status === 200) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        if (response.result && response.result > 0) {
                            lastPositionCheck = new Date();
                            loadDevicePositions();
                        }
                    } catch (e) {}
                }
            }
        };
        xhr.send(JSON.stringify(data));
    }

    function loadLeafletAndInit() {
        if (window.L && window.L.markerClusterGroup) {
            leafletLoaded = true;
            initializeLiveTracking();
            return;
        }
        
        // Leaflet CSS
        const cssLink = document.createElement('link');
        cssLink.rel = 'stylesheet';
        cssLink.href = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css';
        document.head.appendChild(cssLink);

        // MarkerCluster CSS
        const clusterCss = document.createElement('link');
        clusterCss.rel = 'stylesheet';
        clusterCss.href = 'https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.css';
        document.head.appendChild(clusterCss);

        const clusterDefaultCss = document.createElement('link');
        clusterDefaultCss.rel = 'stylesheet';
        clusterDefaultCss.href = 'https://unpkg.com/leaflet.markercluster@1.4.1/dist/MarkerCluster.Default.css';
        document.head.appendChild(clusterDefaultCss);

        // Leaflet JS
        const script = document.createElement('script');
        script.src = 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js';
        script.onload = () => {
            // After Leaflet loads, load MarkerCluster JS
            const clusterScript = document.createElement('script');
            clusterScript.src = 'https://unpkg.com/leaflet.markercluster@1.4.1/dist/leaflet.markercluster.js';
            clusterScript.onload = () => {
                leafletLoaded = true;
                initializeLiveTracking();
            };
            document.head.appendChild(clusterScript);
        };
        document.head.appendChild(script);
    }

    function initializeLiveTracking() {
        if (isInitialized) return;
        const mapElement = document.getElementById('traccar_live_map');
        if (!mapElement) return;

        // Reset state variables to ensure fresh markers on the new map instance
        deviceMarkers = {};
        allDevices = [];
        contactMarkers = {};
        contactCircles = {};
        allContacts = [];
        visitMarkers = {};
        allVisits = [];
        contactClusterGroup = null;
        hasInitialCentered = false;
        hasProcessedFocusPosition = false;
        if (focusPositionMarker) {
            try {
                map.removeLayer(focusPositionMarker);
            } catch(e) {}
            focusPositionMarker = null;
        }

        // Initialize Map
        map = L.map('traccar_live_map', { zoomControl: false }).setView([30.0444, 31.2357], 10);
        L.control.zoom({ position: 'bottomright' }).addTo(map);
        
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '© OpenStreetMap',
            maxZoom: 19
        }).addTo(map);

        contactClusterGroup = L.markerClusterGroup({
            maxClusterRadius: 50,
            showCoverageOnHover: false,
            spiderfyOnMaxZoom: true,
            zoomToBoundsOnClick: true,
            iconCreateFunction: function(cluster) {
                const childCount = cluster.getChildCount();
                
                // Beautiful gradient background depending on the cluster size
                let bgGradient = 'linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%)'; 
                if (childCount >= 10) {
                    bgGradient = 'linear-gradient(135deg, #f59e0b 0%, #d97706 100%)'; 
                }
                if (childCount >= 50) {
                    bgGradient = 'linear-gradient(135deg, #ef4444 0%, #b91c1c 100%)'; 
                }

                return L.divIcon({
                    html: `
                        <div class="custom-map-cluster" style="
                            background: ${bgGradient};
                            color: white;
                            width: 38px;
                            height: 38px;
                            border-radius: 50%;
                            border: 3px solid rgba(255, 255, 255, 0.9);
                            box-shadow: 0 4px 12px rgba(0,0,0,0.35), inset 0 2px 4px rgba(255,255,255,0.4);
                            display: flex;
                            align-items: center;
                            justify-content: center;
                            font-size: 13px;
                            font-weight: 700;
                            transition: transform 0.15s ease-in-out;
                            cursor: pointer;
                        ">
                            <span>${childCount}</span>
                        </div>
                    `,
                    className: 'custom-leaflet-cluster',
                    iconSize: [38, 38],
                    iconAnchor: [19, 19]
                });
            }
        }).addTo(map);

        map.on('zoomend moveend', updateCircleVisibility);

        map.on('dragstart', () => {
            if (followPlaybackCar) {
                updateCarTrackingUI(false);
            }
        });

        loadDevicePositions();
        isInitialized = true;
    }

    function loadDevicePositions() {
        if (positionsInFlight) return;
        positionsInFlight = true;

        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/web/dataset/call_kw', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.timeout = REQUEST_TIMEOUT;

        const data = {
            jsonrpc: "2.0",
            method: "call",
            params: {
                model: "traccar.device",
                method: "search_read",
                args: [],
                kwargs: {
                    fields: ["name", "unique_id", "status", "last_update", "latitude", "longitude", "battery"],
                    domain: [
                        ["unique_id", "!=", "DASHBOARD_ONLY"]
                    ],
                    limit: 100,
                    order: "write_date desc"
                }
            },
            id: Math.floor(Math.random() * 1000000)
        };

        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                positionsInFlight = false;
                if (xhr.status === 200) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        if (response.result) {
                            allDevices = response.result;
                        }
                    } catch (e) {}
                }
                loadContactsData();
            }
        };
        xhr.send(JSON.stringify(data));
    }

    function loadContactsData() {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/web/dataset/call_kw', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.timeout = REQUEST_TIMEOUT;

        const data = {
            jsonrpc: "2.0",
            method: "call",
            params: {
                model: "res.partner",
                method: "search_read",
                args: [],
                kwargs: {
                    fields: ["name", "partner_latitude", "partner_longitude", "visit_latitude", "visit_longitude", "location_radius", "phone", "mobile", "email", "street", "city", "location_type"],
                    domain: [
                        "|",
                        "|",
                        ["partner_latitude", "!=", 0.0],
                        ["partner_longitude", "!=", 0.0],
                        "|",
                        ["visit_latitude", "!=", 0.0],
                        ["visit_longitude", "!=", 0.0]
                    ],
                    limit: 100
                }
            },
            id: Math.floor(Math.random() * 1000000)
        };

        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                if (xhr.status === 200) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        if (response.result) {
                            allContacts = response.result.map(partner => ({
                                id: 'contact_' + partner.id,
                                partner_id: partner.id,
                                name: partner.name,
                                is_contact: true,
                                latitude: partner.visit_latitude || partner.partner_latitude,
                                longitude: partner.visit_longitude || partner.partner_longitude,
                                location_radius: partner.location_radius || 50,
                                phone: partner.phone || partner.mobile || '',
                                email: partner.email || '',
                                street: partner.street || '',
                                city: partner.city || '',
                                location_type: partner.location_type || 'customer',
                                status: 'contact'
                            })).filter(c => c.latitude && c.longitude);
                        }
                    } catch (e) {}
                }
                loadVisitsData();
            }
        };
        xhr.send(JSON.stringify(data));
    }

    function loadVisitsData() {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/web/dataset/call_kw', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.timeout = REQUEST_TIMEOUT;

        const dateFromInput = document.getElementById('visit_date_from');
        const dateToInput = document.getElementById('visit_date_to');
        
        let dateFromUtc = null;
        let dateToUtc = null;
        
        if (dateFromInput && dateFromInput.value) {
            dateFromUtc = getUTCDateString(dateFromInput.value);
        }
        if (dateToInput && dateToInput.value) {
            dateToUtc = getUTCDateString(dateToInput.value);
        }
        
        const domain = [
            ["latitude", "!=", false],
            ["longitude", "!=", false],
            ["latitude", "!=", ""],
            ["longitude", "!=", ""]
        ];
        
        if (dateFromUtc) {
            domain.push(['planned_time', '>=', dateFromUtc]);
        }
        if (dateToUtc) {
            domain.push(['planned_time', '<=', dateToUtc]);
        }

        const data = {
            jsonrpc: "2.0",
            method: "call",
            params: {
                model: "sales.rep.visit",
                method: "search_read",
                args: [],
                kwargs: {                    fields: ["name", "customer_name", "planned_time", "visit_time", "latitude", "longitude", "visit_result", "state", "notes", "partner_id", "sales_rep_id", "duration"],
                    domain: domain,
                    limit: 100
                }
            },
            id: Math.floor(Math.random() * 1000000)
        };
 
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                if (xhr.status === 200) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        if (response.result) {
                            allVisits = response.result.map(v => {
                                const lat = parseFloat(v.latitude);
                                const lng = parseFloat(v.longitude);
                                return {
                                    id: 'visit_' + v.id,
                                    visit_id: v.id,
                                    partner_id: v.partner_id ? v.partner_id[0] : null,
                                    sales_rep_id: v.sales_rep_id ? v.sales_rep_id[0] : null,
                                    sales_rep_name: v.sales_rep_id ? v.sales_rep_id[1] : "", // Full Representative Name
                                    name: `${v.customer_name || 'Visit'} (${v.name})`,
                                    is_visit: true,
                                    latitude: lat,
                                    longitude: lng,
                                    planned_time: v.planned_time || '',
                                    visit_time: v.visit_time || '',
                                    visit_result: v.visit_result || '',
                                    state: v.state || 'planned',
                                    notes: v.notes || '',
                                    duration: v.duration || 0.0,
                                    status: 'visit'
                                };;
                            }).filter(v => v.latitude && v.longitude);
                        }
                    } catch (e) {
                        console.error("Error parsing visits data:", e);
                    }
                }
                processAllData();
            }
        };
        xhr.send(JSON.stringify(data));
    }

    function processAllData() {
        if (!map) return;

        // Remove obsolete contact markers and circles
        for (const id in contactMarkers) {
            if (!allContacts.some(c => c.id === id)) {
                if (contactClusterGroup) {
                    contactClusterGroup.removeLayer(contactMarkers[id]);
                } else {
                    map.removeLayer(contactMarkers[id]);
                }
                delete contactMarkers[id];
                if (contactCircles[id]) {
                    map.removeLayer(contactCircles[id]);
                    delete contactCircles[id];
                }
            }
        }
        // Remove obsolete device markers
        for (const id in deviceMarkers) {
            if (!allDevices.some(d => d.id == id)) {
                map.removeLayer(deviceMarkers[id]);
                delete deviceMarkers[id];
            }
        }
        // Remove obsolete visit markers
        for (const id in visitMarkers) {
            if (!allVisits.some(v => v.id === id)) {
                if (contactClusterGroup) {
                    contactClusterGroup.removeLayer(visitMarkers[id]);
                } else {
                    map.removeLayer(visitMarkers[id]);
                }
                delete visitMarkers[id];
            }
        }

        allDevices.forEach(device => addDeviceMarker(device));
        allContacts.forEach(contact => addContactMarker(contact));
        allVisits.forEach(visit => addVisitMarker(visit));
        
        const term = document.getElementById('device_search')?.value.toLowerCase() || '';
        filterDevices(term);
        
        updateStatistics(allDevices);
        if (!hasInitialCentered && (allDevices.length > 0 || allContacts.length > 0 || allVisits.length > 0)) {
            autoCenterMap();
            hasInitialCentered = true;
        }

        if (!hasProcessedFocusPosition) {
            hasProcessedFocusPosition = true;
            handleFocusPosition();
        }
    }

    function getQueryParam(name) {
        const hash = window.location.hash || '';
        const search = window.location.search || '';
        const match = hash.match(new RegExp('[#&]' + name + '=([^&]*)')) || 
                      search.match(new RegExp('[?&]' + name + '=([^&]*)'));
        return match ? decodeURIComponent(match[1]) : null;
    }

    function createFocusPositionPopupContent(pos) {
        const deviceName = pos.device_id ? pos.device_id[1] : 'Unknown Device';
        const dateStr = pos.device_time ? new Date(pos.device_time.replace(' ', 'T') + 'Z').toLocaleString() : 'N/A';
        return `
            <div class="traccar-popup focus-popup">
                <div class="popup-header" style="background: #e11d48; color: white; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; font-weight: bold; border-top-left-radius: 8px; border-top-right-radius: 8px;">
                    <span>📍 Focused Position</span>
                    <a href="javascript:void(0);" onclick="window._traccar_remove_focus_position();" title="Remove Position" style="color: white; font-size: 16px; text-decoration: none; display: flex; align-items: center;"><i class="fa fa-times"></i></a>
                </div>
                <div class="popup-body" style="padding: 12px; font-size: 12px; color: #374151;">
                    <div style="margin-bottom: 8px; font-weight: 600; font-size: 13px; color: #111827;">
                        Device: ${deviceName}
                    </div>
                    <div class="popup-info-row" style="display: flex; align-items: center; margin-bottom: 6px; gap: 8px;">
                        <i class="fa fa-map-marker text-danger" style="width: 14px;"></i>
                        <span>${pos.latitude.toFixed(6)}, ${pos.longitude.toFixed(6)}</span>
                    </div>
                    <div class="popup-info-row" style="display: flex; align-items: center; margin-bottom: 6px; gap: 8px;">
                        <i class="fa fa-clock-o text-muted" style="width: 14px;"></i>
                        <span>Time: ${dateStr}</span>
                    </div>
                    ${pos.speed_kmh > 0 ? `
                    <div class="popup-info-row" style="display: flex; align-items: center; margin-bottom: 6px; gap: 8px;">
                        <i class="fa fa-tachometer text-muted" style="width: 14px;"></i>
                        <span>Speed: ${pos.speed_kmh.toFixed(1)} km/h</span>
                    </div>` : ''}
                    ${pos.address ? `
                    <div class="popup-info-row" style="display: flex; align-items: flex-start; margin-bottom: 6px; gap: 8px;">
                        <i class="fa fa-home text-muted" style="width: 14px; margin-top: 2px;"></i>
                        <span>Address: ${pos.address}</span>
                    </div>` : ''}
                </div>
            </div>
        `;
    }

    function handleFocusPosition() {
        const focusPositionId = initialFocusPositionId || getQueryParam('focus_position_id');
        if (!focusPositionId) return;

        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/web/dataset/call_kw', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.timeout = REQUEST_TIMEOUT;

        const data = {
            jsonrpc: "2.0",
            method: "call",
            params: {
                model: "traccar.position",
                method: "search_read",
                args: [],
                kwargs: {
                    fields: ["device_id", "device_time", "latitude", "longitude", "speed_kmh", "course", "address", "altitude"],
                    domain: [["id", "=", parseInt(focusPositionId)]],
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
                            const pos = response.result[0];
                            const lat = parseFloat(pos.latitude);
                            const lng = parseFloat(pos.longitude);
                            if (isNaN(lat) || isNaN(lng) || (lat === 0 && lng === 0)) return;

                            if (focusPositionMarker) {
                                map.removeLayer(focusPositionMarker);
                            }

                            const customIcon = L.divIcon({
                                className: 'custom-focus-position-marker',
                                html: `<div class="marker-inner-focus" style="background: #e11d48; width: 32px; height: 32px; border-radius: 50%; border: 3px solid white; box-shadow: 0 0 15px rgba(225, 29, 72, 0.6); display: flex; align-items: center; justify-content: center; color: white;"><i class="fa fa-map-pin" style="font-size: 14px;"></i></div>`,
                                iconSize: [32, 32],
                                iconAnchor: [16, 16]
                            });

                            focusPositionMarker = L.marker([lat, lng], { icon: customIcon })
                                .addTo(map)
                                .bindPopup(createFocusPositionPopupContent(pos));

                            if (map) {
                                map.flyTo([lat, lng], 15);
                                setTimeout(() => {
                                    focusPositionMarker.openPopup();
                                }, 500);
                            }
                        }
                    } catch (e) {
                        console.error("Error loading focus position:", e);
                    }
                }
            }
        };
        xhr.send(JSON.stringify(data));
    }

    function processDevicesData(devices) {
        // Obsolete function, retained for compatibility if called elsewhere, but we use processAllData now.
        processAllData();
    }

    function renderDeviceList(items) {
        const container = document.getElementById('device_list_container');
        if (!container) return;
        
        if (items.length === 0) {
            container.innerHTML = '<div class="p-4 text-center text-muted">No items found</div>';
            return;
        }

        // Group the items
        const reps = items.filter(item => !item.is_contact && !item.is_visit);
        const contacts = items.filter(item => item.is_contact);
        const visits = items.filter(item => item.is_visit);

        // Helper to render a single item
        function renderItem(item) {
            if (item.is_contact) {
                let faIcon = 'fa-user';
                let colorClass = 'text-primary';
                if (item.location_type === 'house') {
                    faIcon = 'fa-home';
                    colorClass = 'text-warning';
                } else if (item.location_type === 'company') {
                    faIcon = 'fa-building';
                    colorClass = 'text-success';
                } else if (item.location_type === 'other') {
                    faIcon = 'fa-map-marker';
                    colorClass = 'text-secondary';
                }

                return `
                    <div class="device-item ${selectedDeviceId === item.id ? 'active' : ''}" data-id="${item.id}">
                        <div class="device-item-header">
                            <div class="device-item-info">
                                <div class="device-item-name">
                                    <i class="fa ${faIcon} ${colorClass} me-2"></i>${item.name}
                                </div>
                                <div class="device-item-sub">
                                    <i class="fa fa-map-marker"></i> ${item.city || item.street || 'No address'}
                                    ${item.phone ? ` · ${item.phone}` : ''}
                                </div>
                                ${selectedDeviceId === item.id ? `
                                    <button class="btn-profile-sidebar">
                                        <i class="fa fa-user"></i> Show Profile
                                    </button>
                                ` : ''}
                            </div>
                            <span class="device-status-dot status-contact"></span>
                        </div>
                    </div>
                `;
            } else if (item.is_visit) {
                return `
                    <div class="device-item ${selectedDeviceId === item.id ? 'active' : ''}" data-id="${item.id}">
                        <div class="device-item-header">
                            <div class="device-item-info">
                                <div class="device-item-name">
                                    <i class="fa fa-calendar-check-o text-success me-2"></i>${item.name}
                                </div>
                                <div class="device-item-sub">
                                    <i class="fa fa-clock-o"></i> Started: ${item.planned_time || 'N/A'}
                                    ${item.visit_result ? ` · Result: ${item.visit_result}` : ''}
                                </div>
                                ${selectedDeviceId === item.id ? `
                                    <button class="btn-profile-sidebar btn-visit-details">
                                        <i class="fa fa-info-circle"></i> Visit Details
                                    </button>
                                ` : ''}
                            </div>
                            <span class="device-status-dot status-visit"></span>
                        </div>
                    </div>
                `;
            } else {
                return `
                    <div class="device-item ${selectedDeviceId === item.id ? 'active' : ''}" data-id="${item.id}">
                        <div class="device-item-header">
                            <div class="device-item-info">
                                <div class="device-item-name">${item.name}</div>
                                <div class="device-item-sub">
                                    <i class="fa fa-microchip"></i> ${item.unique_id}
                                    ${item.battery > 0 ? ` · <span class="battery-badge ${item.battery < 20 ? 'battery-low' : ''}">${item.battery}%</span>` : ''}
                                </div>
                                ${selectedDeviceId === item.id ? `
                                    <button class="btn-replay-sidebar">
                                        <i class="fa fa-history"></i> Replay Route
                                    </button>
                                ` : ''}
                            </div>
                            <span class="device-status-dot status-${item.status || 'unknown'}"></span>
                        </div>
                    </div>
                `;
            }
        }

        // Build HTML with sections
        let html = '';

        if (reps.length > 0) {
            html += `
                <div class="list-category-section" id="sec_reps">
                    <div class="list-category-header" data-target="group_reps">
                        <span><i class="fa fa-users text-primary me-2"></i>Sales Reps (${reps.length})</span>
                        <i class="fa fa-chevron-down toggle-arrow"></i>
                    </div>
                    <div class="list-category-content" id="group_reps">
                        ${reps.map(renderItem).join('')}
                    </div>
                </div>
            `;
        }

        if (contacts.length > 0) {
            html += `
                <div class="list-category-section" id="sec_contacts">
                    <div class="list-category-header" data-target="group_contacts">
                        <span><i class="fa fa-address-book text-success me-2"></i>Contacts (${contacts.length})</span>
                        <i class="fa fa-chevron-down toggle-arrow"></i>
                    </div>
                    <div class="list-category-content" id="group_contacts">
                        ${contacts.map(renderItem).join('')}
                    </div>
                </div>
            `;
        }

        if (visits.length > 0) {
            html += `
                <div class="list-category-section" id="sec_visits">
                    <div class="list-category-header" data-target="group_visits">
                        <span><i class="fa fa-calendar text-warning me-2"></i>Visits (${visits.length})</span>
                        <i class="fa fa-chevron-down toggle-arrow"></i>
                    </div>
                    <div class="list-category-content" id="group_visits">
                        ${visits.map(renderItem).join('')}
                    </div>
                </div>
            `;
        }

        container.innerHTML = html;

        // Restore collapsed state from window cache or click listeners
        if (!window._traccar_collapsed_groups) {
            window._traccar_collapsed_groups = {};
        }

        // Apply collapsed states
        for (const groupId in window._traccar_collapsed_groups) {
            const groupEl = document.getElementById(groupId);
            if (groupEl) {
                const header = groupEl.previousElementSibling;
                if (window._traccar_collapsed_groups[groupId]) {
                    groupEl.classList.add('collapsed');
                    if (header) header.querySelector('.toggle-arrow')?.classList.replace('fa-chevron-down', 'fa-chevron-right');
                }
            }
        }

        // Add Toggle Arrow Click Listeners
        container.querySelectorAll('.list-category-header').forEach(header => {
            header.onclick = function(e) {
                e.stopPropagation();
                const targetId = this.getAttribute('data-target');
                const contentEl = document.getElementById(targetId);
                const arrow = this.querySelector('.toggle-arrow');
                if (contentEl) {
                    const isCollapsed = contentEl.classList.toggle('collapsed');
                    window._traccar_collapsed_groups[targetId] = isCollapsed;
                    if (arrow) {
                        if (isCollapsed) {
                            arrow.classList.replace('fa-chevron-down', 'fa-chevron-right');
                        } else {
                            arrow.classList.replace('fa-chevron-right', 'fa-chevron-down');
                        }
                    }
                }
            };
        });

        // Add Click Events for List Items
        container.querySelectorAll('.device-item').forEach(item => {
            item.onclick = function(e) {
                // If clicked replay button specifically, toggle dialog
                if (e.target.closest('.btn-replay-sidebar')) {
                    const id = this.getAttribute('data-id');
                    window._traccar_toggle_replay(id);
                    return;
                }
                // If clicked visit details button, redirect to Odoo sales.rep.visit form view
                if (e.target.closest('.btn-visit-details')) {
                    const id = this.getAttribute('data-id');
                    const visitId = id.replace('visit_', '');
                    window.location.href = `/web#id=${visitId}&model=sales.rep.visit&view_type=form`;
                    return;
                }
                // If clicked profile button, redirect to Odoo partner form view
                if (e.target.closest('.btn-profile-sidebar')) {
                    const id = this.getAttribute('data-id');
                    const partnerId = id.replace('contact_', '');
                    window.location.href = `/web#id=${partnerId}&model=res.partner&view_type=form`;
                    return;
                }
                const id = this.getAttribute('data-id');
                selectDevice(id);
            };
        });
    }

    function selectDevice(id) {
        if (typeof id === 'string' && id.startsWith('contact_')) {
            selectedDeviceId = id;
            const contact = allContacts.find(c => c.id === selectedDeviceId);
            if (contact && contact.latitude && contact.longitude) {
                if (map) map.flyTo([contact.latitude, contact.longitude], 15);
                if (contactMarkers[selectedDeviceId]) {
                    contactMarkers[selectedDeviceId].openPopup();
                }
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            }
            return;
        }

        if (typeof id === 'string' && id.startsWith('visit_')) {
            selectedDeviceId = id;
            const visit = allVisits.find(v => v.id === selectedDeviceId);
            if (visit && visit.latitude && visit.longitude) {
                if (map) map.flyTo([visit.latitude, visit.longitude], 15);
                if (visitMarkers[selectedDeviceId]) {
                    visitMarkers[selectedDeviceId].openPopup();
                }
                const term = document.getElementById('device_search')?.value.toLowerCase() || '';
                filterDevices(term);
            }
            return;
        }
        
        selectedDeviceId = parseInt(id);
        const device = allDevices.find(d => d.id === selectedDeviceId);
        if (device && device.latitude && device.longitude) {
            // center map on device
            if (map) map.flyTo([device.latitude, device.longitude], 15);
            
            // open popup
            if (deviceMarkers[selectedDeviceId]) {
                deviceMarkers[selectedDeviceId].openPopup();
            }

            // Update UI list keeping current filtering and sorting active
            const term = document.getElementById('device_search')?.value.toLowerCase() || '';
            filterDevices(term);
        }
    }

    function addDeviceMarker(device) {
        if (isReplayMode) return; 

        const lat = parseFloat(device.latitude);
        const lng = parseFloat(device.longitude);
        if (isNaN(lat) || isNaN(lng)) return;
        if (lat === 0 && lng === 0) return; // Safety check: ignore 0,0 positions

        const iconColor = device.status === 'online' ? '#10b981' : (device.status === 'offline' ? '#ef4444' : '#f59e0b');
        const customIcon = L.divIcon({
            className: 'custom-device-marker',
            html: `<div class="marker-inner" style="background: ${iconColor};"></div>`,
            iconSize: [28, 28],
            iconAnchor: [14, 14]
        });

        if (deviceMarkers[device.id]) {
            deviceMarkers[device.id].setLatLng([lat, lng]);
            deviceMarkers[device.id].setPopupContent(createPopupContent(device));
        } else {
            deviceMarkers[device.id] = L.marker([lat, lng], { icon: customIcon })
                .addTo(map)
                .bindPopup(createPopupContent(device));
        }
    }

    function addContactMarker(contact) {
        if (isReplayMode) return;

        const lat = parseFloat(contact.latitude);
        const lng = parseFloat(contact.longitude);
        if (isNaN(lat) || isNaN(lng)) return;
        if (lat === 0 && lng === 0) return;

        let faIcon = 'fa-user';
        let bgColor = '#3b82f6'; // blue
        if (contact.location_type === 'house') {
            faIcon = 'fa-home';
            bgColor = '#f59e0b'; // amber
        } else if (contact.location_type === 'company') {
            faIcon = 'fa-building';
            bgColor = '#10b981'; // green
        } else if (contact.location_type === 'other') {
            faIcon = 'fa-map-marker';
            bgColor = '#6b7280'; // gray
        }

        const customIcon = L.divIcon({
            className: 'custom-contact-marker',
            html: `<div class="marker-inner-contact" style="background: ${bgColor}; width: 28px; height: 28px; border-radius: 50%; border: 2px solid white; box-shadow: 0 4px 8px rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; color: white;"><i class="fa ${faIcon}" style="font-size: 12px;"></i></div>`,
            iconSize: [28, 28],
            iconAnchor: [14, 14]
        });

        if (contactMarkers[contact.id]) {
            contactMarkers[contact.id].setLatLng([lat, lng]);
            contactMarkers[contact.id].setIcon(customIcon);
            contactMarkers[contact.id].setPopupContent(createContactPopupContent(contact));
            if (contactCircles[contact.id]) {
                contactCircles[contact.id].setLatLng([lat, lng]);
                contactCircles[contact.id].setRadius(contact.location_radius || 50);
            }
        } else {
            contactMarkers[contact.id] = L.marker([lat, lng], { icon: customIcon })
                .bindPopup(createContactPopupContent(contact));
            if (contactClusterGroup) {
                contactClusterGroup.addLayer(contactMarkers[contact.id]);
            } else {
                contactMarkers[contact.id].addTo(map);
            }

            contactCircles[contact.id] = L.circle([lat, lng], {
                radius: contact.location_radius || 50,
                color: '#3b82f6',
                fillColor: '#3b82f6',
                fillOpacity: 0.15,
                weight: 1,
                dashArray: '4, 4',
                interactive: false
            }).addTo(map);
        }
    }

    function createPopupContent(device) {
        const status = (device.status || 'unknown').toLowerCase();
        const bgColors = {
            'online': '#10b981',
            'offline': '#ef4444',
            'unknown': '#f59e0b'
        };
        const headerBg = bgColors[status] || '#f59e0b';

        return `
            <div class="traccar-popup">
                <div class="popup-header" style="background: ${headerBg}; display: flex; justify-content: space-between; align-items: center;">
                    <strong>${device.name || 'Device'}</strong>
                    <a href="javascript:void(0);" onclick="window._traccar_toggle_replay('${device.id}')" title="Replay History" style="color: white; font-size: 14px; text-decoration: none;"><i class="fa fa-history"></i></a>
                </div>
                <div class="popup-body">
                    <div class="popup-info-row">
                        <i class="fa fa-map-marker text-primary"></i>
                        <span>${device.latitude.toFixed(6)}, ${device.longitude.toFixed(6)}</span>
                    </div>
                    <div class="popup-info-row">
                        <i class="fa fa-clock-o"></i>
                        <span>${device.last_update ? new Date(device.last_update.replace(' ', 'T') + 'Z').toLocaleString() : 'N/A'}</span>
                    </div>
                    ${device.battery > 0 ? `
                    <div class="popup-info-row">
                        <i class="fa fa-battery-half"></i>
                        <span>Battery: ${device.battery}%</span>
                    </div>` : ''}
                    <div class="popup-badge status-${device.status || 'unknown'}">${(device.status || 'unknown').toUpperCase()}</div>
                </div>
            </div>
        `;
    }

    function createContactPopupContent(contact) {
        const partnerId = String(contact.id).replace('contact_', '');
        return `
            <div class="traccar-popup contact-popup">
                <div class="popup-header" style="background: #3b82f6; color: white; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;">
                    <strong>${contact.name || 'Contact'}</strong>
                    <a href="/web#id=${partnerId}&model=res.partner&view_type=form" title="View Profile" style="color: white; font-size: 14px; text-decoration: none;"><i class="fa fa-user"></i></a>
                </div>
                <div class="popup-body">
                    <div class="popup-info-row">
                        <i class="fa fa-map-marker text-primary"></i>
                        <span>${contact.latitude.toFixed(6)}, ${contact.longitude.toFixed(6)}</span>
                    </div>
                    ${contact.phone ? `
                    <div class="popup-info-row">
                        <i class="fa fa-phone"></i>
                        <span>${contact.phone}</span>
                    </div>` : ''}
                    ${contact.email ? `
                    <div class="popup-info-row">
                        <i class="fa fa-envelope"></i>
                        <span>${contact.email}</span>
                    </div>` : ''}
                    ${(contact.street || contact.city) ? `
                    <div class="popup-info-row">
                        <i class="fa fa-home"></i>
                        <span>${[contact.street, contact.city].filter(Boolean).join(', ')}</span>
                    </div>` : ''}
                    <div class="popup-badge" style="background: #dbeafe; color: #1e40af; padding: 4px 12px; border-radius: 6px; font-size: 11px; font-weight: 700; display: inline-block;">CONTACT</div>
                </div>
            </div>
        `;
    }

    function addVisitMarker(visit) {
        if (isReplayMode) return;

        const lat = parseFloat(visit.latitude);
        const lng = parseFloat(visit.longitude);
        if (isNaN(lat) || isNaN(lng)) return;
        if (lat === 0 && lng === 0) return;

        const customIcon = L.divIcon({
            className: 'custom-visit-marker',
            html: `<div class="marker-inner-visit" style="background: #10b981; width: 28px; height: 28px; border-radius: 50%; border: 2px solid white; box-shadow: 0 4px 8px rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; color: white;"><i class="fa fa-calendar-check-o" style="font-size: 12px;"></i></div>`,
            iconSize: [28, 28],
            iconAnchor: [14, 14]
        });

        if (visitMarkers[visit.id]) {
            visitMarkers[visit.id].setLatLng([lat, lng]);
            visitMarkers[visit.id].setPopupContent(createVisitPopupContent(visit));
        } else {
            visitMarkers[visit.id] = L.marker([lat, lng], { icon: customIcon })
                .bindPopup(createVisitPopupContent(visit));
            if (contactClusterGroup) {
                contactClusterGroup.addLayer(visitMarkers[visit.id]);
            } else {
                visitMarkers[visit.id].addTo(map);
            }
        }
    }

    function formatVisitDuration(hoursFloat) {
        if (!hoursFloat) return '0 mins';
        const totalMinutes = Math.round(hoursFloat * 60);
        const hrs = Math.floor(totalMinutes / 60);
        const mins = totalMinutes % 60;
        if (hrs > 0) {
            return `${hrs}h ${mins}m`;
        }
        return `${mins} mins`;
    }

    function createVisitPopupContent(visit) {
        const visitId = String(visit.id).replace('visit_', '');
        return `
            <div class="traccar-popup visit-popup">
                <div class="popup-header" style="background: #10b981; color: white; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center;">
                    <strong>${visit.name || 'Visit'}</strong>
                    <a href="/web#id=${visitId}&model=sales.rep.visit&view_type=form" title="View Visit Info" style="color: white; font-size: 14px; text-decoration: none;"><i class="fa fa-info-circle"></i></a>
                </div>
                <div class="popup-body" style="padding: 12px 16px;">
                    <div class="popup-info-row" style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px; color: #475569; font-size: 12px;">
                        <i class="fa fa-clock-o text-success" style="width: 14px; text-align: center;"></i>
                        <span>Start Time: ${visit.planned_time ? new Date(visit.planned_time.replace(' ', 'T') + 'Z').toLocaleString() : 'N/A'}</span>
                    </div>
                    ${visit.visit_time ? `
                    <div class="popup-info-row" style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px; color: #475569; font-size: 12px;">
                        <i class="fa fa-check-circle-o text-success" style="width: 14px; text-align: center;"></i>
                        <span>End Time: ${new Date(visit.visit_time.replace(' ', 'T') + 'Z').toLocaleString()}</span>
                    </div>` : ''}
                    ${visit.duration ? `
                    <div class="popup-info-row" style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px; color: #475569; font-size: 12px;">
                        <i class="fa fa-hourglass-half text-success" style="width: 14px; text-align: center;"></i>
                        <span>Duration: ${formatVisitDuration(visit.duration)}</span>
                    </div>` : ''}
                    ${visit.notes ? `
                    <div class="popup-info-row" style="display: flex; align-items: flex-start; gap: 8px; margin-bottom: 6px; color: #475569; font-size: 12px;">
                        <i class="fa fa-sticky-note-o text-success" style="width: 14px; text-align: center; margin-top: 2px;"></i>
                        <span style="font-style: italic; white-space: pre-line;">${visit.notes}</span>
                    </div>` : ''}
                    <div class="popup-badge" style="background: #d1fae5; color: #065f46; padding: 4px 12px; border-radius: 6px; font-size: 11px; font-weight: 700; display: inline-block; margin-top: 4px; text-transform: uppercase;">${visit.state || 'VISIT'}</div>
                </div>
            </div>
        `;
    }

    function autoCenterMap() {
        if (!map) return;
        const markers = [...Object.values(deviceMarkers), ...Object.values(contactMarkers), ...Object.values(visitMarkers)];
        if (markers.length === 0) return;
        const group = new L.featureGroup(markers);
        map.fitBounds(group.getBounds(), { padding: [50, 50] });
    }

    function updateStatistics(devices) {
        const totalEl = document.getElementById('total_devices');
        const onlineEl = document.getElementById('online_devices');
        const offlineEl = document.getElementById('offline_devices');
        const unknownEl = document.getElementById('unknown_devices');
        if (totalEl) totalEl.textContent = devices.length;
        if (onlineEl) onlineEl.textContent = devices.filter(d => d.status === 'online').length;
        if (offlineEl) offlineEl.textContent = devices.filter(d => d.status === 'offline').length;
        if (unknownEl) unknownEl.textContent = devices.filter(d => d.status === 'unknown').length;
    }

    function toggleMapExpansion() {
        console.log('Toggle Map Expansion triggered');
        const btn = document.getElementById('btn_toggle_fullscreen');
        let wrapper = document.querySelector('.traccar-dashboard-wrapper');
        
        // Fallback: search up from the button if selector fails
        if (!wrapper && btn) {
            wrapper = btn.closest('.traccar-dashboard-wrapper');
        }

        if (!wrapper) {
            console.error('Traccar Dashboard Wrapper not found!');
            return;
        }

        const isExpanded = wrapper.classList.toggle('map-expanded');
        console.log('Map Expanded state:', isExpanded);
        
        // Update Icon
        if (btn) {
            const icon = btn.querySelector('i');
            if (icon) {
                icon.className = isExpanded ? 'fa fa-compress' : 'fa fa-arrows-alt';
            }
            btn.title = isExpanded ? 'Collapse Map' : 'Expand Map';
        }

        // Critical: Update Leaflet size after transition
        setTimeout(() => {
            if (map) {
                console.log('Invalidating map size...');
                map.invalidateSize({ animate: true });
                if (isExpanded) {
                    autoCenterMap();
                }
            }
            adjustOverlaysLayout();
        }, 400); // Slightly longer timeout to ensure transition finished
    }

    // -------------------------------
    // Replay Logic
    // -------------------------------
    function adjustOverlaysLayout() {
        const wrapper = document.querySelector('.traccar-dashboard-wrapper');
        const filterOverlay = document.getElementById('traccar_filter_overlay');
        const replayOverlay = document.getElementById('traccar_replay_overlay');
        if (!replayOverlay) return;

        const isMapExpanded = wrapper ? wrapper.classList.contains('map-expanded') : false;
        const isFilterOpen = filterOverlay ? !filterOverlay.classList.contains('d-none') : false;

        // Base offset depending on whether the sidebar is collapsed (map-expanded)
        let leftOffset = isMapExpanded ? 16 : 352;

        if (isFilterOpen) {
            // Shift the replay card to the right of the filter card (280px width + 16px gap)
            leftOffset += 296;
        }

        replayOverlay.style.left = leftOffset + 'px';
    }

    function toggleReplayDialog(show) {
        const overlay = document.getElementById('traccar_replay_overlay');
        if (overlay) {
            overlay.classList.toggle('d-none', !show);
            adjustOverlaysLayout();
            
            if (show) {
                const replayBody = document.querySelector('.replay-body');
                if (replayBody) replayBody.classList.remove('d-none');
                const collapseReplayBtn = document.getElementById('btn_collapse_replay');
                const icon = collapseReplayBtn?.querySelector('i');
                if (icon) icon.className = 'fa fa-chevron-up';
                if (collapseReplayBtn) collapseReplayBtn.title = 'Collapse';
            }
        }
        if (!show) clearReplay();
    }

    function confirmReplay() {
        const fromRaw = document.getElementById('replay_from').value;
        const toRaw = document.getElementById('replay_to').value;
        const from = getUTCDateString(fromRaw);
        const to = getUTCDateString(toRaw);
        const status = document.getElementById('replay_status');
        const btn = document.getElementById('btn_confirm_replay');
 
        if (!from || !to || !selectedDeviceId) return;
 
        status.classList.remove('d-none');
        isReplayMode = true;
 
        let originalBtnHtml = '';
        if (btn) {
            originalBtnHtml = btn.innerHTML;
            btn.style.pointerEvents = 'none';
            btn.style.opacity = '0.6';
            btn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Loading...';
        }
 
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/web/dataset/call_kw', true);
        xhr.setRequestHeader('Content-Type', 'application/json');
        xhr.timeout = 30000;
 
        const data = {
            jsonrpc: "2.0",
            method: "call",
            params: {
                model: "traccar.position",
                method: "search_read",
                args: [],
                kwargs: {
                    domain: [
                        ['device_id', '=', selectedDeviceId],
                        ['device_time', '>=', from],
                        ['device_time', '<=', to]
                    ],
                    fields: ['latitude', 'longitude', 'device_time', 'speed_kmh', 'course'],
                    order: 'device_time asc',
                    limit: 100000
                }
            },
            id: Math.floor(Math.random() * 1000000)
        };
 
        xhr.onreadystatechange = function() {
            if (xhr.readyState === 4) {
                status.classList.add('d-none');
                if (btn) {
                    btn.style.pointerEvents = 'auto';
                    btn.style.opacity = '1.0';
                    btn.innerHTML = originalBtnHtml;
                }
                if (xhr.status === 200) {
                    try {
                        const response = JSON.parse(xhr.responseText);
                        if (response.result && response.result.length > 0) {
                            drawRoute(response.result);
                        } else {
                            alert("No route data found for this period.");
                        }
                    } catch (e) {
                        console.error('Replay parse error:', e);
                    }
                } else {
                    alert("Failed to fetch route history from server.");
                }
            }
        };
        xhr.send(JSON.stringify(data));
    }

    function getSpeedColor(speed) {
        if (speed < 10) return '#ef4444'; // Red
        if (speed < 40) return '#f97316'; // Orange
        if (speed < 80) return '#0ea5e9'; // Blue (Moved away from Yellow for contrast)
        return '#10b981'; // Green
    }

    function calculateDistance(lat1, lon1, lat2, lon2) {
        const R = 6371; // km
        const dLat = (lat2 - lat1) * Math.PI / 180;
        const dLon = (lon2 - lon1) * Math.PI / 180;
        const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                  Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                  Math.sin(dLon / 2) * Math.sin(dLon / 2);
        const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
        return R * c;
    }

    function drawRoute(positions) {
        try {
            clearReplay();
            isReplayMode = true;

            const formatDateTimeTo12h = (dateTimeStr) => {
                if (!dateTimeStr) return "";
                try {
                    let isoStr = dateTimeStr.trim();
                    if (!isoStr.includes('T') && isoStr.includes(' ')) {
                        isoStr = isoStr.replace(' ', 'T');
                    }
                    if (!isoStr.includes('Z') && !isoStr.includes('UTC') && !isoStr.includes('+')) {
                        isoStr = isoStr + 'Z';
                    }
                    const date = new Date(isoStr);
                    if (isNaN(date.getTime())) {
                        return dateTimeStr;
                    }
                    
                    const year = date.getFullYear();
                    const month = String(date.getMonth() + 1).padStart(2, '0');
                    const day = String(date.getDate()).padStart(2, '0');
                    
                    let hours = date.getHours();
                    const minutes = String(date.getMinutes()).padStart(2, '0');
                    const seconds = String(date.getSeconds()).padStart(2, '0');
                    
                    const ampm = hours >= 12 ? 'PM' : 'AM';
                    hours = hours % 12;
                    hours = hours ? hours : 12;
                    const strHours = String(hours).padStart(2, '0');
                    
                    return `${year}-${month}-${day} ${strHours}:${minutes}:${seconds} ${ampm}`;
                } catch (e) {
                    return dateTimeStr;
                }
            };

            const parseDateSafe = (dVal) => {
                if (!dVal) return 0;
                if (dVal instanceof Date) return dVal.getTime();
                const dStr = String(dVal).trim();
                let isoStr = dStr;
                if (!isoStr.includes('T') && isoStr.includes(' ')) {
                    isoStr = isoStr.replace(' ', 'T');
                }
                if (!isoStr.includes('Z') && !isoStr.includes('UTC') && !isoStr.includes('+')) {
                    isoStr = isoStr + 'Z';
                }
                const parsed = new Date(isoStr);
                return isNaN(parsed.getTime()) ? new Date(dStr).getTime() : parsed.getTime();
            };
        
        // Let the general filterDevices handle which live tracking markers should be visible on map
        const term = document.getElementById('device_search')?.value.toLowerCase() || '';
        filterDevices(term);
 
        // 1. Filter out GPS jump spikes (cellular triangulation errors / teleportations)
        const cleanRawPositions = [];
        for (let i = 0; i < positions.length; i++) {
            const p = positions[i];
            if (i > 0 && i < positions.length - 1) {
                const prev = positions[i - 1];
                const next = positions[i + 1];
                
                const distPrevToCurr = calculateDistance(prev.latitude, prev.longitude, p.latitude, p.longitude) * 1000;
                const distCurrToNext = calculateDistance(p.latitude, p.longitude, next.latitude, next.longitude) * 1000;
                const distPrevToNext = calculateDistance(prev.latitude, prev.longitude, next.latitude, next.longitude) * 1000;
                
                // If a point jumps more than 150m away but the next point immediately returns to within 45m of the previous point, skip it.
                if (distPrevToCurr > 150 && distCurrToNext > 150 && distPrevToNext < 45) {
                    continue; 
                }
            }
            cleanRawPositions.push(p);
        }
        positions = cleanRawPositions;

        // Detect stops using a combined spatial (distance <= 20m from group start) and speed check:
        const compressedRoute = [];
        const waitingPoints = [];
        let calculatedDistanceVal = 0;
 
        const stopGroups = [];
        let currentGroup = [];
        
        for (let i = 0; i < positions.length; i++) {
            const p = positions[i];
            if (currentGroup.length === 0) {
                currentGroup.push(p);
            } else {
                const groupStart = currentGroup[0];
                // Calculate distance from the START of this potential stop group
                const distMeters = calculateDistance(groupStart.latitude, groupStart.longitude, p.latitude, p.longitude) * 1000;
                const speed = p.speed_kmh || 0;
                
                // Group points if they remain inside the 20m circle from the start of the stop and speed < 2 km/h
                if (distMeters <= 20 && speed < 2.0) {
                    currentGroup.push(p);
                } else {
                    const startPt = currentGroup[0];
                    const endPt = currentGroup[currentGroup.length - 1];
                    const parsedStart = parseDateSafe(startPt.device_time);
                    const parsedEnd = parseDateSafe(endPt.device_time);
                    const durationMs = parsedEnd - parsedStart;
                    
                    if (durationMs >= 60000) { // stopped for >= 1 minute
                        stopGroups.push([...currentGroup]);
                    } else {
                        compressedRoute.push(...currentGroup);
                    }
                    currentGroup = [p];
                }
            }
        }
        
        if (currentGroup.length > 0) {
            const startPt = currentGroup[0];
            const endPt = currentGroup[currentGroup.length - 1];
            const parsedStart = parseDateSafe(startPt.device_time);
            const parsedEnd = parseDateSafe(endPt.device_time);
            const durationMs = parsedEnd - parsedStart;
            
            if (durationMs >= 60000) {
                stopGroups.push([...currentGroup]);
            } else {
                compressedRoute.push(...currentGroup);
            }
        }
 
        // Add stops as waiting points and keep ONLY the average center point of each stop group
        const tempWaitingPoints = [];
        for (const grp of stopGroups) {
            const startPt = grp[0];
            const endPt = grp[grp.length - 1];
            
            // Calculate center of stop
            let sumLat = 0, sumLon = 0;
            grp.forEach(pt => { sumLat += pt.latitude; sumLon += pt.longitude; });
            const avgLat = sumLat / grp.length;
            const avgLon = sumLon / grp.length;
            
            tempWaitingPoints.push({
                latitude: avgLat,
                longitude: avgLon,
                startTime: startPt.device_time,
                endTime: endPt.device_time,
                pointCount: grp.length
            });
        }
 
        // Merge waiting points that are within 50 meters of each other, provided they never left the area (60m) in between
        const finalWaitingPoints = [];
        tempWaitingPoints.forEach(wp => {
            const closeWp = finalWaitingPoints.find(m => {
                const dist = calculateDistance(m.latitude, m.longitude, wp.latitude, wp.longitude) * 1000;
                if (dist > 50) return false;
                
                // Temporal-spatial check: Did they ever leave the 60m radius between the end of stop A and the start of stop B?
                const tStart = parseDateSafe(m.endTime);
                const tEnd = parseDateSafe(wp.startTime);
                let leftArea = false;
                
                for (let i = 0; i < positions.length; i++) {
                    const p = positions[i];
                    const pTime = parseDateSafe(p.device_time);
                    if (pTime > tStart && pTime < tEnd) {
                        const distFromCenter = calculateDistance(m.latitude, m.longitude, p.latitude, p.longitude) * 1000;
                        if (distFromCenter > 60) {
                            leftArea = true;
                            break;
                        }
                    }
                }
                return !leftArea;
            });
            
            if (closeWp) {
                const totalPoints = closeWp.pointCount + wp.pointCount;
                closeWp.latitude = (closeWp.latitude * closeWp.pointCount + wp.latitude * wp.pointCount) / totalPoints;
                closeWp.longitude = (closeWp.longitude * closeWp.pointCount + wp.longitude * wp.pointCount) / totalPoints;
                
                if (parseDateSafe(wp.startTime) < parseDateSafe(closeWp.startTime)) closeWp.startTime = wp.startTime;
                if (parseDateSafe(wp.endTime) > parseDateSafe(closeWp.endTime)) closeWp.endTime = wp.endTime;
                
                closeWp.pointCount = totalPoints;
            } else {
                finalWaitingPoints.push({ ...wp });
            }
        });

        // Reconstruct the route path (cleanedPositions) by filtering out all raw points
        // that fall inside any of the final waiting point time windows, and replacing them
        // with a single center point for that stop to keep the line connected.
        const cleanedPositions = [];
        const processedStops = new Set();
        
        for (let i = 0; i < positions.length; i++) {
            const p = positions[i];
            const pTime = parseDateSafe(p.device_time);
            
            let stopIndex = -1;
            for (let j = 0; j < finalWaitingPoints.length; j++) {
                const wp = finalWaitingPoints[j];
                const wpStart = parseDateSafe(wp.startTime);
                const wpEnd = parseDateSafe(wp.endTime);
                
                if (pTime >= wpStart && pTime <= wpEnd) {
                    stopIndex = j;
                    break;
                }
            }
            
            if (stopIndex === -1) {
                cleanedPositions.push(p);
            } else {
                if (!processedStops.has(stopIndex)) {
                    processedStops.add(stopIndex);
                    const wp = finalWaitingPoints[stopIndex];
                    cleanedPositions.push({
                        latitude: wp.latitude,
                        longitude: wp.longitude,
                        device_time: wp.startTime,
                        speed_kmh: 0,
                        course: p.course
                    });
                }
            }
        }
 
        // Update global waitingPoints array
        waitingPoints.length = 0;
        waitingPoints.push(...finalWaitingPoints);
 
        // Sort cleanedPositions by time to keep chronological order
        cleanedPositions.sort((a, b) => {
            return parseDateSafe(a.device_time) - parseDateSafe(b.device_time);
        });
 
        if (cleanedPositions.length === 0 && positions.length > 0) {
            cleanedPositions.push(positions[0]);
        }
 
        // Calculate total distance using cleaned positions (filtering out GPS drift, ignoring speed limits)
        for (let k = 0; k < cleanedPositions.length - 1; k++) {
            const p1 = cleanedPositions[k];
            const p2 = cleanedPositions[k+1];
            const dist = calculateDistance(p1.latitude, p1.longitude, p2.latitude, p2.longitude);
            if (dist > 0.05) { // Only count movement greater than 50 meters
                calculatedDistanceVal += dist;
            }
        }
 
        playbackPositions = cleanedPositions;

        // Show playback UI and center car button
        document.getElementById('playback_container').classList.remove('d-none');
        const centerCarBtn = document.getElementById('btn_center_car');
        if (centerCarBtn) centerCarBtn.classList.remove('d-none');
        
        const slider = document.getElementById('replay_slider');
        const distanceEl = document.getElementById('replay_total_distance');
        
        if (slider) {
            slider.max = playbackPositions.length - 1;
            slider.value = 0;
        }

        if (distanceEl) {
            distanceEl.textContent = calculatedDistanceVal.toFixed(2) + ' km';
        }

        // FeatureGroup to hold all arrows and route line
        replayPolyline = L.featureGroup().addTo(map);

        // Draw polyline connecting all positions in the playback sequence
        if (playbackPositions.length > 0) {
            const routeLatLngs = playbackPositions.map(p => [p.latitude, p.longitude]);
            routePolylineObj = L.polyline(routeLatLngs, {
                color: '#2563eb', // Premium royal blue
                weight: 4,
                opacity: 0.8
            });
            const showRouteLine = document.getElementById('toggle_route_line')?.checked !== false;
            if (showRouteLine) {
                routePolylineObj.addTo(replayPolyline);
            }
        }
        
        // Draw direction arrows at every position along the route
        playbackPositions.forEach((p, index) => {
            const speed = p.speed_kmh || 0;
            const rotation = p.course || 0;
            const speedColor = getSpeedColor(speed);
            
            const arrowIcon = L.divIcon({
                className: 'replay-arrow',
                html: `<div style="transform: rotate(${rotation}deg); color: ${speedColor}; display: flex; align-items: center; justify-content: center;">
                         <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor">
                            <path d="M12 2L4.5 20.29l.71.71L12 18l6.79 3 .71-.71z"/>
                         </svg>
                       </div>`,
                iconSize: [16, 16],
                iconAnchor: [8, 8]
            });
            
            const arrowMarker = L.marker([p.latitude, p.longitude], { 
                icon: arrowIcon,
                interactive: true 
            });
            
            arrowMarker.bindPopup(`
                <div class="traccar-popup">
                    <div class="popup-header" style="background: linear-gradient(135deg, #1e3a8a, #3b82f6); color: white; padding: 8px 12px; border-top-left-radius: 4px; border-top-right-radius: 4px; display: flex; align-items: center; justify-content: space-between;">
                        <strong><i class="fa fa-map-marker"></i> Position #${index + 1}</strong>
                        <span style="background: rgba(255,255,255,0.2); padding: 1px 6px; border-radius: 10px; font-size: 9px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;">Replay</span>
                    </div>
                    <div class="popup-body" style="padding: 10px 12px; font-size: 12px; line-height: 1.4; background: white;">
                         <div style="margin-bottom: 6px; display: flex; align-items: center; gap: 8px;">
                             <i class="fa fa-clock-o" style="color: #2563eb; width: 14px; text-align: center;"></i>
                             <span><strong>Time:</strong> ${formatDateTimeTo12h(p.device_time)}</span>
                         </div>
                         <div style="margin-bottom: 6px; display: flex; align-items: center; gap: 8px;">
                             <i class="fa fa-tachometer" style="color: ${speed > 0 ? '#16a34a' : '#ef4444'}; width: 14px; text-align: center;"></i>
                             <span><strong>Speed:</strong> <span style="font-weight: 700; color: ${speed > 0 ? '#16a34a' : '#dc2626'};">${speed.toFixed(1)} km/h</span></span>
                         </div>
                         <div style="display: flex; align-items: center; gap: 8px;">
                             <i class="fa fa-compass" style="color: #7c3aed; width: 14px; text-align: center;"></i>
                             <span><strong>Coords:</strong> <span style="font-family: monospace;">${p.latitude.toFixed(6)}°, ${p.longitude.toFixed(6)}°</span></span>
                         </div>
                    </div>
                </div>
            `);
            
            arrowMarker.addTo(replayPolyline);
        });

        // Zoom to route
        const start = playbackPositions[0];
        if (playbackPositions.length >= 2 && replayPolyline.getLayers().length > 0) {
            map.fitBounds(replayPolyline.getBounds(), { padding: [60, 60] });
        } else if (start) {
            map.setView([start.latitude, start.longitude], 16);
        }

        // Markers for Start and Finish
        const end = playbackPositions[playbackPositions.length - 1];

        const startMarker = L.circleMarker([start.latitude, start.longitude], {
            radius: 10, color: 'white', fillColor: '#10b981', fillOpacity: 1, weight: 3
        }).addTo(map);
        
        const startLabel = L.marker([start.latitude, start.longitude], {
            icon: L.divIcon({
                className: 'point-label start',
                html: 'START',
                iconSize: [50, 20],
                iconAnchor: [25, 35]
            })
        }).addTo(map);

        // Finish Point
        const finishMarker = L.circleMarker([end.latitude, end.longitude], {
            radius: 10, color: 'white', fillColor: '#ef4444', fillOpacity: 1, weight: 3
        }).addTo(map);

        const finishLabel = L.marker([end.latitude, end.longitude], {
            icon: L.divIcon({
                className: 'point-label finish',
                html: 'FINISH',
                iconSize: [50, 20],
                iconAnchor: [25, 35]
            })
        }).addTo(map);

        // Add playback marker (the "car")
        const carIcon = L.divIcon({
            className: 'playback-marker',
            html: '<div class="playback-marker-inner"><i class="fa fa-car"></i></div>',
            iconSize: [32, 32],
            iconAnchor: [16, 16]
        });
        playbackMarker = L.marker([start.latitude, start.longitude], { icon: carIcon, zIndexOffset: 1000 }).addTo(map);

        // Click on the car to toggle tracking / auto-centering
        playbackMarker.on('click', () => {
            updateCarTrackingUI(!followPlaybackCar);
        });

        replayMarkers.push(startMarker, startLabel, finishMarker, finishLabel, playbackMarker);

        // Update waiting points count badge
        const waitingCountEl = document.getElementById('replay_waiting_count');
        if (waitingCountEl) {
            waitingCountEl.textContent = waitingPoints.length;
        }

        // Add Waiting Points to Map and keep tracking reference
        waitingPointMarkers = [];
        waitingPointCircles = [];
        const showWaitingPoints = document.getElementById('toggle_waiting_points')?.checked !== false;

        waitingPoints.forEach(wp => {
            const clockIcon = L.divIcon({
                className: 'waiting-marker',
                html: `<div style="background: #f59e0b; width: 24px; height: 24px; border-radius: 50%; border: 2px solid white; box-shadow: 0 2px 4px rgba(0,0,0,0.3); display: flex; align-items: center; justify-content: center; color: white;"><i class="fa fa-clock-o" style="font-size: 11px;"></i></div>`,
                iconSize: [24, 24],
                iconAnchor: [12, 12]
            });
            const marker = L.marker([wp.latitude, wp.longitude], { icon: clockIcon, zIndexOffset: 2000 })
                .bindPopup(createWaitingPopupContent(wp));
            
            const circle = L.circle([wp.latitude, wp.longitude], {
                radius: 20,
                color: '#f59e0b',
                fillColor: '#f59e0b',
                fillOpacity: 0.15,
                weight: 1.5,
                dashArray: '4, 4'
            });
            
            if (showWaitingPoints) {
                marker.addTo(map);
                circle.addTo(map);
            }
            waitingPointMarkers.push(marker);
            waitingPointCircles.push(circle);
        });

        function createWaitingPopupContent(wp) {
            const parsedStart = new Date(wp.startTime.includes('Z') || wp.startTime.includes('UTC') ? wp.startTime : wp.startTime.replace(' ', 'T') + 'Z');
            const parsedEnd = new Date(wp.endTime.includes('Z') || wp.endTime.includes('UTC') ? wp.endTime : wp.endTime.replace(' ', 'T') + 'Z');
            const durationMs = parsedEnd - parsedStart;
            const totalSec = Math.floor(durationMs / 1000);
            const hours = Math.floor(totalSec / 3600);
            const mins = Math.floor((totalSec % 3600) / 60);
            const secs = totalSec % 60;
            const durationFormatted = `${String(hours).padStart(2,'0')}:${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}`;
            return `
                <div class="traccar-popup waiting-popup">
                    <div class="popup-header" style="background: #f59e0b; color: white; padding: 8px 12px; border-top-left-radius: 4px; border-top-right-radius: 4px;">
                        <strong><i class="fa fa-clock-o"></i> Stopped / Waiting</strong>
                    </div>
                    <div class="popup-body" style="padding: 10px 12px; font-size: 12px; line-height: 1.4;">
                        <div style="margin-bottom: 4px;"><strong>From:</strong> ${parsedStart.toLocaleString()}</div>
                        <div style="margin-bottom: 4px;"><strong>To:</strong> ${parsedEnd.toLocaleString()}</div>
                        <div style="font-weight: 700; color: #d97706; margin-top: 6px; font-size: 13px;">Duration: ${durationFormatted}</div>
                    </div>
                </div>
            `;
        }
        
        // Add Speed Legend if not already there
        if (!document.querySelector('.speed-legend')) {
            const legend = document.createElement('div');
            legend.className = 'speed-legend';
            legend.innerHTML = `
                <div class="legend-item"><div class="legend-color" style="background:#ef4444"></div><span>&lt; 10 km/h</span></div>
                <div class="legend-item"><div class="legend-color" style="background:#f97316"></div><span>10-40 km/h</span></div>
                <div class="legend-item"><div class="legend-color" style="background:#0ea5e9"></div><span>40-80 km/h</span></div>
                <div class="legend-item"><div class="legend-color" style="background:#10b981"></div><span>&gt; 80 km/h</span></div>
            `;
            document.querySelector('.traccar-map-section').appendChild(legend);
        }

        seekTo(0);
        } catch (e) {
            showRuntimeError(e);
        }
    }

    // -------------------------------
    // Playback Engine
    // -------------------------------
    function togglePlayback() {
        if (isPlaying) pausePlayback();
        else startPlayback();
    }

    function startPlayback() {
        if (isPlaying || playbackPositions.length === 0) return;
        isPlaying = true;
        document.getElementById('btn_play_pause').innerHTML = '<i class="fa fa-pause"></i>';
        
        const multiplier = parseFloat(document.getElementById('playback_speed_mult').value) || 1.0;
        const interval = 1000 / (multiplier * 2); // 2 steps per second base
        
        playbackTimer = setInterval(() => {
            if (playbackIndex < playbackPositions.length - 1) {
                playbackIndex++;
                updatePlaybackUI();
            } else {
                pausePlayback();
            }
        }, interval);
    }

    function pausePlayback() {
        isPlaying = false;
        if (playbackTimer) clearInterval(playbackTimer);
        document.getElementById('btn_play_pause').innerHTML = '<i class="fa fa-play"></i>';
    }

    function seekTo(index) {
        playbackIndex = index;
        updatePlaybackUI();
    }

    function updateCarTrackingUI(active) {
        followPlaybackCar = active;
        const btn = document.getElementById('btn_center_car');
        const inner = playbackMarker?.getElement()?.querySelector('.playback-marker-inner');
        
        if (active) {
            if (playbackMarker && map) {
                map.panTo(playbackMarker.getLatLng());
            }
            if (inner) inner.classList.add('tracking-active');
            if (btn) {
                btn.classList.add('active');
                btn.title = 'Stop tracking car';
            }
        } else {
            if (inner) inner.classList.remove('tracking-active');
            if (btn) {
                btn.classList.remove('active');
                btn.title = 'Track car';
            }
        }
    }

    function updatePlaybackUI() {
        try {
            const p = playbackPositions[playbackIndex];
            if (!p || !playbackMarker) return;

            playbackMarker.setLatLng([p.latitude, p.longitude]);
            
            // Update slider
            const slider = document.getElementById('replay_slider');
            if (slider) slider.value = playbackIndex;

            // Update time, speed, and coords text
            const timeEl = document.getElementById('replay_current_time');
            const speedEl = document.getElementById('replay_current_speed');
            const coordsEl = document.getElementById('replay_current_coords');
            
            if (timeEl) {
                const date = new Date(p.device_time.replace(' ', 'T') + 'Z');
                timeEl.textContent = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            }
            if (speedEl) {
                speedEl.textContent = Math.round(p.speed_kmh || 0) + ' km/h';
            }
            if (coordsEl) {
                coordsEl.innerHTML = `<i class="fa fa-map-marker"></i> ${p.latitude.toFixed(6)}, ${p.longitude.toFixed(6)}`;
            }

            // Map follows car if tracking is active
            if (followPlaybackCar && map) {
                map.panTo([p.latitude, p.longitude]);
            }
        } catch (e) {
            showRuntimeError(e);
        }
    }

    function clearReplay() {
        pausePlayback();
        updateCarTrackingUI(false);
        playbackPositions = [];
        playbackIndex = 0;
        routePolylineObj = null;
        
        const pbContainer = document.getElementById('playback_container');
        if (pbContainer) pbContainer.classList.add('d-none');
        const centerCarBtn = document.getElementById('btn_center_car');
        if (centerCarBtn) centerCarBtn.classList.add('d-none');

        const legend = document.querySelector('.speed-legend');
        if (legend) legend.remove();

        if (replayPolyline) {
            map.removeLayer(replayPolyline);
            replayPolyline = null;
        }
        replayMarkers.forEach(m => map.removeLayer(m));
        replayMarkers = [];
        
        waitingPointMarkers.forEach(m => map.removeLayer(m));
        waitingPointMarkers = [];
        waitingPointCircles.forEach(c => map.removeLayer(c));
        waitingPointCircles = [];
        
        isReplayMode = false;
        
        if (contactClusterGroup && !map.hasLayer(contactClusterGroup)) {
            contactClusterGroup.addTo(map);
        }
        
        // Return to live view by showing all markers again
        loadDevicePositions();
    }

    function updateCircleVisibility() {
        if (isReplayMode) return;
        
        // Find visible contact IDs from the filtered search results (in sidebar or UI)
        const term = document.getElementById('device_search')?.value.toLowerCase() || '';
        let filteredContacts = [];
        if (showCustomers) {
            filteredContacts = allContacts.filter(c => {
                if (c.location_type === 'customer' && !showLocCustomer) return false;
                if (c.location_type === 'house' && !showLocHouse) return false;
                if (c.location_type === 'company' && !showLocCompany) return false;
                if (c.location_type === 'other' && !showLocOther) return false;

                return !term || 
                    (c.name && c.name.toLowerCase().includes(term)) ||
                    (c.phone && c.phone.toLowerCase().includes(term)) ||
                    (c.city && c.city.toLowerCase().includes(term)) ||
                    (c.street && c.street.toLowerCase().includes(term));
            });
        }

        for (const id in contactMarkers) {
            const circle = contactCircles[id];
            if (circle) {
                const isVisible = filteredContacts.some(c => c.id === id);
                if (isVisible) {
                    if (!map.hasLayer(circle)) circle.addTo(map);
                } else {
                    if (map.hasLayer(circle)) map.removeLayer(circle);
                }
            }
        }
    }

    initializeWhenReady();

})();