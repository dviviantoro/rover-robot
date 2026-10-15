(() => {
  const { el, HEALTH } = Robot;
  const tabsEl = document.getElementById('tabs');
  const panelEl = document.getElementById('panel');
  const liveDot = document.getElementById('live-dot');
  const liveText = document.getElementById('live-text');
  const toastEl = document.getElementById('toast');

  let currentGroup = null;
  const staged = {};        // key -> value edited but not yet applied
  const inputs = {};        // key -> input element

  let toastTimer = null;
  const ui = (t, k) => toast(t, k);
  function toast(text, kind = '') {
    toastEl.textContent = text;
    toastEl.className = `show ${kind}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toastEl.className = ''; }, 4500);
  }

  function setLive(up) {
    liveDot.className = `dot ${up ? 'live' : 'dead'}`;
    liveText.textContent = up ? 'connected' : 'disconnected';
  }

  function widgetFor(setting, value) {
    // Controls you nudge while watching or listening are sliders, with the number beside
    // them following the cursor. Range and step come from the registry, so the slider can
    // never offer a value the backend would refuse.
    const SLIDERS = { tts_volume: '%', mic_gain: '%', asr_noise_gate_db: ' dB' };
    if (setting.key in SLIDERS) {
      const unit = SLIDERS[setting.key];
      const range = el('input', {
        type: 'range', class: 'slider', min: setting.minimum ?? 0, max: setting.maximum ?? 100,
        step: setting.step ?? 1, value: value ?? setting.default ?? 0 });
      const read = el('span', { class: 'hint slider-read' });
      const paint = () => {
        const loud = unit === '%' && Number(range.value) > 100;
        read.textContent = `${range.value}${unit}` + (loud ? '  (amplifying — may clip)' : '');
      };
      range.addEventListener('input', paint);
      paint();
      const wrap = el('div', { class: 'slider-wrap' }, range, read);
      // The row code reads .value off whatever this returns.
      Object.defineProperty(wrap, 'value', {
        get: () => range.value,
        set: (v) => { range.value = v; paint(); },
      });
      wrap.addEventListener = range.addEventListener.bind(range);
      wrap.isSlider = true;
      Object.defineProperty(wrap, 'disabled', {
        get: () => range.disabled, set: (v) => { range.disabled = v; },
      });
      return wrap;
    }
    if (setting.type === 'number') {
      return el('input', {
        type: 'number', value: value ?? '', step: setting.step ?? 'any',
        min: setting.minimum ?? '', max: setting.maximum ?? '',
      });
    }
    if (setting.type === 'textarea') {
      return el('textarea', { rows: '4' }, value ?? '');
    }
    if (setting.type === 'select' && (setting.options || []).length) {
      const sel = el('select', {});
      for (const option of setting.options) {
        sel.append(el('option', { value: option, selected: option === value ? '' : null }, option));
      }
      return sel;
    }
    return el('input', { type: 'text', value: value ?? '' });
  }

  function serviceState(component) {
    const svc = Robot.state.services.find((s) => s.name === component);
    return svc ? svc.state : 'unknown';
  }

  /* Which settings this page owns. Choosing a model belongs on the Memory page, next
     to what it costs against the ceiling -- offering the same choice here, with none of
     that context, gave two places to change one value and two places to disagree. The
     registry says which page owns each setting; this page renders only its own. */
  const mine = () => Robot.state.settings.filter((s) => (s.page || 'config') === 'config');

  function renderTabs() {
    tabsEl.textContent = '';
    for (const group of Robot.state.groups) {
      const count = mine().filter((s) => s.group === group.key).length;
      if (!count) continue;
      tabsEl.append(el('button', {
        class: group.key === currentGroup ? 'current' : '',
        onclick: () => { currentGroup = group.key; renderTabs(); renderPanel(); },
      }, group.label));
    }
  }

  function renderPanel() {
    panelEl.textContent = '';
    const group = Robot.state.groups.find((g) => g.key === currentGroup);
    const settings = mine().filter((s) => s.group === currentGroup);
    if (!group || !settings.length) {
      panelEl.append(el('p', { class: 'muted', text: 'Nothing here.' }));
      return;
    }

    if (currentGroup === 'listening') panelEl.append(meterCard());
    if (currentGroup === 'hardware') panelEl.append(pingCard());
    const elsewhere = Robot.state.settings.filter(
      (s) => s.group === currentGroup && (s.page || 'config') !== 'config');
    if (elsewhere.length) {
      panelEl.append(el('div', { class: 'kind-note' },
        el('span', { class: 'badge badge-info', text: 'memory' }),
        `${elsewhere.map((s) => s.label).join(' and ')} `
        + `${elsewhere.length > 1 ? 'are' : 'is'} chosen on `,
        el('a', { href: '/memory' }, 'Memory'),
        ', which prices the choice against the 7620 MiB ceiling first and applies it '
        + 'for you — including the restart.'));
    }

    const hot = settings.filter((s) => s.kind === 'hot');
    const restart = settings.filter((s) => s.kind === 'restart');
    panelEl.append(el('p', { class: 'group-note', text: group.blurb }));

    if (hot.length) {
      const fs = el('fieldset', {}, el('legend', { text: 'Applies immediately' }));
      fs.append(el('div', { class: 'kind-note' },
        el('span', { class: 'badge badge-success', text: 'live' }),
        'Changes take effect on the next thing the robot says. Nothing here needs a restart.'));
      hot.forEach((s) => fs.append(settingRow(s)));
      panelEl.append(fs);
    }
    if (restart.length) {
      const fs = el('fieldset', {}, el('legend', { text: 'Needs a restart' }));
      fs.append(el('div', { class: 'kind-note' },
        el('span', { class: 'badge badge-warning', text: 'staged' }),
        'Editing a field here only stages the value. Press its button to restart that '
        + 'one component and apply it.'));
      restart.forEach((s) => fs.append(settingRow(s)));
      panelEl.append(fs);
    }
  }

  /* ---------- ping every serial port ----------
     Sends PING to each serial device that exists, not just the configured one. The port
     that answers OK is the body; the rest are something else on the same bus. */
  let pingOut = null;
  function pingCard() {
    const out = el('div', { class: 'ping-results muted', text: 'Not run yet.' });
    pingOut = out;
    const button = el('button', { type: 'button', class: 'btn-secondary', text: 'Ping all ports' });
    button.onclick = () => {
      out.textContent = 'Pinging…';
      if (!Robot.send({ type: 'ping_ports' })) {
        out.textContent = 'Not connected to the orchestrator.';
      }
    };
    return el('fieldset', { class: 'ping-card' },
      el('legend', { text: 'Find the body' }),
      el('div', { class: 'kind-note' },
        el('span', { class: 'badge badge-info', text: 'ping' }),
        'Sends PING to every serial port on the board and shows which ones answer.'),
      button, out);
  }

  function showPing(results) {
    if (!pingOut) return;
    pingOut.textContent = '';
    if (!results.length) {
      pingOut.textContent = 'No serial ports found.';
      return;
    }
    results.forEach((r) => {
      pingOut.append(el('div', { class: 'ping-row' },
        el('span', { class: 'badge ' + (r.ok ? 'badge-success' : 'badge-neutral'),
                     text: r.ok ? 'answered' : 'silent' }),
        ` ${r.port} `,
        el('span', { class: 'muted',
                     text: r.ok ? `${r.reply} · ${r.ms} ms` : (r.error || r.reply || '') })));
    });
  }

  /* ---------- live input meter ----------
     The VAD threshold is a Silero probability, not a level in dB, so it is not drawn
     directly. The meter shows the room -- where the noise floor sits, how far speech
     rises above it, whether the VAD is triggering -- and the knob on it (below) sets the
     trigger. Tune by watching the pill: if it says SPEECH when nobody is talking, the
     trigger is too sensitive for this room. */
  let meterEls = null;
  function meterCard() {
    const fill = el('div', { class: 'fill' });
    const peak = el('div', { class: 'peak' });
    const floor = el('div', { class: 'floor' });
    const pill = el('span', { class: 'badge badge-neutral speech-pill', text: 'no signal' });
    const stats = el('div', { class: 'meter-stats' });
    const knob = el('div', { class: 'knob', tabindex: '0', role: 'slider',
                             'aria-label': 'Trigger threshold', 'aria-valuemin': '-60',
                             'aria-valuemax': '0' });
    const tip = el('span', { class: 'tip' });
    knob.append(tip);
    meterEls = { fill, peak, floor, pill, stats, knob, tip };
    bindKnob(knob);

    const card = el('fieldset', { class: 'meter-card' },
      el('legend', { text: 'Microphone level' }),
      el('div', { class: 'kind-note' },
        el('span', { class: 'badge badge-info', text: 'live' }),
        'What the robot is hearing right now. Raise the threshold until the pill stops '
        + 'saying SPEECH when the room is quiet, then check it still catches you talking.'),
      el('div', { class: 'meter-row' },
        el('div', { class: 'meter' }, fill, floor, peak, knob), pill),
      el('div', { class: 'meter-scale' },
        el('span', { text: '-60 dB' }), el('span', { text: '-40' }),
        el('span', { text: '-20' }), el('span', { text: '0' })),
      stats,
    );
    paintMeter(Robot.state.level);
    return card;
  }

  const METER_FLOOR_DB = -60;
  const toPercent = (db) => Math.max(0, Math.min(100,
    ((Math.max(db, METER_FLOOR_DB) - METER_FLOOR_DB) / -METER_FLOOR_DB) * 100));

  /* ---------- the trigger knob ----------
     What it does: sets how loud a sound has to be, above the room's noise floor, before
     the robot treats it as someone talking to it. Left (towards -60 dB) is sensitive --
     a fan or an air conditioner can start a turn. Right (towards 0 dB) is stricter --
     only clear, close speech does.

     One handle, two settings, because "less easily triggered" is one wish:
       asr_noise_gate_db   dB over the floor an utterance must reach (0-30)
       vad_threshold       how readily the voice detector fires (0.40-0.90 here)
     The knob's position is floor + gate margin in dBFS, so it follows the floor when the
     room changes while the margin is what is stored. With Auto trigger on, the robot
     chooses both and the knob only shows them. */
  const GATE = 'asr_noise_gate_db';
  const VAD = 'vad_threshold';
  const GATE_MAX = 30;
  const SEND_EVERY_MS = 150;      // while dragging; the release always sends the final value
  const HOLD_MS = 2000;

  // `held` is the margin the person last chose, and while it is set it is the ONLY thing
  // the knob is drawn from. Replies to the sends made during a drag arrive later and out
  // of date; each used to write its older value back and the knob jumped to where the
  // drag had been 150 ms earlier. Released when the backend confirms the same value, or
  // after HOLD_MS with nothing contradicting it.
  const trigger = { dragging: false, held: null, timer: null };

  const liveLevel = () => Robot.state.level || {};
  const isAuto = () => !!liveLevel().auto;
  const floorDb = () => (Number.isFinite(liveLevel().floor_dbfs)
    ? liveLevel().floor_dbfs : METER_FLOOR_DB);
  const clampMargin = (m) => Math.max(0, Math.min(GATE_MAX, m));

  // Further right is stricter: 0 dB over the floor is 0.40, 30 dB is 0.90, in the 0.05
  // steps the setting takes. The default margin of 6 dB lands on the default 0.50.
  const sensitivityFor = (margin) =>
    Math.round((0.4 + (margin / GATE_MAX) * 0.5) * 20) / 20;

  function currentMargin() {
    if (isAuto() && liveLevel().gate_db !== undefined) return liveLevel().gate_db;
    if (trigger.held !== null) return trigger.held;
    return Number(Robot.state.values[GATE] ?? 6) || 0;
  }

  function holdMargin(margin) {
    trigger.held = margin;
    clearTimeout(trigger.timer);
    trigger.timer = setTimeout(() => {
      // Uncontradicted for HOLD_MS, so it is the value. Store it, or a stale `values`
      // broadcast would pull the knob back the moment the hold lets go.
      if (trigger.held !== null) Robot.state.values[GATE] = trigger.held;
      releaseGateHold();
      paintKnob();
    }, HOLD_MS);
  }

  function releaseGateHold() {
    trigger.held = null;
    clearTimeout(trigger.timer);
  }

  // From the set_result handler: may this reply touch the UI? One for a value the person
  // has already moved past is dropped; the one matching their choice confirms it.
  function acceptGateReply(value) {
    if (trigger.held === null) return true;
    if (Number(value) !== trigger.held) return false;
    releaseGateHold();
    return true;
  }

  function paintKnob(margin = currentMargin()) {
    if (!meterEls) return;
    const at = Math.max(METER_FLOOR_DB, Math.min(0, floorDb() + margin));
    const auto = isAuto();
    const sens = auto && liveLevel().threshold !== undefined
      ? liveLevel().threshold : sensitivityFor(margin);
    meterEls.knob.style.left = `${toPercent(at)}%`;
    meterEls.knob.classList.toggle('locked', auto);
    meterEls.knob.setAttribute('aria-valuenow', at.toFixed(0));
    meterEls.tip.textContent =
      `${at.toFixed(0)} dB · sens ${sens.toFixed(2)}${auto ? ' · auto' : ''}`;
    const slider = inputs[GATE];
    if (slider && !trigger.dragging) slider.value = margin;
  }

  function sendTrigger(margin) {
    apply(GATE, margin);
    const sens = sensitivityFor(margin);
    apply(VAD, sens);
    const w = inputs[VAD];
    if (w && document.activeElement !== w) w.value = sens;
  }

  // The one path every gesture takes: pin it, draw it, and (optionally) send it.
  function chooseMargin(margin, send) {
    holdMargin(margin);
    paintKnob(margin);
    const slider = inputs[GATE];
    if (slider) slider.value = margin;
    if (send) sendTrigger(margin);
  }

  function bindKnob(knob) {
    const meter = () => knob.parentElement;
    let lastSent = 0;

    const marginAt = (clientX) => {
      const r = meter().getBoundingClientRect();
      const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
      const dbfs = METER_FLOOR_DB + frac * -METER_FLOOR_DB;
      return clampMargin(Math.round(dbfs - floorDb()));
    };

    knob.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      if (isAuto()) return;                       // Auto trigger owns it
      trigger.dragging = true;
      knob.setPointerCapture(e.pointerId);
    });
    knob.addEventListener('pointermove', (e) => {
      if (!trigger.dragging) return;
      const now = performance.now();
      const due = now - lastSent > SEND_EVERY_MS;
      if (due) lastSent = now;
      chooseMargin(marginAt(e.clientX), due);     // live, but not on every pixel
    });
    knob.addEventListener('pointerup', (e) => {
      if (!trigger.dragging) return;
      trigger.dragging = false;
      knob.releasePointerCapture?.(e.pointerId);
      chooseMargin(marginAt(e.clientX), true);    // the final value, always sent
    });
    knob.addEventListener('pointercancel', () => { trigger.dragging = false; paintKnob(); });
    knob.addEventListener('lostpointercapture', () => { trigger.dragging = false; });
    knob.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step || isAuto()) return;
      e.preventDefault();
      chooseMargin(clampMargin(currentMargin() + step), true);
    });
  }

  function paintMeter(level) {
    if (!meterEls || !level || level.dbfs === undefined) return;
    meterEls.fill.style.width = `${toPercent(level.dbfs)}%`;
    meterEls.peak.style.left = `${toPercent(level.peak_dbfs)}%`;
    meterEls.floor.style.left = `${toPercent(level.floor_dbfs)}%`;
    if (!trigger.dragging) paintKnob();
    meterEls.pill.className = `badge speech-pill ${level.speech ? 'badge-success' : 'badge-neutral'}`;
    meterEls.pill.textContent = level.speech ? 'SPEECH' : 'quiet';
    const headroom = level.peak_dbfs - level.floor_dbfs;
    meterEls.stats.textContent = '';
    meterEls.stats.append(
      el('span', {}, 'now ', el('b', { text: `${level.dbfs.toFixed(0)} dB` })),
      el('span', {}, 'peak ', el('b', { text: `${level.peak_dbfs.toFixed(0)} dB` })),
      el('span', {}, 'noise floor ', el('b', { text: `${level.floor_dbfs.toFixed(0)} dB` })),
      el('span', {}, 'speech above floor ', el('b', { text: `${headroom.toFixed(0)} dB` })),
    );
  }

  function settingRow(setting) {
    const value = Robot.state.values[setting.key];
    const widget = widgetFor(setting, value);
    inputs[setting.key] = widget;

    const row = el('div', { class: 'setting', 'data-key': setting.key },
      el('div', {},
        el('div', { class: 'name', text: setting.label }),
        setting.help ? el('div', { class: 'help', text: setting.help }) : null,
        setting.enabled ? null : el('div', { class: 'disabled-why', text: setting.disabled_reason }),
      ),
      el('div', {}, widget,
        el('div', { class: 'staged', text: 'staged — press the button to apply' })),
    );

    if (!setting.enabled) {
      widget.disabled = true;
    } else if (setting.kind === 'hot') {
      // Commit on blur / Enter so a half-typed number never applies mid-keystroke.
      const commit = () => apply(setting.key, widget.value);
      widget.addEventListener('blur', commit);
      // A slider is committed when released, not only when it loses focus.
      if (widget.isSlider) widget.addEventListener('change', commit);
      widget.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && setting.type !== 'textarea') { e.preventDefault(); widget.blur(); }
      });
    } else {
      widget.addEventListener('input', () => {
        staged[setting.key] = widget.value;
        row.classList.add('is-staged');
      });
    }

    // A voice you cannot hear is a name in a dropdown. Preview plays a sample line in
    // the selected voice without adopting it. One button per language the control can
    // be auditioned in -- Supertonic speaks all three with one voice, and hearing it in
    // English tells you very little about how it reads Arabic.
    const langs = setting.preview_langs || [];
    if (langs.length) {
      const buttons = el('div', { class: 'restart-col' });
      for (const lang of langs) {
        buttons.append(el('button', {
          class: 'btn-secondary',
          disabled: setting.enabled ? null : '',
          onclick: (event) => {
            const button = event.currentTarget;
            const voice = inputs[setting.key]?.value ?? widget.value;
            if (!Robot.send({ type: 'preview_voice', lang, voice })) {
              ui('Not connected to the orchestrator.', 'bad');
              return;
            }
            const label = button.textContent;
            button.disabled = true;
            button.textContent = 'Playing…';
            setTimeout(() => { button.disabled = false; button.textContent = label; }, 6000);
          },
        }, langs.length > 1 ? `Preview ${lang}` : 'Preview'));
      }
      row.append(buttons);
      return row;
    }

    if (setting.kind === 'restart') {
      const state = serviceState(setting.component);
      const look = HEALTH[state] || HEALTH.unknown;
      const lamp = el('span', { class: 'lamp', style: `background:${look.colour}`,
                                title: `${setting.component}: ${state}` });
      const button = el('button', {
        class: 'btn-warn',
        onclick: () => restartComponent(setting),
      }, `Restart ${setting.component}`);
      if (state === 'restarting') button.disabled = true;
      row.append(el('div', { class: 'restart-col' }, lamp, button));
    } else {
      row.append(el('div', {}));
    }
    return row;
  }

  function apply(key, value) {
    if (String(Robot.state.values[key] ?? '') === String(value)) return;
    Robot.state.values[key] = value;
    if (!Robot.send({ type: 'set', key, value })) {
      toast('Not connected to the orchestrator.', 'bad');
    }
  }

  function restartComponent(setting) {
    if (setting.key in staged) {
      Robot.send({ type: 'set', key: setting.key, value: staged[setting.key] });
      delete staged[setting.key];
      const row = panelEl.querySelector(`[data-key="${setting.key}"]`);
      if (row) row.classList.remove('is-staged');
    }
    if (Robot.send({ type: 'restart', component: setting.component })) {
      toast(`Restarting ${setting.component}…`, 'warn');
    } else {
      toast('Not connected to the orchestrator.', 'bad');
    }
  }

  Robot.on((message) => {
    switch (message.type) {
      case 'hello':
        if (!currentGroup && Robot.state.groups.length) currentGroup = Robot.state.groups[0].key;
        renderTabs(); renderPanel();
        break;
      case '_link':
        setLive(message.up);
        if (!message.up) {
          // Every control here needs the orchestrator, so there is nothing honest to
          // render -- but an empty page does not say that, and the usual reason is a
          // process that is simply not running. Point at where that is fixable.
          panelEl.textContent = '';
          panelEl.append(el('p', { class: 'muted' },
            'The orchestrator is not connected, so nothing here can be applied. ',
            el('a', { href: '/services' }, 'Services'),
            ' shows whether robot-core is running, and can restart it.'));
        }
        break;
      case 'level':
        Robot.state.level = message;
        paintMeter(message);
        break;
      case 'health':
        // Keep the restart lamps honest without stomping on a field being typed into.
        renderPanel();
        break;
      case 'set_result':
        if (message.key === GATE) {
          if (message.ok && !acceptGateReply(message.value)) break;  // stale reply
          if (!message.ok) { releaseGateHold(); paintKnob(); }          // refused: show truth
        }
        if (!message.ok) toast(`Rejected: ${message.error}`, 'bad');
        else if (message.staged) toast(message.note || 'staged', 'warn');
        else if (message.note) toast(message.note, 'warn');
        else {
          toast(`${message.key} applied`, 'ok');
          if (message.value !== undefined) {
            Robot.state.values[message.key] = message.value;
            const widget = inputs[message.key];
            // The orchestrator clamps; show what was actually stored.
            if (widget && document.activeElement !== widget) widget.value = message.value;
          }
        }
        break;
      case 'restart_result':
        toast(message.message || '', message.ok ? 'ok' : 'bad');
        break;
      case 'ping_result':
        showPing(message.results || []);
        break;
      case 'preview_result':
        if (message.ok) toast(`Playing ${message.voice}`, 'ok');
        else toast(`Preview failed: ${message.note}`, 'bad');
        break;
    }
  });

  setLive(false);
  Robot.connect();
})();
