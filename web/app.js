const $ = (sel) => document.querySelector(sel);
const api = async (path, opts = {}) => {
  const res = await fetch(`/api${path}`, {
    headers: opts.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || res.statusText);
  }
  return res.json();
};

const PRAYERS = ["fajr", "dhuhr", "asr", "maghrib", "isha"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PREF_KEYS = ["ramadan_mode", "suhoor_enabled", "suhoor_minutes", "suhoor_mp3",
                   "jumuah_action", "jumuah_mp3", "fajr_fade_seconds", "hijri_offset"];

let state = { status: null, media: [], devices: [] };

const prettyName = (f) => f.replace(/^Adhan-|\.(mp3|m4a|wav)$/g, "");

// live numeric badge next to a range slider
function showValue(range) {
  const out = document.createElement("span");
  out.className = "slider-val";
  out.textContent = range.value;
  range.after(out);
  range.addEventListener("input", () => (out.textContent = range.value));
}
const syncBadge = (range) => {
  const b = range.nextElementSibling;
  if (b?.classList.contains("slider-val")) b.textContent = range.value;
};
const todayAt = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  const t = new Date(); t.setHours(h, m, 0, 0);
  return t;
};

// ---------- status header ----------

function renderStatus() {
  const s = state.status;
  if (!s) return;
  $("#hijri").textContent = s.hijri ? s.hijri.text + (s.ramadan_active ? " · Ramadan" : "") : "";

  const next = s.next;
  $("#next-prayer").textContent = next
    ? `${next.name} at ${new Date(next.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
    : s.times ? "Done for today" : "Set your location below";

  const playing = s.playing;
  const el = $("#now-playing");
  el.classList.toggle("hidden", !playing);
  if (playing) {
    $("#np-text").textContent = `${playing.paused ? "Paused" : "Playing"} ${playing.label}: ${prettyName(playing.mp3)}`;
    $("#np-pause").textContent = playing.paused ? "▶" : "⏸";
    const vol = $("#np-volume");
    vol.classList.toggle("hidden", !playing.live_volume);
    if (vol.nextElementSibling?.classList.contains("slider-val"))
      vol.nextElementSibling.classList.toggle("hidden", !playing.live_volume);
    if (document.activeElement !== vol) { vol.value = playing.volume; syncBadge(vol); }
  }

  // mute / skip chips
  const muted = !!s.mute_until;
  $("#unmute").classList.toggle("hidden", !muted);
  $("#mute-today").classList.toggle("hidden", muted);
  $("#mute-until").classList.toggle("hidden", muted);
  $("#mute-banner").classList.toggle("hidden", !muted);
  if (muted) {
    const until = new Date(s.mute_until);
    $("#mute-banner").textContent = `Muted until ${until.toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
  }
  $("#skip-next").textContent = s.skip_next ? `Cancel skip (${s.skip_next.name})` : "Skip next";
  $("#skip-next").classList.toggle("active", !!s.skip_next);

  const grid = $("#times");
  grid.innerHTML = "";
  if (!s.times) { $("#extras").textContent = ""; return; }
  const now = new Date();
  for (const name of PRAYERS) {
    const hhmm = s.times[name];
    const cell = document.createElement("div");
    cell.className = "cell" + (todayAt(hhmm) < now ? " past" : "")
      + (next && next.name === name ? " next-up" : "");
    cell.innerHTML = `<div class="name">${name}</div><div class="time">${hhmm}</div>`;
    grid.appendChild(cell);
  }
  const ex = s.extras || {};
  $("#extras").textContent = `sunrise ${ex.sunrise ?? "–"}` +
    (s.ramadan_active ? ` · imsak ${ex.imsak ?? "–"}` : "");
}

