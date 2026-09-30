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

  function paint(data) {
    $('chunks').textContent = (data.chunks ?? 0).toLocaleString();
    $('sources').textContent = (data.sources || []).length;
    $('pending').textContent = data.pending ?? 0;
    const rows = $('source-rows');
    rows.textContent = '';
    if (!(data.sources || []).length) {
      rows.append(el('tr', {}, el('td', { colspan: '2', class: 'muted',
        text: 'Nothing stored yet.' })));
      return;
    }
    for (const [name, count] of data.sources) {
      rows.append(el('tr', {},
        el('td', { text: name }),
        el('td', { class: 'num', text: String(count) })));
    }
  }

  $('ingest-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const text = $('text').value.trim();
    const path = $('path').value.trim();
    if (!text && !path) return;
    const message = path ? { type: 'ingest', path } : { type: 'ingest', text, source: 'typed' };
    if (Robot.send(message)) { $('text').value = ''; $('path').value = ''; }
  });

  $('refresh').addEventListener('click', refresh);

  /* ---------- drag and drop upload ---------- */
  const drop = $('drop');
  const fileInput = $('file-input');
  const uploads = $('uploads');

  /** A line under the drop zone reporting how an ingestion actually finished. */
  function note(text, bad) {
    const row = el('div', { class: `upload${bad ? ' err' : ''}` },
      el('span', { class: 'name', text }),
      el('span', { class: `badge ${bad ? 'badge-danger' : 'badge-success'}`,
                   text: bad ? 'failed' : 'done' }));
    uploads.append(row);
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
      // Ingestion is a background job, so the counts arrive a moment later.
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

  // dragover must be cancelled or the browser navigates to the file instead.
  for (const event of ['dragenter', 'dragover']) {
    drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.add('over'); });
  }
  for (const event of ['dragleave', 'drop']) {
    drop.addEventListener(event, (e) => { e.preventDefault(); drop.classList.remove('over'); });
  }
  drop.addEventListener('drop', (e) => {
    if (e.dataTransfer?.files?.length) send(e.dataTransfer.files);
  });
  // Dropping anywhere else must not make the browser leave the page.
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => e.preventDefault());

  Robot.on((message) => {
    switch (message.type) {
      case '_link': setLive(message.up); if (message.up) refresh(); break;
      case 'hello': refresh(); break;
      case 'knowledge': paint(message); break;
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
