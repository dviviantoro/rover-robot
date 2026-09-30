(() => {
  const { el } = Robot;
  const messages = document.getElementById('messages');
  const input = document.getElementById('chat-input');
  const form = document.getElementById('composer');
  const liveDot = document.getElementById('live-dot');
  const liveText = document.getElementById('live-text');
  const LANGS = { en: 'English', id: 'Bahasa Indonesia', ar: 'Arabic' };

  let openReply = null;   // the robot turn currently being streamed into

  /* This page is the KEYBOARD. Voice turns belong to /headless and are not shown here.
   *
   * Every turn event carries the modality that started it, so a question asked out loud
   * in the same room does not scroll past while you are typing -- and, more to the
   * point, what you type here is answered silently. See Orchestrator.start_turn. */
  const mine = (message) => (message.source || 'text') === 'text';

  const scroll = () => window.scrollTo({ top: document.body.scrollHeight, behavior: 'smooth' });

  function addTurn(who, text, lang) {
    const node = el('div', { class: `turn ${who}` },
      el('span', { class: 'who' }, who === 'you' ? 'You' : 'Robot',
        lang ? el('span', { class: 'lang', text: LANGS[lang] || lang }) : null),
      text || '');
    messages.append(node);
    scroll();
    return node;
  }

  function setLive(up) {
    liveDot.className = `dot ${up ? 'live' : 'dead'}`;
    liveText.textContent = up ? 'connected' : 'disconnected — the robot keeps running';
  }

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    input.style.height = 'auto';
    if (!Robot.send({ type: 'say', text })) {
      messages.append(el('div', { class: 'note', text: 'Not connected to the orchestrator.' }));
    }
  });

  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });

  document.getElementById('interrupt').addEventListener('click', () => Robot.send({ type: 'cancel' }));
  document.getElementById('clear').addEventListener('click', () => { messages.textContent = ''; openReply = null; });

  Robot.on((message) => {
    if (message.turn_id && !mine(message)) return;
    switch (message.type) {
      case '_link': setLive(message.up); break;
      case 'transcript':
        openReply = null;
        addTurn('you', message.text);
        break;
      case 'language':
        // The robot decides the reply language itself; show which and how it decided,
        // because "declared" (the model said so) and a fallback are not the same thing.
        if (!openReply) openReply = addTurn('robot', '', message.language);
        else openReply.querySelector('.who').append(
          el('span', { class: 'lang', text: LANGS[message.language] || message.language }));
        break;
      case 'chunk':
        if (!openReply) openReply = addTurn('robot', '');
        openReply.append(document.createTextNode(` ${message.text}`));
        scroll();
        break;
      case 'serial_tx':
        // The literal line sent to the STM32, not a reconstruction of it.
        messages.append(el('div', { class: 'wire', text: `→ ${message.line}` }));
        scroll();
        break;
      case 'tool_result':
        messages.append(el('div', { class: 'note', text: `${message.tool}: ${message.note}` }));
        break;
      case 'reply_fields':
        // The whole object the model produced, of which only `answer` was ever
        // spoken. Attached to the reply it belongs to, collapsed.
        if (openReply) openReply.append(Robot.replyFields(message));
        scroll();
        break;
      case 'turn_timeout':
        messages.append(el('div', { class: 'note',
          text: `The robot gave up on that turn after ${message.after_s}s.` }));
        openReply = null;
        break;
      case 'turn_done': {
        const bits = [];
        // Nothing is spoken on this page, so report when the words appeared, not when
        // audio started -- there is no audio, and `first_audio_ms` is always null.
        if (message.first_chunk_ms) bits.push(`answered after ${message.first_chunk_ms} ms`);
        if (message.retrieved) bits.push(`${message.retrieved} notes used`);
        if (message.tools) bits.push(`${message.tools} action${message.tools > 1 ? 's' : ''}`);
        if (message.language_source && message.language_source !== 'declared') {
          bits.push(`language via ${message.language_source}`);
        }
        if (message.barged_in) bits.push('interrupted');
        if (bits.length) messages.append(el('div', { class: 'meta', text: bits.join(' · ') }));
        openReply = null;
        scroll();
        break;
      }
    }
  });

  setLive(false);
  Robot.connect();
})();
