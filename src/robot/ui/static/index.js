/* The dashboard: what is configured, what it costs, and what it has done.
 *
 * Everything here arrives on the `hello` and `health` messages the orchestrator already
 * broadcasts every 3 s -- no extra polling, and no number on this page is computed in
 * the browser from something it guessed.
 */
(() => {
  const badge = document.getElementById('state-badge');
  const dot = badge.querySelector('.dot');
  const stateText = document.getElementById('state-text');
  const servicesEl = document.getElementById('services');
  const recentEl = document.getElementById('recent-body');
  const cfgEl = document.getElementById('cfg');
  const { el, HEALTH } = Robot;

  const STATE_COLOUR = {
    idle: 'var(--text-faint)', thinking: 'var(--warning)',
    speaking: 'var(--success)', unknown: 'var(--text-faint)',
  };

  /** 1,284 / 12.9K / 4.2M -- a stat tile reads at a glance or it is just a table. */
  function compact(n) {
    if (n === null || n === undefined) return '—';
    if (n < 1000) return String(n);
    if (n < 1e6) return `${(n / 1000).toFixed(n < 10000 ? 1 : 0)}K`;
    return `${(n / 1e6).toFixed(1)}M`;
  }
  const num = (n) => (n === null || n === undefined ? '—' : Math.round(n).toLocaleString());
  const set = (id, text) => { const n = document.getElementById(id); if (n) n.textContent = text; };

  function paintState() {
    const value = Robot.isConnected() ? Robot.state.robotState : 'offline';
    stateText.textContent = value;
    const colour = STATE_COLOUR[value] || 'var(--danger)';
    dot.style.background = colour;
  }

  function paintServices() {
    servicesEl.textContent = '';
    if (!Robot.state.services.length) {
      servicesEl.className = 'muted';
      servicesEl.textContent = Robot.isConnected()
        ? 'Waiting for the orchestrator…'
        : 'Dashboard is not connected to the orchestrator.';
      return;
    }
    servicesEl.className = '';
    for (const svc of Robot.state.services) {
      const look = HEALTH[svc.state] || HEALTH.unknown;
      servicesEl.append(el('div', { class: 'svc-row' },
        el('span', { class: 'sdot', style: `background:${look.colour}` }),
        el('span', { class: 'sname', text: svc.label }),
        svc.rss_mib ? el('span', { class: 'srss', text: `${num(svc.rss_mib)} MiB` }) : null,
        svc.swap_mib ? el('span', { class: 'sswap', text: `${num(svc.swap_mib)} swap` }) : null,
        el('span', { class: `badge ${look.badge}`, text: look.label || svc.state }),
      ));
    }
    paintKeepalive();
  }

  /* What the supervisor has had to do. Silent when it has had to do nothing, which is
     most of the time -- but a model that was swapped out from under you is the first
     thing you want to know when answers get worse. */
  function paintKeepalive() {
    const note = document.getElementById('ka-note');
    if (!note) return;
    const ka = (Robot.state.overview || {}).keepalive || {};
    const lines = [];
    for (const [name, info] of Object.entries(ka)) {
      if (info.gave_up) {
        lines.push({ text: `${name}: not coming back — keepalive gave up, see Logs`,
                     bad: true });
      } else if (info.downgrades) {
        lines.push({ text: `${name}: fell back to a smaller model ${info.downgrades}×`
                           + ' — pick one on the Memory page when there is room',
                     bad: false });
      } else if (info.restarts) {
        lines.push({ text: `${name}: restarted ${info.restarts}×`, bad: false });
      }
    }
    note.textContent = '';
    for (const line of lines) {
      note.append(el('div', { class: `ka-note${line.bad ? ' bad' : ''}`, text: line.text }));
    }
  }

  /* The meter and the tiles.
   *
   * `used_mib` is /proc/meminfo, not a projection -- the whole board including the OS
   * and whatever else is signed in. The Memory page explains the difference; here it is
   * labelled "measured now" and left at that.
   */
  function paintMemory() {
    const b = Robot.state.budget;
    const sys = b && b.system;
    if (!sys || !sys.total_mib) return;
    const pct = Math.min(100, (sys.used_mib / sys.total_mib) * 100);
    set('kpi-mem', num(sys.used_mib));
    set('kpi-mem-sub', `of ${num(sys.total_mib)} MiB · ${pct.toFixed(0)}%`);
    set('meter-num', `${num(sys.used_mib)} / ${num(sys.total_mib)} MiB`);

    // Severity on the fill, and the track is a lighter step of the SAME ramp so the
    // state reads across the whole bar rather than only the filled part.
    const swapping = (sys.swap_used_mib || 0) > 50;
    const level = (pct >= 96 || swapping) ? 'danger' : pct >= 88 ? 'warning' : 'accent';
    const fill = document.getElementById('meter-fill');
    const track = document.getElementById('meter-track');
    fill.style.width = `${pct}%`;
    fill.style.background = `var(--${level})`;
    // The track by its own id, not via `fill.parentElement` -- the painting code should
    // not break the day someone wraps the bar in another div.
    if (track) track.style.background = `var(--${level}-soft)`;

    const rows = document.getElementById('mem-rows');
    rows.textContent = '';
    const item = (label, value, cls = '') => rows.append(
      el('div', { class: 'r' }, el('span', { text: label }),
         el('b', { class: cls, text: value })));
    item('free', `${num(sys.free_mib)} MiB`);
    item('cached (reclaimable)', `${num(sys.cached_mib)} MiB`);
    // On Tegra, "available" is optimistic for anything that needs the GPU: CUDA cannot
    // reclaim page cache. Worth labelling rather than presenting as headroom.
    item('available to the CPU', `${num(sys.available_mib)} MiB`);
    item('ceiling', `${num(b.ceiling_mib ?? sys.total_mib)} MiB`);
    if (b.measured_mib) item('models, measured', `${num(b.measured_mib)} MiB`);
    if (b.headroom_mib !== undefined) item('projected headroom', `${num(b.headroom_mib)} MiB`);
    // Swap is the single best predictor of a slow reply on this board, so it is never
    // folded into another figure.
    item('in swap', sys.swap_used_mib ? `${num(sys.swap_used_mib)} MiB` : 'none',
         sys.swap_used_mib ? 'bad' : '');
    set('meter-caption', swapping
      ? 'Measured now — paging to disk, replies will be slow'
      : 'Measured now, from /proc/meminfo');
  }

  function paintOverview() {
    const o = Robot.state.overview;
    if (!o) return;
    const u = o.usage || {};
    const k = o.knowledge || {};
    set('kpi-know', num(k.chunks));
    set('kpi-know-sub', k.sources
      ? `chunks from ${k.sources} source${k.sources === 1 ? '' : 's'}`
      : 'nothing ingested yet');
    set('kpi-in', compact(u.prompt_tokens));
    set('kpi-in-sub', u.cached_tokens
      ? `prompt · ${compact(u.cached_tokens)} re-used`
      : 'prompt');
    set('kpi-out', compact(u.completion_tokens));
    set('kpi-out-sub', `generated over ${num(u.turns || 0)} turn${u.turns === 1 ? '' : 's'}`);
  }

  /* What is actually in force. Models come from `selection` (a saved choice, else the
   * service's own env file); the rest from `values`, which is the same registry the
   * Config page writes -- so this cannot drift from the thing it describes. */
  function paintConfig() {
    const v = Robot.state.values || {};
    const sel = Robot.state.selection || {};
    const o = Robot.state.overview || {};
    const base = (path) => (path ? String(path).split('/').pop() : null);
    const rows = [
      ['Thinking', base(sel.llm_model)],
      ['Context', sel.llm_ctx ? `${num(sel.llm_ctx)} tokens` : null],
      ['Listening', base(sel.asr_model)],
      ['Speaking', sel.supertonic_voice
        ? `supertonic ${sel.supertonic_voice}` : (sel.tts_engine || null)],
      ['Default language', o.language_default],
      ['Microphone', v.listen_mode],
      ['Acknowledgement', o.filler === undefined ? null : (o.filler ? 'on' : 'off')],
      ['Reply format', o.reply_format],
      ['Temperature', v.temperature],
      ['Retrieved chunks', v.rag_k],
      ['Endpoint silence', v.vad_min_silence ? `${v.vad_min_silence} s` : null],
      ['Noise suppression', v.asr_denoise],
      ['Output volume', v.tts_volume === undefined ? null : `${v.tts_volume}%`],
      ['Up for', o.uptime_s ? uptime(o.uptime_s) : null],
    ];
    cfgEl.textContent = '';
    for (const [key, value] of rows) {
      const unset = value === null || value === undefined || value === '';
      cfgEl.append(el('div', { class: 'row' },
        el('span', { class: 'ck', text: key }),
        el('span', { class: `cv${unset ? ' off' : ''}`,
                     text: unset ? '—' : String(value) })));
    }
  }

  function uptime(seconds) {
    const s = Math.round(seconds);
    if (s < 90) return `${s} s`;
    const m = Math.round(s / 60);
    if (m < 90) return `${m} min`;
    const h = Math.floor(m / 60);
    return `${h} h ${m % 60} min`;
  }

  /* ---- the three graphs ----------------------------------------------------------
   *
   * CPU and GPU share one axis because they share a unit. Two measures of different
   * scale would need two charts; a second y-axis is the one thing never to do.
   */
  const SERIES = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)'];
  let monitorTimer = null;

  async function paintLoad() {
    const host = document.getElementById('chart-load');
    const memHost = document.getElementById('chart-mem');
    if (!host) return;
    let data;
    try {
      const response = await fetch('/api/monitor?history=1', { cache: 'no-store' });
      if (!response.ok) return;
      data = await response.json();
    } catch (err) {
      return;           // a page left open through a restart; the next tick retries
    }
    const h = data.history || {};
    const stamps = h.t || [];
    const when = (i) => (stamps[i]
      ? new Date(stamps[i] * 1000).toLocaleTimeString([], { hour12: false })
      : `sample ${i + 1}`);

    host.textContent = '';
    host.append(
      Robot.lineChart([
        { name: 'CPU', values: h.cpu || [], colour: SERIES[0] },
        { name: 'GPU', values: h.gpu || [], colour: SERIES[1] },
      ], { max: 100, unit: '%', labels: when,
           empty: 'sampling — a few seconds of history is needed' }),
      Robot.legend([{ name: 'CPU', colour: SERIES[0] }, { name: 'GPU', colour: SERIES[1] }]));
    const now = [];
    if (data.cpu) now.push(`CPU ${Math.round(data.cpu.total)}%`);
    if (data.gpu && data.gpu.load !== null && data.gpu.load !== undefined) {
      now.push(`GPU ${Math.round(data.gpu.load)}%`);
    }
    // The zones are named by the kernel (`cpu-thermal`, `tj-thermal`, ...), not `cpu`.
    // Tj is the junction temperature -- the one that throttles.
    const th = data.thermal || {};
    const hottest = th['tj-thermal'] ?? th['cpu-thermal']
      ?? Math.max(0, ...Object.values(th).filter((v) => typeof v === 'number'));
    if (hottest) now.push(`${Math.round(hottest)}°C`);
    if (data.power && data.power.VDD_IN) now.push(`${data.power.VDD_IN.toFixed(1)} W`);
    if (now.length) {
      host.append(el('div', { class: 'muted', style: 'font-size:0.76rem;margin-top:0.2rem',
                              text: `now: ${now.join(' · ')}` }));
    }

    if (memHost) {
      memHost.textContent = '';
      // Resident memory over the same window, in MiB against the board's ceiling -- the
      // shape of a model load is the thing worth seeing here.
      memHost.append(
        Robot.lineChart([
          { name: 'used', values: h.mem || [], colour: SERIES[0] },
          { name: 'swap', values: h.swap || [], colour: SERIES[1] },
        ], { unit: ' MiB', labels: when, height: 70,
             empty: 'sampling — a few seconds of history is needed' }),
        Robot.legend([{ name: 'used', colour: SERIES[0] },
                      { name: 'swap', colour: SERIES[1] }]));
    }
  }

  /* One stacked bar per turn: prompt tokens that were re-used, prompt tokens that were
     new, and tokens generated. Same unit, so a stack is honest about the total. */
  function paintTokens() {
    const host = document.getElementById('chart-tokens');
    if (!host) return;
    const turns = ((Robot.state.overview || {}).recent) || [];
    const groups = turns.map((t, i) => ({
      label: `turn ${i + 1}${t.truncated ? ' (cut off)' : ''}`,
      parts: [
        { name: 'prompt, re-used', value: t.cached || 0, colour: SERIES[2] },
        { name: 'prompt, new', value: t.in || 0, colour: SERIES[0] },
        { name: 'generated', value: t.out || 0, colour: SERIES[1] },
      ],
    }));
    host.textContent = '';
    host.append(
      Robot.stackedBars(groups, { unit: ' tok',
                                  empty: 'no turns yet — speak to the robot' }),
      Robot.legend([{ name: 'prompt, re-used', colour: SERIES[2] },
                    { name: 'prompt, new', colour: SERIES[0] },
                    { name: 'generated', colour: SERIES[1] }]));
    const u = (Robot.state.overview || {}).usage || {};
    set('tok-total', u.turns
      ? `${compact(u.prompt_tokens)} in · ${compact(u.completion_tokens)} out`
      : '—');
  }

  const recent = [];
  function pushRecent(who, text) {
    recent.push({ who, text });
    while (recent.length > 6) recent.shift();
    recentEl.textContent = '';
    recentEl.className = '';
    for (const turn of recent) {
      recentEl.append(el('div', { class: 'turn' },
        el('b', { text: `${turn.who}: ` }), turn.text));
    }
  }

  Robot.on((message) => {
    switch (message.type) {
      case 'hello':
        paintServices(); paintState(); paintMemory(); paintOverview(); paintConfig();
        paintTokens();
        break;
      case 'health':
        // Carries `overview` and rides the same 3 s poll the Memory page uses.
        paintServices(); paintState(); paintOverview(); paintMemory(); paintTokens();
        break;
      case 'budget': paintMemory(); break;
      case 'values': case 'set_result': paintConfig(); break;
      case '_link': paintState(); paintServices(); break;
      case 'transcript': pushRecent('you', message.text); paintState(); break;
      case 'reply': pushRecent('robot', message.text); break;
      case 'turn_done': paintState(); paintOverview(); paintTokens(); break;
      case 'chunk': paintState(); break;
    }
  });

  paintState();
  paintLoad();
  // The board's own sampler runs at 2 s; asking more often than that would redraw the
  // same points. Separate from the websocket because /proc is robot-ui's to read.
  monitorTimer = setInterval(paintLoad, 3000);
  window.addEventListener('beforeunload', () => clearInterval(monitorTimer));
  Robot.connect();
})();