function tickCountdown() {
  const s = state.status;
  const next = s?.next;
  if (next) {
    const diff = new Date(next.at) - new Date();
    if (diff <= 0) { refreshStatus(); return; }
    const h = Math.floor(diff / 3.6e6), m = Math.floor(diff / 6e4) % 60, sec = Math.floor(diff / 1e3) % 60;
    $("#countdown").textContent = `in ${h}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
  } else {
    $("#countdown").textContent = "";
  }
  const iftar = $("#iftar");
  if (s?.ramadan_active && s.iftar_at) {
    const diff = todayAt(s.iftar_at) - new Date();
    iftar.classList.remove("hidden");
    if (diff > 0) {
      const h = Math.floor(diff / 3.6e6), m = Math.floor(diff / 6e4) % 60;
      iftar.textContent = `🌙 Iftar in ${h}h ${String(m).padStart(2, "0")}m`;
    } else {
      iftar.textContent = "🌙 It's iftar time — taqabbal Allahu";
    }
  } else {
    iftar.classList.add("hidden");
  }
}
setInterval(tickCountdown, 1000);

async function refreshStatus() {
  state.status = await api("/status");
  renderStatus();
}

// ---------- mute / skip ----------

$("#skip-next").onclick = () => api("/skip-next", { method: "POST" }).then(refreshStatus).catch((e) => alert(e.message));
$("#unmute").onclick = () => api("/mute", { method: "PUT", body: JSON.stringify({ until: null }) }).then(refreshStatus);
$("#mute-today").onclick = () => {
  const t = new Date(); t.setHours(24, 0, 0, 0); // upcoming midnight
  api("/mute", { method: "PUT", body: JSON.stringify({ until: localIso(t) }) }).then(refreshStatus);
};
$("#mute-until").onclick = () => $("#mute-until-row").classList.toggle("hidden");
$("#mute-date-go").onclick = () => {
  const d = $("#mute-date").value;
  if (!d) return;
  api("/mute", { method: "PUT", body: JSON.stringify({ until: `${d}T00:00:00` }) })
    .then(() => { $("#mute-until-row").classList.add("hidden"); refreshStatus(); });
};
const localIso = (d) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}` +
  `T${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}:00`;

// ---------- test panel & upload ----------

function fillSelect(sel, options, selected) {
  sel.innerHTML = "";
  const labels = options.map((opt) => (typeof opt === "string" ? prettyName(opt) : opt.label));
  options.forEach((opt, i) => {
    const o = document.createElement("option");
    if (typeof opt === "string") {
      o.value = opt;
      // same recording in two formats: show the extension so they're tellable apart
      const dup = labels.filter((l) => l === labels[i]).length > 1;
      o.textContent = dup ? `${labels[i]} (${opt.split(".").pop()})` : labels[i];
    } else { o.value = opt.id ?? ""; o.textContent = opt.label; }
    if (o.value === (selected ?? "")) o.selected = true;
    sel.appendChild(o);
  });
}

function fillDeviceSelect(sel, selected) {
  const outputs = [...state.devices];
  if (selected && !outputs.some((output) => output.id === selected)) {
    outputs.push({ id: selected, label: `${selected} (unavailable)` });
  }
  fillSelect(sel, outputs, selected);
}

async function refreshDevices() {
  state.devices = await api("/devices");
  document.querySelectorAll('[data-audio-output]').forEach((select) => fillDeviceSelect(select, select.value));
}

$("#audio-refresh").onclick = () => refreshDevices().catch((e) => alert(e.message));

$("#test-play").onclick = async () => {
  try {
    await api("/test", {
      method: "POST",
      body: JSON.stringify({
        mp3: $("#test-mp3").value,
        volume: Number($("#test-volume").value),
        device: $("#test-device").value || null,
      }),
    });
  } catch (e) { alert(e.message); }
};
$("#test-stop").onclick = () => api("/stop", { method: "POST" });
$("#np-stop").onclick = () => api("/stop", { method: "POST" });
$("#np-pause").onclick = () => {
  const paused = state.status?.playing?.paused;
  api("/playback", { method: "POST", body: JSON.stringify({ action: paused ? "resume" : "pause" }) })
    .catch((e) => alert(e.message));
};
$("#np-volume").onchange = (e) =>
  api("/playback", { method: "POST", body: JSON.stringify({ volume: Number(e.target.value) }) })
    .catch((err) => alert(err.message));

const simulate = (body) =>
  api("/simulate", { method: "POST", body: JSON.stringify(body) }).catch((e) => alert(e.message));
$("#test-suhoor").onclick = () => {
  const v = $("#test-suhoor-vol").value;
  simulate({ kind: "suhoor", volume: v === "" ? null : Number(v) });
};

$("#upload").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  $("#upload-status").textContent = "Uploading…";
  try {
    const res = await api("/media", { method: "POST", body: fd });
    state.media = res.media;
    $("#upload-status").textContent = `Added ${prettyName(res.name)} ✓`;
    fillSelect($("#test-mp3"), state.media, res.name);
    renderPrayers();
    renderPrefSelects();
  } catch (err) {
    $("#upload-status").textContent = err.message;
  }
  e.target.value = "";
};

// ---------- prayers ----------

async function renderPrayers() {
  const prayers = await api("/prayers");
  const box = $("#prayers");
  box.innerHTML = "";
  for (const p of prayers) {
    const row = document.createElement("div");
    row.className = "prayer-row";
    row.innerHTML = `
      <input type="checkbox" ${p.enabled ? "checked" : ""} title="Enabled">
      <span class="pname">${p.name}</span>
      <select class="mp3"></select>
      <span class="volwrap"><input type="range" min="0" max="100" value="${p.volume}" title="Volume"></span>
      <button class="sm gear" title="More options">⚙</button>
    `;
    fillSelect(row.querySelector(".mp3"), state.media, p.mp3);
    showValue(row.querySelector("input[type=range]"));
    const save = (fields) =>
      api(`/prayers/${p.name}`, { method: "PUT", body: JSON.stringify(fields) }).then(refreshStatus);
    row.querySelector("input[type=checkbox]").onchange = (e) => save({ enabled: e.target.checked });
    row.querySelector(".mp3").onchange = (e) => save({ mp3: e.target.value });
    row.querySelector("input[type=range]").onchange = (e) => save({ volume: Number(e.target.value) });
    box.appendChild(row);

    const detail = document.createElement("div");
    detail.className = "prayer-detail hidden";
    detail.innerHTML = `
      <div class="row">
        <label class="grow">Adjust <input type="number" class="offset" min="-60" max="60" value="${p.offset_minutes ?? 0}"> min</label>
        <label class="grow">Remind <input type="number" class="reminder" min="0" max="120" value="${p.reminder_minutes ?? 0}"> min before</label>
      </div>
      <div class="row">
        <label class="grow">After adhan <select class="dua"></select></label>
      </div>
      <div class="row">
        <label class="grow">Output <select class="device" data-audio-output></select></label>
      </div>
      <div class="row">
        <label>Test vol <input type="number" class="testvol" min="0" max="100" value="${p.volume}"></label>
        <button class="sm preview">▶ Preview 10s</button>
        <button class="sm test-reminder">Test reminder</button>
        <button class="sm test-full">Test full sequence</button>
      </div>
    `;
    fillSelect(detail.querySelector(".dua"), ["", ...state.media], p.dua_mp3 ?? "");
    fillDeviceSelect(detail.querySelector(".device"), p.device);
    detail.querySelector(".device").onchange = (e) => save({ device: e.target.value || null });
    detail.querySelector(".dua").options[0].textContent = "Nothing";
    detail.querySelector(".offset").onchange = (e) => save({ offset_minutes: Number(e.target.value) });
    detail.querySelector(".reminder").onchange = (e) => save({ reminder_minutes: Number(e.target.value) });
    detail.querySelector(".dua").onchange = (e) => save({ dua_mp3: e.target.value });
    const testVol = () => Number(detail.querySelector(".testvol").value);
    detail.querySelector(".preview").onclick = () =>
      api("/test", {
        method: "POST",
        body: JSON.stringify({ mp3: row.querySelector(".mp3").value, volume: testVol(),
                               device: detail.querySelector(".device").value || null, duration: 10 }),
      }).catch((e) => alert(e.message));
    detail.querySelector(".test-reminder").onclick = () =>
      simulate({ kind: "reminder", name: p.name, volume: testVol() });
    detail.querySelector(".test-full").onclick = () => {
      if (confirm(`Play the full ${p.name} sequence now (hooks + adhan + dua) at volume ${testVol()}?`))
        simulate({ kind: "prayer", name: p.name, volume: testVol() });
    };
    row.querySelector(".gear").onclick = () => detail.classList.toggle("hidden");
    box.appendChild(detail);
  }
}

// ---------- preferences (Ramadan & Friday) ----------

function renderPrefSelects() {
  const suhoorSel = $("#pref-suhoor_mp3"), jumSel = $("#pref-jumuah_mp3");
  const suhoorVal = suhoorSel.value, jumVal = jumSel.value;
  fillSelect(suhoorSel, state.media, suhoorVal);
  fillSelect(jumSel, state.media, jumVal);
}

async function renderPreferences() {
  const prefs = await api("/preferences");
  renderPrefSelects();
  for (const key of PREF_KEYS) {
    const el = $(`#pref-${key}`);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!prefs[key];
    else if (prefs[key] != null) el.value = prefs[key];
  }
  $("#pref-jumuah_mp3").classList.toggle("hidden", prefs.jumuah_action !== "mp3");
  for (const key of PREF_KEYS) {
    const el = $(`#pref-${key}`);
    if (!el) continue;
    el.onchange = async () => {
      const value = el.type === "checkbox" ? el.checked
        : el.type === "number" ? Number(el.value) : el.value;
      try {
        await api("/preferences", { method: "PUT", body: JSON.stringify({ [key]: value }) });
        if (key === "jumuah_action") $("#pref-jumuah_mp3").classList.toggle("hidden", el.value !== "mp3");
        refreshStatus();
      } catch (e) { alert(e.message); }
    };
  }
}

// ---------- hooks ----------

function checkGroup(container, items, labels) {
  container.innerHTML = "";
  items.forEach((val, i) => {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" value="${val}">${labels[i]}`;
    container.appendChild(label);
  });
}

async function renderHooks() {
  const hooksList = await api("/hooks");
  const box = $("#hooks");
  box.innerHTML = hooksList.length ? "" : `<div class="meta">No hooks yet.</div>`;
  for (const h of hooksList) {
    const card = document.createElement("div");
    card.className = "hook-card";
    const days = h.days.length === 7 ? "every day" : h.days.map((d) => DAYS[d]).join(", ");
    const off = h.offset_minutes || 0;
    const timing = off === 0 ? h.position
      : `${Math.abs(off)} min ${off < 0 ? "before" : "after"}`;
    const vol = h.volume != null ? ` · vol ${h.volume}` : "";
    card.innerHTML = `
      <div>
        <div>${h.enabled ? "" : "⏸ "}${h.name}</div>
        <div class="meta">${timing} · ${h.prayers.join(", ")} · ${days} · ${h.script}${vol}</div>
      </div>
      <div class="row" style="margin:0">
        <button class="sm run">Run now</button>
        <button class="sm toggle">${h.enabled ? "Disable" : "Enable"}</button>
        <button class="sm danger del">Delete</button>
      </div>`;
    card.querySelector(".run").onclick = () => simulate({ kind: "hook", id: h.id });
    card.querySelector(".toggle").onclick = () =>
      api(`/hooks/${h.id}`, { method: "PUT", body: JSON.stringify({ enabled: !h.enabled }) }).then(renderHooks);
    card.querySelector(".del").onclick = () => {
      if (confirm(`Delete hook "${h.name}"?`))
        api(`/hooks/${h.id}`, { method: "DELETE" }).then(renderHooks);
    };
    box.appendChild(card);
  }
}

async function setupHookForm() {
  const scripts = await api("/hook-scripts");
  const posSel = $("#hook-position");
  const fillScripts = () => fillSelect($("#hook-script"), scripts[posSel.value].map((s) => ({ id: s, label: s })));
  posSel.onchange = fillScripts;
  fillScripts();
  checkGroup($("#hook-prayers"), PRAYERS, PRAYERS);
  checkGroup($("#hook-days"), [0, 1, 2, 3, 4, 5, 6], DAYS);

  $("#hook-form").onsubmit = async (e) => {
    e.preventDefault();
    const checked = (sel) => [...document.querySelectorAll(`${sel} input:checked`)].map((i) => i.value);
    try {
      await api("/hooks", {
        method: "POST",
        body: JSON.stringify({
          name: $("#hook-name").value,
          position: posSel.value,
          script: $("#hook-script").value,
          prayers: checked("#hook-prayers"),
          days: checked("#hook-days").map(Number),
          offset_minutes: Number($("#hook-offset").value) || 0,
          volume: $("#hook-volume").value === "" ? null : Number($("#hook-volume").value),
        }),
      });
      e.target.reset();
      $("#hook-form-details").open = false;
      renderHooks();
    } catch (err) { alert(err.message); }
  };
}

// ---------- Bluetooth ----------

async function renderBluetooth() {
  const bt = await api("/bluetooth");
  $("#bt-unavailable").textContent = bt.error || "Bluetooth pairing is managed on the Pi (bluetoothctl not found here).";
  $("#bt-unavailable").classList.toggle("hidden", bt.available && !bt.error);
  $("#bt-scan").classList.toggle("hidden", !bt.available);
  const list = $("#bt-list");
  list.innerHTML = "";
  for (const d of bt.devices) {
    const card = document.createElement("div");
    card.className = "device-card";
    card.innerHTML = `
      <span><span class="dot ${d.connected ? "on" : ""}"></span>${d.name}</span>
      <span class="row" style="margin:0">
        <button class="sm conn">${d.connected ? "Disconnect" : "Connect"}</button>
        <button class="sm danger forget">Forget</button>
      </span>`;
    const act = (action) =>
      api(`/bluetooth/${action}`, { method: "POST", body: JSON.stringify({ mac: d.mac }) })
        .then(async () => {
          await renderBluetooth();
          await refreshDevices();
          if (action === "connect") setTimeout(() => refreshDevices().catch(() => {}), 2000);
        }).catch((e) => alert(e.message));
    card.querySelector(".conn").onclick = () => act(d.connected ? "disconnect" : "connect");
    card.querySelector(".forget").onclick = () => { if (confirm(`Forget ${d.name}?`)) act("forget"); };
    list.appendChild(card);
  }
}

$("#bt-scan").onclick = async () => {
  $("#bt-status").textContent = "Scanning ~8s…";
  try {
    const found = await api("/bluetooth/scan", { method: "POST" });
    $("#bt-status").textContent = found.length ? "" : "Nothing new found";
    const box = $("#bt-found");
    box.innerHTML = "";
    for (const d of found) {
      const card = document.createElement("div");
      card.className = "device-card";
      card.innerHTML = `<span>${d.name}</span><button class="sm primary">Pair</button>`;
      card.querySelector("button").onclick = () =>
        api("/bluetooth/pair", { method: "POST", body: JSON.stringify({ mac: d.mac }) })
          .then(async () => {
            card.remove();
            await renderBluetooth();
            await refreshDevices();
            setTimeout(() => refreshDevices().catch(() => {}), 2000);
          })
          .catch((e) => alert(e.message));
      box.appendChild(card);
    }
  } catch (e) {
    $("#bt-status").textContent = e.message;
  }
};

// ---------- settings ----------

async function renderSettings() {
  const s = await api("/settings");
  $("#set-lat").value = s.lat ?? "";
  $("#set-lng").value = s.lng ?? "";
  fillSelect($("#set-method"), s.methods.map((m) => ({ id: m, label: m })), s.method);
  fillSelect($("#set-asr"), s.asr_methods.map((m) => ({ id: m, label: m })), s.asr_method);
  fillSelect($("#set-highlats"), s.high_lat_rules.map((m) => ({ id: m, label: m })), s.high_lats);
}

$("#use-location").onclick = () => {
  if (!navigator.geolocation) return alert("Geolocation not available in this browser");
  navigator.geolocation.getCurrentPosition(
    (pos) => {
      $("#set-lat").value = pos.coords.latitude.toFixed(5);
      $("#set-lng").value = pos.coords.longitude.toFixed(5);
    },
    (err) => alert(err.message),
  );
};

$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/settings", {
      method: "PUT",
      body: JSON.stringify({
        lat: Number($("#set-lat").value),
        lng: Number($("#set-lng").value),
        method: $("#set-method").value,
        asr_method: $("#set-asr").value,
        high_lats: $("#set-highlats").value,
      }),
    });
    refreshStatus();
  } catch (err) { alert(err.message); }
};

