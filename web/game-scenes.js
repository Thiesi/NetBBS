// ---- scene players: recorded game scenes, replayed at the game's own timing ----
  // Each scene's data is a <script type="application/json" id="scene-NAME">
  // written by scripts/website_scene_to_html.py: a style table and, per
  // frame, the wait the game asked for and the runs of cells it changed.
  (function(){
    var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    var HOLD = 900;  // ms after a scene's own final hold, before the next one
    var cache = {};

    // Block glyphs are drawn as fills of the cell, not as text: a font's
    // half block neither spans the 1ch cell nor the line height, so thin
    // strips of background showed between the picture's pixels.
    // Same rule as scripts/website_ansi_to_html.py.
    var BLOCKS = {'▀': 'top', '▄': 'bottom', '█': 'full', '▌': 'left', '▐': 'right'};

    function blockCss(fill, css){
      var fg = /(?:^|;)color:(rgb\([^)]*\))/.exec(css);
      var bg = /(?:^|;)background:(rgb\([^)]*\))/.exec(css);
      fg = fg ? fg[1] : 'rgb(199,202,212)';
      bg = bg ? bg[1] : 'transparent';
      var paint = fill === 'full' ? fg
        : 'linear-gradient(' + (fill === 'left' || fill === 'right' ? 'to right,' : '')
          + (fill === 'top' || fill === 'left' ? fg + ' 50%,' + bg + ' 50%' : bg + ' 50%,' + fg + ' 50%') + ')';
      return css.replace(/(?:^|;)background:rgb\([^)]*\)/, '') + ';background:' + paint;
    }

    function scene(name){
      if (!cache[name]){
        var node = document.getElementById('scene-' + name);
        cache[name] = node ? JSON.parse(node.textContent) : null;
      }
      return cache[name];
    }

    Array.prototype.forEach.call(document.querySelectorAll('.scene-player'), function(player){
      var screen = player.querySelector('.term-body');
      var chips = Array.prototype.slice.call(player.querySelectorAll('.scene-chip'));
      var toggle = player.querySelector('.scene-toggle');
      var title = player.querySelector('.term-title');
      var capT = player.querySelector('.shot-caption .t');
      var capD = player.querySelector('.shot-caption .d');
      if (!screen || !chips.length || !toggle) return;

      var cells = null, current = 0, frame = 0, timer = null;
      var playing = false, visible = false, wanted = !reduceMotion;

      function blank(data){
        // One span per cell, as in the stills: no web font gives every
        // block and box glyph the same advance, so a run would bend.
        var rows = [];
        var frag = document.createDocumentFragment();
        for (var r = 0; r < data.rows; r++){
          var row = [];
          for (var c = 0; c < data.cols; c++){
            var span = document.createElement('span');
            span.textContent = ' ';
            span.style.cssText = 'display:inline-block;width:1ch';
            frag.appendChild(span);
            row.push(span);
          }
          if (r < data.rows - 1) frag.appendChild(document.createTextNode('\n'));
          rows.push(row);
        }
        screen.textContent = '';
        screen.appendChild(frag);
        cells = rows;
      }

      function apply(data, index){
        var runs = data.frames[index][1];
        for (var i = 0; i < runs.length; i++){
          var run = runs[i], row = cells[run[0]], css = data.styles[run[2]];
          var text = Array.from(run[3]);
          for (var k = 0; k < text.length; k++){
            var cell = row[run[1] + k];
            if (!cell) break;
            var fill = BLOCKS[text[k]];
            if (fill){
              cell.textContent = ' ';
              cell.style.cssText = blockCss(fill, css);
            } else {
              cell.textContent = text[k];
              cell.style.cssText = css;
            }
          }
        }
      }

      function select(index){
        current = index;
        frame = 0;
        chips.forEach(function(chip, i){ chip.setAttribute('aria-pressed', i === index ? 'true' : 'false'); });
        var chip = chips[index];
        if (title) title.textContent = chip.getAttribute('data-title');
        if (capT) capT.textContent = chip.getAttribute('data-caption');
        if (capD) capD.textContent = chip.getAttribute('data-detail');
        blank(scene(chip.getAttribute('data-scene')));
      }

      function finish(){
        // Draw every frame at once: the scene as it ends, without the motion.
        var data = scene(chips[current].getAttribute('data-scene'));
        for (; frame < data.frames.length; frame++) apply(data, frame);
      }

      function step(){
        timer = null;
        if (!playing) return;
        var data = scene(chips[current].getAttribute('data-scene'));
        if (frame < data.frames.length){
          apply(data, frame);
          var wait = data.frames[frame][0];
          frame++;
          timer = setTimeout(step, Math.max(wait, 16));
        } else {
          timer = setTimeout(function(){
            timer = null;
            if (!playing) return;
            select((current + 1) % chips.length);
            step();
          }, HOLD);
        }
      }

      function play(){
        if (playing) return;
        playing = true;
        toggle.textContent = 'Pause';
        toggle.setAttribute('aria-label', 'Pause the scenes');
        if (!cells) select(current);
        step();
      }

      function pause(){
        playing = false;
        if (timer) clearTimeout(timer);
        timer = null;
        toggle.textContent = 'Play';
        toggle.setAttribute('aria-label', 'Play the scenes');
      }

      chips.forEach(function(chip, i){
        chip.addEventListener('click', function(){
          var was = playing;
          pause();
          select(i);
          if (reduceMotion && !was){ finish(); return; }
          wanted = true;
          play();
        });
      });

      toggle.addEventListener('click', function(){
        if (playing){ wanted = false; pause(); }
        else {
          wanted = true;
          var data = cells && scene(chips[current].getAttribute('data-scene'));
          if (data && frame >= data.frames.length) select(current);
          play();
        }
      });

      pause();
      if ('IntersectionObserver' in window){
        new IntersectionObserver(function(entries){
          visible = entries[0].isIntersecting;
          if (visible && wanted) play();
          else if (!visible && playing) pause();
        }, {threshold: 0.35}).observe(player);
      } else if (wanted){
        play();
      }
    });
  })();
