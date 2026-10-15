(() => {
  const { el } = Robot;
  const $ = (id) => document.getElementById(id);
  const stream = $('stream');
  const LEVELS = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 };

  const SOURCES = [
    ['live', 'Orchestrator (live)'], ['core', 'core'], ['asr', 'asr'],
    ['tts', 'tts'], ['llm', 'llm'], ['embed', 'embed'], ['body', 'body'], ['ui', 'ui'],
  ];

  let source = 'live';
  let minLevel = 'INFO';
  let filter = '';
  let paused = false;
  let pollTimer = null;
  const counts = { DEBUG: 0, INFO: 0, WARNING: 0, ERROR: 0 };

  const MAX_LINES = 2000;

  function visible(node) {
    const level = node.dataset.level || 'INFO';
    if ((LEVELS[level] || 20) < LEVELS[minLevel]) return false;
    if (filter && !node.textContent.toLowerCase().includes(filter)) return false;
    return true;
  }

  /* `2026-10-06 19:11:31,225` is 23 characters of which 8 carry information on a page
     you are reading live. The date is today and the milliseconds are noise. */
  const hhmmss = (iso) => {
    const m = /(\d{2}:\d{2}:\d{2})/.exec(String(iso || ''));
    return m ? m[1] : (iso || '').slice(0, 8);
  };

  /* The logger name is `robot-core` on nearly every line of the core log, which is the
     one thing you already know -- you chose that source. Shown only when it says
     something more, e.g. `robot.asr.recognizer` inside the asr log. */
  function loggerSuffix(logger) {
    const name = String(logger || '');
    if (!name) return '';
    const expected = source === 'live' ? 'robot-core' : `robot-${source}`;
    if (name === expected || name === source) return '';
    return name.replace(/^robot[.-]/, '');
  }

  function append(entry) {
    const extra = loggerSuffix(entry.logger);
    const node = el('div', { class: `line ${entry.level || 'INFO'}` },
      el('time', { text: hhmmss(entry.iso) }),
      el('span', { class: 'lvl', text: (entry.level || 'INFO').slice(0, 4) }),
      extra ? el('span', { class: 'src', text: extra }) : null,
      el('span', { class: 'msg', text: entry.message || '' }));
    if (entry.exception) node.append(el('span', { class: 'exc', text: entry.exception }));
    node.dataset.level = entry.level || 'INFO';
    if (!visible(node)) node.classList.add('hidden');
    stream.append(node);
    while (stream.children.length > MAX_LINES) stream.firstChild.remove();

    if (counts[entry.level] !== undefined) counts[entry.level] += 1;
    paintCounts();
    if (!paused) stream.scrollTop = stream.scrollHeight;
  }

  function paintCounts() {
    $('counts').textContent =
      `${counts.ERROR} errors · ${counts.WARNING} warnings · ${counts.INFO} info`
      + (counts.DEBUG ? ` · ${counts.DEBUG} debug` : '');
  }

  function reapply() {
    for (const node of stream.children) node.classList.toggle('hidden', !visible(node));
  }

  function clear() {
    stream.textContent = '';
    for (const k of Object.keys(counts)) counts[k] = 0;
    paintCounts();
  }

  /* ---------- file/journal sources are polled ---------- */
  async function pollFile() {
    if (paused) return;
    try {
      const res = await fetch(`/api/logs?source=${encodeURIComponent(source)}&lines=400`,
                              { cache: 'no-store' });
      if (res.status === 401 || res.redirected) { location.href = '/login'; return; }
      const data = await res.json();
      clear();
      if (data.note) {
        append({ level: 'WARNING', logger: source, message: data.note, iso: '' });
      }
      for (const raw of data.lines || []) {
        // These are already-formatted lines; pull the level out so filtering works.
        const level = (raw.match(/\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b/) || [])[1] || 'INFO';
        append({ level, logger: '', message: raw, iso: '' });
      }
      if (data.origin) {
        $('level-note').textContent = `Reading ${data.origin}. Polled every 3 s.`;
      }
    } catch (err) { /* transient */ }
  }

  function selectSource(next) {
    source = next;
    clearInterval(pollTimer);
    clear();
    paintSources();
    if (next === 'live') {
      $('level-note').textContent =
        "Debug makes the orchestrator log the model's raw reply before any stripping — "
        + 'the fastest way to see why a language tag or an action marker went wrong.';
      Robot.send({ type: 'logs', level: 'DEBUG', limit: 400 });
    } else {
      pollFile();
      pollTimer = setInterval(pollFile, 3000);
    }
  }

  /* Which service a source belongs to, so the buttons can follow reality. `live` is the
     orchestrator's own stream and `ui` is this process -- both are here by definition. */
  const SOURCE_SERVICE = { core: 'core', asr: 'asr', tts: 'tts', llm: 'llm',
                           embed: 'embed', body: 'serial' };

  function paintSources() {
    const health = {};
    for (const svc of Robot.state.services || []) health[svc.name] = svc.state;
    const seg = $('source-seg');
    seg.textContent = '';
    for (const [key, label] of SOURCES) {
      const service = SOURCE_SERVICE[key];
      const state = service ? health[service] : 'up';
      // A log for something that is not running is almost always the wrong place to
      // look, and eight equal buttons gave no clue which. Off services stay reachable
      // -- the reason a service is down is in its log -- but they stop competing.
      const off = service !== undefined && state !== undefined
                  && !['up', 'idle', 'restarting'].includes(state);
      seg.append(el('button', {
        'data-source': key, class: `${key === source ? 'on' : ''}${off ? ' dim' : ''}`,
        title: off ? `${label} is ${state}` : label,
        onclick: () => selectSource(key),
      }, label, off ? el('span', { class: 'offdot', title: state }) : null));
    }
  }
  paintSources();
  for (const button of $('level-seg').children) {
    button.addEventListener('click', () => {
      minLevel = button.dataset.level;
      for (const b of $('level-seg').children) b.classList.toggle('on', b === button);
      reapply();
    });
  }
  for (const button of $('setlevel-seg').children) {
    button.addEventListener('click', () => {
      Robot.send({ type: 'set_log_level', level: button.dataset.set });
    });
  }
  $('filter').addEventListener('input', (e) => { filter = e.target.value.toLowerCase(); reapply(); });
  $('pause').addEventListener('click', (e) => {
    paused = !paused;
    e.target.textContent = paused ? 'Resume' : 'Pause';
    e.target.className = paused ? 'btn-warn' : 'btn-secondary';
  });
  $('clear').addEventListener('click', clear);

  Robot.on((message) => {
    if (message.type === 'hello' || message.type === 'health') paintSources();
    if (message.type === 'log') {
      if (source === 'live' && !paused) append(message);
    } else if (message.type === 'logs') {
      if (source !== 'live') return;
      clear();
      for (const entry of message.entries || []) append(entry);
      for (const b of $('setlevel-seg').children) {
        b.classList.toggle('on', b.dataset.set === message.level);
      }
    } else if (message.type === 'log_level') {
      for (const b of $('setlevel-seg').children) {
        b.classList.toggle('on', b.dataset.set === message.level);
      }
      append({ level: 'WARNING', logger: 'dashboard', iso: '',
               message: `orchestrator log level set to ${message.level}`
                        + (message.note ? ` (${message.note})` : '') });
    } else if (message.type === '_link' && message.up && source === 'live') {
      Robot.send({ type: 'logs', level: 'DEBUG', limit: 400 });
    }
  });

  paintCounts();
  // The Services panel links here as /logs?source=llm when a start fails, so the
  // reason is one click away instead of a source that has to be found by hand.
  const wanted = new URLSearchParams(location.search).get('source');
  if (wanted && SOURCES.some(([key]) => key === wanted)) selectSource(wanted);
  Robot.connect();
})();
