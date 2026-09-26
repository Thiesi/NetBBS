// Executes the real browser shim's file-transfer path (issue #511) with
// doubles for the DOM, fetch, FormData and AbortController.
//
//   node transfer_web_shim.cjs <netbbs-terminal.js> <page href> <expected transfer base>
//
// The page href is where the terminal page was loaded from; the transfer
// base is where a link must point for the fetch to be same-origin and keep
// the page's reverse-proxy prefix.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const [scriptPath, pageHref, expectedBase] = process.argv.slice(2);

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.listeners = {}; this.parent = null;
    this.textContent = ''; this.id = ''; this.disabled = false; this.files = null;
    this.classList = {add() {}, remove() {}};
    this.selected = {};
  }
  appendChild(child) { this.children.push(child); child.parent = this; return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); child.parent = null; }
  remove() { if (this.parent) this.parent.removeChild(this); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  focus() {}
  click() {
    if (this.tagName === 'a') clicks.push({href: this.href, download: this.download});
    if (this.listeners.click) this.listeners.click();
  }
  // The upload panel is built with innerHTML; its named parts are handed
  // out as stable doubles so a test can drive them.
  querySelector(selector) { return this.selected[selector] ||= new Element('stub'); }
  *walk() { yield this; for (const child of this.children) yield* child.walk(); }
}

const clicks = [];
const fetches = [];
let replies = [];
const body = new Element('body');
const document = {
  body,
  createElement: tag => new Element(tag),
  getElementById(id) {
    for (const element of body.walk()) if (element.id === id) return element;
    return id === 'transfer-panel' ? null : new Element('div');
  },
};

function respond(status, headers = {}) {
  return {
    ok: status >= 200 && status < 300, status,
    headers: {get: name => headers[name] ?? null},
    text: async () => '', json: async () => ({filename: 'up.bin'}),
  };
}

function fetch(url, init = {}) {
  fetches.push({url, method: init.method || 'GET'});
  const reply = replies.shift();
  if (reply === 'reject') return Promise.reject(new TypeError('Failed to fetch'));
  return Promise.resolve(reply);
}

let term, socket;
class Terminal {
  constructor() { term = this; this.cols = 80; this.rows = 24; }
  loadAddon() {} open() {} write() {} resize() {} focus() {}
  onData(callback) { this.input = callback; }
}
class WebSocket {
  static OPEN = 1;
  constructor() { socket = this; this.readyState = 1; this.bufferedAmount = 0; this.sent = []; }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  close() { this.readyState = 3; }
}

vm.runInNewContext(fs.readFileSync(scriptPath, 'utf8'), {
  Terminal, WebSocket, TextDecoder, Uint8Array, URL, document, fetch, setTimeout,
  FormData: class { append() {} },
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  atob: value => Buffer.from(value, 'base64').toString('binary'),
  FitAddon: {FitAddon: class { fit() {} }},
  window: {location: {protocol: 'https:', host: new URL(pageHref).host, href: pageHref}, addEventListener() {}},
});

const receive = value => socket.onmessage({data: JSON.stringify(value)});
const settle = () => new Promise(resolve => setImmediate(resolve));
const panel = () => document.getElementById('transfer-panel');
const panelText = () => [...panel().walk()].map(e => e.textContent).join(' | ');
const buttons = () => [...panel().walk()].filter(e => e.tagName === 'button');

// The link is built from `[web] public_url`, a different origin here.
const offered = 'https://public.example.net/elsewhere/transfer/TOKEN123';
const target = expectedBase + 'transfer/TOKEN123';

(async () => {
  // 1. A good download: probed with HEAD, then saved through the anchor,
  //    both at the same-origin URL that keeps the page's prefix.
  replies = [respond(200)];
  receive({type: 'transfer', direction: 'download', url: offered, filename: 'game.zip'});
  await settle();
  assert.deepEqual(fetches, [{url: target, method: 'HEAD'}]);
  assert.deepEqual(clicks, [{href: target, download: 'game.zip'}]);
  assert.equal(panel(), null);

  // 2. A refused download saves nothing and says why, in the server's words.
  fetches.length = 0; clicks.length = 0;
  replies = [respond(403, {'X-NetBBS-Transfer-Message': 'you may no longer read this file area'})];
  receive({type: 'transfer', direction: 'download', url: offered, filename: 'game.zip'});
  await settle();
  assert.equal(clicks.length, 0);
  assert.match(panelText(), /you may no longer read this file area/);
  assert.deepEqual(buttons().map(b => b.textContent), ['Close']);
  buttons()[0].click();
  assert.equal(panel(), null);

  // 3. A busy node can be asked again, and the second answer is honoured.
  replies = [respond(429, {'X-NetBBS-Transfer-Message': 'This node is already busy sending files.'}), respond(200)];
  receive({type: 'transfer', direction: 'download', url: offered, filename: 'game.zip'});
  await settle();
  assert.equal(clicks.length, 0);
  assert.deepEqual(buttons().map(b => b.textContent), ['Try again', 'Close']);
  buttons()[0].click();
  await settle();
  assert.deepEqual(clicks, [{href: target, download: 'game.zip'}]);

  // 4. An unreachable node saves nothing either.
  clicks.length = 0;
  replies = ['reject'];
  receive({type: 'transfer', direction: 'download', url: offered, filename: 'game.zip'});
  await settle();
  assert.equal(clicks.length, 0);
  assert.match(panelText(), /Could not reach the BBS/);
  buttons().at(-1).click();

  // 5. An upload posts to the same-origin URL too.
  fetches.length = 0;
  replies = [respond(200)];
  receive({type: 'transfer', direction: 'upload', url: offered});
  const input = panel().querySelector('#transfer-input');
  input.files = [{name: 'up.bin'}];
  input.listeners.change();
  await settle();
  assert.deepEqual(fetches, [{url: target, method: 'POST'}]);
})().catch(error => { console.error(error); process.exit(1); });
