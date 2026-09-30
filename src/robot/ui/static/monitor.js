(() => {
  const { el } = Robot;
  const HISTORY = 120;                  // samples kept for the sparklines
  const history = { mem: [], cpu: [], gpu: [], procs: {} };
  let timer = null;

  const $ = (id) => document.getElementById(id);
  const liveDot = $('live-dot');
  const liveText = $('live-text');

  /** Minimal SVG sparkline. No chart library: one polyline and a filled area. */
  function sparkline(svg, values, { min = 0, max = null, colour = 'var(--accent)' } = {}) {
    svg.textContent = '';
    if (values.length < 2) return;
    const top = max !== null ? max : Math.max(...values, 1);
    const bottom = Math.min(min, ...values);
    const span = Math.max(top - bottom, 1e-6);
    const w = 100, h = 100;
    const points = values.map((v, i) => {
      const x = (i / (values.length - 1)) * w;
      const y = h - ((v - bottom) / span) * h;
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    });
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.append(
      el('polygon', {
        points: `0,${h} ${points.join(' ')} ${w},${h}`,
        fill: colour, opacity: '0.12', stroke: 'none',
      }),
      el('polyline', {
        points: points.join(' '), fill: 'none', stroke: colour,
        'stroke-width': '2', 'vector-effect': 'non-scaling-stroke',
        'stroke-linejoin': 'round', 'stroke-linecap': 'round',
      }),
    );
  }

  function push(list, value) {
    list.push(value);
    while (list.length > HISTORY) list.shift();
  }

  function trend(values) {
    if (values.length < 6) return '';
    const half = Math.floor(values.length / 2);
    const early = values.slice(0, half).reduce((a, b) => a + b, 0) / half;
    const late = values.slice(half).reduce((a, b) => a + b, 0) / (values.length - half);
    const delta = late - early;
    if (Math.abs(delta) < 5) return '·';
    return delta > 0 ? `▲ ${Math.round(delta)}` : `▼ ${Math.round(-delta)}`;
  }

  function paint(data) {
    // ---- memory -------------------------------------------------------
    const m = data.memory;
    push(history.mem, m.used_mib);
    $('mem-used').textContent = Math.round(m.used_mib).toLocaleString();
    $('mem-total').textContent = `of ${Math.round(m.total_mib).toLocaleString()} MiB`;
    const frac = m.used_mib / m.total_mib;
    const bar = $('mem-bar');
    bar.className = `bar${m.used_mib > m.target_mib ? ' danger' : frac > 0.8 ? ' warn' : ''}`;
    bar.firstElementChild.style.width = `${Math.min(100, frac * 100).toFixed(1)}%`;
    $('mem-sub').textContent =
      `${Math.round(m.total_mib - m.used_mib).toLocaleString()} MiB headroom · `
      + `target ≤ ${Math.round(m.target_mib).toLocaleString()} MiB`;
    sparkline($('mem-spark'), history.mem, { min: 0, max: m.total_mib });
    if (history.mem.length > 1) {
      $('mem-range').textContent =
        `range ${Math.round(Math.min(...history.mem))}–${Math.round(Math.max(...history.mem))} MiB `
        + `over the last ${history.mem.length} samples`;
    }

    // ---- swap ---------------------------------------------------------
    const s = data.swap;
    $('swap-used').textContent = Math.round(s.used_mib).toLocaleString();
    $('swap-total').textContent = `of ${Math.round(s.total_mib).toLocaleString()} MiB`;
    const swapBar = $('swap-bar');
    swapBar.className = `bar${s.used_mib > 256 ? ' danger' : s.used_mib > 0 ? ' warn' : ''}`;
    swapBar.firstElementChild.style.width =
      `${s.total_mib ? Math.min(100, (s.used_mib / s.total_mib) * 100) : 0}%`;

    // ---- cpu ----------------------------------------------------------
    push(history.cpu, data.cpu.total);
    $('cpu-pct').textContent = data.cpu.total.toFixed(0);
    $('cpu-cores-note').textContent = `${data.cpu.cores.length} cores`;
    sparkline($('cpu-spark'), history.cpu, { min: 0, max: 100 });
    const cores = $('cores');
    if (cores.children.length !== data.cpu.cores.length) {
      cores.textContent = '';
      data.cpu.cores.forEach(() => cores.append(el('div', { class: 'core' }, el('span'))));
    }
    data.cpu.cores.forEach((pct, i) => {
      cores.children[i].firstElementChild.style.height = `${pct}%`;
      cores.children[i].title = `core ${i}: ${pct}%`;
    });

    // ---- gpu ----------------------------------------------------------
    const g = data.gpu || {};
    if (g.load !== null && g.load !== undefined) {
      push(history.gpu, g.load);
      $('gpu-pct').textContent = g.load.toFixed(0);
      sparkline($('gpu-spark'), history.gpu, { min: 0, max: 100, colour: 'var(--success)' });
    } else {
      $('gpu-pct').textContent = '–';
    }
    $('gpu-freq').textContent = g.freq_mhz ? `${g.freq_mhz} MHz` : '';
    $('gpu-note').textContent = g.freq_mhz >= 1000
      ? 'MAXN SUPER clocks' : g.freq_mhz ? 'below MAXN SUPER' : 'no devfreq node';

    // ---- power & thermals --------------------------------------------
    const rails = data.power || {};
    const total = rails.VDD_IN ?? Object.values(rails).reduce((a, b) => a + b, 0);
    $('power-total').textContent = total ? total.toFixed(1) : '–';
    const chips = $('power-chips');
    chips.textContent = '';
    for (const [name, watts] of Object.entries(rails)) {
      if (name === 'VDD_IN') continue;
      chips.append(el('span', { class: 'chip', text: `${name} ${watts.toFixed(1)} W` }));
    }
    const temps = $('temp-chips');
    temps.textContent = '';
    for (const [zone, celsius] of Object.entries(data.thermal || {})) {
      // The Orin's real throttle points are in the high 90s; 80 is the "watch it" line.
      temps.append(el('span', {
        class: `chip${celsius >= 80 ? ' hot' : ''}`,
        text: `${zone.replace('-thermal', '')} ${celsius.toFixed(0)}°C`,
      }));
    }

    // ---- per-service --------------------------------------------------
    const tbody = $('procs');
    tbody.textContent = '';
    if (!data.processes.length) {
      tbody.append(el('tr', {}, el('td', { colspan: '5', class: 'stat-sub',
        text: 'no robot services running' })));
    }
    for (const proc of data.processes) {
      const series = history.procs[proc.name] || (history.procs[proc.name] = []);
      push(series, proc.rss_mib);
      tbody.append(el('tr', {},
        el('td', { text: proc.name }),
        el('td', { class: 'stat-sub', text: proc.pid }),
        el('td', { class: 'num', text: `${Math.round(proc.rss_mib).toLocaleString()} MiB` }),
        el('td', { class: 'num', text: `${proc.cpu.toFixed(0)}%` }),
        el('td', { class: 'num stat-sub', text: trend(series) }),
      ));
    }
  }

  async function tick() {
    try {
      const res = await fetch('/api/monitor', { cache: 'no-store' });
      if (res.status === 401 || res.redirected) { location.href = '/login'; return; }
      paint(await res.json());
      liveDot.className = 'dot live';
      liveText.textContent = 'sampling';
    } catch (err) {
      liveDot.className = 'dot dead';
      liveText.textContent = 'sampling failed';
    }
  }

  function schedule() {
    clearInterval(timer);
    const period = Number($('refresh').value);
    if (period > 0) timer = setInterval(tick, period);
    if (period === 0) { liveDot.className = 'dot'; liveText.textContent = 'paused'; }
  }

  $('refresh').addEventListener('change', () => { schedule(); if (Number($('refresh').value)) tick(); });
  tick();
  schedule();
})();
