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
    if (setting.key === 'tts_volume') {
      // A slider, because volume is something you nudge while listening.
      const range = el('input', { type: 'range', min: 0, max: 200, step: 5,
                                  value: value ?? 100 });
      const read = el('span', { class: 'hint', style: 'margin:0;font-variant-numeric:tabular-nums' });
      const paint = () => {
        read.textContent = `${range.value}%` + (Number(range.value) > 100 ? '  (amplifying — may clip)' : '');
      };
      range.addEventListener('input', paint);
      paint();
      const wrap = el('div', {}, range, read);
      // The row code reads .value off whatever this returns.
      Object.defineProperty(wrap, 'value', {
        get: () => range.value,
        set: (v) => { range.value = v; paint(); },
      });
      wrap.addEventListener = range.addEventListener.bind(range);
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

  /* ---------- live input meter ----------
     The VAD threshold is a Silero probability, not a level in dB, so it cannot honestly
     be drawn on a dB meter. What the meter does show is the room: where the noise floor
     sits, how far speech rises above it, and whether the VAD is currently triggering.
     Tune by watching the pill -- if it says SPEECH when nobody is talking, the threshold
     is too low for this room. */
  let meterEls = null;
  function meterCard() {
    const fill = el('div', { class: 'fill' });
    const peak = el('div', { class: 'peak' });
    const floor = el('div', { class: 'floor' });
    const pill = el('span', { class: 'badge badge-neutral speech-pill', text: 'no signal' });
    const stats = el('div', { class: 'meter-stats' });
    meterEls = { fill, peak, floor, pill, stats };

    const card = el('fieldset', { class: 'meter-card' },
      el('legend', { text: 'Microphone level' }),
      el('div', { class: 'kind-note' },
        el('span', { class: 'badge badge-info', text: 'live' }),
        'What the robot is hearing right now. Raise the threshold until the pill stops '
        + 'saying SPEECH when the room is quiet, then check it still catches you talking.'),
      el('div', { class: 'meter-row' },
        el('div', { class: 'meter' }, fill, floor, peak), pill),
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

  function paintMeter(level) {
    if (!meterEls || !level || level.dbfs === undefined) return;
    meterEls.fill.style.width = `${toPercent(level.dbfs)}%`;
    meterEls.peak.style.left = `${toPercent(level.peak_dbfs)}%`;
    meterEls.floor.style.left = `${toPercent(level.floor_dbfs)}%`;
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
      case 'preview_result':
        if (message.ok) toast(`Playing ${message.voice}`, 'ok');
        else toast(`Preview failed: ${message.note}`, 'bad');
        break;
    }
  });

  setLive(false);
  Robot.connect();
})();
