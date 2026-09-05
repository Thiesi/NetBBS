// Executes the real browser shim with small xterm/socket API doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
let term, socket, resize, fits = 0;
class Terminal {
  constructor() { term = this; this.cols = 120; this.rows = 40; this.output = ''; }
  loadAddon() {} open() {}
  write(data) { this.output += data; }
  resize(cols, rows) { this.cols = cols; this.rows = rows; }
  onData(callback) { this.input = callback; }
}
class WebSocket {
  static OPEN = 1;
  constructor() { socket = this; this.readyState = 1; this.bufferedAmount = 0; this.sent = []; }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  close() { this.readyState = 3; }
}
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), {
  Terminal, WebSocket, TextDecoder, Uint8Array,
  atob: value => Buffer.from(value, 'base64').toString('binary'),
  FitAddon: {FitAddon: class {fit() {fits++; term.resize(120, 40);}}},
  document: {getElementById() {return {}; }},
  window: {location: {protocol: 'https:', host: 'example.test'}, addEventListener(_, fn) {resize = fn;}}
});
const receive = value => socket.onmessage({data: JSON.stringify(value)});
receive({type:'door_mode', active:true, stream:1, cols:80, rows:25, encoding:'cp437'});
assert.equal(term.cols, 80);
resize(); assert.equal(term.cols, 80);
const bytes = Buffer.from('é█\x1b[0m');
for (const byte of bytes) receive({type:'door_output', stream:1, data:Buffer.from([byte]).toString('base64')});
assert.equal(term.output, 'é█\x1b[0m');
term.input('\x1b[A');
assert.equal(socket.sent.at(-1).type, 'door_key');
assert.equal(socket.sent.at(-1).data, '\x1b[A');
receive({type:'door_mode', active:false, stream:1});
assert.equal(term.cols, 120);
receive({type:'door_output', stream:1, data:Buffer.from('stale').toString('base64')});
assert.equal(term.output, 'é█\x1b[0m');
term.input('B'); assert.equal(socket.sent.at(-1).type, 'key');
receive({type:'door_mode', active:true, stream:2});
term.input('é'.repeat(3000));
assert.ok(socket.sent.slice(-3).every(v => Array.from(v.data).length <= 1024 && v.stream === 2));
socket.bufferedAmount = 65537; term.input('overflow'); assert.equal(socket.readyState, 3);
