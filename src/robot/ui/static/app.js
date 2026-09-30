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
      } else if (message.type === 'health') {
        state.services = message.services || [];
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
   * rest is what the body did while it spoke. Showing it inline would read like the
   * robot narrating its own stage directions, so it collapses -- but it is right there,
   * because "why did it turn left?" is answered by these seven fields and by nothing
   * else in the UI.
   *
   * Fields that did nothing are dimmed rather than hidden: `foot_direction: none` is
   * information, and a table that changes shape every turn is hard to read at a glance.
   */
  const REPLY_FIELDS = ['answer', 'language', 'foot_direction', 'foot_step',
                        'hand_movement', 'face', 'style'];
  const REPLY_IDLE = { foot_direction: 'none', foot_step: 0, hand_movement: 'none',
                       face: 'neutral', style: 'neutral' };

  function replyFields(fields) {
    const rows = REPLY_FIELDS.map((key) => {
      const value = fields[key];
      const idle = String(REPLY_IDLE[key] ?? '') === String(value ?? '');
      return el('div', { class: `rf-row${idle ? ' idle' : ''}` },
        el('span', { class: 'rf-key', text: key }),
        el('span', { class: 'rf-val', text: value === null || value === undefined
                                            ? '—' : String(value) }));
    });
    // The raw object too: the fields above are what the robot ACTED on after
    // validation, which is not always what the model wrote.
    const raw = {};
    for (const key of REPLY_FIELDS) raw[key] = fields[key] ?? null;
    return el('details', { class: 'reply-json' },
      el('summary', {}, 'Reply JSON'),
      el('div', { class: 'rf-grid' }, ...rows),
      el('pre', { class: 'rf-raw', text: JSON.stringify(raw, null, 2) }));
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
    connect, send, el, clock, mib, state, HEALTH, replyFields,
    on: (fn) => listeners.push(fn),
    isConnected: () => connected,
  };
})();
