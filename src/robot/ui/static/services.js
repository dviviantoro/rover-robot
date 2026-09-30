/* The Services panel: what is running, and start / restart / stop for one at a time.
 *
 * Deliberately does NOT use the orchestrator websocket. Everything here comes from
 * robot-ui's own REST endpoints, because the case this page exists for is "something is
 * down" -- and the orchestrator is one of the things that can be down. A panel that goes
 * blank exactly when you need it is worse than no panel.
 */
(() => {
  const { el, mib } = Robot;
  const listEl = document.getElementById('list');
  const badgeEl = document.getElementById('supervisor-badge');
  const supervisorEl = document.getElementById('supervisor-text');
  const barEl = document.getElementById('mem-bar');
  const figuresEl = document.getElementById('mem-figures');

  const POLL_MS = 3000;

  let rows = [];
  let supervisor = null;
  const busy = {};          // key -> the action currently running
  const outcome = {};       // key -> { ok, message, log }
  let confirming = null;    // { key, action }
  let pollTimer = null;

  function duration(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    const s = Math.floor(seconds);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
    const h = Math.floor(s / 3600);
    return `${h}h ${Math.floor((s % 3600) / 60)}m`;
  }

  async function refresh() {
    try {
      const res = await fetch('/api/services', { headers: { Accept: 'application/json' } });
      if (res.redirected) { location.href = res.url; return; }
      const data = await res.json();
      rows = data.services || [];
      supervisor = data.supervisor;
      badgeEl.textContent = data.supervisor === 'dev' ? 'dev stack' : 'systemd';
      badgeEl.className = `badge ${data.supervisor === 'dev' ? 'badge-warning' : 'badge-success'}`;
      supervisorEl.textContent = `Supervised by ${data.supervisor_label}.`
        + (data.supervisor === 'dev'
           ? '  The systemd units in deploy/systemd/ are not installed on this board, so'
             + ' these buttons drive scripts/run_stack.sh instead — and nothing here'
             + ' survives a reboot.'
           : '');
      render();
    } catch (err) {
      supervisorEl.textContent = 'The dashboard is not reachable.';
    }
  }

  async function memory() {
    try {
      const res = await fetch('/api/monitor');
      if (!res.ok) return;
      const data = await res.json();
      const { used_mib: used, total_mib: total, target_mib: target } = data.memory;
      const pct = Math.min(100, (used / total) * 100);
      barEl.className = `bar ${pct > 92 ? 'danger' : pct > (target / total) * 100 ? 'warn' : 'success'}`;
      barEl.firstElementChild.style.width = `${pct}%`;
      figuresEl.textContent = '';
      figuresEl.append(
        el('span', {}, 'used ', el('b', { text: mib(used) })),
        el('span', {}, 'free ', el('b', { text: mib(total - used) })),
        el('span', {}, 'ceiling ', el('b', { text: mib(total) })),
        el('span', {}, 'swap ', el('b', { text: mib(data.swap.used_mib) })),
      );
    } catch (err) { /* transient */ }
  }

  function actionButtons(row) {
    if (!row.controllable) {
      return [el('span', { class: 'hint', style: 'margin:0;max-width:16rem;text-align:right',
                           text: row.why_not })];
    }
    // Starting a decoder nothing is configured to read would fail, and a button that
    // exists to fail is worse than no button. Say why instead.
    if (!row.running && row.expected === false) {
      return [el('span', { class: 'hint', style: 'margin:0;max-width:17rem;text-align:right' },
                 'Not in use — the selected ASR model uses the other decoder. Choose one on ',
                 el('a', { href: '/memory' }, 'Memory'), ' to start it.')];
    }
    const running = busy[row.key];
    const button = (label, action, cls) => el('button', {
      class: cls, disabled: running ? '' : null,
      onclick: () => request(row, action),
    }, running === action ? `${label}…` : label);

    return row.running
      ? [button('Restart', 'restart', 'btn-warn'), button('Stop', 'stop', 'btn-danger')]
      : [button('Start', 'start', 'btn-primary')];
  }

  /* Stopping or restarting is not undoable from a browser tab -- a stopped LLM has to
     be started again from here, and a stopped orchestrator takes the conversation with
     it. Ask first, and say what it costs, rather than doing it on a mis-click. */
  function request(row, action) {
    if (action === 'start') return run(row.key, action);
    confirming = { key: row.key, action };
    render();
  }

  async function run(key, action) {
    confirming = null;
    busy[key] = action;
    delete outcome[key];
    render();
    try {
      const res = await fetch(`/api/services/${key}/${action}`, { method: 'POST' });
      outcome[key] = await res.json();
    } catch (err) {
      outcome[key] = { ok: false, message: String(err) };
    } finally {
      delete busy[key];
      await refresh();
      memory();
    }
  }

  function confirmBlock(row) {
    const { action } = confirming;
    const block = el('div', { class: 'confirm' },
      el('div', {}, el('b', { text: `${action === 'stop' ? 'Stop' : 'Restart'} ${row.label}?` }),
        row.warning ? ` ${row.warning}` : ''),
      el('div', { class: 'buttons' },
        el('button', { class: action === 'stop' ? 'btn-danger' : 'btn-warn',
                       onclick: () => run(row.key, action) },
           action === 'stop' ? 'Stop it' : 'Restart it'),
        el('button', { class: 'btn-ghost', onclick: () => { confirming = null; render(); } },
           'Cancel')));
    return block;
  }

  function outcomeBlock(row) {
    const result = outcome[row.key];
    const block = el('div', { class: 'outcome' },
      el('span', { class: `badge ${result.ok ? 'badge-success' : 'badge-danger'}`,
                   text: result.ok ? 'done' : 'failed' }),
      ' ', result.message || '');
    // A failed start never explains itself in the supervisor's exit status. The reason
    // -- `cudaMalloc failed: out of memory`, a missing model file -- is in the service's
    // own log, so it is put in front of you here instead of on another page.
    const lines = result.log || (result.ok ? null : result.output);
    if (lines && lines.length) block.append(el('pre', { text: lines.join('\n') }));
    return block;
  }

  function render() {
    listEl.textContent = '';
    if (!rows.length) {
      listEl.append(el('p', { class: 'muted', text: 'No services found.' }));
      return;
    }
    for (const row of rows) {
      const state = row.running ? 'up' : (row.controllable ? 'down' : 'unknown');
      const look = busy[row.key]
        ? { badge: 'badge-warning', label: `${busy[row.key]}ing` }
        : { up: { badge: 'badge-success', label: 'running' },
            down: { badge: 'badge-danger', label: 'stopped' },
            unknown: { badge: 'badge-neutral', label: 'running' } }[state];

      const facts = el('div', { class: 'facts' });
      // What it actually loaded. Read from the process, not from the config -- see
      // robot/common/services.py. This is the line that answers "am I really talking to
      // the model I picked?", so it comes before the pid and the uptime.
      if (row.model) facts.append(el('span', {}, el('code', { text: row.model })));
      if (row.chosen && row.chosen !== row.model) {
        facts.append(el('span', {}, 'chosen ', el('code', { text: row.chosen })));
      }
      if (row.running) {
        // GPU or CPU, read from the process's own file descriptors rather than its
        // flags: on this board both draw from one unified pool, so which processes are
        // on the GPU is the difference between the LLM getting its KV cache and not.
        facts.append(el('span', { class: row.accelerator === 'GPU' ? 'on-gpu' : null },
                        row.accelerator));
        facts.append(el('span', {}, 'pid ', el('code', { text: String(row.pid) })));
        facts.append(el('span', { text: `up ${duration(row.uptime_s)}` }));
        if (row.rss_mib !== null) facts.append(el('span', { text: mib(row.rss_mib) }));
        if (row.instances.length > 1) {
          facts.append(el('span', { text: `${row.instances.length} processes` }));
        }
      } else {
        facts.append(el('span', { text: 'not running' }));
      }
      facts.append(el('a', { href: `/logs?source=${row.key}`, text: 'log' }));
      if (row.unit) facts.append(el('span', {}, el('code', { text: `${row.unit}.service` })));

      const node = el('div', { class: `svc${busy[row.key] ? ' busy' : ''}` },
        el('div', {}, el('span', { class: `badge ${look.badge}`, text: look.label })),
        el('div', {},
          el('div', { class: 'name', text: row.label }),
          el('div', { class: 'blurb', text: row.blurb }),
          facts),
        el('div', { class: 'actions' }, ...actionButtons(row)));

      // A service running something other than its config is not broken, so it does not
      // get a red badge -- but it is answering questions with the wrong model, and only
      // a restart closes that. Say it in full, next to the button that fixes it.
      if (row.stale) {
        node.append(el('div', { class: 'mismatch' },
          el('span', { class: 'badge badge-warning', text: 'not applied' }), ' ',
          'Running ', el('code', { text: row.model }),
          ', but the config now says ', el('code', { text: row.configured }),
          '. Restart it to load what is configured.'));
      } else if (row.unapplied) {
        node.append(el('div', { class: 'mismatch' },
          el('span', { class: 'badge badge-warning', text: 'not applied' }), ' ',
          el('code', { text: row.chosen }), ' is selected on ',
          el('a', { href: '/memory' }, 'Memory'),
          ', but ', el('code', { text: row.configured }), ' is what is configured. '
          + 'Press Apply there.'));
      }
      // Exactly one ASR decoder is ever meant to be loaded. A second one is not a
      // fault, so it gets no red light -- it is just holding memory nothing reads,
      // which on this board is the thing that actually hurts.
      if (row.unexpected) {
        node.append(el('div', { class: 'mismatch' },
          el('span', { class: 'badge badge-warning', text: 'not in use' }), ' ',
          'Running, but nothing is reading it — the selected ASR model does not use this '
          + 'decoder. Stop it to give the memory back.'));
      }
      if (row.missing) {
        node.append(el('div', { class: 'mismatch' },
          el('span', { class: 'badge badge-danger', text: 'needed' }), ' ',
          'The selected ASR model needs this decoder and it is not running. '
          + 'The robot cannot hear until it is.'));
      }
      if (confirming && confirming.key === row.key) node.append(confirmBlock(row));
      if (outcome[row.key]) node.append(outcomeBlock(row));
      listEl.append(node);
    }
  }

  refresh();
  memory();
  pollTimer = setInterval(() => {
    // Polling while a confirmation is open would re-render the dialog out from under
    // the pointer; while an action runs, the answer is the action's own result.
    if (!confirming && !Object.keys(busy).length) { refresh(); memory(); }
  }, POLL_MS);
  window.addEventListener('beforeunload', () => clearInterval(pollTimer));
})();
