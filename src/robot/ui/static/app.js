/* Shared dashboard runtime.
 *
 * One websocket per page to robot-ui, which relays to the orchestrator. The browser
 * never talks to robot-core directly: it has no token, and robot-core listens on
 * loopback only. The socket carries the same signed session cookie as the page, so an
 * unauthenticated one cannot be opened even from this machine.
 */
window.Robot = (() => {
  const listeners = [];
  let socket = null;
  let connected = false;
  let reconnectTimer = null;

  /** Latest known state, so a page can render before the first event arrives. */
  const state = {
    services: [], settings: [], groups: [], values: {}, level: {},
    robotState: 'unknown', linkUp: false,
    // Kept here so a page that opens mid-session renders from the last known payload
    // instead of a row of dashes until the next 3 s poll.
    budget: null, selection: {}, overview: null,
  };

  function emit(message) {
    for (const fn of listeners) {
      try { fn(message); } catch (err) { console.error('listener failed', err); }
    }
  }

  function connect() {
    clearTimeout(reconnectTimer);
    // The session cookie authenticates the socket; nothing else is needed.
    const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
    socket = new WebSocket(`${scheme}://${location.host}/api/ws`);

    socket.onopen = () => { connected = true; emit({ type: '_link', up: true }); };
    socket.onclose = (event) => {
      connected = false;
      emit({ type: '_link', up: false });
      if (event.code === 4401) { location.href = '/login'; return; }
      reconnectTimer = setTimeout(connect, 2000);
    };
    socket.onerror = () => socket && socket.close();
    socket.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (err) { return; }
      if (message.type === 'hello') {
        state.settings = message.settings || [];
        state.groups = message.groups || [];
        state.values = message.values || {};
        state.services = message.services || [];
        state.robotState = message.state || 'unknown';
        state.linkUp = !!message.connected;
        state.budget = message.budget || null;
        state.selection = message.selection || {};
        state.overview = message.overview || null;
      } else if (message.type === 'health') {
        state.services = message.services || [];
        if (message.overview) state.overview = message.overview;
      } else if (message.type === 'budget') {
        state.budget = message;
      } else if (message.type === 'values') {
        state.values = message.values || state.values;
      } else if (message.type === 'link') {
        state.linkUp = !!message.connected;
      } else if (message.type === 'turn_done') {
        state.robotState = 'idle';
      } else if (message.type === 'transcript') {
        state.robotState = 'thinking';
      } else if (message.type === 'chunk') {
        state.robotState = 'speaking';
      }
      emit(message);
    };
  }

  function send(message) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return false;
    socket.send(JSON.stringify(message));
    return true;
  }

  /* ---------- small DOM helpers ---------- */
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else if (value !== null && value !== undefined) node.setAttribute(key, value);
    }
    for (const child of children) {
      if (child === null || child === undefined) continue;
      node.append(child.nodeType ? child : document.createTextNode(child));
    }
    return node;
  }

  /* The structured reply, folded away under the answer it produced.
   *
   * The model returns one JSON object per turn and only `answer` is ever spoken; the
   * rest is what the body did while it spoke. It collapses, because showing it inline
   * would read like the robot narrating its own stage directions -- but it is right
   * there, because "why did it turn left?" is answered by these fields and by nothing
   * else in the UI.
   *
   * Just the JSON. This used to render a labelled grid of the seven fields AND the raw
   * object underneath it -- the same data twice, in two shapes, in a panel that is open
   * for a few seconds at a time. One block of JSON is what the model actually said.
   */
  const REPLY_FIELDS = ['answer', 'language', 'foot_direction', 'foot_step',
                        'hand_movement', 'face', 'style'];

  function replyFields(fields) {
    // Fixed key order rather than whatever order the object arrived in, so the same
    // field is on the same line every turn and the panel can be read by position.
    const raw = {};
    for (const key of REPLY_FIELDS) raw[key] = fields[key] ?? null;
    // Open when it belongs to a phase: acknowledgement, answering and idle are meant to be
    // read side by side, and a collapsed block per phase looked like only one existed.
    return el('details', { class: `reply-json${fields.phase ? ' phase-' + fields.phase : ''}`,
                           open: fields.phase ? '' : null },
      el('summary', {}, fields.phase ? `Reply JSON · ${fields.phase}` : 'Reply JSON'),
      el('pre', { class: 'rf-raw', text: JSON.stringify(raw, null, 2) }));
  }

  /* ---------- the one definition of the navigation ----------
   *
   * Nine pages each carried their own copy of the nav as HTML. Adding a page meant
   * editing ten files and the tenth was always missed, so it lives here once and every
   * page renders it from `document.body.dataset.page`.
   *
   * Grouped because a flat list of eight had no shape: Chat and Voice are the same
   * activity through different senses, and Memory, Monitor and Services are three views
   * of one question -- what is this board doing right now.
   */
  const ICON = {
    dashboard: 'M3 13h8V3H3v10zm10 8h8V11h-8v10zM3 21h8v-6H3v6zM13 9h8V3h-8v6z',
    chat: 'M21 11.5a8.4 8.4 0 0 1-8.5 8.5 8.6 8.6 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.4 8.4 0 0 1 8.5-8.5 8.4 8.4 0 0 1 8.5 8.5z',
    mic: 'M12 1a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3zM19 10v1a7 7 0 0 1-14 0v-1M12 19v4M8 23h8',
    book: 'M4 19.5A2.5 2.5 0 0 1 6.5 17H20M4 19.5A2.5 2.5 0 0 0 6.5 22H20V2H6.5A2.5 2.5 0 0 0 4 4.5v15z',
    chip: 'M6 6h12v12H6zM9 2v2M15 2v2M9 20v2M15 20v2M2 9h2M2 15h2M20 9h2M20 15h2',
    gauge: 'M12 20a8 8 0 1 1 8-8M12 12l4-4',
    stack: 'M4 7h16M4 12h16M4 17h16',
    logs: 'M4 4h16v16H4zM8 9h8M8 13h8M8 17h4',
    cog: 'M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2 2 2 0 1 1-4 0 1.7 1.7 0 0 0-2.9-1.2l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.7 1.7 0 0 0 3 15a2 2 0 1 1 0-4 1.7 1.7 0 0 0 1.2-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1A1.7 1.7 0 0 0 10 4a2 2 0 1 1 4 0 1.7 1.7 0 0 0 2.9 1.2l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1A1.7 1.7 0 0 0 21 11a2 2 0 1 1 0 4z',
  };

  const NAV = [
    { href: '/dashboard', label: 'Dashboard', page: 'dashboard', icon: 'dashboard' },
    { group: 'Interaction', items: [
      { href: '/chat', label: 'Chat', page: 'chat', icon: 'chat' },
      { href: '/headless', label: 'Voice', page: 'headless', icon: 'mic' },
    ] },
    { href: '/knowledge', label: 'Knowledge', page: 'knowledge', icon: 'book' },
    { group: 'System', items: [
      { href: '/memory', label: 'Memory', page: 'memory', icon: 'chip' },
      { href: '/monitor', label: 'Monitor', page: 'monitor', icon: 'gauge' },
      { href: '/services', label: 'Services', page: 'services', icon: 'stack' },
    ] },
    { href: '/logs', label: 'Logs', page: 'logs', icon: 'logs' },
    { href: '/config', label: 'Config', page: 'config', icon: 'cog' },
  ];

  function icon(name) {
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.7');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', ICON[name] || ICON.dashboard);
    svg.append(path);
    return svg;
  }

  function navLink(item, current) {
    return el('a', {
      href: item.href,
      class: `nav-item${item.page === current ? ' current' : ''}`,
      'aria-current': item.page === current ? 'page' : null,
    }, icon(item.icon), el('span', { text: item.label }));
  }

  /** Build the sidebar into `nav.sidebar`, marking `data-page` as current. */
  function shell() {
    const side = document.querySelector('nav.sidebar');
    if (!side) return;
    const current = document.body.dataset.page || '';
    const brand = el('a', { class: 'brand', href: '/dashboard' },
      el('span', { class: 'brand-mark' }), el('span', { text: 'Robot' }));
    const list = el('div', { class: 'nav-list' });
    for (const entry of NAV) {
      if (entry.group) {
        list.append(el('div', { class: 'nav-group', text: entry.group }));
        for (const item of entry.items) list.append(navLink(item, current));
      } else {
        list.append(navLink(entry, current));
      }
    }
    side.append(brand, list, el('div', { class: 'nav-foot' },
      el('span', { class: 'nav-link-state' },
        el('span', { class: 'dot', id: 'nav-dot' }),
        el('span', { id: 'nav-state-text', text: 'connecting…' })),
      el('a', { class: 'btn-ghost', href: '/logout', text: 'Sign out' })));
    // A hamburger rather than a drawer library: at phone width the sidebar becomes a
    // sheet, and one class toggle is the whole of it.
    document.body.append(el('button', {
      class: 'nav-toggle', 'aria-label': 'Menu',
      onclick: () => document.body.classList.toggle('nav-open'),
    }, icon('stack')));
    // `listeners.push`, not the exported `on` -- that name exists only on the returned
    // object, and calling it bare from in here is a ReferenceError that takes the whole
    // module down with it. It did, for one commit: `window.Robot` was never assigned and
    // every page went blank.
    listeners.push((msg) => {
      if (msg.type !== '_link' && msg.type !== 'link') return;
      const up = msg.type === '_link' ? msg.up : !!msg.connected;
      const dot = document.getElementById('nav-dot');
      const text = document.getElementById('nav-state-text');
      if (dot) dot.style.background = up ? 'var(--success)' : 'var(--danger)';
      if (text) text.textContent = up ? 'live' : 'reconnecting…';
    });
  }

  /* ---- what the turn actually spent its time on ----------------------------------
   *
   * Every figure in `turn_done` is measured from the moment speech ENDED, so they are
   * cumulative. Cumulative numbers do not answer "which part was slow" -- a reader has
   * to subtract them in their head, and the stage boundaries are not obvious. These are
   * the differences, in order, with the largest one marked.
   *
   * The acknowledgement complicates it on purpose: with CORE_FILLER on, `first_audio_ms`
   * is when the ACK started, which is normally BEFORE the answer's first clause exists.
   * So it is reported as its own line rather than folded into synthesis, where it would
   * have produced a negative duration.
   */
  function turnStages(m) {
    const at = (k) => (typeof m[k] === 'number' ? m[k] : null);
    const decode = at('asr_decode_ms') || 0;
    const transcript = at('transcript_ms');
    const retrieval = at('retrieval_ms');
    const firstToken = at('first_token_ms');
    const firstChunk = at('first_chunk_ms');
    const firstAudio = at('first_audio_ms');
    const finished = at('finished_ms');
    const rows = [];
    const add = (name, ms, why) => {
      if (ms !== null && ms !== undefined && ms >= 0) rows.push({ name, ms, why });
    };

    if (transcript !== null) {
      // What the VAD spent waiting for silence before it would call the turn over.
      add('endpointing', Math.max(0, transcript - decode), 'vad_min_silence');
      add('ASR decode', decode, 'WHISPER_MODEL size');
    }
    if (retrieval !== null && transcript !== null) {
      add('retrieval', Math.max(0, retrieval - transcript), 'rag_k, embeddings');
    }
    const promptFrom = retrieval !== null ? retrieval : transcript;
    if (firstToken !== null && promptFrom !== null) {
      add('prompt → first token', Math.max(0, firstToken - promptFrom), 'prompt size');
    }
    if (firstChunk !== null && firstToken !== null) {
      add('first speakable clause', Math.max(0, firstChunk - firstToken),
          'CORE_FIRST_MAX_CHARS');
    }
    // The acknowledgement, when it got in first.
    const ackFirst = firstAudio !== null && firstChunk !== null && firstAudio < firstChunk;
    if (ackFirst) {
      rows.unshift({ name: 'acknowledged at', ms: firstAudio, why: 'CORE_FILLER',
                     absolute: true });
    } else if (firstAudio !== null && firstChunk !== null) {
      add('synthesis → first sound', Math.max(0, firstAudio - firstChunk),
          'Supertonic, TTS_TRIM_*');
    }
    if (finished !== null) {
      const speakFrom = Math.max(firstAudio ?? 0, firstChunk ?? 0);
      add('speaking', Math.max(0, finished - speakFrom), `${m.audio_s ?? '?'} s of audio`);
    }
    return rows;
  }

  /* ---------- charts ----------
   *
   * Hand-written SVG rather than a library: this board has ~100 MiB of headroom and a
   * charting bundle is hundreds of kilobytes of parse on a page that draws two lines and
   * a bar per turn. Both forms ship a hover layer, because an HTML chart that cannot be
   * interrogated is a picture of data.
   */
  const SVG = 'http://www.w3.org/2000/svg';
  const svgEl = (tag, attrs = {}) => {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v !== null && v !== undefined) node.setAttribute(k, String(v));
    }
    return node;
  };

  function chartTip() {
    return el('div', { class: 'chart-tip' });
  }

  /** Move a tooltip to a point inside `host`, or hide it when `rows` is null. */
  function showTip(tip, host, x, title, rows) {
    if (!rows) { tip.style.opacity = '0'; return; }
    tip.textContent = '';
    tip.append(el('div', { class: 'tt', text: title }));
    for (const row of rows) {
      tip.append(el('div', { class: 'tr' },
        el('span', { class: 'sw', style: `background:${row.colour}` }),
        el('span', { text: row.name }), el('b', { text: row.value })));
    }
    tip.style.left = `${x}px`;
    tip.style.top = '0px';
    tip.style.opacity = '1';
  }

  /**
   * Time series, one shared y-axis. Two measures of different scale would need two
   * charts -- never a second axis.
   * series: [{ name, values: number[], colour }]
   */
  function lineChart(series, opts = {}) {
    const { height = 86, max = null, unit = '%', labels = null } = opts;
    const host = el('div', { class: 'chart' });
    const live = series.filter((s) => (s.values || []).some((v) => v !== null));
    const n = Math.max(...series.map((s) => (s.values || []).length), 0);
    if (!n || !live.length) {
      host.append(el('div', { class: 'chart-empty', text: opts.empty || 'no samples yet' }));
      return host;
    }
    const W = 300, H = height, PAD_L = 26, PAD_B = 12, PAD_T = 6;
    const top = max !== null ? max : Math.max(
      1, ...series.flatMap((s) => (s.values || []).filter((v) => v !== null)));
    const nice = max !== null ? top : Math.ceil(top * 1.15);
    const px = (i) => PAD_L + (i / Math.max(n - 1, 1)) * (W - PAD_L - 2);
    const py = (v) => PAD_T + (1 - v / nice) * (H - PAD_T - PAD_B);

    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`,
                               preserveAspectRatio: 'none', role: 'img' });
    // Recessive grid: three lines, labelled, nothing more.
    for (const frac of [0, 0.5, 1]) {
      const v = nice * frac;
      svg.append(svgEl('line', { class: 'grid-line', x1: PAD_L, x2: W - 2,
                                 y1: py(v), y2: py(v) }));
      const t = svgEl('text', { class: 'axis-label', x: 0, y: py(v) + 3 });
      t.textContent = frac === 0 ? '0' : Math.round(v).toLocaleString();
      svg.append(t);
    }
    for (const s of series) {
      const pts = (s.values || []).map((v, i) => (v === null ? null : [px(i), py(v)]));
      // A gap in the data is a gap in the line, not a straight leap across it.
      let d = '', pen = false;
      for (const p of pts) {
        if (!p) { pen = false; continue; }
        d += `${pen ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)} `;
        pen = true;
      }
      if (d) svg.append(svgEl('path', { class: 'series-line', d, stroke: s.colour }));
    }
    host.append(svg);

    const tip = chartTip();
    host.append(tip);
    const cross = svgEl('line', { class: 'crosshair', y1: PAD_T, y2: H - PAD_B,
                                  x1: -10, x2: -10 });
    svg.append(cross);
    const hit = svgEl('rect', { class: 'hit', x: PAD_L, y: 0,
                                width: W - PAD_L, height: H });
    svg.append(hit);
    const onMove = (event) => {
      const box = host.getBoundingClientRect();
      if (!box.width) return;
      const frac = (event.clientX - box.left) / box.width;
      const i = Math.max(0, Math.min(n - 1, Math.round(frac * (n - 1))));
      cross.setAttribute('x1', px(i));
      cross.setAttribute('x2', px(i));
      showTip(tip, host, (px(i) / W) * box.width,
        labels ? labels(i) : `sample ${i + 1} of ${n}`,
        series.filter((s) => s.values[i] !== null && s.values[i] !== undefined)
              .map((s) => ({ name: s.name, colour: s.colour,
                             value: `${Math.round(s.values[i]).toLocaleString()}${unit}` })));
    };
    host.addEventListener('mousemove', onMove);
    host.addEventListener('mouseleave', () => {
      cross.setAttribute('x1', -10); cross.setAttribute('x2', -10);
      showTip(tip, host, 0, '', null);
    });
    return host;
  }

  /**
   * One stacked bar per group. Segments of the same unit only -- a stack of different
   * units is a lie about a total.
   * groups: [{ label, parts: [{ name, value, colour }] }]
   */
  function stackedBars(groups, opts = {}) {
    const { height = 86, unit = '' } = opts;
    const host = el('div', { class: 'chart' });
    if (!groups.length) {
      host.append(el('div', { class: 'chart-empty', text: opts.empty || 'nothing yet' }));
      return host;
    }
    const W = 300, H = height, PAD_L = 30, PAD_B = 10, PAD_T = 6, GAP = 2;
    const totals = groups.map((g) => g.parts.reduce((t, p) => t + (p.value || 0), 0));
    const nice = Math.max(1, Math.ceil(Math.max(...totals) * 1.15));
    const band = (W - PAD_L - 2) / groups.length;
    const bar = Math.max(2, Math.min(18, band - 3));
    const py = (v) => PAD_T + (1 - v / nice) * (H - PAD_T - PAD_B);

    const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`,
                               preserveAspectRatio: 'none', role: 'img' });
    for (const frac of [0, 0.5, 1]) {
      const v = nice * frac;
      svg.append(svgEl('line', { class: 'grid-line', x1: PAD_L, x2: W - 2,
                                 y1: py(v), y2: py(v) }));
      const t = svgEl('text', { class: 'axis-label', x: 0, y: py(v) + 3 });
      t.textContent = frac === 0 ? '0' : Math.round(v).toLocaleString();
      svg.append(t);
    }
    const tip = chartTip();
    groups.forEach((g, gi) => {
      const x = PAD_L + gi * band + (band - bar) / 2;
      let acc = 0;
      g.parts.forEach((part, pi) => {
        const value = part.value || 0;
        if (value <= 0) return;
        const y0 = py(acc + value), y1 = py(acc);
        // A 2px surface gap between segments, so a stack reads as parts and not a blur.
        const h = Math.max(1, y1 - y0 - (pi < g.parts.length - 1 ? GAP : 0));
        const rect = svgEl('rect', { x, y: y0, width: bar, height: h,
                                     rx: 2, fill: part.colour });
        svg.append(rect);
        acc += value;
      });
      // One hit area per bar, wider than the mark.
      const hit = svgEl('rect', { class: 'hit', x: PAD_L + gi * band, y: 0,
                                  width: band, height: H });
      hit.addEventListener('mouseenter', () => {
        const box = host.getBoundingClientRect();
        showTip(tip, host, ((PAD_L + gi * band + band / 2) / W) * box.width, g.label,
          g.parts.filter((p) => (p.value || 0) > 0).map((p) => ({
            name: p.name, colour: p.colour,
            value: `${Math.round(p.value).toLocaleString()}${unit}` })));
      });
      svg.append(hit);
    });
    host.append(svg, tip);
    host.addEventListener('mouseleave', () => showTip(tip, host, 0, '', null));
    return host;
  }

  /** The legend that always accompanies two or more series. */
  function legend(items) {
    return el('div', { class: 'legend-row' }, ...items.map((i) =>
      el('span', {}, el('span', { class: 'sw', style: `background:${i.colour}` }),
         el('span', { text: i.name }))));
  }

  const clock = () => new Date().toLocaleTimeString([], { hour12: false });
  const mib = (v) => `${Math.round(v).toLocaleString()} MiB`;

  /** Health state -> the badge class and dot colour used everywhere. */
  const HEALTH = {
    up: { badge: 'badge-success', colour: 'var(--success)', label: 'up' },
    down: { badge: 'badge-danger', colour: 'var(--danger)', label: 'down' },
    restarting: { badge: 'badge-warning', colour: 'var(--warning)', label: 'restarting' },
    // On purpose, not a fault: the embedding server asleep between ingestion jobs.
    idle: { badge: 'badge-info', colour: 'var(--accent)', label: 'on demand' },
    unknown: { badge: 'badge-neutral', colour: 'var(--text-faint)', label: 'unknown' },
    absent: { badge: 'badge-neutral', colour: 'var(--text-faint)', label: 'absent' },
  };

  return {
    connect, send, el, clock, mib, state, HEALTH, replyFields, shell, icon, NAV,
    turnStages, lineChart, stackedBars, legend,
    on: (fn) => listeners.push(fn),
    isConnected: () => connected,
  };
})();

/* Every signed-in page wants the same sidebar, so it is built here rather than being
 * one more line each page could forget. app.js is loaded at the end of <body>, so the
 * container already exists; pages without one (landing, login) are left alone. */
Robot.shell();
