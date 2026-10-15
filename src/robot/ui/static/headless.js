(() => {
  const { el, clock } = Robot;
  const stateEl = document.getElementById('state');
  const stateDot = stateEl.querySelector('.dot');
  const stateText = document.getElementById('state-text');
  const conversation = document.getElementById('conversation');
  const activity = document.getElementById('activity');
  const banner = document.getElementById('banner');
  const showWire = document.getElementById('show-wire');
  const LANGS = { en: 'English', id: 'Bahasa Indonesia', ar: 'Arabic' };

  /* One entry per pipeline stage. Kept distinct on purpose -- "looking through what it
     knows" is its own step, and seeing it is how you tell retrieval from thinking when
     a turn feels slow. */
  const STAGE = {
    idle:        { text: 'Idle — waiting for you to speak',
                   colour: 'var(--text-faint)', glyph: '·' },
    hearing:     { text: 'Hearing speech',
                   colour: 'var(--st-input)', glyph: '●' },
    transcribing:{ text: 'Transcribing what you said',
                   colour: 'var(--st-input)', glyph: '◐' },
    retrieving:  { text: 'Looking through what it knows',
                   colour: 'var(--st-think)', glyph: '◆' },
    thinking:    { text: 'Thinking — generating a reply',
                   colour: 'var(--st-think)', glyph: '▲' },
    speaking:    { text: 'Speaking',
                   colour: 'var(--st-speak)', glyph: '■' },
  };

  // null, not 'idle': the first setStage('idle') has to actually paint. Initialising
  // this to 'idle' made it a no-op, so the badge sat on its placeholder for ever.
  let stage = null;
  function setStage(name) {
    if (name === stage) return;          // a lagging link can repeat a state
    const first = stage === null;
    stage = name;
    const info = STAGE[name] || STAGE.idle;
    stateText.textContent = name;
    stateDot.style.background = info.colour;
    stateEl.style.color = info.colour;
    if (!first) log(info.text, info.colour, { glyph: info.glyph, strong: true });
  }

  /** The badge shows the link when there is no orchestrator to have a stage. */
  function setOffline() {
    stage = null;
    stateText.textContent = 'offline';
    stateDot.style.background = 'var(--danger)';
    stateEl.style.color = 'var(--danger)';
  }

  /* Each panel's own scroller, found once. `parentElement` happened to be it, which was
     true until the panel grew a legend. */
  const scroller = (node) => node.closest('.panel-body') || node.parentElement;
  const stick = (node) => {
    const box = scroller(node);
    if (box) box.scrollTop = box.scrollHeight;
  };

  /* ---- one answering loop, one block -------------------------------------------
   *
   * A turn's events go into their own bordered group; anything that belongs to no turn
   * goes outside them. Filing a health transition under whichever turn happened to be
   * open would be a lie about causation, and that is exactly what someone reading this
   * page is trying to establish.
   */
  let loopBox = null;
  let loopCount = 0;

  function openLoop(text) {
    loopCount += 1;
    const head = el('div', { class: 'loop-head' },
      el('span', { text: `turn ${loopCount}` }),
      el('span', { class: 'what', text: text || '' }),
      el('span', { class: 'ms', text: clock() }));
    loopBox = el('div', { class: 'loop live' }, head);
    activity.append(loopBox);
    trimEvents();
    return loopBox;
  }

  function closeLoop(ms) {
    if (!loopBox) return;
    loopBox.classList.remove('live');
    loopBox.classList.add('done');
    const head = loopBox.querySelector('.loop-head');
    const stamp = head && head.querySelector('.ms');
    if (stamp && ms) stamp.textContent = `${Math.round(ms).toLocaleString()} ms`;
    loopBox = null;
  }

  /* Trim whole loops, not individual lines: removing the first few events of the oldest
     turn would leave a block whose header describes something you can no longer see. */
  const MAX_LOOPS = 40;
  function trimEvents() {
    const loops = activity.querySelectorAll('.loop');
    for (let i = 0; i < loops.length - MAX_LOOPS; i += 1) loops[i].remove();
  }

  const MAX_EVENTS = 400;
  function log(text, colour, opts = {}) {
    if (opts.wire && !showWire.checked) return;
    const node = el('div', { class: `event${opts.strong ? ' strong' : ''}${opts.wire ? ' wire' : ''}${opts.bad ? ' bad' : ''}` },
      el('time', { text: clock() }),
      // A glyph as well as a hue. Three phase colours cannot name five stages on their
      // own, and under deuteranopia two of them would not be distinguishable at all.
      opts.glyph
        ? el('span', { class: 'eglyph', style: colour ? `color:${colour}` : '',
                       text: opts.glyph })
        : el('span', { class: 'edot', style: colour ? `background:${colour}` : '' }),
      el('span', { class: 'etext', text }),
    );
    // Into the open turn when there is one, and outside the blocks when there is not.
    (opts.background ? backgroundBox() : (loopBox || backgroundBox())).append(node);
    stick(activity);
  }

  /** A holder for events that belong to no turn, kept at the end of the list. */
  function backgroundBox() {
    let box = activity.lastElementChild;
    if (!box || !box.classList.contains('bg-events')) {
      box = el('div', { class: 'bg-events' });
      activity.append(box);
    }
    return box;
  }

  /* This page is the ROBOT AS IT SHIPS: hands-free, spoken, no keyboard. Turns typed
     into /chat are answered silently and do not belong in this log -- seeing them here
     would suggest the robot said something it never said out loud. */
  const spoken = (message) => (message.source || 'voice') === 'voice';

  let openReply = null;
  let idleBlock = null;      // the one idle Reply JSON on screen; a new one replaces it
  function addTurn(who, text, lang) {
    const node = el('div', { class: `turn ${who}` },
      el('span', { class: 'who' }, who === 'you' ? 'You' : 'Robot',
        lang ? el('span', { class: 'lang', text: LANGS[lang] || lang }) : null),
      text || '');
    conversation.append(node);
    stick(conversation);
    return node;
  }

  function recap(m) {
    const rows = Robot.turnStages(m);
    if (!rows.length) return null;
    // The slowest of the DURATIONS; an absolute marker is not a duration and cannot win.
    const durations = rows.filter((r) => !r.absolute);
    const worst = durations.length
      ? durations.reduce((a, b) => (b.ms > a.ms ? b : a)) : null;
    const scale = worst ? Math.max(worst.ms, 1) : 1;

    const grid = el('div', { class: 'stages' });
    for (const row of rows) {
      const slow = row === worst ? ' slow' : '';
      grid.append(
        el('span', { class: `sn${slow}`, text: row.name }),
        el('span', { class: `sb${slow}` },
          el('i', { style: `width:${Math.round((row.ms / scale) * 100)}%` })),
        el('span', { class: `sv${slow}`,
                     text: `${Math.round(row.ms).toLocaleString()} ms` }));
    }
    const heard = m.first_audio_ms ? `${Math.round(m.first_audio_ms).toLocaleString()} ms` : '—';
    const done = m.finished_ms ? `${Math.round(m.finished_ms).toLocaleString()} ms` : '—';
    const note = [];
    if (worst) note.push(`slowest: ${worst.name} (${worst.why})`);
    if (m.truncated) note.push('reply hit its token budget and was cut off');
    if (m.barged_in) note.push('interrupted');
    if (m.timed_out) note.push('timed out');
    return el('details', { class: 'recap' },
      el('summary', {}, 'Timing',
        el('span', { class: 'total', text: `first sound ${heard} · finished ${done}` })),
      grid,
      note.length ? el('div', { class: 'note', text: note.join(' · ') }) : null);
  }

  function showBanner(text, kind) {
    banner.textContent = text;
    banner.className = kind;
  }
  const hideBanner = () => { banner.className = ''; banner.textContent = ''; };

  document.getElementById('clear-chat').addEventListener('click', () => {
    conversation.textContent = ''; openReply = null;
  });
  document.getElementById('clear-activity').addEventListener('click', () => {
    activity.textContent = '';
    loopBox = null;
    loopCount = 0;
  });

  /* Services whose state we have already reported, so the log records CHANGES rather
     than repeating the same health snapshot every three seconds. */
  const lastHealth = {};

  Robot.on((message) => {
    if (message.turn_id && !spoken(message)) return;
    switch (message.type) {
      case '_link':
        if (message.up) {
          hideBanner();
          log('Dashboard connected', 'var(--success)');
          setStage('idle');       // the robot has a stage again
        }
        else {
          showBanner('Dashboard lost its link to the orchestrator. The robot is still '
                     + 'running — this page is optional.', 'error');
          log('Dashboard disconnected', 'var(--danger)', { bad: true });
          setOffline();
        }
        break;

      case 'level': case 'log': break;   // their own pages; not activity
      case 'speech_start':
        // Open the block here, so hearing and transcribing sit inside the loop they
        // belong to rather than above it.
        if (!loopBox) openLoop('listening…');
        setStage('hearing');
        break;
      case 'speech_end':
        setStage('transcribing');
        if (message.audio_s) log(`Heard ${message.audio_s.toFixed(1)}s of speech`, 'var(--info)');
        break;

      case 'transcript': {
        openReply = null;
        if (idleBlock) { idleBlock.remove(); idleBlock = null; }
        // A typed turn has no speech_start, so the block is opened here instead.
        if (!loopBox) openLoop(message.text);
        else {
          const what = loopBox.querySelector('.loop-head .what');
          if (what) what.textContent = message.text || '';
        }
        addTurn('you', message.text);
        log(`Heard: “${message.text}”`, 'var(--info)', { strong: true });
        setStage('retrieving');
        break;
      }

      case 'language':
        log(`Replying in ${LANGS[message.language] || message.language} `
            + `(${message.language_source})`,
            ['declared', 'text', 'script', 'asr'].includes(message.language_source)
              ? 'var(--success)' : 'var(--warning)');
        if (!openReply) openReply = addTurn('robot', '', message.language);
        setStage('speaking');
        break;

      case 'chunk':
        if (!openReply) openReply = addTurn('robot', '');
        openReply.append(document.createTextNode(` ${message.text}`));
        stick(conversation);
        setStage('speaking');
        break;

      case 'reply_fields':
        // Everything the model decided this turn, of which only `answer` was spoken.
        if (openReply) openReply.append(Robot.replyFields(message));
        else if (message.phase) {
          // No turn, so no reply to attach to: the idle face stands on its own -- once.
          if (idleBlock) idleBlock.remove();
          idleBlock = el('div', { class: 'idle-json' }, Robot.replyFields(message));
          conversation.append(idleBlock);
        }
        stick(conversation);
        break;

      case 'serial_tx':
        // The literal line sent to the microcontroller.
        log(`→ ${message.line}`, 'var(--warning)', { wire: true });
        break;

      case 'tool_result':
        log(`${message.tool}: ${message.note}`, 'var(--danger)', { bad: true });
        break;

      case 'serial_error':
        log(`Body refused ${message.verb}: ${message.reason}`, 'var(--danger)', { bad: true });
        break;
      case 'serial_status':
        if (message.motors === 'stall') log('A wheel is stalled', 'var(--danger)', { bad: true });
        break;
      case 'serial_up':
        log('Body connected', 'var(--success)', { background: true }); break;
      case 'serial_down':
        log('Body disconnected', 'var(--danger)', { bad: true, background: true }); break;

      case 'turn_timeout':
        log(`Turn abandoned after ${message.after_s}s`, 'var(--danger)', { bad: true });
        closeLoop();
        setStage('idle');
        break;

      case 'turn_done': {
        const bits = [];
        if (message.first_audio_ms) bits.push(`spoke after ${message.first_audio_ms} ms`);
        if (message.retrieved) bits.push(`${message.retrieved} notes`);
        if (message.tokens) bits.push(`${message.tokens} tokens`);
        if (message.barged_in) bits.push('interrupted');
        log(`Turn finished${bits.length ? ' — ' + bits.join(', ') : ''}`, 'var(--text-faint)');
        // The breakdown goes under the reply it describes, not into the activity log --
        // "which part was slow" is a question about one turn, and it should be readable
        // next to what that turn said.
        if (spoken(message)) {
          const card = recap(message);
          if (card) {
            if (openReply) openReply.append(card);
            else { conversation.append(card); }
            stick(conversation);
          }
        }
        openReply = null;
        closeLoop(message.finished_ms);
        setStage('idle');
        break;
      }

      case 'ingesting':
        log(`Ingesting ${message.source}…`, 'var(--st-think)', { background: true }); break;
      case 'ingested':
        log(`Stored ${message.chunks} chunk(s) from ${message.source}`,
            'var(--success)', { background: true });
        break;
      case 'ingest_failed':
        log(`Ingest failed for ${message.source}: ${message.reason}`,
            'var(--danger)', { bad: true, background: true });
        break;

      case 'health':
        for (const svc of message.services || []) {
          if (lastHealth[svc.name] === undefined) { lastHealth[svc.name] = svc.state; continue; }
          if (lastHealth[svc.name] === svc.state) continue;
          lastHealth[svc.name] = svc.state;
          const bad = svc.state === 'down';
          log(`${svc.label} is now ${svc.state}`,
              bad ? 'var(--danger)' : svc.state === 'up' ? 'var(--success)' : 'var(--warning)',
              { bad, strong: true, background: true });
        }
        break;
    }
  });

  setOffline();          // until the socket opens, say so plainly
  Robot.connect();
})();