$("#preset-icna").onclick = async () => {
  try {
    await api("/settings/preset/icna", { method: "POST" });
    await renderSettings();
    refreshStatus();
  } catch (err) { alert(err.message); }
};

// ---------- system health & update ----------

function healthCell(label, value) {
  return `<div class="cell"><div class="label">${label}</div><div class="value">${value}</div></div>`;
}

async function renderHealth() {
  try {
    const h = await api("/health");
    const up = h.uptime_seconds ?? h.daemon_uptime_seconds;
    const days = Math.floor(up / 86400), hrs = Math.floor(up / 3600) % 24;
    $("#health").innerHTML =
      healthCell("CPU temp", h.cpu_temp_c != null ? `${h.cpu_temp_c}°C` : "–") +
      healthCell("Uptime", days ? `${days}d ${hrs}h` : `${hrs}h ${Math.floor(up / 60) % 60}m`) +
      healthCell("Disk free", `${(h.disk_free_mb / 1024).toFixed(1)} GB`) +
      healthCell("Time sync", h.time_synced == null ? "–" : h.time_synced ? "✓" : "✗") +
      healthCell("Version", h.version ?? "–");
  } catch { /* non-fatal */ }
}
setInterval(renderHealth, 60000);

$("#do-update").onclick = async () => {
  $("#update-status").textContent = "Checking…";
  try {
    const r = await api("/update", { method: "POST" });
    $("#update-status").textContent = r.restarting
      ? "Updated — restarting, back in a few seconds…"
      : (r.output.split("\n").pop() || "Up to date");
    if (r.restarting) setTimeout(() => location.reload(), 6000);
  } catch (e) {
    $("#update-status").textContent = e.message;
  }
};

