// NetBBS web terminal shim (design doc round 22/25).
//
// Speaks the structured JSON protocol netbbs.net.web.WebSession expects:
//   browser -> server: {"type": "key", "data": "<raw onData string>"}
//                       {"type": "resize", "cols": N, "rows": N}
//   server -> browser: {"type": "output", "data": "<text to display>"}
//
// Deliberately not raw byte passthrough (xterm.js's addon-attach) --
// see design doc round 22 point 7 for why: a browser has already
// resolved the raw-terminal-byte ambiguity a byte-oriented protocol
// exists to handle, and structured messages give resize a first-class
// signal instead of a bolted-on side channel.
(function () {
  "use strict";

  var term = new Terminal({
    cursorBlink: true,
    scrollback: 2000,
    theme: { background: "#000000" },
  });
  var fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(document.getElementById("terminal"));
  fitAddon.fit();

  var scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
  var ws = new WebSocket(scheme + "//" + window.location.host + "/ws");

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
    if (msg.type === "output" && typeof msg.data === "string") {
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
      ws.send(JSON.stringify({ type: "key", data: data }));
    }
  });

  window.addEventListener("resize", function () {
    fitAddon.fit();
    sendResize();
  });
})();
