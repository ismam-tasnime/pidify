// 3D background: floating document sheets drifting upward, with gentle mouse parallax.
(() => {
  const sheets = document.getElementById("sheets");
  const book = document.getElementById("book");
  if (!sheets) return;

  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const TAGS = [
    ["PDF", "#e5484d"], ["PDF", "#e5484d"], ["PDF", "#e5484d"],
    ["DOCX", "#2b6ce6"], ["XLSX", "#16965a"], ["PPTX", "#e06a1a"], ["JPG", "#b44ad8"],
  ];
  const rand = (a, b) => a + Math.random() * (b - a);
  const narrow = innerWidth < 700;
  const count = narrow ? 8 : 14;

  function sheetHTML() {
    const [tag, color] = TAGS[Math.floor(Math.random() * TAGS.length)];
    const lines = Math.floor(rand(5, 9));
    const img = Math.random() < 0.45 ? '<div class="img"></div>' : "";
    let body = '<div class="h"></div>';
    for (let i = 0; i < lines; i++) {
      body += '<div class="l"></div>';
      if (i === 1) body += img;
    }
    return `<div class="sheet-inner">${body}<span class="tag" style="--tag:${color}">${tag}</span></div>`;
  }

  for (let i = 0; i < count; i++) {
    const el = document.createElement("div");
    el.className = "sheet";
    // Spread sheets evenly across the width, then jitter.
    const lane = (i + 0.5) / count;
    const z = rand(-700, 150);
    const x = lane * 108 - 8 + rand(-4, 4);
    // Sheets that drift behind the centre column are faded so text stays readable.
    const colHalf = Math.min(50, (880 / 2 / innerWidth) * 100 + 4);
    const behindText = Math.abs(x + 6 - 50) < colHalf;
    const dur = rand(26, 46);
    el.style.cssText = [
      `--x:${x.toFixed(1)}vw`,
      `--o:${behindText ? (narrow ? 0.28 : 0.35) : 0.95}`,
      `--dx:${rand(-12, 12).toFixed(1)}vw`,
      `--z:${z.toFixed(0)}px`,
      `--w:${rand(narrow ? 90 : 110, narrow ? 130 : 170).toFixed(0)}px`,
      `--dur:${dur.toFixed(1)}s`,
      `--delay:${(-rand(0, dur)).toFixed(1)}s`,
      `--spin:${rand(7, 14).toFixed(1)}s`,
      `--rz0:${rand(-30, 30).toFixed(0)}deg`,
      `--rz1:${rand(-50, 50).toFixed(0)}deg`,
      `--rx0:${rand(-35, 35).toFixed(0)}deg`,
      `--rx1:${rand(-35, 35).toFixed(0)}deg`,
      `--ry0:${rand(-55, 55).toFixed(0)}deg`,
      `--ry1:${rand(-55, 55).toFixed(0)}deg`,
      // Depth of field: sheets far back or very close are softer.
      `--blur:${z < -450 ? 2.5 : z > 80 ? 1.5 : 0}px`,
    ].join(";");
    el.innerHTML = sheetHTML();
    sheets.append(el);
  }

  if (reduced) return;

  // Parallax: tilt the whole field slightly toward the pointer.
  let tx = 0, ty = 0, cx = 0, cy = 0, raf = 0;
  function tick() {
    cx += (tx - cx) * 0.06;
    cy += (ty - cy) * 0.06;
    sheets.style.transform = `rotateY(${cx * 6}deg) rotateX(${-cy * 5}deg) translate3d(${cx * -20}px, ${cy * -14}px, 0)`;
    if (book) book.style.translate = `${cx * -26}px ${cy * -18}px`;
    raf = Math.abs(tx - cx) + Math.abs(ty - cy) > 0.001 ? requestAnimationFrame(tick) : 0;
  }
  addEventListener("pointermove", (e) => {
    tx = e.clientX / innerWidth - 0.5;
    ty = e.clientY / innerHeight - 0.5;
    if (!raf) raf = requestAnimationFrame(tick);
  }, { passive: true });
})();
