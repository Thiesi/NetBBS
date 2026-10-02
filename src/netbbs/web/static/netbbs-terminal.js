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
    } else if (msg.type === "transfer" && typeof msg.url === "string") {
      // Issue #475: the BBS has handed this browser a one-use transfer
      // link. A download starts by itself; an upload opens a drop
      // target, because the caller is already in a browser and should
      // not have to copy a URL out of a terminal.
      if (msg.direction === "download") startDownload(msg.url, msg.filename);
      else openUploadPanel(msg.url);
    }
  };

  ws.onclose = function () {
    term.write("\r\n\x1b[90m[Connection closed]\x1b[0m\r\n");
  };

  ws.onerror = function () {
    term.write("\r\n\x1b[90m[Connection error]\x1b[0m\r\n");
  };

  // Where a chunk of `chars` from `start` may end at or before `end`
  // without cutting an escape sequence in two (issue #754): the server
  // parses each message on its own, so a pasted color split across two
  // would lose its `ESC[` and leave `31m` in the text. Only the last 64
  // characters are searched -- the server's own cap on one sequence.
  function chunkEnd(chars, start, end) {
    if (end >= chars.length) return chars.length;
    for (var j = end - 1; j > start && j >= end - 64; j--) {
      if (chars[j] !== "\x1b") continue;
      if (chars[j + 1] === "[") {
        for (var k = j + 2; k < end; k++) {
          if (chars[k] >= "@" && chars[k] <= "~") return end;
        }
        return j;
      }
      return j + 2 < end ? end : j;
    }
    return end;
  }

  function sendData(data) {
    if (ws.readyState === WebSocket.OPEN) {
      // Bound pasted chunks and browser-side queued writes as well as server queues.
      var chars = Array.from(data);
      for (var i = 0; i < chars.length;) {
        var end = chunkEnd(chars, i, i + 1024);
        if (ws.bufferedAmount > 65536) { ws.close(); return; }
        ws.send(JSON.stringify({ type: doorStream === null ? "key" : "door_key",
                                stream: doorStream, data: chars.slice(i, end).join("") }));
        i = end;
      }
    }
  }

  term.onData(function (data) {
    if (composer && composer.swallows(data)) return;
    sendData(data);
  });

  // -- Android keyboards (issue #1066) --------------------------------------
  //
  // An Android keyboard with prediction composes every letter, and xterm.js
  // sends a composition only when it ends -- on Enter, space or punctuation.
  // A letter hotkey therefore waited for Enter while `?` acted at once. On
  // Android the composition is mirrored to the server as it is typed: each
  // change to the composed word goes out at once, as the letters added and a
  // DEL for each letter taken away, and xterm's own send of the word when the
  // composition ends is suppressed, so nothing arrives twice.
  //
  // Only on Android: a desktop input method (Japanese, Chinese) composes
  // romaji or pinyin that is then converted, and mirroring would send the
  // keys of the spelling rather than the text chosen. Desktop typing does not
  // touch any of this, and its bytes are unchanged.
  //
  // What the word becomes *as* it ends is not sent: the letters are already
  // out, so a keyboard that still autocorrects "teh" into "the" at the space
  // leaves "teh" on the line. Sending the correction too would turn a hotkey
  // pressed once into two answers. The prediction attributes below ask the
  // keyboard not to correct at all; some keyboards ignore them, which is why
  // the mirroring is needed as well.

  // The keys that turn `sent` (what the server already has of the word) into
  // `now` (what the keyboard shows): a DEL for each code point after their
  // common start, then the rest of `now`.
  function compositionEdit(sent, now) {
    var before = Array.from(sent), after = Array.from(now);
    var same = 0;
    while (same < before.length && same < after.length && before[same] === after[same]) same++;
    return "\x7f".repeat(before.length - same) + after.slice(same).join("");
  }

  var composer = null;
  var textarea = term.textarea;
  if (textarea) {
    // Prediction, correction and capitals off: with them the keyboard
    // composes every word (and capitalises the first letter of an empty
    // field, which the textarea is after each key). `inputmode` and
    // `enterkeyhint` stay unset: the default text keyboard is the right one
    // for letters, digits and punctuation, and a hint only relabels Enter.
    textarea.setAttribute("autocomplete", "off");
    textarea.setAttribute("autocorrect", "off");
    textarea.setAttribute("autocapitalize", "off");
    textarea.setAttribute("spellcheck", "false");
  }
  var android = typeof navigator !== "undefined" && /Android/i.test(navigator.userAgent || "");
  if (textarea && android && term.element) {
    composer = (function () {
      var composing = false;
      var sent = "";      // what the server has of the word being composed
      var ended = null;   // the finished word, until xterm's own send of it has passed

      function mirror(now) {
        if (typeof now !== "string") return;
        var edit = compositionEdit(sent, now);
        sent = now;
        if (edit) sendData(edit);
      }

      // xterm sends a finished composition as what its textarea holds past
      // the length the textarea had when the composition began. So the
      // textarea must be empty whenever a composition can begin: anything
      // left in it -- a digit or comma the keyboard did not compose, which
      // xterm sends from the textarea and leaves there -- moves that offset
      // past the start, and once `finish` has emptied the textarea the
      // space or comma ending the next word falls before the offset and is
      // never sent (review of #1067). Two turns after a key, xterm's own
      // timers for it have sent it; the textarea is emptied then, unless a
      // composition has begun. Not at `compositionstart`: setting a field's
      // value while a composition is open ends the composition.
      function emptySoon() {
        setTimeout(function () {
          setTimeout(function () {
            ended = null;
            if (!composing) textarea.value = "";
          }, 0);
        }, 0);
      }

      // Emptying the textarea is also what keeps xterm from sending the word
      // a second time, and means the next key starts a new word rather than
      // extending one the keyboard remembers. Listeners in the capture phase
      // on xterm's own element run before xterm's handlers on the textarea.
      function finish() {
        if (!composing) return;
        composing = false;
        ended = sent;
        sent = "";
        textarea.value = "";
        // Again once xterm has sent whatever ended the word (a space, a
        // comma), which it reads from the textarea.
        emptySoon();
      }

      var root = term.element;
      root.classList.add("netbbs-mirrored-composition");
      root.addEventListener("compositionstart", function () {
        composing = true;
        sent = "";
        ended = null;
      }, true);
      root.addEventListener("compositionupdate", function (event) {
        if (composing) mirror(event.data);
      }, true);
      root.addEventListener("input", function (event) {
        if (composing && event.inputType === "insertCompositionText") mirror(event.data);
        else if (!composing) emptySoon();
      }, true);
      root.addEventListener("compositionend", finish, true);
      // A key other than the keyboard's own (229) or a modifier ends the
      // composition in xterm, which then sends the word straight away.
      root.addEventListener("keydown", function (event) {
        var code = event.keyCode;
        if (composing && code !== 229 && code !== 16 && code !== 17 && code !== 18 && code !== 20) {
          finish();
        }
      }, true);

      return {
        // Whether xterm's `onData` must not be sent: nothing (the emptied
        // textarea's word), or the finished word itself should a browser
        // send it from elsewhere than the textarea.
        swallows: function (data) {
          if (data === "") return true;
          if (ended !== null && data === ended) { ended = null; return true; }
          return false;
        },
      };
    })();
  }


  // -- clicking a key (issue #840) ----------------------------------------
  //
  // A first-time caller in a browser clicks "[C]hat" and expects it to
  // work. A click on a menu entry sends its bracketed key, and a click on
  // a numbered list row sends its number, exactly as if typed. A click on
  // anything else says once that the terminal is driven by the keyboard.
  // Door games get their clicks left alone; a drag still selects text. The
  // server ignores a click while a line or a post is being typed.
  function sendKey(key) {
    if (ws.readyState !== WebSocket.OPEN) return;
    // Its own type, so the server can drop it while text is being typed.
    ws.send(JSON.stringify({ type: "click", data: key }));
  }

  // The key a click at `col` on `text` means, or null. Menu entries are
  // separated by two or more spaces, so the entry around the click is the
  // run between such gaps; its bracketed letter is its key.
  function keyAt(text, col) {
    var row = /^(?:> |  )?\s*(\d{2})\.\s/.exec(text);
    if (row) return row[1];
    // A numbered row drawn inside SysOp art (issue #929) starts after the
    // art's own frame -- box-drawing or punctuation, no letters or digits --
    // and the whole row picks it, its value column included.
    var drawn = /^[^A-Za-z0-9\[]*?\s(\d{2})\.\s/.exec(text);
    if (drawn) return drawn[1];
    var start = col, end = col;
    while (start > 0 && !(text[start - 1] === " " && text[start - 2] === " ")) start--;
    while (end < text.length && !(text[end] === " " && text[end + 1] === " ")) end++;
    var entry = /\[([^\]\s])\]/.exec(text.slice(start, end));
    return entry ? entry[1].toLowerCase() : null;
  }

  var hint = null;
  function showKeyboardHint() {
    if (hint) return;
    hint = document.createElement("div");
    hint.textContent = "Use your keyboard: press the letter in [brackets]. Clicking a [letter] works too.";
    hint.style.cssText = "position:fixed;left:50%;bottom:1rem;transform:translateX(-50%);" +
      "background:#14161b;color:#e2e8f0;border:1px solid #4a5568;border-radius:4px;" +
      "padding:.4rem .8rem;font:14px system-ui,sans-serif;z-index:5;";
    document.body.appendChild(hint);
    setTimeout(function () { hint.remove(); hint = null; }, 5000);
  }

  if (term.element) term.element.addEventListener("mouseup", function (event) {
    if (event.button !== 0 || doorStream !== null || term.hasSelection()) return;
    var screen = term.element.querySelector(".xterm-screen");
    if (!screen) return;
    var box = screen.getBoundingClientRect();
    var col = Math.floor((event.clientX - box.left) / (box.width / term.cols));
    var row = Math.floor((event.clientY - box.top) / (box.height / term.rows));
    if (col < 0 || row < 0 || col >= term.cols || row >= term.rows) return;
    var buffer = term.buffer.active;
    var line = buffer.getLine(buffer.viewportY + row);
    var key = line ? keyAt(line.translateToString(true), col) : null;
    if (key) sendKey(key);
    else showKeyboardHint();
    term.focus();
  });

  // -- file transfer (issue #475) ---------------------------------------
  //
  // Zmodem cannot work in a browser tab, so the BBS hands this page a
  // single-use HTTP link instead. Everything below is presentation: the
  // link is already scoped to one caller, one file or area, and one use
  // by the server, and nothing here can widen it.

  function transferUrl(url) {
    // The link the BBS hands over is built from `[web] public_url`, which
    // need not be the origin this page was reached on (issue #511): a
    // cross-origin fetch cannot read the response, and the endpoint sends
    // no CORS headers. Resolving the token against this page's own address
    // is same-origin by construction and keeps whatever path prefix a
    // reverse proxy serves the page under (`/bbs/` -> `/bbs/transfer/...`).
    try {
      var token = new URL(url).pathname.split("/").pop();
      // The page's own path as a directory, always: the node serves this
      // page only at `/`, so whatever path the browser shows is the mount
      // point itself -- `/`, or a proxy prefix with or without its slash.
      // Resolving against `/bbs` rather than `/bbs/` would drop the prefix
      // (Codex review of #702), and a prefix may contain a dot.
      var page = new URL(window.location.href);
      if (!page.pathname.endsWith("/")) page.pathname += "/";
      return new URL("transfer/" + token, page).href;
    } catch (error) {
      return url;
    }
  }

  function startDownload(url, filename) {
    var target = transferUrl(url);
    if (typeof fetch !== "function") {
      saveDownload(target, filename);
      return;
    }
    // Probe first (issue #511): an `<a download>` saves whatever comes
    // back under the requested name, so a refused download would land as
    // a file that looks like the one asked for. HEAD spends nothing and
    // answers as the GET would; only a yes starts the real download, which
    // the anchor then streams to disk rather than this page buffering it.
    fetch(target, { method: "HEAD", cache: "no-store", credentials: "same-origin" })
      .then(function (response) {
        if (response.ok) {
          saveDownload(target, filename);
          return;
        }
        var reason = response.headers.get("X-NetBBS-Transfer-Message") ||
          ("The BBS refused the download (HTTP " + response.status + ").");
        showTransferNotice("Download failed", reason,
          response.status === 429 ? function () { startDownload(url, filename); } : null);
      })
      .catch(function () {
        showTransferNotice("Download failed",
          "Could not reach the BBS to start the download. Ask it for a new link.", null);
      });
  }

  function saveDownload(target, filename) {
    // An anchor click rather than assigning window.location, so the tab
    // keeps the live terminal session rather than navigating away from
    // it mid-transfer.
    var link = document.createElement("a");
    link.href = target;
    if (filename) link.download = filename;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

  function showTransferNotice(title, message, retry) {
    var existing = document.getElementById("transfer-panel");
    if (existing) existing.remove();

    var panel = document.createElement("div");
    panel.id = "transfer-panel";
    panel.className = "transfer-panel";
    var card = document.createElement("div");
    card.className = "transfer-card";
    var heading = document.createElement("h2");
    heading.textContent = title;
    var body = document.createElement("p");
    body.className = "transfer-status";
    // textContent, never innerHTML: the reason is the server's text.
    body.textContent = message;
    card.appendChild(heading);
    card.appendChild(body);
    var buttons = document.createElement("p");
    function close() {
      panel.remove();
      term.focus();
    }
    if (retry) {
      var again = document.createElement("button");
      again.type = "button";
      again.textContent = "Try again";
      again.addEventListener("click", function () { close(); retry(); });
      buttons.appendChild(again);
    }
    var dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.textContent = "Close";
    dismiss.addEventListener("click", close);
    buttons.appendChild(dismiss);
    card.appendChild(buttons);
    panel.appendChild(card);
    document.body.appendChild(panel);
    dismiss.focus();
  }

  function openUploadPanel(url) {
    url = transferUrl(url);
    var existing = document.getElementById("transfer-panel");
    if (existing) existing.remove();

    var panel = document.createElement("div");
    panel.id = "transfer-panel";
    panel.className = "transfer-panel";
    panel.innerHTML =
      '<div class="transfer-card">' +
      '<h2>Upload a file</h2>' +
      '<p class="transfer-drop" id="transfer-drop">Drop a file here, or choose one.</p>' +
      '<p><input type="file" id="transfer-input"></p>' +
      '<p class="transfer-status" id="transfer-status">This link works once.</p>' +
      '<p><button type="button" id="transfer-cancel">Cancel</button></p>' +
      "</div>";
    document.body.appendChild(panel);

    var drop = panel.querySelector("#transfer-drop");
    var input = panel.querySelector("#transfer-input");
    var status = panel.querySelector("#transfer-status");
    var done = false;
    var inFlight = null;

    function close() {
      // Cancel means cancel (issue #475 review): without this the panel
      // disappears while the browser keeps sending the file, and the
      // caller believes they stopped it.
      if (inFlight) inFlight.abort();
      panel.remove();
      term.focus();
    }

    function send(file) {
      if (done || !file) return;
      done = true;
      status.textContent = "Uploading " + file.name + "...";
      var body = new FormData();
      body.append("file", file, file.name);
      inFlight = typeof AbortController === "function" ? new AbortController() : null;
      fetch(url, { method: "POST", body: body, signal: inFlight ? inFlight.signal : undefined })
        .then(function (response) {
          if (!response.ok) {
            return response.text().then(function (text) {
              var rejection = new Error(text || ("HTTP " + response.status));
              // The server has already spent the single-use token by the
              // time it rejects, so a retry here can only ever 404
              // (issue #475 review). Marked so the catch below does not
              // invite one.
              rejection.spent = true;
              throw rejection;
            });
          }
          return response.json();
        })
        .then(function (stored) {
          status.textContent = "Uploaded " + stored.filename + ".";
          // Nudge the BBS into repainting, so the file appears in the
          // listing the caller is looking at rather than after their
          // next keystroke.
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "key", data: "\f" }));
          }
          setTimeout(close, 1200);
        })
        .catch(function (error) {
          if (error && error.name === "AbortError") return;  // the caller cancelled
          inFlight = null;
          var detail = error && error.message ? error.message : error;
          if (error && error.spent) {
            // Nothing to retry with: tell them how to get another link.
            status.textContent = detail + " Ask the BBS for a new link.";
            input.disabled = true;
            return;
          }
          // A rejected fetch does not prove the request never arrived
          // (issue #475 review, unaddressed at merge): the connection can
          // drop after the POST redeemed its token, or after the file was
          // stored but before the response came back. Offering the same
          // link again would 404 at best, and at worst would hide an
          // upload that actually succeeded -- so point them at the
          // listing and at a fresh link instead of claiming this one
          // still works.
          status.textContent =
            "Upload failed: " + detail +
            " It may still have arrived \u2014 check the file listing, and ask the BBS" +
            " for a new link if it did not.";
          // Repaint too (Codex review of #508). This branch sends the
          // caller to the listing, and the listing behind the panel is
          // the page queried *before* the upload -- so without this the
          // advice points at stale evidence, the file that did arrive is
          // missing from it, and the obvious conclusion is to upload it
          // again. The success path already does this; the ambiguous
          // case needs it more, not less.
          if (ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: "key", data: "\f" }));
          }
          input.disabled = true;
        });
    }

    input.addEventListener("change", function () { send(input.files && input.files[0]); });
    panel.querySelector("#transfer-cancel").addEventListener("click", close);
    ["dragenter", "dragover"].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.add("is-over");
      });
    });
    ["dragleave", "drop"].forEach(function (name) {
      drop.addEventListener(name, function (event) {
        event.preventDefault();
        drop.classList.remove("is-over");
      });
    });
    drop.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      send(files && files[0]);
    });
    input.focus();
  }

  window.addEventListener("resize", function () {
    if (!fixedDoorSize) fitAddon.fit();
    sendResize();
  });
})();
