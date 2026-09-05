// NetBBS web terminal shim (design doc round 22/25).
//
// Speaks the structured JSON protocol netbbs.net.web.WebSession expects:
//   browser -> server: {"type": "key", "data": "<raw onData string>"}
//                       {"type": "resize", "cols": N, "rows": N}
//   server -> browser: {"type": "output", "data": "<text to display>"}
// Door mode adds stream-tagged door_key/door_output frames; output carries
// base64 UTF-8 bytes (legacy CP437 is converted by the server). A streaming
// decoder preserves code points split across frames.
//
// Menus deliberately do not use raw byte passthrough (addon-attach) --
// see design doc round 22 point 7 for why: a browser has already
// resolved the raw-terminal-byte ambiguity a byte-oriented protocol
// exists to handle, and structured messages give resize a first-class
// signal instead of a bolted-on side channel.
(function () {
  "use strict";

  var term = new Terminal({
    cursorBlink: true,
    scrollback: 2000,
    fontFamily: '"Cascadia Code", "Fira Code", "JetBrains Mono", "Consolas", "Courier New", monospace',
    fontSize: 15,
    letterSpacing: 0,
    lineHeight: 1.15,
    theme: {
      background: "#0c0d10",
      foreground: "#e2e8f0",
      cursor: "#f6ad55",
      cursorAccent: "#000000",
      selectionBackground: "#4a5568",
    },
  });
  var fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById("terminal"));
  fitAddon.fit();

  var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  var ws = new WebSocket(scheme + "//" + window.location.host + "/ws");
  var doorStream = null;
  var doorDecoder = null;
  var fixedDoorSize = false;

  function sendResize() {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "resize", cols: term.cols, rows: term.rows }));
    }
  }

  ws.onopen = function () {
    sendResize();
  };

  ws.onmessage = function (event) {
    var msg;
    try {
      msg = JSON.parse(event.data);
    } catch (e) {
      return;
    }
    if (msg.type === "door_mode") {
      if (doorDecoder) term.write(doorDecoder.decode());
      doorStream = msg.active ? msg.stream : null;
      doorDecoder = msg.active ? new TextDecoder("utf-8") : null;
      fixedDoorSize = !!(msg.active && msg.cols && msg.rows);
      if (fixedDoorSize) term.resize(msg.cols, msg.rows);
      else fitAddon.fit();
      if (!msg.active) sendResize();
    } else if (msg.type === "door_output" && msg.stream === doorStream && doorDecoder) {
      var bytes = Uint8Array.from(atob(msg.data), function (c) { return c.charCodeAt(0); });
      term.write(doorDecoder.decode(bytes, { stream: true }));
    } else if (msg.type === "output" && typeof msg.data === "string") {
      term.write(msg.data);
    }
  };

  ws.onclose = function () {
    term.write("\r\n\x1b[90m[Connection closed]\x1b[0m\r\n");
  };

  ws.onerror = function () {
    term.write("\r\n\x1b[90m[Connection error]\x1b[0m\r\n");
  };

  term.onData(function (data) {
    if (ws.readyState === WebSocket.OPEN) {
      // Bound pasted chunks and browser-side queued writes as well as server queues.
      var chars = Array.from(data);
      for (var i = 0; i < chars.length; i += 1024) {
        if (ws.bufferedAmount > 65536) { ws.close(); return; }
        ws.send(JSON.stringify({ type: doorStream === null ? "key" : "door_key",
                                stream: doorStream, data: chars.slice(i, i + 1024).join("") }));
      }
    }
  });

  window.addEventListener("resize", function () {
    if (!fixedDoorSize) fitAddon.fit();
    sendResize();
  });
})();
