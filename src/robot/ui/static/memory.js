(() => {
  const { el, mib } = Robot;
  const $ = (id) => document.getElementById(id);

  const COLOUR = {
    os: 'var(--text-faint)', llm: 'var(--accent)', asr: 'var(--info)',
    tts: 'var(--success)', embed: 'var(--violet)', core: 'var(--warning)',
    ui: 'var(--border-strong)',
  };

  let models = [];
  let applied = {};      // what the orchestrator currently has configured
  let budget = null;
  let applying = false;
  let running = {};      // what each service actually has open, from /api/services

  const nameOf = (path) => String(path).split('/').filter(Boolean).pop() || '';

  function setLive(up) {
    $('live-dot').className = `dot ${up ? 'live' : 'dead'}`;
    $('live-text').textContent = up ? 'connected' : 'disconnected';
  }

  /* ---------- the selection currently shown on the page ---------- */
  function selection() {
    const sel = {
      llm_model: $('sel-llm').value || null,
      asr_model: $('sel-asr').value || null,
      voice_en: $('sel-en').value || null,
      voice_id: $('sel-id').value || null,
      voice_ar: $('sel-ar').value || null,
      llm_ctx: Number($('sel-ctx').value),
      tts_engine: applied.tts_engine || 'piper',
    };
    // Supertonic is one voice for all three languages, so it is one control -- and
    // sending the three Piper names alongside it would price a speaker that is not
    // going to be loaded.
    if (sel.tts_engine === 'supertonic') sel.supertonic_voice = $('sel-style').value || null;
    return sel;
  }

  function requestPrice() {
    Robot.send({ type: 'budget', preview: selection() });
  }

  function fillSelect(id, options, current, { blank = null } = {}) {
    const select = $(id);
    select.textContent = '';
    if (blank) select.append(el('option', { value: '' }, blank));
    for (const opt of options) {
      select.append(el('option', { value: opt.value, selected: opt.value === current ? '' : null },
                       opt.label));
    }
    if (current && !options.some((o) => o.value === current)) {
      // Configured but not installed -- show it rather than silently switching.
      select.append(el('option', { value: current, selected: '' }, `${nameOf(current)} (missing)`));
    }
    select.value = current ?? '';
  }

  function populate() {
    const llms = models.filter((m) => m.kind === 'llm');
    const asrs = models.filter((m) => m.kind === 'asr');
    const voices = (lang) => models.filter(
      (m) => m.kind === 'tts' && m.name.toLowerCase().startsWith(`${lang}_`));

    fillSelect('sel-llm', llms.map((m) => ({ value: m.path, label: `${m.name} — ${m.ram_mib} MiB` })),
               applied.llm_model, { blank: 'none' });
    fillSelect('sel-asr', asrs.map((m) => ({ value: m.path, label: `${m.name} — ${m.ram_mib} MiB` })),
               applied.asr_model, { blank: 'none' });
    for (const lang of ['en', 'id', 'ar']) {
      fillSelect(`sel-${lang}`,
                 voices(lang).map((m) => ({ value: m.name, label: `${m.name} — ${m.ram_mib} MiB` })),
                 applied[`voice_${lang}`], { blank: 'none' });
    }

    // One picker or three, depending on which speaker is configured. Showing both at
    // once would offer a choice that has no effect, which is how the Supertonic voice
    // came to be un-findable on this page in the first place.
    const supertonic = models.find((m) => m.name === 'supertonic');
    const styles = (supertonic && supertonic.meta && supertonic.meta.styles) || [];
    const single = applied.tts_engine === 'supertonic';
    $('field-style').hidden = !single;
    for (const node of document.querySelectorAll('.piper-voice')) node.hidden = single;
    if (single) {
      fillSelect('sel-style', styles.map((s) => ({ value: s, label: s })),
                 applied.supertonic_voice);
      $('sub-style').textContent = styles.length
        ? `${supertonic.ram_mib} MiB for the model, whichever voice. F = female, `
          + 'M = male. Audition them on Config \u2192 Speaking, which plays each one in '
          + 'all three languages without adopting it.'
        : 'Supertonic is not installed \u2014 run scripts/models/fetch_supertonic.sh';
    }
    if (applied.llm_ctx) $('sel-ctx').value = String(applied.llm_ctx);

    const rows = $('models');
    rows.textContent = '';
    if (!models.length) {
      rows.append(el('tr', {}, el('td', { colspan: '4', class: 'muted', text: 'No models found.' })));
    }
    for (const m of models) {
      rows.append(el('tr', {},
        el('td', {}, el('div', { text: m.name }),
           el('div', { class: 'hint', style: 'margin:0;font-size:0.74rem', text: m.detail })),
        el('td', {}, el('span', { class: 'badge badge-neutral', text: m.kind })),
        el('td', { class: 'num', text: mib(m.size_mib) }),
        el('td', { class: 'num', text: mib(m.ram_mib) })));
    }
  }

  function paintBudget(b) {
    budget = b;
    if (!b.preview) paintActual(b);
    const ceiling = b.ceiling_mib;
    $('ceiling').textContent = ceiling.toLocaleString();
    $('total').textContent = b.peak_mib.toLocaleString();
    $('steady').textContent = mib(b.steady_mib);
    if (b.resident_mib !== undefined) {
      $('resident').textContent = mib(b.resident_mib);
      $('resident-note').textContent = b.asleep_mib
        ? `${mib(b.asleep_mib)} of the projection is asleep and not held now`
        : 'everything in the projection is loaded';
    }
    $('peak').textContent = mib(b.peak_mib);
    $('headroom').textContent = mib(b.headroom_mib);
    $('target-label').textContent = `target ${b.target_mib.toLocaleString()}`;
    $('ceiling-label').textContent = ceiling.toLocaleString();

    const verdict = $('verdict');
    if (!b.fits) {
      verdict.className = 'badge badge-danger';
      verdict.textContent = `over by ${Math.abs(b.headroom_mib)} MiB`;
    } else if (!b.within_target) {
      verdict.className = 'badge badge-warning';
      verdict.textContent = 'over the target, under the ceiling';
    } else {
      verdict.className = 'badge badge-success';
      verdict.textContent = 'fits';
    }

    // Stacked bar, one segment per line item, scaled to the ceiling.
    const alloc = $('alloc');
    alloc.textContent = '';
    for (const item of b.items) {
      const colour = COLOUR[item.kind] || 'var(--text-faint)';
      // A line that is not resident right now is drawn hatched, not solid: it is part
      // of the projection but nothing is holding that memory at this moment.
      const fill = item.resident === false
        ? `repeating-linear-gradient(45deg,${colour},${colour} 3px,transparent 3px,transparent 7px)`
        : colour;
      // Draw the MEASURED width when there is one. The estimate is what a model would
      // cost; this bar is meant to be the board as it is, so where /proc has an answer
      // it wins. Lines with no process behind them keep the projection.
      const width = item.measured_mib ?? item.mib;
      const drift = item.drift_mib;
      alloc.append(el('div', {
        class: 'seg',
        style: `width:${(width / ceiling) * 100}%;background:${fill}`,
        title: `${item.label}: ${width} MiB`
             + (item.measured_mib != null
                 ? ` measured (estimate ${item.mib}${drift ? `, ${drift > 0 ? '+' : ''}${drift}` : ''})`
                 : item.resident === false ? ' (projected; not loaded now)' : ' (projected)'),
      }));
    }
    // The decode peak sits on top of steady state.
    if (b.peak_mib > b.steady_mib) {
      alloc.append(el('div', {
        class: 'seg',
        style: `width:${((b.peak_mib - b.steady_mib) / ceiling) * 100}%;`
             + 'background:repeating-linear-gradient(45deg,var(--info),var(--info) 4px,transparent 4px,transparent 8px)',
        title: `ASR decode peak: +${b.peak_mib - b.steady_mib} MiB`,
      }));
    }
    alloc.append(el('div', { class: 'target-marker', style: `left:${(b.target_mib / ceiling) * 100}%` }));

    const legend = $('legend');
    legend.textContent = '';
    for (const item of b.items) {
      legend.append(el('span', { class: 'item' },
        el('span', { class: 'swatch', style: `background:${COLOUR[item.kind] || 'var(--text-faint)'}` }),
        `${item.label} · ${item.measured_mib ?? item.mib} MiB`));
    }

    const rows = $('items');
    rows.textContent = '';
    for (const item of b.items) {
      const drift = item.drift_mib;
      // A drift worth reading. Under 32 MiB is measurement noise -- page cache, a
      // Python heap that has not settled -- and flagging it would train people to
      // ignore the column that matters.
      const notable = drift != null && Math.abs(drift) >= 32;
      rows.append(el('tr', { style: item.resident === false ? 'opacity:0.62' : null },
        el('td', {}, item.label,
           item.resident === false
             ? el('span', { class: 'badge badge-neutral', style: 'margin-left:0.4rem',
                            text: 'not loaded' })
             : null),
        el('td', { class: 'num', text: item.mib.toLocaleString() }),
        el('td', { class: 'num' },
           item.measured_mib != null
             ? el('span', {}, item.measured_mib.toLocaleString(),
                  notable
                    ? el('span', { class: `delta ${drift > 0 ? 'up' : 'down'}`,
                                   style: 'margin-left:0.35rem',
                                   text: `${drift > 0 ? '+' : ''}${drift}` })
                    : null)
             : el('span', { class: 'muted', text: '–' })),
        el('td', { class: 'hint', style: 'margin:0', text: item.detail || '' })));
    }
    if (b.peak_mib > b.steady_mib) {
      rows.append(el('tr', {},
        el('td', { text: 'Decode peak' }),
        el('td', { class: 'num', text: `+${b.peak_mib - b.steady_mib}` }),
        el('td', { class: 'num muted', text: '–' }),
        el('td', { class: 'hint', style: 'margin:0',
                   text: 'transient, but it overlaps a resident LLM during a turn' })));
    }

    // Show how the projection compares with what is actually loaded right now.
    const sel = selection();
    const keys = applied.tts_engine === 'supertonic'
      ? ['llm_model', 'asr_model', 'supertonic_voice']
      : ['llm_model', 'asr_model', 'voice_en', 'voice_id', 'voice_ar'];
    const changed = keys
      .some((k) => String(sel[k] ?? '') !== String(applied[k] ?? ''))
      || sel.llm_ctx !== (applied.llm_ctx ?? sel.llm_ctx);
    if (!applying) {
      $('apply-note').textContent = changed
        ? 'Not applied yet. Applying restarts whatever has to reload — the robot is '
          + 'deaf or unable to answer for a few seconds while it does.'
        : 'This is what is configured and running now.';
      $('apply').disabled = !changed;
    }
  }

  /* The three layers, from robot-ui's own endpoint rather than the orchestrator: the
     running column has to come from the processes, and this page must be able to say
     "your choice is loaded" without asking the thing that would be restarted. */
  async function refreshRunning() {
    const body = $('running-rows');
    try {
      const res = await fetch('/api/services', { cache: 'no-store' });
      if (!res.ok) return;
      const rows = (await res.json()).services
        .filter((r) => r.model || r.configured || r.chosen);
      running = Object.fromEntries(rows.map((r) => [r.key, r]));
      body.textContent = '';
      const problems = [];
      for (const r of rows) {
        const cell = (value, warn) => el('td', {},
          value ? el('code', { text: value }) : el('span', { class: 'muted', text: '—' }),
          warn ? ' ' : null,
          warn ? el('span', { class: 'badge badge-warning', text: warn }) : null);
        body.append(el('tr', {},
          el('td', { text: r.label }),
          cell(r.chosen, null),
          cell(r.configured, r.unapplied ? 'not applied' : null),
          cell(r.running ? r.model : null, r.stale ? 'needs restart' : null)));
        if (r.unapplied) problems.push(`${r.label}: ${r.chosen} is chosen but ${r.configured} is configured — press Apply.`);
        if (r.stale) problems.push(`${r.label}: running ${r.model}, configured ${r.configured} — restart it from Services.`);
        if (r.unexpected) problems.push(`${r.label} is running but nothing is reading it — stop it from Services to free ${r.rss_mib} MiB.`);
        if (r.missing) problems.push(`${r.label} is needed by the selected ASR model and is not running.`);
        /* Total swap is on the tile above, but the total does not say which engine is
           going to be slow. A synthesiser with its pages on disk faults them back on
           every utterance: measured 2026-09-15, 799 MiB out turned a 0.7 s first word
           into 10.5 s, with the GPU reporting itself perfectly healthy throughout. */
        if ((r.swap_mib || 0) >= 50) problems.push(`${r.label} has ${Math.round(r.swap_mib)} MiB paged out to swap — it will be slow on every turn until the board has room.`);
      }
      $('running-note').textContent = problems.length
        ? problems.join('  ')
        : 'Chosen, configured and running all agree.';
      $('running-note').className = problems.length ? 'hint bad' : 'hint';
    } catch (err) {
      body.textContent = '';
      body.append(el('tr', {}, el('td', { colspan: '4', class: 'muted',
                                          text: 'Could not read the running services.' })));
    }
  }

  /* The board as it is, from the same /proc read that built the rest of the budget.
     This used to fetch /api/monitor on its own timer, which meant the tile and the
     table were sampled at different moments and could disagree by a model load. */
  function paintActual(b) {
    const sys = b.system;
    if (!sys || !sys.total_mib) return;
    $('actual').textContent = mib(sys.used_mib);
    const delta = sys.used_mib - b.steady_mib;
    const swap = sys.swap_used_mib
      ? ` · <span class="delta up">${mib(sys.swap_used_mib)} swap</span>`
      : '';
    $('actual-note').innerHTML =
      `${mib(sys.free_mib)} free, ${mib(sys.cached_mib)} cached · `
      + `<span class="delta ${delta > 0 ? 'up' : 'down'}">`
      + `${delta > 0 ? '+' : ''}${Math.round(delta)} MiB vs projection</span>${swap}`;
  }

  for (const id of ['sel-llm', 'sel-asr', 'sel-en', 'sel-id', 'sel-ar', 'sel-style', 'sel-ctx']) {
    $(id).addEventListener('change', requestPrice);
  }

  $('reset').addEventListener('click', () => { populate(); requestPrice(); });

  /* Applying is one message, not a set here and a restart somewhere else.
     The orchestrator writes every changed value, then restarts each affected component
     once -- so choosing a model and a context size together costs one LLM restart. It
     is also the only path: this page is where models are chosen now, so it has to be
     able to make the choice real. */
  $('apply').addEventListener('click', () => {
    const sel = selection();
    applying = true;
    $('apply').disabled = true;
    $('apply').textContent = 'Applying…';
    $('apply-note').textContent =
      'Writing the config and restarting what has to reload. A model takes a few '
      + 'seconds to load; the page will say when it is running.';
    if (!Robot.send({ type: 'apply', values: sel })) {
      applying = false;
      $('apply').textContent = 'Apply';
      $('apply-note').textContent = 'Not connected to the orchestrator.';
    }
  });

  Robot.on((message) => {
    switch (message.type) {
      case 'hello': {
        models = message.models || [];
        const sel = message.selection || {};
        applied = {
          llm_model: sel.llm_model || null,
          asr_model: sel.asr_model || null,
          llm_ctx: sel.llm_ctx || 2048,
          tts_engine: sel.tts_engine || 'piper',
          supertonic_voice: sel.supertonic_voice || null,
        };
        // The orchestrator reports the voices it would load, in en/id/ar order.
        (sel.voices || []).forEach((name) => {
          const lang = String(name).split('_')[0];
          if (['en', 'id', 'ar'].includes(lang)) applied[`voice_${lang}`] = name;
        });
        populate();
        if (message.budget) paintBudget(message.budget);
        requestPrice();
        break;
      }
      case 'models': models = message.models || []; populate(); break;
      case 'budget': paintBudget(message); break;
      case 'set_result':
        if (message.ok && message.key) applied[message.key] = message.value;
        break;
      case 'apply_result': {
        applying = false;
        $('apply').textContent = 'Apply';
        // Trust the orchestrator's own view of what is configured now rather than what
        // this page hoped for -- a restart that failed must not look like it worked.
        const sel = message.selection || {};
        applied.llm_model = sel.llm_model || null;
        applied.asr_model = sel.asr_model || null;
        applied.llm_ctx = sel.llm_ctx || applied.llm_ctx;
        applied.tts_engine = sel.tts_engine || applied.tts_engine;
        applied.supertonic_voice = sel.supertonic_voice || applied.supertonic_voice;
        (sel.voices || []).forEach((name) => {
          const lang = String(name).split('_')[0];
          if (['en', 'id', 'ar'].includes(lang)) applied[`voice_${lang}`] = name;
        });
        populate();
        if (message.budget) paintBudget(message.budget);
        const parts = [];
        if (message.restarted && message.restarted.length) {
          parts.push(`restarted ${message.restarted.join(', ')}`);
        } else if (message.applied && message.applied.length) {
          parts.push('applied live');
        }
        $('apply-note').textContent = message.ok
          ? `Running now — ${parts.join('; ') || 'nothing needed changing'}.`
          : `Failed: ${(message.errors || []).join('; ')}`;
        $('apply-note').className = message.ok ? 'hint' : 'hint bad';
        $('apply').disabled = message.ok;
        refreshRunning();
        break;
      }
      case '_link': setLive(message.up); break;
    }
  });

  setLive(false);
  Robot.connect();
  /* nothing to kick off: the orchestrator pushes a budget every 3 s */
  refreshRunning();
  // Slower than the memory sample: this only moves when something is applied or
  // restarted, and it reads every process's environment to answer.
  setInterval(refreshRunning, 5000);
})();
