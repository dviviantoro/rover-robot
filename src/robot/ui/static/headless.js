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
    idle:        { text: 'Idle — waiting for you to speak', colour: 'var(--text-faint)' },
    hearing:     { text: 'Hearing speech', colour: 'var(--info)' },
    transcribing:{ text: 'Transcribing what you said', colour: 'var(--info)' },
    retrieving:  { text: 'Looking through what it knows', colour: '#a855f7' },
    thinking:    { text: 'Thinking — generating a reply', colour: 'var(--warning)' },
    speaking:    { text: 'Speaking', colour: 'var(--success)' },
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
    if (!first) log(info.text, info.colour);   // don't log the initial paint
  }

  /** The badge shows the link when there is no orchestrator to have a stage. */
  function setOffline() {
    stage = null;
    stateText.textContent = 'offline';
    stateDot.style.background = 'var(--danger)';
    stateEl.style.color = 'var(--danger)';
  }

  const MAX_EVENTS = 400;
  function log(text, colour, opts = {}) {
    if (opts.wire && !showWire.checked) return;
    const node = el('div', { class: `event${opts.strong ? ' strong' : ''}${opts.wire ? ' wire' : ''}${opts.bad ? ' bad' : ''}` },
      el('time', { text: clock() }),
      el('span', { class: 'edot', style: colour ? `background:${colour}` : '' }),
      el('span', { class: 'etext', text }),
    );
    activity.append(node);
    while (activity.children.length > MAX_EVENTS) activity.firstChild.remove();
    activity.parentElement.scrollTop = activity.parentElement.scrollHeight;
  }

  /* This page is the ROBOT AS IT SHIPS: hands-free, spoken, no keyboard. Turns typed
     into /chat are answered silently and do not belong in this log -- seeing them here
     would suggest the robot said something it never said out loud. */
  const spoken = (message) => (message.source || 'voice') === 'voice';

  let openReply = null;
  function addTurn(who, text, lang) {
    const node = el('div', { class: `turn ${who}` },
      el('span', { class: 'who' }, who === 'you' ? 'You' : 'Robot',
        lang ? el('span', { class: 'lang', text: LANGS[lang] || lang }) : null),
      text || '');
    conversation.append(node);
    conversation.parentElement.scrollTop = conversation.parentElement.scrollHeight;
    return node;
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
      case 'speech_start': setStage('hearing'); break;
      case 'speech_end':
        setStage('transcribing');
        if (message.audio_s) log(`Heard ${message.audio_s.toFixed(1)}s of speech`, 'var(--info)');
        break;

      case 'transcript':
        openReply = null;
        addTurn('you', message.text);
        log(`Heard: “${message.text}”`, 'var(--info)', { strong: true });
        setStage('retrieving');
        break;

      case 'language':
        log(`Replying in ${LANGS[message.language] || message.language} `
            + `(${message.language_source})`,
            message.language_source === 'declared' ? 'var(--success)' : 'var(--warning)');
        if (!openReply) openReply = addTurn('robot', '', message.language);
        setStage('speaking');
        break;

      case 'chunk':
        if (!openReply) openReply = addTurn('robot', '');
        openReply.append(document.createTextNode(` ${message.text}`));
        conversation.parentElement.scrollTop = conversation.parentElement.scrollHeight;
        setStage('speaking');
        break;

      case 'reply_fields':
        // Everything the model decided this turn, of which only `answer` was spoken.
        if (openReply) openReply.append(Robot.replyFields(message));
        conversation.parentElement.scrollTop = conversation.parentElement.scrollHeight;
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
      case 'serial_up': log('Body connected', 'var(--success)'); break;
      case 'serial_down': log('Body disconnected', 'var(--danger)', { bad: true }); break;

      case 'turn_timeout':
        log(`Turn abandoned after ${message.after_s}s`, 'var(--danger)', { bad: true });
        setStage('idle');
        break;

      case 'turn_done': {
        const bits = [];
        if (message.first_audio_ms) bits.push(`spoke after ${message.first_audio_ms} ms`);
        if (message.retrieved) bits.push(`${message.retrieved} notes`);
        if (message.tokens) bits.push(`${message.tokens} tokens`);
        if (message.barged_in) bits.push('interrupted');
        log(`Turn finished${bits.length ? ' — ' + bits.join(', ') : ''}`, 'var(--text-faint)');
        openReply = null;
        setStage('idle');
        break;
      }

      case 'ingesting': log(`Ingesting ${message.source}…`, '#a855f7'); break;
      case 'ingested':
        log(`Stored ${message.chunks} chunk(s) from ${message.source}`, 'var(--success)');
        break;
      case 'ingest_failed':
        log(`Ingest failed for ${message.source}: ${message.reason}`, 'var(--danger)', { bad: true });
        break;

      case 'health':
        for (const svc of message.services || []) {
          if (lastHealth[svc.name] === undefined) { lastHealth[svc.name] = svc.state; continue; }
          if (lastHealth[svc.name] === svc.state) continue;
          lastHealth[svc.name] = svc.state;
          const bad = svc.state === 'down';
          log(`${svc.label} is now ${svc.state}`,
              bad ? 'var(--danger)' : svc.state === 'up' ? 'var(--success)' : 'var(--warning)',
              { bad, strong: true });
        }
        break;
    }
  });

  setOffline();          // until the socket opens, say so plainly
  Robot.connect();
})();
