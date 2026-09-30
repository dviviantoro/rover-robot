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

  function append(entry) {
    const node = el('div', { class: `line ${entry.level || 'INFO'}` },
      el('time', { text: entry.iso || '' }),
      el('span', { class: 'lvl', text: (entry.level || '').slice(0, 5) }),
      el('span', { class: 'src', text: entry.logger || '' }),
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
    for (const button of $('source-seg').children) {
      button.classList.toggle('on', button.dataset.source === next);
    }
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

  for (const [key, label] of SOURCES) {
    $('source-seg').append(el('button', {
      'data-source': key, class: key === 'live' ? 'on' : '',
      onclick: () => selectSource(key),
    }, label));
  }
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
