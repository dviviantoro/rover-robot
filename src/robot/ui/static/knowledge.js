(() => {
  const { el } = Robot;
  const $ = (id) => document.getElementById(id);
  const liveDot = $('live-dot');
  const liveText = $('live-text');

  function setLive(up) {
    liveDot.className = `dot ${up ? 'live' : 'dead'}`;
    liveText.textContent = up ? 'connected' : 'disconnected';
  }

  const refresh = () => Robot.send({ type: 'knowledge' });

  /* ---------- the system prompt ----------
   *
   * It lives here rather than on Config because it is knowledge: the standing
   * instruction the robot carries into every turn, ahead of anything retrieved. A
   * tuning dial belongs with the other dials; this belongs beside the documents.
   */
  let personaSaved = '';
  function paintPersona() {
    const value = Robot.state.values?.persona;
    if (value === undefined || value === null) return;
    personaSaved = String(value);
    // Do not stamp on something half-typed.
    if ($('persona') !== document.activeElement) $('persona').value = personaSaved;
    paintPersonaSize();
  }
  function paintPersonaSize() {
    const chars = $('persona').value.length;
    // ~4 characters a token, the same figure prompt.py budgets with. Worth showing: the
    // persona is re-sent every turn and is usually the largest fixed part of the prompt.
    $('persona-size').textContent =
      `${chars.toLocaleString()} characters · roughly ${Math.round(chars / 4).toLocaleString()} tokens of every prompt`;
    const dirty = $('persona').value !== personaSaved;
    $('persona-state').textContent = dirty ? 'unsaved changes' : '';
  }
  $('persona').addEventListener('input', paintPersonaSize);
  $('persona-reset').addEventListener('click', () => {
    $('persona').value = personaSaved; paintPersonaSize();
  });
  $('persona-save').addEventListener('click', () => {
    const value = $('persona').value;
    if (Robot.send({ type: 'set', key: 'persona', value })) {
      personaSaved = value;
      $('persona-state').textContent = 'saved — it applies to the next turn';
      paintPersonaSize();
    }
  });

  /* ---------- the document list ---------- */
  function paint(data) {
    $('chunks').textContent = (data.chunks ?? 0).toLocaleString();
    $('sources').textContent = (data.sources || []).length;
    $('pending').textContent = data.pending ?? 0;
    const rows = $('source-rows');
    rows.textContent = '';
    const sources = data.sources || [];
    $('doc-count').textContent = sources.length
      ? 'click a row to read it' : '';
    if (!sources.length) {
      rows.append(el('tr', {}, el('td', { colspan: '3', class: 'muted',
        text: 'Nothing stored yet.' })));
      return;
    }
    for (const [name, count] of sources) {
      rows.append(el('tr', { class: 'row-open', onclick: () => openDoc(name) },
        el('td', { text: name }),
        el('td', { class: 'num', text: String(count) }),
        el('td', { class: 'num' },
          el('button', { class: 'btn-ghost', onclick: (e) => {
            e.stopPropagation();
            if (confirm(`Forget everything from ${name}?`)) {
              Robot.send({ type: 'forget', source: name });
            }
          } }, 'Forget'))));
    }
  }

  /* ---------- one document ---------- */
  let doc = null;            // the last `document` payload
  let described = null;      // the last `described` payload, for the same source
  let view = 'raw';

  function openDoc(source) {
    doc = null; described = null; view = 'raw';
    $('doc-card').hidden = false;
    $('doc-title').textContent = source;
    $('doc-meta').textContent = '';
    $('doc-body').textContent = '';
    $('doc-body').append(el('div', { class: 'muted', text: 'reading…' }));
    setTab('raw');
    Robot.send({ type: 'document', source });
    $('doc-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }

  $('doc-close').addEventListener('click', () => {
    $('doc-card').hidden = true; doc = null; described = null;
  });
  $('doc-describe').addEventListener('click', () => {
    if (!doc) return;
    $('doc-describe').disabled = true;
    $('doc-describe').textContent = 'reading it…';
    Robot.send({ type: 'describe', source: doc.source });
  });
  function setTab(next) {
    view = next;
    for (const b of $('doc-tabs').children) b.classList.toggle('on', b.dataset.view === next);
  }
  for (const button of $('doc-tabs').children) {
    button.addEventListener('click', () => { setTab(button.dataset.view); paintDoc(); });
  }

  const when = (t) => (t ? new Date(t * 1000).toLocaleString() : '—');

  function paintDoc() {
    if (!doc) return;
    if (doc.error) {
      $('doc-body').textContent = '';
      $('doc-body').append(el('div', { class: 'hint bad', text: doc.error }));
      return;
    }
    // Metadata, all of it derived from the rows that are actually stored.
    const meta = $('doc-meta');
    meta.textContent = '';
    const row = (k, v) => meta.append(el('div', { class: 'row' },
      el('span', { class: 'ck', text: k }), el('span', { class: 'cv', text: v })));
    row('chunks', `${doc.total_chunks}`);
    row('characters', doc.chars.toLocaleString());
    row('language', doc.languages?.length ? doc.languages.join(', ') : 'not tagged');
    row('first ingested', when(doc.added_first));
    row('last ingested', when(doc.added_last));
    row('original', doc.raw_from === 'file'
      ? 'read from the file on disk' : 'rebuilt from the stored chunks');

    const body = $('doc-body');
    body.textContent = '';
    if (view === 'raw') {
      if (doc.raw_from !== 'file') {
        // Being precise about this matters: a rebuilt document has lost whatever the
        // splitter dropped between chunks, and must not be read as the file.
        body.append(el('p', { class: 'hint', style: 'margin-bottom:0.5rem',
          text: 'Ingestion stores chunks, not the source text. This is those chunks '
                + 'joined back together — close to the original, but not byte-for-byte.' }));
      }
      body.append(el('pre', { class: 'doc-text', text: doc.raw || '' }));
      if (doc.raw_truncated) {
        body.append(el('p', { class: 'hint', text: 'Truncated at 200,000 characters.' }));
      }
    } else if (view === 'chunks') {
      body.append(el('p', { class: 'hint', style: 'margin-bottom:0.5rem',
        text: `What retrieval actually searches: ${doc.total_chunks} chunk`
              + `${doc.total_chunks === 1 ? '' : 's'}, each embedded separately.`
              + (doc.truncated ? ' Showing the first 400.' : '') }));
      body.append(el('pre', { class: 'doc-json',
                              text: JSON.stringify(doc.chunks, null, 2) }));
    } else {
      paintAbout(body);
    }
  }

  function paintAbout(body) {
    // Terms first, because they cost nothing and are always available.
    const basis = doc.terms_basis === 'distinctive'
      ? 'Terms this document uses and the others do not (tf-idf across the store).'
      : 'Most frequent terms. With fewer than three documents there is nothing to be '
        + 'distinctive against, so this is frequency, not significance.';
    body.append(el('h3', { text: 'Key terms' }),
                el('p', { class: 'hint', style: 'margin-bottom:0.3rem', text: basis }));
    const terms = el('div', { class: 'terms' });
    for (const t of doc.terms || []) {
      terms.append(el('span', { class: 'term' }, t.term, el('b', { text: String(t.count) })));
    }
    body.append((doc.terms || []).length ? terms
      : el('p', { class: 'muted', text: 'nothing above the stopword list' }));

    body.append(el('h3', { text: 'Summary and entities' }));
    if (!described) {
      body.append(el('p', { class: 'hint measure',
        text: 'Not computed. A named-entity model would mean adding transformers to the '
              + 'runtime, which the 8 GB budget cannot pay for — so this asks the LLM '
              + 'that is already loaded instead, on request. It costs a few seconds of '
              + 'the GPU and nothing else.' }));
      return;
    }
    if (described.error) {
      body.append(el('p', { class: 'hint bad', text: described.error }));
      if (described.raw) body.append(el('pre', { class: 'doc-json', text: described.raw }));
      return;
    }
    const d = described.described || {};
    body.append(el('p', { class: 'hint', style: 'margin-bottom:0.4rem',
      text: `From ${described.model?.split('/').pop() || 'the LLM'}, reading `
            + `${described.chars_read?.toLocaleString()} characters. Generated, so check it.` }));
    if (d.summary) body.append(el('p', { class: 'measure', text: d.summary }));
    if (Array.isArray(d.topics) && d.topics.length) {
      const chips = el('div', { class: 'terms' });
      for (const t of d.topics) chips.append(el('span', { class: 'term', text: String(t) }));
      body.append(el('div', { class: 'ent' }, el('h4', { text: 'topics' }), chips));
    }
    for (const [group, items] of Object.entries(d.entities || {})) {
      if (!Array.isArray(items) || !items.length) continue;
      const chips = el('div', { class: 'terms' });
      for (const item of items) chips.append(el('span', { class: 'term', text: String(item) }));
      body.append(el('div', { class: 'ent' }, el('h4', { text: group }), chips));
    }
  }

  /* ---------- ingest, upload, drag and drop ---------- */
  $('ingest-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const text = $('text').value.trim();
    const path = $('path').value.trim();
    if (!text && !path) return;
    const message = path ? { type: 'ingest', path } : { type: 'ingest', text, source: 'typed' };
    if (Robot.send(message)) { $('text').value = ''; $('path').value = ''; }
  });
  $('refresh').addEventListener('click', refresh);

  const drop = $('drop');
  const fileInput = $('file-input');
  const uploads = $('uploads');

  function note(text, bad) {
    uploads.append(el('div', { class: `upload${bad ? ' err' : ''}` },
      el('span', { class: 'name', text }),
      el('span', { class: `badge ${bad ? 'badge-danger' : 'badge-success'}`,
                   text: bad ? 'failed' : 'done' })));
  }

  function uploadRow(name) {
    const status = el('span', { class: 'badge badge-neutral', text: 'uploading' });
    const row = el('div', { class: 'upload' }, el('span', { class: 'name', text: name }), status);
    uploads.append(row);
    return { row, status };
  }

  async function upload(file) {
    const { row, status } = uploadRow(file.name);
    const body = new FormData();
    body.append('file', file);
    try {
      const res = await fetch('/api/upload', { method: 'POST', body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        row.classList.add('err');
        status.className = 'badge badge-danger';
        status.textContent = data.error || `failed (${res.status})`;
        return;
      }
      status.className = 'badge badge-success';
      status.textContent = data.queued ? 'ingesting' : 'saved';
      if (data.note) row.append(el('span', { class: 'hint', style: 'margin:0', text: data.note }));
      setTimeout(refresh, 1500);
      setTimeout(refresh, 6000);
      setTimeout(() => { status.className = 'badge badge-info'; status.textContent = 'stored'; }, 6000);
    } catch (err) {
      row.classList.add('err');
      status.className = 'badge badge-danger';
      status.textContent = String(err).slice(0, 60);
    }
  }

  const send = (files) => Array.from(files).forEach(upload);
  drop.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { send(fileInput.files); fileInput.value = ''; });
  for (const event of ['dragenter', 'dragover']) {
    drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.add('over'); });
  }
  for (const event of ['dragleave', 'drop']) {
    drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.remove('over'); });
  }
  drop.addEventListener('drop', (e) => {
    if (e.dataTransfer?.files?.length) send(e.dataTransfer.files);
  });
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  Robot.on((message) => {
    switch (message.type) {
      case '_link': setLive(message.up); if (message.up) refresh(); break;
      case 'hello': refresh(); paintPersona(); break;
      case 'values': paintPersona(); break;
      case 'knowledge': paint(message); break;
      case 'document': doc = message; paintDoc(); break;
      case 'described':
        described = message;
        $('doc-describe').disabled = false;
        $('doc-describe').textContent = 'Describe with the LLM';
        setTab('about');
        paintDoc();
        break;
      case 'forgotten': refresh(); $('doc-card').hidden = true; break;
      case 'ingesting': setTimeout(refresh, 1500); setTimeout(refresh, 6000); break;
      case 'ingested':
        note(`${message.source}: ${message.chunks} chunk${message.chunks === 1 ? '' : 's'} stored`, false);
        refresh();
        break;
      case 'ingest_failed':
        note(`${message.source} failed: ${message.reason}`, true);
        break;
      case 'error':
        if (message.reason) liveText.textContent = message.reason;
        break;
    }
  });

  setLive(false);
  Robot.connect();
})();
