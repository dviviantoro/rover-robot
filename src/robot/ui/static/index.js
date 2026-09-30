(() => {
  const badge = document.getElementById('state-badge');
  const dot = badge.querySelector('.dot');
  const stateText = document.getElementById('state-text');
  const light = document.getElementById('robot-light');
  const servicesEl = document.getElementById('services');
  const recentEl = document.getElementById('recent-body');
  const { el, HEALTH } = Robot;

  const STATE_COLOUR = {
    idle: 'var(--text-faint)', thinking: 'var(--warning)',
    speaking: 'var(--success)', unknown: 'var(--text-faint)',
  };

  function paintState() {
    const value = Robot.isConnected() ? Robot.state.robotState : 'offline';
    stateText.textContent = value;
    const colour = STATE_COLOUR[value] || 'var(--danger)';
    dot.style.background = colour;
    light.style.background = colour;
    light.style.boxShadow = `0 0 9px ${colour}`;
  }

  function paintServices() {
    servicesEl.textContent = '';
    if (!Robot.state.services.length) {
      servicesEl.className = 'muted';
      servicesEl.textContent = Robot.isConnected()
        ? 'Waiting for the orchestrator…'
        : 'Dashboard is not connected to the orchestrator.';
      return;
    }
    servicesEl.className = '';
    for (const svc of Robot.state.services) {
      const look = HEALTH[svc.state] || HEALTH.unknown;
      servicesEl.append(el('div', { class: 'svc-row' },
        el('span', { class: 'sdot', style: `background:${look.colour}` }),
        el('span', { class: 'sname', text: svc.label }),
        el('span', { class: `badge ${look.badge}`, text: svc.state }),
      ));
    }
  }

  const recent = [];
  function pushRecent(who, text) {
    recent.push({ who, text });
    while (recent.length > 6) recent.shift();
    recentEl.textContent = '';
    recentEl.className = '';
    for (const turn of recent) {
      recentEl.append(el('div', { class: 'turn' },
        el('b', { text: `${turn.who}: ` }), turn.text));
    }
  }

  Robot.on((message) => {
    switch (message.type) {
      case 'hello': case 'health': paintServices(); paintState(); break;
      case '_link': paintState(); paintServices(); break;
      case 'transcript': pushRecent('you', message.text); paintState(); break;
      case 'reply': pushRecent('robot', message.text); break;
      case 'turn_done': paintState(); break;
      case 'chunk': paintState(); break;
    }
  });

  paintState();
  Robot.connect();
})();
