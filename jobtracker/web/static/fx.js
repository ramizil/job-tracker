// Futuristic constellation / starfield backdrop. Lightweight, pauses when the
// tab is hidden and disables itself for users who prefer reduced motion.
(function () {
  var canvas = document.getElementById("fx");
  if (!canvas) return;
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

  var ctx = canvas.getContext("2d");
  var dpr = Math.min(window.devicePixelRatio || 1, 2);
  var w = 0, h = 0, particles = [], raf = null;
  var mouse = { x: -9999, y: -9999 };

  function resize() {
    w = canvas.clientWidth = window.innerWidth;
    h = canvas.clientHeight = window.innerHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    var target = Math.round((w * h) / 22000); // density scales with screen
    target = Math.max(36, Math.min(110, target));
    particles = [];
    for (var i = 0; i < target; i++) {
      particles.push({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.28,
        vy: (Math.random() - 0.5) * 0.28,
        r: Math.random() * 1.6 + 0.6,
      });
    }
  }

  function step() {
    ctx.clearRect(0, 0, w, h);
    var LINK = 132, LINK2 = LINK * LINK;

    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      p.x += p.vx; p.y += p.vy;
      if (p.x < 0 || p.x > w) p.vx *= -1;
      if (p.y < 0 || p.y > h) p.vy *= -1;

      // gentle attraction toward the cursor
      var mdx = mouse.x - p.x, mdy = mouse.y - p.y;
      var md2 = mdx * mdx + mdy * mdy;
      if (md2 < 26000) { p.x += mdx * 0.0012; p.y += mdy * 0.0012; }

      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, 6.2832);
      ctx.fillStyle = "rgba(125,211,252,0.85)";
      ctx.shadowColor = "rgba(34,211,238,0.9)";
      ctx.shadowBlur = 8;
      ctx.fill();
      ctx.shadowBlur = 0;
    }

    // links between near particles + to the cursor
    for (var a = 0; a < particles.length; a++) {
      for (var b = a + 1; b < particles.length; b++) {
        var dx = particles[a].x - particles[b].x;
        var dy = particles[a].y - particles[b].y;
        var d2 = dx * dx + dy * dy;
        if (d2 < LINK2) {
          var o = 1 - d2 / LINK2;
          ctx.strokeStyle = "rgba(124,92,246," + (o * 0.5).toFixed(3) + ")";
          ctx.lineWidth = o * 1.1;
          ctx.beginPath();
          ctx.moveTo(particles[a].x, particles[a].y);
          ctx.lineTo(particles[b].x, particles[b].y);
          ctx.stroke();
        }
      }
    }
    raf = requestAnimationFrame(step);
  }

  function start() { if (!raf) raf = requestAnimationFrame(step); }
  function stop() { if (raf) { cancelAnimationFrame(raf); raf = null; } }

  window.addEventListener("resize", resize, { passive: true });
  window.addEventListener("mousemove", function (e) { mouse.x = e.clientX; mouse.y = e.clientY; }, { passive: true });
  window.addEventListener("mouseout", function () { mouse.x = mouse.y = -9999; });
  document.addEventListener("visibilitychange", function () { document.hidden ? stop() : start(); });

  resize();
  start();
})();
