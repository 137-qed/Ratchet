/* ============================================================
   RATCHET — Edge Device Health Dashboard  |  app.js
   Now powered by the Ratchet Edge Agent backend (real metrics)
   ============================================================ */

(() => {
  'use strict';

  // ── API Configuration ──
  const API_BASE = window.location.origin;
  const SSE_URL = `${API_BASE}/api/stream?devices=1`;

  // ── State ──
  let devices = [];
  let localDevice = null;
  let externalDevices = [];
  let selectedDeviceId = null;
  let cpuHistory = [];
  let memHistory = [];
  let netHistory = [];
  const HISTORY_LEN = 60;
  let events = [];
  let agentEvents = [];
  let currentMetrics = null;
  let sseSource = null;
  let currentAnalyticsType = null;
  const deviceHistory = new Map();
  const DEVICE_COLORS = [
    'rgb(56,139,253)', 'rgb(163,113,247)', 'rgb(63,185,80)', 'rgb(210,153,34)',
    'rgb(248,81,73)', 'rgb(57,197,187)', 'rgb(219,112,147)', 'rgb(255,159,67)',
    'rgb(46,213,115)', 'rgb(112,161,255)', 'rgb(255,107,107)', 'rgb(29,209,161)',
  ];

  // ── DOM refs ──
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);

  const elTotalDevices = $('#totalDevices');
  const elOnlineDevices = $('#onlineDevices');
  const elWarningDevices = $('#warningDevices');
  const elCriticalDevices = $('#criticalDevices');
  const elDeviceList = $('#deviceList');
  const elDeviceCount = $('#deviceCount');
  const elNearbyDevices = $('#nearbyDevices');
  const elNearbyList = $('#nearbyList');
  const elPairingNotice = $('#pairingNotice');
  const elSshDialog = $('#sshDialog');
  const elSshForm = $('#sshForm');
  let nearbySnapshot;
  let connectingDeviceId;
  const elDeviceFilter = $('#deviceFilterInput');
  const elEventsTbody = $('#eventsTbody');
  const elAlertBadge = $('#alertBadge');
  const elCpuAvg = $('#cpuAvg');
  const elMemAvg = $('#memAvg');
  const elNetAvg = $('#netAvg');
  const elDeviceDetail = $('#deviceDetail');
  const elStatCards = $('#statCards');
  const elChartsArea = $('#chartsArea');
  const elEventsSection = $('#eventsSection');
  const elDetailName = $('#detailName');
  const elDetailStatus = $('#detailStatus');
  const elDetailGrid = $('#detailGrid');
  const elDetailCpuVal = $('#detailCpuVal');
  const elDetailMemVal = $('#detailMemVal');
  const elViewTitle = $('#viewTitle');
  const elViewSubtitle = $('#viewSubtitle');
  const elChatView = $('#chatView');
  const elActiveModelName = $('#activeModelName');
  const elAgentTbody = $('#agentEventsTbody');
  const elAgentStatus = $('#agentStatusDot');
  const elSearchInput = $('#deviceFilterInput');
  const elAnalyticsView = $('#analyticsView');
  const elAnalyticsHome = $('#analyticsHome');
  const elAnalyticsDetail = $('#analyticsDetail');
  const elAnalyticsStandard = $('#analyticsStandard');
  const elAnaCpuFullCard = $('#anaCpuFullCard');
  const elAnalyticsTitle = $('#analyticsTitle');
  const elAnalyticsBadge = $('#analyticsBadge');
  const elAnalyticsGrid = $('#analyticsGrid');
  const elAnalyticsEventsTitle = $('#analyticsEventsTitle');
  const elAnalyticsEventsTbody = $('#analyticsEventsTbody');

  // ── Helpers ──
  function rand(min, max) { return Math.random() * (max - min) + min; }
  function randInt(min, max) { return Math.floor(rand(min, max + 1)); }
  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function timeAgo(ts) {
    const s = Math.floor((Date.now() / 1000 - ts));
    if (s < 0) return 'just now';
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  }

  function escapeHtml(value) {
    return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Determine device status from metrics ──
  function deriveStatus(cpu, mem, temp) {
    if (cpu >= 95 || mem >= 95 || temp >= 85) return 'critical';
    if (cpu >= 80 || mem >= 80 || temp >= 70) return 'warning';
    return 'online';
  }

  // ── Build "device" from real metrics ──
  function metricsToDevice(m) {
    const sys = m.system || {};
    const cpu = m.cpu?.percent || 0;
    const mem = m.memory?.percent || 0;
    const disk = m.disk?.percent || 0;
    const temp = m.temperature?.celsius || 0;
    const net = m.network || {};

    return {
      id: 'local',
      name: sys.hostname || 'Local Device',
      ip: getLocalIP(net),
      location: 'Local Machine',
      type: 'Edge Agent',
      os: sys.os || 'Unknown',
      uptime: sys.uptime || '0d 0h',
      status: deriveStatus(cpu, mem, temp),
      cpu: cpu,
      memory: mem,
      disk: disk,
      network: (net.active_connections || 0),
      temp: temp,
      processes: m.process_count || 0,
      lastSeen: m.timestamp || Date.now() / 1000,
      topProcesses: m.processes || [],
    };
  }

  function reportingDeviceToDevice(record) {
    const metrics = record.metrics;
    const uptime = metrics.uptime_seconds;
    return {
      id: `external:${record.id}`,
      name: metrics.hostname,
      ip: record.address || '—',
      subtitle: record.address || record.id,
      location: 'External Device',
      type: record.source === 'ssh' ? 'SSH Telemetry' : 'Remote Telemetry',
      os: metrics.os,
      uptime: uptime == null ? '—' : `${Math.floor(uptime / 86400)}d ${Math.floor(uptime / 3600) % 24}h`,
      status: record.online ? deriveStatus(metrics.cpu_percent, metrics.memory_percent, metrics.temperature_celsius) : 'offline',
      cpu: metrics.cpu_percent,
      memory: metrics.memory_percent,
      disk: metrics.disk_percent,
      network: metrics.network_connections,
      temp: metrics.temperature_celsius,
      processes: metrics.process_count,
      lastSeen: record.last_seen,
    };
  }

  function getLocalIP(net) {
    if (net.interfaces) {
      for (const [name, info] of Object.entries(net.interfaces)) {
        if (name === 'lo') continue;
        if (info.addresses) {
          for (const addr of info.addresses) {
            if (addr.family && addr.family.includes('AF_INET') && !addr.address.startsWith('127.')) {
              return addr.address;
            }
          }
        }
      }
    }
    return '127.0.0.1';
  }

  // ── Stat Cards ──
  function updateStats() {
    const total = devices.length;
    const online = devices.filter(d => d.status !== 'offline').length;
    const warnings = devices.filter(d => d.status === 'warning').length;
    const critical = devices.filter(d => d.status === 'critical').length;
    animateNumber(elTotalDevices, total);
    animateNumber(elOnlineDevices, online);
    animateNumber(elWarningDevices, warnings);
    animateNumber(elCriticalDevices, critical);
  }

  function animateNumber(el, target) {
    if (!el) return;
    const current = parseInt(el.textContent || '0', 10);
    if (current === target) return;
    el.textContent = target;
    el.style.transform = 'scale(1.15)';
    el.style.transition = 'transform 0.25s ease';
    setTimeout(() => { el.style.transform = 'scale(1)'; }, 250);
  }

  // ── Device List (right panel) ──
  function renderDeviceList(filter = '') {
    const filtered = filter
      ? devices.filter(d => d.name.toLowerCase().includes(filter) || (d.subtitle || d.ip).includes(filter))
      : devices;

    if (elDeviceCount) elDeviceCount.textContent = filtered.length;

    if (!elDeviceList) return;
    const focusedId = elDeviceList.contains(document.activeElement) ? document.activeElement.dataset.id : null;
    elDeviceList.innerHTML = filtered.map(d => `
      <button type="button" class="device-item ${d.id === selectedDeviceId ? 'selected' : ''}"
        data-id="${d.id}" aria-pressed="${d.id === selectedDeviceId}">
        <span class="device-status-dot ${d.status}"></span>
        <span class="device-meta">
          <span class="device-name">${escapeHtml(d.name)}</span>
          <span class="device-ip">${escapeHtml(d.subtitle || d.ip)}</span>
        </span>
        <span class="device-cpu">${d.status === 'offline' ? 'Offline' : `${d.cpu.toFixed(0)}%`}</span>
      </button>
    `).join('');

    // click handlers
    elDeviceList.querySelectorAll('.device-item').forEach(el => {
      el.addEventListener('click', () => selectDevice(el.dataset.id));
      if (el.dataset.id === focusedId) el.focus({ preventScroll: true });
    });
  }

  async function fetchNearbyDevices(refresh = false) {
    if (!elNearbyList) return;
    try {
      const query = refresh ? '?refresh=1' : '';
      const response = await fetch(`${API_BASE}/api/devices/nearby${query}`, { cache: 'no-store' });
      if (!response.ok) return;
      const { devices: pending, peers, scanning, discovery_error: error } = await response.json();
      elNearbyDevices.hidden = false;
      $('#refreshNearby').disabled = scanning;
      const snapshot = JSON.stringify([pending, peers, scanning, error]);
      if (snapshot === nearbySnapshot) return;
      nearbySnapshot = snapshot;
      const expanded = new Set(Array.from(elNearbyList.querySelectorAll('details[open]'), peer => peer.dataset.address));
      const focusKey = document.activeElement?.dataset.nearbyFocus;
      const discovered = peers.map(peer => {
        const address = escapeHtml(peer.address);
        const hostname = escapeHtml(peer.hostname);
        return `
          <details class="nearby-peer" data-address="${address}" ${expanded.has(peer.address) ? 'open' : ''}>
            <summary data-nearby-focus="${address}-name">
              <span class="device-name">${hostname}</span>
              ${hostname === address ? '' : `<span class="device-ip">${address}</span>`}
            </summary>
            <div class="nearby-actions">
              <button type="button" class="nearby-connect" data-action="publisher" data-nearby-focus="${address}-publisher">Send publisher</button>
              <button type="button" class="nearby-connect" data-action="${peer.ssh ? 'disconnect' : 'ssh'}"
                data-nearby-focus="${address}-ssh">${peer.ssh ? 'Disconnect SSH' : 'SSH connection'}</button>
            </div>
          </details>`;
      }).join('');
      const requests = pending.map(device => `
        <div class="nearby-device">
          <span class="device-meta">
            <span class="device-name">${escapeHtml(device.hostname)}</span>
            <span class="device-ip">${escapeHtml(device.address || device.os)} · ${escapeHtml(device.code)}</span>
          </span>
          <button type="button" class="nearby-connect" data-connect-id="${escapeHtml(device.id)}"
            data-nearby-focus="pair-${escapeHtml(device.id)}"
            aria-label="Connect ${escapeHtml(device.hostname)}">Connect</button>
        </div>
      `).join('');
      const status = error || (scanning ? 'Looking for devices on Wi-Fi…' : 'No nearby devices found.');
      elNearbyList.innerHTML = (discovered || `<p class="nearby-note">${escapeHtml(status)}</p>`) + requests;
      Array.from(elNearbyList.querySelectorAll('[data-nearby-focus]'))
        .find(element => element.dataset.nearbyFocus === focusKey)?.focus({ preventScroll: true });
    } catch (error) {
      console.error('Nearby devices request failed:', error);
    }
  }

  async function deviceAction(address, action, options) {
    const response = await fetch(`${API_BASE}/api/devices/nearby/${encodeURIComponent(address)}/${action}`, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    return result;
  }

  async function selectNearbyAction(button) {
    const peer = button.closest('[data-address]');
    const address = peer.dataset.address;
    const hostname = peer.querySelector('.device-name').textContent;
    const label = hostname === address ? address : `${hostname} (${address})`;
    const action = button.dataset.action;
    if (action === 'ssh') {
      elSshForm.reset();
      elSshForm.dataset.address = address;
      $('#sshInstructions').textContent = `Connect to ${label} to read its Linux system metrics.`;
      $('#sshNotice').textContent = '';
      elSshDialog.showModal();
      return;
    }

    button.disabled = true;
    elPairingNotice.textContent = '';
    try {
      if (action === 'publisher') {
        const result = await deviceAction(address, 'publisher', { method: 'POST' });
        $('#publisherLink').value = result.url;
        $('#publisherInstructions').textContent = `Share this link with ${label}. Open it on that device within 15 minutes, then follow the publisher’s instructions to authorize metrics.`;
        $('#publisherNotice').textContent = '';
        const dialog = $('#publisherDialog');
        if (!dialog.open) dialog.showModal();
      } else {
        await deviceAction(address, 'ssh', { method: 'DELETE' });
        elPairingNotice.textContent = 'SSH disconnected.';
        await fetchNearbyDevices();
      }
    } catch (error) {
      elPairingNotice.textContent = error.message || 'Could not connect to the device.';
    } finally {
      button.disabled = false;
    }
  }

  async function connectSsh(event) {
    event.preventDefault();
    const button = $('#submitSsh');
    const notice = $('#sshNotice');
    const password = $('#sshPassword');
    const body = JSON.stringify({
      username: $('#sshUsername').value.trim(), port: $('#sshPort').valueAsNumber,
      password: password.value || null, trusted_host: $('#sshTrustedHost').value.trim() || null,
    });
    password.value = '';
    button.disabled = true;
    $('#closeSshDialog').disabled = true;
    notice.textContent = 'Connecting over SSH…';
    try {
      const result = await deviceAction(elSshForm.dataset.address, 'ssh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
      });
      connectingDeviceId = `external:${result.id}`;
      elPairingNotice.textContent = 'SSH connected. Loading live readings.';
      elSshDialog.close();
      await fetchNearbyDevices();
    } catch (error) {
      notice.textContent = error.message || 'Could not connect over SSH.';
    } finally {
      button.disabled = false;
      $('#closeSshDialog').disabled = false;
    }
  }

  async function connectNearbyDevice(button) {
    if (button.disabled) return;
    button.disabled = true;
    elPairingNotice.textContent = '';
    try {
      const id = encodeURIComponent(button.dataset.connectId);
      const response = await fetch(`${API_BASE}/api/devices/nearby/${id}/connect`, { method: 'POST' });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      connectingDeviceId = `external:${result.id}`;
      elPairingNotice.textContent = 'Connected. Waiting for the first reading.';
      await fetchNearbyDevices();
    } catch (error) {
      elPairingNotice.textContent = error.message || 'Could not connect to the device.';
    } finally {
      button.disabled = false;
    }
  }

  // ── Select Device → show detail ──
  function selectDevice(id) {
    const d = devices.find(x => x.id === id);
    if (!d) return;
    selectedDeviceId = id;
    currentAnalyticsType = null;

    renderDeviceList(elDeviceFilter ? elDeviceFilter.value.toLowerCase() : '');

    // hide dashboard, show detail
    if (elStatCards) elStatCards.style.display = 'none';
    if (elChartsArea) elChartsArea.style.display = 'none';
    if (elEventsSection) elEventsSection.style.display = 'none';
    const elAgentSection = $('#agentSection');
    if (elAgentSection) elAgentSection.style.display = 'none';
    if (elAnalyticsView) elAnalyticsView.style.display = 'none';
    if (elChatView) elChatView.hidden = true;
    const settings = $('#settingsView');
    if (settings) settings.style.display = 'none';
    const headerRight = $('.header-right');
    if (headerRight) headerRight.style.display = '';
    if (elDeviceDetail) elDeviceDetail.style.display = 'block';
    renderDeviceDetail(d);
  }

  function renderDeviceDetail(d) {
    if (elViewTitle) elViewTitle.textContent = d.name;
    if (elViewSubtitle) elViewSubtitle.textContent = d.location;

    if (elDetailName) elDetailName.textContent = d.name;
    if (elDetailStatus) {
      elDetailStatus.textContent = d.status;
      elDetailStatus.className = 'status-pill ' + d.status;
    }

    if (elDetailGrid) {
      elDetailGrid.innerHTML = `
        <div class="detail-metric"><div class="detail-metric-label">IP Address</div><div class="detail-metric-value">${escapeHtml(d.ip)}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Type</div><div class="detail-metric-value">${d.type}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">OS</div><div class="detail-metric-value">${escapeHtml(d.os)}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Uptime</div><div class="detail-metric-value">${d.uptime}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">CPU</div><div class="detail-metric-value" style="color:${statusColor(d.cpu, 85, 60)}">${d.cpu.toFixed(1)}%</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Memory</div><div class="detail-metric-value" style="color:${statusColor(d.memory, 85, 65)}">${d.memory.toFixed(1)}%</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Disk</div><div class="detail-metric-value" style="color:${statusColor(d.disk, 90, 70)}">${d.disk.toFixed(1)}%</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Temperature</div><div class="detail-metric-value" style="color:${statusColor(d.temp, 80, 60)}">${d.temp == null ? '—' : `${d.temp.toFixed(1)} °C`}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Processes</div><div class="detail-metric-value">${d.processes ?? '—'}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Connections</div><div class="detail-metric-value">${d.network ?? '—'}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Last Seen</div><div class="detail-metric-value">${timeAgo(d.lastSeen)}</div></div>
        <div class="detail-metric"><div class="detail-metric-label">Status</div><div class="detail-metric-value" style="color:var(--${d.status})">${d.status.charAt(0).toUpperCase() + d.status.slice(1)}</div></div>
      `;
    }

    const history = deviceHistory.get(d.id);
    if (elDetailCpuVal) elDetailCpuVal.textContent = `${d.cpu.toFixed(1)}%`;
    if (elDetailMemVal) elDetailMemVal.textContent = `${d.memory.toFixed(1)}%`;
    drawSparkline('detailCpuChart', history?.cpu || [], 'rgb(56,139,253)', 'rgba(56,139,253,0.25)', 'rgba(56,139,253,0)', 100);
    drawSparkline('detailMemChart', history?.memory || [], 'rgb(163,113,247)', 'rgba(163,113,247,0.25)', 'rgba(163,113,247,0)', 100);
  }

  function statusColor(val, high, med) {
    if (val >= high) return 'var(--critical)';
    if (val >= med) return 'var(--warning)';
    return 'var(--online)';
  }

  function showDashboard() {
    selectedDeviceId = null;
    currentAnalyticsType = null;
    if (elStatCards) elStatCards.style.display = '';
    if (elChartsArea) elChartsArea.style.display = '';
    if (elEventsSection) elEventsSection.style.display = '';
    const elAgentSection = $('#agentSection');
    if (elAgentSection) elAgentSection.style.display = '';
    if (elDeviceDetail) elDeviceDetail.style.display = 'none';
    if (elAnalyticsView) elAnalyticsView.style.display = 'none';
    if (elChatView) elChatView.hidden = true;
    const elSettingsView = $('#settingsView');
    if (elSettingsView) elSettingsView.style.display = 'none';
    const elHeaderRight = $('.header-right');
    if (elHeaderRight) elHeaderRight.style.display = '';
    if (elViewTitle) elViewTitle.textContent = 'Dashboard';
    if (elViewSubtitle) elViewSubtitle.textContent = 'Real-time edge device health overview';
    renderDeviceList(elDeviceFilter ? elDeviceFilter.value.toLowerCase() : '');
  }

  function showChatView() {
    selectedDeviceId = null;
    currentAnalyticsType = null;
    if (elStatCards) elStatCards.style.display = 'none';
    if (elChartsArea) elChartsArea.style.display = 'none';
    if (elEventsSection) elEventsSection.style.display = 'none';
    const elAgentSection = $('#agentSection');
    if (elAgentSection) elAgentSection.style.display = 'none';
    if (elDeviceDetail) elDeviceDetail.style.display = 'none';
    if (elAnalyticsView) elAnalyticsView.style.display = 'none';
    if (elChatView) elChatView.hidden = false;
    const elSettingsView = $('#settingsView');
    if (elSettingsView) elSettingsView.style.display = 'none';
    const elHeaderRight = $('.header-right');
    if (elHeaderRight) elHeaderRight.style.display = '';
    if (elViewTitle) elViewTitle.textContent = 'Talk with Doc';
    if (elViewSubtitle) elViewSubtitle.textContent = 'AI-powered assistant for your edge devices';
  }

  function showSettingsView() {
    selectedDeviceId = null;
    currentAnalyticsType = null;
    if (elStatCards) elStatCards.style.display = 'none';
    if (elChartsArea) elChartsArea.style.display = 'none';
    if (elEventsSection) elEventsSection.style.display = 'none';
    const elAgentSection = $('#agentSection');
    if (elAgentSection) elAgentSection.style.display = 'none';
    if (elDeviceDetail) elDeviceDetail.style.display = 'none';
    if (elAnalyticsView) elAnalyticsView.style.display = 'none';
    if (elChatView) elChatView.hidden = true;
    const elSettingsView = $('#settingsView');
    if (elSettingsView) elSettingsView.style.display = '';
    const elSettingsGrid = $('#settingsGrid');
    if (elSettingsGrid) elSettingsGrid.style.display = '';
    const elManageView = $('#manageModelsView');
    if (elManageView) elManageView.style.display = 'none';
    const elHeaderRight = $('.header-right');
    if (elHeaderRight) elHeaderRight.style.display = 'none';
    if (elViewTitle) elViewTitle.textContent = 'Settings';
    if (elViewSubtitle) elViewSubtitle.textContent = '';
  }

  // ── Analytics View (elaborated drilldown for a dashboard stat) ──
  const ANALYTICS_TITLES = {
    total: 'Total Devices', online: 'Online Devices', warning: 'Warnings', critical: 'Critical Issues',
    cpu: 'CPU Utilization by Device', memory: 'Memory Usage', network: 'Network I/O',
    devices: 'Devices', hardware: 'Hardware',
  };

  // One Analytics page: the Devices / Hardware row stays pinned at the top and the
  // selected drilldown (CPU by default) renders directly below it.
  function showAnalyticsView(type) {
    selectedDeviceId = null;
    currentAnalyticsType = type || null;
    if (elStatCards) elStatCards.style.display = 'none';
    if (elChartsArea) elChartsArea.style.display = 'none';
    if (elEventsSection) elEventsSection.style.display = 'none';
    const elAgentSection = $('#agentSection');
    if (elAgentSection) elAgentSection.style.display = 'none';
    if (elDeviceDetail) elDeviceDetail.style.display = 'none';
    if (elChatView) elChatView.hidden = true;
    const elSettingsView = $('#settingsView');
    if (elSettingsView) elSettingsView.style.display = 'none';
    if (elAnalyticsView) elAnalyticsView.style.display = 'block';
    const elHeaderRight = $('.header-right');
    if (elHeaderRight) elHeaderRight.style.display = '';

    // Devices / Hardware row and the CPU-by-device box are always on this page
    if (elAnalyticsHome) elAnalyticsHome.style.display = '';
    if (elAnaCpuFullCard) elAnaCpuFullCard.style.display = '';
    if (elViewTitle) elViewTitle.textContent = 'Analytics';
    if (elViewSubtitle) {
      elViewSubtitle.textContent = currentAnalyticsType
        ? (ANALYTICS_TITLES[currentAnalyticsType] || 'Analytics')
        : 'Fleet-wide CPU and device health';
    }

    // Highlight whichever entry box matches the active drilldown
    $$('#analyticsHome .stat-card').forEach(c => {
      c.classList.toggle('selected', c.dataset.atype === currentAnalyticsType);
    });

    renderAnalytics(currentAnalyticsType);
  }

  // CPU across every monitored edge device — one line per device, always shown
  function renderDeviceCpuChart() {
    const series = devices.map((dev, i) => ({
      data: deviceHistory.get(dev.id)?.cpu || [],
      color: DEVICE_COLORS[i % DEVICE_COLORS.length],
    }));
    drawMultiSparkline('anaCpuFullChart', series, 100);

    const avg = devices.length
      ? devices.reduce((a, dev) => a + dev.cpu, 0) / devices.length
      : 0;
    const anaCpuFullAvg = $('#anaCpuFullAvg');
    if (anaCpuFullAvg) {
      anaCpuFullAvg.textContent = `Avg: ${avg.toFixed(1)}% across ${devices.length} device(s)`;
    }

    const legend = $('#anaCpuLegend');
    if (legend) {
      legend.innerHTML = devices.map((dev, i) => `
        <span class="cpu-legend-item">
          <span class="cpu-legend-dot" style="background:${DEVICE_COLORS[i % DEVICE_COLORS.length]}"></span>
          ${escapeHtml(dev.name)}: ${dev.status === 'offline' ? 'Offline' : `${dev.cpu.toFixed(0)}%`}
        </span>
      `).join('') || '<span class="cpu-legend-item">No devices reporting yet.</span>';
    }
  }

  function renderAnalytics(type) {
    if (!elAnalyticsView || elAnalyticsView.style.display === 'none') return;
    const d = localDevice || metricsToDevice(currentMetrics || {});

    // Fleet CPU chart is always rendered, regardless of the selected drilldown
    renderDeviceCpuChart();

    // No specific box selected (or the CPU box itself) → nothing further to show
    if (!type || type === 'cpu') {
      if (elAnalyticsDetail) elAnalyticsDetail.style.display = 'none';
      return;
    }

    if (elAnalyticsDetail) elAnalyticsDetail.style.display = '';
    if (elAnalyticsStandard) elAnalyticsStandard.style.display = '';
    if (elAnalyticsTitle) elAnalyticsTitle.textContent = ANALYTICS_TITLES[type] || 'Analytics';

    const tile = (label, value, color) => `
      <div class="detail-metric">
        <div class="detail-metric-label">${label}</div>
        <div class="detail-metric-value"${color ? ` style="color:${color}"` : ''}>${value}</div>
      </div>`;

    let badgeText = '', badgeClass = 'online', gridHtml = '';

    if (['total', 'online', 'warning', 'critical', 'devices'].includes(type)) {
      let list = devices;
      if (type === 'online') list = devices.filter(d => d.status !== 'offline');
      if (type === 'warning' || type === 'critical') list = devices.filter(d => d.status === type);
      badgeClass = type === 'warning' ? 'warning' : type === 'critical' ? 'critical' : 'online';
      badgeText = `${list.length} device(s)`;
      gridHtml = list.length === 0
        ? tile('Status', 'No matching devices')
        : list.map(x => [
            tile('Device', escapeHtml(x.name)),
            tile('Status', x.status.charAt(0).toUpperCase() + x.status.slice(1), `var(--${x.status})`),
            tile('CPU', `${x.cpu.toFixed(1)}%`, statusColor(x.cpu, 85, 60)),
            tile('Memory', `${x.memory.toFixed(1)}%`, statusColor(x.memory, 85, 65)),
          ].join('')).join('');
    } else if (type === 'memory') {
      const mem = (currentMetrics && currentMetrics.memory) || {};
      badgeClass = (d.memory || 0) >= 85 ? 'critical' : (d.memory || 0) >= 65 ? 'warning' : 'online';
      badgeText = `${(d.memory || 0).toFixed(1)}%`;
      gridHtml = [
        tile('Used', `${mem.used_gb ?? 0} GB`),
        tile('Available', `${mem.available_gb ?? 0} GB`),
        tile('Total', `${mem.total_gb ?? 0} GB`),
        tile('Swap Used', `${mem.swap_used_gb ?? 0} GB`),
        tile('Swap Total', `${mem.swap_total_gb ?? 0} GB`),
        tile('Utilization', `${(d.memory || 0).toFixed(1)}%`, statusColor(d.memory || 0, 85, 65)),
      ].join('');
    } else if (type === 'network') {
      const net = (currentMetrics && currentMetrics.network) || {};
      badgeText = `${net.active_connections || 0} connections`;
      gridHtml = [
        tile('Connections', net.active_connections || 0),
        tile('Sent', `${net.mbps_sent ?? 0} MB`),
        tile('Received', `${net.mbps_recv ?? 0} MB`),
        tile('Errors In', net.errors_in ?? 0),
        tile('Errors Out', net.errors_out ?? 0),
        tile('Drops', (net.drops_in ?? 0) + (net.drops_out ?? 0)),
      ].join('');
    } else if (type === 'hardware') {
      const cpuInfo = (currentMetrics && currentMetrics.cpu) || {};
      const disk = (currentMetrics && currentMetrics.disk) || {};
      const net = (currentMetrics && currentMetrics.network) || {};
      badgeText = `${cpuInfo.core_count || 0} cores`;
      gridHtml = [
        tile('CPU Cores', cpuInfo.core_count || 0),
        tile('CPU Frequency', `${Math.round(cpuInfo.frequency_mhz || 0)} MHz`),
        tile('Disk Partitions', (disk.partitions || []).length),
        tile('Disk Used', `${(disk.percent || 0).toFixed(1)}%`, statusColor(disk.percent || 0, 90, 70)),
        tile('Network Interfaces', Object.keys(net.interfaces || {}).length),
        tile('Temperature', `${(d.temp || 0).toFixed(1)} °C`),
      ].join('');
    }

    if (elAnalyticsBadge) {
      elAnalyticsBadge.textContent = badgeText;
      elAnalyticsBadge.className = 'status-pill ' + badgeClass;
    }
    if (elAnalyticsGrid) elAnalyticsGrid.innerHTML = gridHtml;

    drawSparkline('anaCpuChart', cpuHistory, 'rgb(56,139,253)', 'rgba(56,139,253,0.25)', 'rgba(56,139,253,0)', 100);
    drawSparkline('anaMemChart', memHistory, 'rgb(163,113,247)', 'rgba(163,113,247,0.25)', 'rgba(163,113,247,0)', 100);
    const anaCpuVal = $('#anaCpuVal'); if (anaCpuVal) anaCpuVal.textContent = `${(d.cpu || 0).toFixed(1)}%`;
    const anaMemVal = $('#anaMemVal'); if (anaMemVal) anaMemVal.textContent = `${(d.memory || 0).toFixed(1)}%`;

    const filtered = (type === 'warning' || type === 'critical')
      ? events.filter(e => e.severity === type)
      : events;
    if (elAnalyticsEventsTitle) {
      elAnalyticsEventsTitle.textContent = (type === 'warning' || type === 'critical')
        ? `${type === 'warning' ? 'Warning' : 'Critical'} Event Log`
        : 'All Recent Events';
    }
    if (elAnalyticsEventsTbody) {
      elAnalyticsEventsTbody.innerHTML = filtered.length === 0
        ? `<tr><td colspan="3" style="color:var(--text-muted);padding:14px;">No matching events.</td></tr>`
        : filtered.slice(0, 20).map(e => `
          <tr class="fade-enter">
            <td><span class="severity-dot ${e.severity}"></span>${e.severity}</td>
            <td>${e.message}</td>
            <td>${timeAgo(e.time)}</td>
          </tr>
        `).join('');
    }
  }

  // ── Events ──
  function addEvent(severity, deviceName, message) {
    events.unshift({
      severity,
      device: deviceName,
      message,
      time: Date.now() / 1000,
    });
    if (events.length > 30) events = events.slice(0, 30);
  }

  function renderEvents() {
    const critCount = events.filter(e => e.severity === 'critical').length;
    if (elAlertBadge) {
      elAlertBadge.textContent = critCount || '';
      elAlertBadge.style.display = critCount ? '' : 'none';
    }

    if (!elEventsTbody) return;
    elEventsTbody.innerHTML = events.slice(0, 12).map(e => `
      <tr class="fade-enter">
        <td><span class="severity-dot ${e.severity}"></span>${e.severity}</td>
        <td>${e.device}</td>
        <td>${e.message}</td>
        <td>${timeAgo(e.time)}</td>
      </tr>
    `).join('');
  }

  // ── Agent Events ──
  function renderAgentEvents() {
    if (!elAgentTbody) return;

    elAgentTbody.innerHTML = agentEvents.slice(0, 3).map(e => {
      const severityClass = e.severity === 'critical' ? 'critical'
        : e.severity === 'warning' ? 'warning' : 'info';
      const icon = e.type === 'action_taken' ? '🔧'
        : e.type === 'anomaly_detected' ? '⚠️'
          : e.type === 'diagnosis' ? '🔍'
            : e.type === 'metrics_collected' ? '📊'
              : e.type === 'agent_start' ? '🚀'
                : '📋';

      return `
        <tr class="fade-enter">
          <td><span class="severity-dot ${severityClass}"></span>${icon} ${e.type.replace(/_/g, ' ')}</td>
          <td class="agent-event-msg">${e.message}</td>
          <td>${timeAgo(e.timestamp)}</td>
        </tr>
      `;
    }).join('');

    // Update agent status indicator
    if (elAgentStatus) {
      elAgentStatus.className = 'agent-status-dot active';
    }
  }

  // ── Sparkline Charts (Canvas) ──
  function drawSparkline(canvasId, data, color, gradient1, gradient2, maxVal) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width;
    const H = canvas.height;
    const padding = 4;

    ctx.clearRect(0, 0, W, H);

    if (!data.length) return;

    const max = maxVal || Math.max(...data, 1);
    const step = (W - padding * 2) / (data.length - 1 || 1);

    const points = data.map((v, i) => ({
      x: padding + i * step,
      y: H - padding - ((v / max) * (H - padding * 2)),
    }));

    // gradient fill
    const grad = ctx.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, gradient1);
    grad.addColorStop(1, gradient2);

    ctx.beginPath();
    ctx.moveTo(points[0].x, H);
    points.forEach((p, i) => {
      if (i === 0) { ctx.lineTo(p.x, p.y); return; }
      const prev = points[i - 1];
      const cpx = (prev.x + p.x) / 2;
      ctx.bezierCurveTo(cpx, prev.y, cpx, p.y, p.x, p.y);
    });
    ctx.lineTo(points[points.length - 1].x, H);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    // line
    ctx.beginPath();
    points.forEach((p, i) => {
      if (i === 0) { ctx.moveTo(p.x, p.y); return; }
      const prev = points[i - 1];
      const cpx = (prev.x + p.x) / 2;
      ctx.bezierCurveTo(cpx, prev.y, cpx, p.y, p.x, p.y);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.stroke();

    // glow dot for latest
    const last = points[points.length - 1];
    ctx.beginPath();
    ctx.arc(last.x, last.y, 4, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.beginPath();
    ctx.arc(last.x, last.y, 7, 0, Math.PI * 2);
    ctx.fillStyle = color.replace(')', ',0.25)').replace('rgb', 'rgba');
    ctx.fill();
  }

  // ── Multi-series sparkline (one line per CPU core, same box) ──
  function drawMultiSparkline(canvasId, seriesList, maxVal) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const W = canvas.width;
    const H = canvas.height;
    const padding = 6;

    ctx.clearRect(0, 0, W, H);
    if (!seriesList.length) return;

    const max = maxVal || 100;

    seriesList.forEach((series) => {
      const data = series.data;
      if (!data.length) return;
      const step = (W - padding * 2) / (data.length - 1 || 1);
      const points = data.map((v, i) => ({
        x: padding + i * step,
        y: H - padding - ((v / max) * (H - padding * 2)),
      }));

      ctx.beginPath();
      points.forEach((p, i) => {
        if (i === 0) { ctx.moveTo(p.x, p.y); return; }
        const prev = points[i - 1];
        const cpx = (prev.x + p.x) / 2;
        ctx.bezierCurveTo(cpx, prev.y, cpx, p.y, p.x, p.y);
      });
      ctx.strokeStyle = series.color;
      ctx.lineWidth = 1.75;
      ctx.globalAlpha = 0.85;
      ctx.stroke();
      ctx.globalAlpha = 1;

      const last = points[points.length - 1];
      ctx.beginPath();
      ctx.arc(last.x, last.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = series.color;
      ctx.fill();
    });
  }

  function updateCharts() {
    const d = localDevice;
    if (!d) return;
    const cpu = d.cpu;
    const mem = d.memory;
    const net = d.network;

    cpuHistory.push(cpu);
    memHistory.push(mem);
    netHistory.push(net);
    if (cpuHistory.length > HISTORY_LEN) cpuHistory.shift();
    if (memHistory.length > HISTORY_LEN) memHistory.shift();
    if (netHistory.length > HISTORY_LEN) netHistory.shift();

    if (elCpuAvg) elCpuAvg.textContent = `Current: ${cpu.toFixed(1)}%`;
    if (elMemAvg) elMemAvg.textContent = `Current: ${mem.toFixed(1)}%`;
    if (elNetAvg) elNetAvg.textContent = `Connections: ${net}`;

    if (selectedDeviceId === null) {
      drawSparkline('cpuChart', cpuHistory, 'rgb(56,139,253)', 'rgba(56,139,253,0.25)', 'rgba(56,139,253,0)', 100);
      drawSparkline('memChart', memHistory, 'rgb(163,113,247)', 'rgba(163,113,247,0.25)', 'rgba(163,113,247,0)', 100);
      drawSparkline('netChart', netHistory, 'rgb(63,185,80)', 'rgba(63,185,80,0.2)', 'rgba(63,185,80,0)', Math.max(...netHistory, 50));
    }

  }

  function recordDeviceSamples() {
    for (const device of devices) {
      const previous = deviceHistory.get(device.id);
      if (previous?.lastSeen >= device.lastSeen) continue;
      const history = previous || { cpu: [], memory: [] };
      history.lastSeen = device.lastSeen;
      history.cpu.push(device.cpu);
      history.memory.push(device.memory);
      if (history.cpu.length > HISTORY_LEN) history.cpu.shift();
      if (history.memory.length > HISTORY_LEN) history.memory.shift();
      deviceHistory.set(device.id, history);
    }
  }

  // ── SSE: Connect to real-time stream ──
  function connectSSE() {
    if (sseSource) {
      sseSource.close();
    }

    sseSource = new EventSource(SSE_URL);

    sseSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.error) {
          console.error('SSE error:', data.error);
          return;
        }
        handleStreamData(data);
      } catch (e) {
        console.error('SSE parse error:', e);
      }
    };

    sseSource.onerror = () => {
      console.warn('SSE connection lost, reconnecting in 5s...');
      sseSource.close();
      setTimeout(connectSSE, 5000);
    };

    console.log('SSE connected to', SSE_URL);
  }

  function handleStreamData(data) {
    const metrics = data.metrics || (data.cpu ? data : null);
    const hasLocalSample = Boolean(metrics?.cpu);
    if (hasLocalSample) {
      currentMetrics = metrics;
      localDevice = metricsToDevice(metrics);
    }
    if (Array.isArray(data.devices)) externalDevices = data.devices.map(reportingDeviceToDevice);
    if (connectingDeviceId && externalDevices.some(device => device.id === connectingDeviceId)) {
      connectingDeviceId = undefined;
      elPairingNotice.textContent = 'Device is reporting live metrics.';
    }
    devices = localDevice ? [localDevice, ...externalDevices] : externalDevices;
    recordDeviceSamples();

    // Generate events from agent status
    if (localDevice && data.recent_events && data.recent_events.length > 0) {
      agentEvents = data.recent_events;

      // Sync critical agent events to the main events table
      for (const evt of data.recent_events) {
        if (evt.severity === 'critical' || evt.severity === 'warning') {
          const exists = events.find(e =>
            Math.abs(e.time - evt.timestamp) < 2 && e.message === evt.message
          );
          if (!exists) {
            addEvent(evt.severity, localDevice.name, evt.message);
          }
        }
      }
    }

    // Generate status-based events
    if (hasLocalSample && localDevice.status === 'critical') {
      if (localDevice.cpu >= 95) addEvent('critical', localDevice.name, `CPU at ${localDevice.cpu.toFixed(1)}%`);
      if (localDevice.memory >= 95) addEvent('critical', localDevice.name, `Memory at ${localDevice.memory.toFixed(1)}%`);
    } else if (hasLocalSample && localDevice.status === 'warning') {
      // Occasional warning events
      if (Math.random() < 0.1) {
        if (localDevice.cpu >= 80) addEvent('warning', localDevice.name, `CPU elevated: ${localDevice.cpu.toFixed(1)}%`);
      }
    }

    // Update all UI
    updateStats();
    renderDeviceList(elDeviceFilter ? elDeviceFilter.value.toLowerCase() : '');
    renderEvents();
    renderAgentEvents();
    if (hasLocalSample) updateCharts();

    // Update detail if visible
    if (selectedDeviceId !== null) {
      const d = devices.find(x => x.id === selectedDeviceId);
      if (d) renderDeviceDetail(d);
      else showDashboard();
    }

    // Update analytics drilldown if visible
    if (currentAnalyticsType !== null) {
      renderAnalytics(currentAnalyticsType);
    }
  }

  // ── Fallback: Fetch API if SSE not available ──
  async function fetchMetrics() {
    const [local, registry] = await Promise.allSettled([
      fetch(`${API_BASE}/api/metrics`).then(response => response.ok ? response.json() : null),
      fetch(`${API_BASE}/api/devices`).then(response => response.ok ? response.json() : null),
    ]);
    handleStreamData({ metrics: local.value, devices: registry.value?.devices });
  }

  async function fetchAgentEvents() {
    try {
      const resp = await fetch(`${API_BASE}/api/agent/events?limit=10`);
      if (resp.ok) {
        agentEvents = await resp.json();
        renderAgentEvents();
      }
    } catch (e) {
      console.error('Agent events fetch error:', e);
    }
  }

  // ── Nav Links ──
  function setupNav() {
    $$('.nav-link').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        $$('.nav-link').forEach(l => l.classList.remove('active'));
        link.classList.add('active');
        const view = link.getAttribute('data-view');
        if (view === 'talkwithdoc') {
          showChatView();
        } else if (view === 'settings') {
          showSettingsView();
        } else if (view === 'analytics') {
          showAnalyticsView(null);
        } else {
          showDashboard();
        }
      });
    });
  }

  // ── Event listeners ──
  function setupListeners() {
    elNearbyList?.addEventListener('click', event => {
      const button = event.target.closest('button');
      if (button?.dataset.connectId) connectNearbyDevice(button);
      if (button?.dataset.action) selectNearbyAction(button);
    });
    $('#refreshNearby')?.addEventListener('click', () => fetchNearbyDevices(true));
    $('#copyPublisherLink')?.addEventListener('click', async () => {
      const link = $('#publisherLink');
      try {
        await navigator.clipboard.writeText(link.value);
        $('#publisherNotice').textContent = 'Link copied.';
      } catch {
        link.select();
        $('#publisherNotice').textContent = 'Copy the selected link.';
      }
    });
    elSshForm?.addEventListener('submit', connectSsh);
    $('#closeSshDialog')?.addEventListener('click', () => elSshDialog.close());
    elSshDialog?.addEventListener('cancel', event => {
      if ($('#submitSsh').disabled) event.preventDefault();
    });
    elSshDialog?.addEventListener('close', () => { $('#sshPassword').value = ''; });
    if (elDeviceFilter) {
      elDeviceFilter.addEventListener('input', () => {
        renderDeviceList(elDeviceFilter.value.toLowerCase());
      });
    }

    const backBtn = $('#detailBack');
    if (backBtn) backBtn.addEventListener('click', showDashboard);

    const analyticsBackBtn = $('#analyticsBack');
    if (analyticsBackBtn) analyticsBackBtn.addEventListener('click', showDashboard);

    // Every clickable dashboard box (stat cards, chart cards) and the Analytics
    // home boxes (Devices / Hardware) all route into the Analytics drilldown.
    const analyticsTriggers = [
      ...(elStatCards ? elStatCards.querySelectorAll('.stat-card[data-type]') : []),
      ...document.querySelectorAll('#chartsArea .chart-card[data-atype]'),
      ...document.querySelectorAll('#analyticsHome .stat-card[data-atype]'),
    ];
    analyticsTriggers.forEach(card => {
      const atype = card.dataset.type || card.dataset.atype;
      card.addEventListener('click', () => showAnalyticsView(atype));
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          showAnalyticsView(atype);
        }
      });
    });

    const clearBtn = $('#clearEventsBtn');
    if (clearBtn) clearBtn.addEventListener('click', () => {
      events = [];
      renderEvents();
    });

    const refreshBtn = $('#refreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        fetchMetrics();
        fetchAgentEvents();
        const svg = refreshBtn.querySelector('svg');
        if (svg) {
          svg.style.transition = 'transform 0.5s ease';
          svg.style.transform = 'rotate(360deg)';
          setTimeout(() => { svg.style.transition = 'none'; svg.style.transform = ''; }, 550);
        }
      });
    }

    if (elSearchInput) {
      elSearchInput.addEventListener('input', () => {
        const q = elSearchInput.value.toLowerCase();
        renderDeviceList(q);
      });
    }

    // Clear agent events button
    const clearAgentBtn = $('#clearAgentEventsBtn');
    if (clearAgentBtn) {
      clearAgentBtn.addEventListener('click', () => {
        agentEvents = [];
        renderAgentEvents();
      });
    }
  }

  // ── Chat System ──
  const DOC_ICON = '🩺';
  let chatHistory = [];
  let activeChatRequest = null;

  function setupChat() {
    const chatInput = $('#chatInput');
    const sendBtn = $('#chatSendBtn');
    if (!chatInput || !sendBtn) return;

    // Auto-resize textarea
    chatInput.addEventListener('input', () => {
      chatInput.style.height = 'auto';
      chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + 'px';
    });

    // Enter to send (Shift+Enter for newline)
    chatInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
      }
    });

    sendBtn.addEventListener('click', () => {
      if (activeChatRequest) {
        activeChatRequest.abort();
      } else {
        sendChatMessage();
      }
    });

    // Suggestion buttons
    $$('.chat-suggestion').forEach(btn => {
      btn.addEventListener('click', () => {
        const q = btn.getAttribute('data-q');
        if (q) {
          chatInput.value = q;
          sendChatMessage();
        }
      });
    });
  }

  async function sendChatMessage() {
    const chatInput = $('#chatInput');
    const sendBtn = $('#chatSendBtn');
    const messages = $('#chatMessages');
    const msg = chatInput.value.trim();
    if (!msg || activeChatRequest) return;

    const controller = new AbortController();
    activeChatRequest = controller;
    setChatButtonMode(sendBtn, true);
    chatInput.value = '';
    chatInput.style.height = 'auto';

    // Remove welcome screen
    const welcome = $('.chat-welcome');
    if (welcome) welcome.remove();

    appendChatMsg('user', msg);
    const bubble = appendChatMsg('assistant', null);
    const draft = document.createTextNode('');
    const history = chatHistory.slice(-10);
    let reply = '';
    let completed = false;
    let errorMessage = '';

    try {
      const response = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: msg, history, stream: true }),
        signal: controller.signal,
      });
      if (!response.ok || !response.body) throw new Error(`Chat request failed (${response.status})`);

      for await (const event of readChatEvents(response.body)) {
        if (controller.signal.aborted) break;
        if (event.error) throw new Error(event.error);
        if (event.done) {
          completed = true;
          break;
        }
        if (typeof event.delta !== 'string' || !event.delta) continue;

        const followTail = isChatAtBottom(messages);
        if (!reply) {
          bubble.classList.add('streaming');
          bubble.replaceChildren(draft);
        }
        reply += event.delta;
        draft.appendData(event.delta);
        if (followTail) messages.scrollTop = messages.scrollHeight;
      }

      if (!controller.signal.aborted && (!completed || !reply.trim())) {
        throw new Error('Chat stream ended before the reply was complete');
      }
    } catch (error) {
      if (!controller.signal.aborted) {
        console.error('Chat error:', error);
        errorMessage = '⚠️ Doc could not finish the reply. Try again.';
      }
    } finally {
      const stopped = controller.signal.aborted;
      const hasReply = Boolean(reply.trim());
      if (hasReply && !errorMessage) {
        chatHistory.push({ role: 'user', content: msg }, { role: 'assistant', content: reply });
      }
      const displayReply = stopped && !hasReply
        ? 'Generation stopped.'
        : [reply, errorMessage].filter(Boolean).join('\n\n');
      const followTail = isChatAtBottom(messages);
      bubble.classList.remove('streaming');
      bubble.innerHTML = formatChatContent(displayReply);
      bubble.removeAttribute('aria-busy');
      if (stopped && hasReply) bubble.nextElementSibling.textContent += ' · Stopped';
      if (followTail) messages.scrollTop = messages.scrollHeight;
      activeChatRequest = null;
      setChatButtonMode(sendBtn, false);
    }
  }

  function setChatButtonMode(button, generating) {
    const label = generating ? 'Stop' : 'Send';
    button.classList.toggle('is-generating', generating);
    button.title = label;
    button.setAttribute('aria-label', label);
  }

  async function* readChatEvents(body) {
    let pending = '';
    for await (const chunk of body.pipeThrough(new TextDecoderStream())) {
      const lines = (pending + chunk).split('\n');
      pending = lines.pop();
      for (const line of lines) {
        if (line) yield JSON.parse(line);
      }
    }
  }

  function isChatAtBottom(messages) {
    return messages.scrollHeight - messages.scrollTop - messages.clientHeight < 32;
  }

  function appendChatMsg(role, content) {
    const container = $('#chatMessages');
    if (!container) return;

    const now = new Date();
    const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const avatar = role === 'user' ? 'U' : DOC_ICON;
    const pending = content === null;

    const div = document.createElement('div');
    div.className = `chat-msg ${role}`;
    div.innerHTML = `
      <div class="chat-msg-avatar">${avatar}</div>
      <div class="chat-msg-body">
        <div class="chat-msg-bubble"></div>
        <span class="chat-msg-time">${timeStr}</span>
      </div>
    `;
    const bubble = div.querySelector('.chat-msg-bubble');
    if (pending) {
      bubble.setAttribute('aria-busy', 'true');
      bubble.innerHTML = `
        <div class="chat-typing" aria-label="Doc is responding">
          <span class="chat-typing-dot"></span>
          <span class="chat-typing-dot"></span>
          <span class="chat-typing-dot"></span>
        </div>
      `;
    } else {
      bubble.innerHTML = formatChatContent(content);
    }
    container.appendChild(div);
    container.scrollTop = container.scrollHeight;
    return bubble;
  }

  function formatChatContent(text) {
    // Basic formatting: code blocks, bold, newlines
    return escapeHtml(text)
      .replace(/```([\s\S]*?)```/g, '<pre>$1</pre>')
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/\n/g, '<br>');
  }

  // ── Manage Models (Settings sub-view) ──
  function setupManageModels() {
    const manageBtn = $('#manageModelsBtn');
    const backBtn = $('#mmBackBtn');
    const settingsGrid = $('#settingsGrid');
    const manageView = $('#manageModelsView');
    const connectBtn = $('#mmConnectBtn');

    if (!manageBtn || !manageView) {
      console.error('setupManageModels: Missing elements', { manageBtn, manageView });
      return;
    }

    console.log('setupManageModels: Attaching listener to', manageBtn);

    // Navigate to Manage Models sub-view
    manageBtn.addEventListener('click', () => {
      console.log('Manage Models button clicked');
      if (settingsGrid) settingsGrid.style.display = 'none';
      manageView.style.display = '';
      if (elViewTitle) elViewTitle.textContent = 'Manage Models';
      renderMMModelList();
    });

    // Back to settings grid
    if (backBtn) {
      backBtn.addEventListener('click', () => {
        manageView.style.display = 'none';
        if (settingsGrid) settingsGrid.style.display = '';
        if (elViewTitle) elViewTitle.textContent = 'Settings';
      });
    }

    // Connect button
    if (connectBtn) {
      connectBtn.addEventListener('click', async () => {
        const input = $('#mmApiKeyInput');
        const key = input ? input.value.trim() : '';
        if (!key) return;

        connectBtn.disabled = true;
        connectBtn.textContent = 'Connecting…';

        try {
          const resp = await fetch(`${API_BASE}/api/llm/apikey`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ api_key: key }),
          });
          if (resp.ok) {
            if (input) input.value = '';
            await loadLLMModels();
            renderMMModelList();
          }
        } catch (e) {
          console.error('API key submit error:', e);
        } finally {
          connectBtn.disabled = false;
          connectBtn.textContent = 'Connect';
        }
      });
    }
  }
  function renderMMModelList() {
    const listCloud = $('#mmModelsList');
    const listLocal = $('#mmLocalModelsList');

    if (!listCloud) return;

    // Use the models already loaded by loadLLMModels()
    // But fetch fresh to be sure
    fetch(`${API_BASE}/api/llm/models`)
      .then(r => r.json())
      .then(data => {
        const models = data.models || data || [];

        // Split into Cloud vs Local
        const cloudModels = models.filter(m => !m.provider.toLowerCase().includes('local'));
        const localModels = models.filter(m => m.provider.toLowerCase().includes('local'));

        const renderItem = (m) => {
          const isActive = m.active;
          const statusClass = m.status === 'connected' ? 'connected' : 'offline';
          const badgeClass = isActive ? 'active' : statusClass;
          const badgeText = isActive ? 'Active' : (m.status || 'offline');
          return `
            <div class="mm-model-item ${isActive ? 'active' : ''}">
              <span class="mm-model-dot ${statusClass}"></span>
              <div class="mm-model-info">
                <span class="mm-model-name">${m.name}</span>
                <span class="mm-model-provider">${m.provider}</span>
              </div>
              <span class="mm-model-badge ${badgeClass}">${badgeText}</span>
            </div>`;
        };

        listCloud.innerHTML = cloudModels.length === 0
          ? '<div style="padding:15px;color:var(--text-muted);font-size:0.9em;">No cloud models configured.</div>'
          : cloudModels.map(renderItem).join('');

        if (listLocal) {
          listLocal.innerHTML = localModels.length === 0
            ? '<div style="padding:15px;color:var(--text-muted);font-size:0.9em;">No local models found.</div>'
            : localModels.map(renderItem).join('');
        }
      })
      .catch((e) => {
        console.error('renderMMModelList error:', e);
        listCloud.innerHTML = '<div style="padding:15px;color:var(--text-muted);">Error loading models.</div>';
      });
  }

  // ── Agent LLM Dropdown ──
  function setupAgentDropdown() {
    const dropdown = $('#agentDropdown');
    const btn = $('#agentDropdownBtn');
    if (!dropdown || !btn) return;

    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      dropdown.classList.toggle('open');
    });

    // Close on outside click
    document.addEventListener('click', (e) => {
      if (!dropdown.contains(e.target)) {
        dropdown.classList.remove('open');
      }
    });

    // Close on Escape
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') dropdown.classList.remove('open');
    });
  }

  function loadLLMModels() {
    fetch(`${API_BASE}/api/llm/models`)
      .then(r => r.json())
      .then(data => {
        // Handle both {models: [...]} and raw array
        const list = data.models || data || [];
        renderModelList(list);
      })
      .catch(err => console.warn('LLM models fetch failed:', err));
  }

  function renderModelList(models) {
    const list = $('#modelList');
    if (!list) return;

    list.innerHTML = '';
    models.forEach(m => {
      const li = document.createElement('li');
      li.className = `model-item${m.active ? ' active' : ''}`;
      li.innerHTML = `
        <span class="model-status-dot ${m.status}"></span>
        <div class="model-info">
          <span class="model-name">${m.name}</span>
          <span class="model-provider">${m.provider}</span>
        </div>
        ${m.active ? '<span class="model-active-badge">Active</span>' : ''}
      `;

      if (m.status === 'connected' && !m.active) {
        li.addEventListener('click', () => setActiveModel(m.name));
      }

      list.appendChild(li);
    });

    // Update button label
    const active = models.find(m => m.active);
    if (active && elActiveModelName) {
      elActiveModelName.textContent = active.name;
    }
  }

  function setActiveModel(name) {
    fetch(`${API_BASE}/api/llm/models/active`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    })
      .then(r => r.json())
      .then(res => {
        if (res.success) {
          loadLLMModels();  // Refresh list
          $('#agentDropdown')?.classList.remove('open');
        }
      })
      .catch(err => console.error('Set active model failed:', err));
  }

  // ── Boot ──
  function init() {
    console.log('Init called');
    // Seed some initial history with zeros
    for (let i = 0; i < HISTORY_LEN; i++) {
      cpuHistory.push(0);
      memHistory.push(0);
      netHistory.push(0);
    }

    updateStats();
    renderDeviceList();
    renderEvents();
    renderAgentEvents();
    updateCharts();
    setupNav();
    setupListeners();
    setupAgentDropdown();
    loadLLMModels();
    setupChat();
    setupManageModels();

    // Connect to SSE stream for real-time data
    connectSSE();

    // Also do an initial fetch
    fetchMetrics();
    fetchAgentEvents();
    fetchNearbyDevices();
    setInterval(fetchNearbyDevices, 3000);
  }

  // start
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
