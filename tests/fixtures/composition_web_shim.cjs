// Executes the real browser shim's handling of keyboard compositions
// (issue #1066) against a double of xterm.js's CompositionHelper.
//
//   node composition_web_shim.cjs <netbbs-terminal.js> <android|desktop>
//
// The double copies what the bundled xterm.js does with a composition: it
// remembers where in its textarea the composition began, sends the text
// from there when the composition ends (on a timer), or at once when
// another key ends it, and Enter empties the textarea. An input method is
// played by writing the textarea and dispatching the events a browser
// dispatches, capture listeners on xterm's element first.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const [scriptPath, platform] = process.argv.slice(2);
const android = platform === 'android';

class Node {
  constructor(parent) {
    this.parent = parent; this.capture = {}; this.bubble = {}; this.attributes = {};
    this.value = ''; this.classes = new Set();
    this.classList = {add: name => this.classes.add(name)};
  }
  addEventListener(name, fn, capture) {
    const table = capture ? this.capture : this.bubble;
    (table[name] ||= []).push(fn);
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  querySelector() { return null; }
}

// Capture listeners from the outside in, then the target's own.
function dispatch(target, name, fields = {}) {
  const event = {type: name, ...fields};
  const path = [];
  for (let node = target.parent; node; node = node.parent) path.unshift(node);
  for (const node of path) for (const fn of node.capture[name] || []) fn(event);
  for (const fn of target.capture[name] || []) fn(event);
  for (const fn of target.bubble[name] || []) fn(event);
}

let term, socket;
class Terminal {
  constructor() { term = this; this.cols = 80; this.rows = 24; }
  loadAddon() {} write() {} resize() {} focus() {}
  open() {
    this.element = new Node(null);
    this.textarea = new Node(this.element);
    installXtermDouble(this.textarea, data => this.input(data));
  }
  onData(callback) { this.input = callback; }
}

function installXtermDouble(textarea, fire) {
  // CompositionHelper: `start` is the textarea's length when a composition
  // begins, and a finished one is sent from there, past `alreadySent`.
  let composing = false, sending = false, start = 0, end = 0, alreadySent = '';
  let keyDownSeen = false;
  function finalize(wait) {
    composing = false;
    if (wait) {
      const position = {start, end};
      sending = true;
      setTimeout(() => {
        if (!sending) return;
        sending = false;
        position.start += alreadySent.length;
        const text = composing ? textarea.value.substring(position.start, start)
          : textarea.value.substring(position.start);
        if (text.length > 0) fire(text);
      }, 0);
    } else {
      sending = false;
      fire(textarea.value.substring(start, end));
    }
  }
  // A key the keyboard did not compose (keyCode 229): what it adds to the
  // textarea is sent on a timer, and stays in the textarea.
  function anyTextareaChanges() {
    const before = textarea.value;
    setTimeout(() => {
      if (composing) return;
      const after = textarea.value, added = after.replace(before, '');
      alreadySent = added;
      if (after.length > before.length) fire(added);
      else if (after.length < before.length) fire('\x7f');
      else if (after !== before) fire(after);
    }, 0);
  }
  textarea.addEventListener('compositionstart', () => {
    composing = true; start = textarea.value.length; alreadySent = '';
  });
  textarea.addEventListener('compositionupdate', () => { setTimeout(() => { end = textarea.value.length; }, 0); });
  textarea.addEventListener('compositionend', () => finalize(true));
  textarea.addEventListener('keydown', event => {
    keyDownSeen = true;
    if (composing || sending) {
      if (event.keyCode === 229 || event.keyCode === 20) return;
      if ([16, 17, 18].includes(event.keyCode)) return;
      finalize(false);
    }
    if (event.keyCode === 229) { anyTextareaChanges(); return; }
    if (event.keyCode === 13) { textarea.value = ''; fire('\r'); }
  });
  textarea.addEventListener('keyup', () => { keyDownSeen = false; });
  // Terminal._inputEvent: text inserted with no keydown seen is sent and
  // kept out of the textarea; otherwise the browser inserts it.
  textarea.addEventListener('input', event => {
    if (event.data && event.inputType === 'insertText' && !keyDownSeen) {
      fire(event.data);
      event.cancelled = true;
    }
  });
}

class WebSocket {
  static OPEN = 1;
  constructor() { socket = this; this.readyState = 1; this.bufferedAmount = 0; this.sent = []; }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  close() { this.readyState = 3; }
}

const context = {
  Terminal, WebSocket, TextDecoder, Uint8Array, URL, setTimeout,
  atob: value => Buffer.from(value, 'base64').toString('binary'),
  FitAddon: {FitAddon: class { fit() {} }},
  document: {getElementById() { return {}; }},
  navigator: {userAgent: android
    ? 'Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0'
    : 'Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0'},
  window: {location: {protocol: 'https:', host: 'example.test', href: 'https://example.test/'},
           addEventListener() {}},
};
vm.runInNewContext(fs.readFileSync(scriptPath, 'utf8'), context);

const textarea = term.textarea;
const settle = () => new Promise(resolve => setTimeout(() => setTimeout(() => setTimeout(resolve, 0), 0), 0));
const sent = () => socket.sent.map(frame => frame.data);
const clear = () => { socket.sent.length = 0; };

// An input method: the word it composes sits in the textarea after
// whatever was there when the composition began.
let base = '';
const ime = {
  // The first key of a word. Like every key, it is its own task: xterm's
  // timers for it run before the next key, while the word is composed.
  async start(first) {
    dispatch(textarea, 'keydown', {keyCode: 229});
    base = textarea.value;
    dispatch(textarea, 'compositionstart', {data: ''});
    this.update(first);
    await settle();
  },
  update(word) {
    textarea.value = base + word;
    dispatch(textarea, 'compositionupdate', {data: word});
    dispatch(textarea, 'input', {inputType: 'insertCompositionText', data: word, isComposing: true});
  },
  type(word) { dispatch(textarea, 'keydown', {keyCode: 229}); this.update(word); },
  // A key the keyboard does not compose (a digit, a comma), with or
  // without the keydown a browser may or may not dispatch first.
  plain(char, keydown = true) {
    if (keydown) dispatch(textarea, 'keydown', {keyCode: 229});
    const event = {inputType: 'insertText', data: char};
    dispatch(textarea, 'input', event);
    if (!event.cancelled) textarea.value += char;
    if (keydown) dispatch(textarea, 'keyup', {keyCode: 229});
  },
  // The keyboard's space or comma that ends a word: its keydown comes while
  // the word is still composed, the character after the composition ends.
  delimit(word, char) {
    dispatch(textarea, 'keydown', {keyCode: 229});
    this.end(word);
    const event = {inputType: 'insertText', data: char};
    dispatch(textarea, 'input', event);
    if (!event.cancelled) textarea.value += char;
    dispatch(textarea, 'keyup', {keyCode: 229});
  },
  // `word` null: the composition ends without the keyboard writing it again.
  end(word) {
    if (word !== null) textarea.value = base + word;
    dispatch(textarea, 'compositionend', {data: word});
  },
};

(async () => {
  // Prediction is asked off on every platform.
  for (const [name, value] of [['autocomplete', 'off'], ['autocorrect', 'off'],
                               ['autocapitalize', 'off'], ['spellcheck', 'false']]) {
    assert.equal(textarea.attributes[name], value, name);
  }

  if (!android) {
    // A desktop input method is left to xterm: nothing goes out while the
    // word is composed, and the converted text goes out once, as before.
    assert.equal(term.element.classes.size, 0);
    await ime.start('n'); ime.type('ni'); ime.type('nih');
    assert.deepEqual(sent(), []);
    ime.end('日本');
    await settle();
    assert.deepEqual(sent(), ['日本']);
    // And a plain key is sent byte for byte.
    clear();
    term.input('m'); term.input('\x1b[A');
    assert.deepEqual(sent(), ['m', '\x1b[A']);
    return;
  }

  assert.ok(term.element.classes.has('netbbs-mirrored-composition'));

  // 1. A letter hotkey goes out on the keystroke, with no Enter; a second
  //    key in the same word goes out alone; nothing is sent twice.
  await ime.start('m');
  assert.deepEqual(sent(), ['m']);
  ime.type('mb');
  assert.deepEqual(sent(), ['m', 'b']);

  // 2. Backspace inside the word deletes on the server too.
  ime.type('m');
  assert.deepEqual(sent(), ['m', 'b', '\x7f']);
  ime.type('mo');
  ime.end('mo');
  await settle();
  assert.deepEqual(sent(), ['m', 'b', '\x7f', 'o']);
  assert.equal(textarea.value, '');

  // 3. A line typed and ended with Enter while still composed: the letters
  //    went out as typed, Enter goes out once, the word is not repeated.
  clear();
  await ime.start('h'); ime.type('hi');
  dispatch(textarea, 'keydown', {keyCode: 13});
  ime.end('hi');
  await settle();
  assert.deepEqual(sent(), ['h', 'i', '\r']);
  assert.equal(textarea.value, '');

  // 4. A word the keyboard corrects as it ends keeps what was typed: the
  //    letters are already out, and a hotkey must not answer twice.
  clear();
  await ime.start('t'); ime.type('te'); ime.type('teh');
  ime.end('the');
  await settle();
  assert.deepEqual(sent(), ['t', 'e', 'h']);

  // 5. The space that ends a word goes out after it, once, and the next
  //    word starts from an empty textarea and is not swallowed.
  clear();
  await ime.start('a');
  ime.delimit('a', ' ');
  await settle();
  assert.deepEqual(sent(), ['a', ' ']);
  assert.equal(textarea.value, '');
  await ime.start('a');
  ime.end('a');
  await settle();
  assert.deepEqual(sent(), ['a', ' ', 'a']);

  // 5b. A key the keyboard does not compose stays in xterm's textarea; a
  //     word after it, and the space ending that word, still go out once
  //     each -- and so does a key after a word.
  for (const keydown of [true, false]) {
    clear();
    ime.plain('5', keydown);
    await settle();
    await ime.start('h'); ime.type('he'); ime.type('hel'); ime.type('hell'); ime.type('hello');
    ime.delimit('hello', ' ');
    await settle();
    ime.plain(',', keydown);
    await settle();
    await ime.start('o'); ime.type('ok');
    ime.delimit('ok', '.');
    await settle();
    assert.deepEqual(sent(), ['5', 'h', 'e', 'l', 'l', 'o', ' ', ',', 'o', 'k', '.'], `keydown ${keydown}`);
  }

  // 6. Characters beyond the BMP count as one each when deleted.
  clear();
  await ime.start('x😀');
  ime.type('x');
  ime.end('x');
  await settle();
  assert.deepEqual(sent(), ['x😀', '\x7f']);

  // 7. Should a browser deliver the finished word some other way, it is
  //    still not sent twice -- but the same word typed afterwards is.
  clear();
  await ime.start('q');
  ime.end('q');
  term.input('q');
  await settle();
  term.input('q');
  assert.deepEqual(sent(), ['q', 'q']);

  // 8. In a door the mirrored keys are door keys on the door's stream.
  clear();
  socket.onmessage({data: JSON.stringify({type: 'door_mode', active: true, stream: 7})});
  await ime.start('n');
  ime.end('n');
  await settle();
  assert.deepEqual(socket.sent, [{type: 'door_key', stream: 7, data: 'n'}]);
})().catch(error => { console.error(error); process.exit(1); });
