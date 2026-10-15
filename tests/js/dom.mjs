/* A DOM small enough to read and big enough to run the dashboard's own scripts.
 *
 * `node --check` parses; it does not EXECUTE, so it is blind to a call to a function
 * that does not exist. That blindness shipped a blank dashboard: a later edit spliced
 * away the block defining `shell`, `NAV` and `icon`, the module's return object then
 * referenced three undefined identifiers, the IIFE threw, `window.Robot` was never
 * assigned, and every page went white. The whole Python suite passed.
 *
 * So this executes the real files and fails on any throw. It is not a browser and does
 * not pretend to be: no layout, no CSS. It answers one question -- does this code run.
 */
class Node {
  constructor(tag = 'div') {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.dataset = {};
    this._text = '';
    this.className = '';
    this.hidden = false;
    this.parentElement = null;
    this.classList = {
      _set: new Set(),
      add: (c) => this.classList._set.add(c),
      remove: (c) => this.classList._set.delete(c),
      toggle: (c) => (this.classList._set.has(c)
        ? this.classList._set.delete(c) : this.classList._set.add(c)),
      contains: (c) => this.classList._set.has(c),
    };
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent ?? '').join('');
  }
  set innerHTML(v) { this._text = String(v); }
  get innerHTML() { return this._text; }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(k, fn) { (this.listeners[k] ||= []).push(fn); }
  append(...kids) {
    for (const kid of kids) {
      const node = (kid && kid.tagName) ? kid : new Text(String(kid));
      node.parentElement = this;
      this.children.push(node);
    }
  }
  appendChild(kid) { this.append(kid); }
  remove() {
    if (!this.parentElement) return;
    const i = this.parentElement.children.indexOf(this);
    if (i >= 0) this.parentElement.children.splice(i, 1);
  }
  querySelector() { return new Node('div'); }
  querySelectorAll() { return []; }
  closest() { return null; }
  focus() {}
  scrollTo() {}
  get scrollHeight() { return 0; }
  get clientHeight() { return 0; }
}
class Text {
  constructor(data) { this.nodeType = 3; this.data = data; this.parentElement = null; }
  get textContent() { return this.data; }
}

export function install({ page = '', ids = [] } = {}) {
  const byId = new Map();
  const body = new Node('body');
  body.dataset.page = page;
  const document = {
    body,
    documentElement: new Node('html'),
    createElement: (tag) => new Node(tag),
    createElementNS: (_ns, tag) => new Node(tag),
    createTextNode: (t) => new Text(String(t)),
    // Any id the page asks for exists. A real page either has the element or the
    // script already guards for it; inventing one keeps this about code paths.
    getElementById: (id) => {
      if (!byId.has(id)) {
        const node = new Node('div');
        // Real elements have a parent. Without one, any code that walks upwards --
        // `el.parentElement.scrollTop` -- throws here and nowhere else.
        node.parentElement = new Node('div');
        byId.set(id, node);
      }
      return byId.get(id);
    },
    querySelector: () => new Node('div'),
    querySelectorAll: () => [],
    addEventListener: () => {},
    cookie: '',
  };
  for (const id of ids) document.getElementById(id);

  const sockets = [];
  class FakeSocket {
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = 1;
      sockets.push(this);
    }
    send() {}
    close() { if (this.onclose) this.onclose({ code: 1000 }); }
  }

  const g = globalThis;
  g.document = document;
  g.window = g;
  g.location = { host: 'localhost:8081', protocol: 'http:', search: '', href: '' };
  g.WebSocket = FakeSocket;
  g.Node = Node;
  g.addEventListener = () => {};
  g.removeEventListener = () => {};
  g.scrollTo = () => {};
  g.scrollBy = () => {};
  g.setTimeout = ((fn, ms) => 0);        // never fire: no reconnect storm in a test
  g.clearTimeout = () => {};
  g.setInterval = () => 0;
  g.clearInterval = () => {};
  g.fetch = async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' });
  g.requestAnimationFrame = (fn) => { fn(0); return 0; };
  g.EventSource = class { close() {} };
  g.alert = () => {};
  g.confirm = () => true;
  g.navigator = { userAgent: 'node', clipboard: { writeText: async () => {} } };
  return { document, body, sockets };
}