$("#restore-file").onchange = async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!confirm("Replace all current settings with this backup?")) return;
  const fd = new FormData();
  fd.append("file", file);
  $("#restore-status").textContent = "Restoring…";
  try {
    await api("/restore", { method: "POST", body: fd });
    $("#restore-status").textContent = "Restored ✓ reloading…";
    setTimeout(() => location.reload(), 1000);
  } catch (err) {
    $("#restore-status").textContent = err.message;
  }
};

// ---------- events, banner, live updates ----------

function eventLi(ev) {
  const li = document.createElement("li");
  li.className = `type-${ev.type}`;
  const ts = new Date(ev.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  li.innerHTML = `<span class="ts">${ts}</span>${ev.detail}`;
  return li;
}

function updateErrorBanner(events) {
  const dayAgo = Date.now() - 24 * 3.6e6;
  const err = events.find((ev) => ev.type === "error" && new Date(ev.ts) > dayAgo);
  const banner = $("#error-banner");
  banner.classList.toggle("hidden", !err);
  if (err) banner.textContent = `⚠ ${err.detail} — tap to dismiss`;
  banner.onclick = () => banner.classList.add("hidden");
}

async function renderEvents() {
  const events = await api("/events?limit=30");
  const ul = $("#events");
  ul.innerHTML = "";
  events.forEach((ev) => ul.appendChild(eventLi(ev)));
  updateErrorBanner(events);
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onmessage = (msg) => {
    const data = JSON.parse(msg.data);
    if (data.kind === "playing") { state.status && (state.status.playing = data.playing); renderStatus(); }
    if (data.kind === "event") {
      $("#events").prepend(eventLi(data.event));
      if (data.event.type === "error") updateErrorBanner([data.event]);
      if (["schedule", "adhan", "mute"].includes(data.event.type)) refreshStatus();
    }
  };
  ws.onclose = () => setTimeout(connectWs, 3000);
}

// ---------- boot ----------

(async function init() {
  showValue($("#test-volume"));
  showValue($("#np-volume"));
  [state.media, state.devices] = await Promise.all([api("/media"), api("/devices")]);
  fillSelect($("#test-mp3"), state.media);
  fillDeviceSelect($("#test-device"));
  await Promise.all([
    refreshStatus(), renderPrayers(), renderPreferences(), renderHooks(),
    setupHookForm(), renderBluetooth(), renderSettings(), renderHealth(), renderEvents(),
  ]);
  connectWs();
})();
