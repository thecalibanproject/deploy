// Injected into every page of the recorded walkthrough (walkthrough.py, add_init_script). It
// draws what a screen recording of a person would show and a console cannot: a caption bar
// (chapter, stage, one line on what Caliban does), a visible cursor with click ripples, a ring
// around the element about to be used, and a full-screen "customer app" panel for the requests
// the demo client sends. Nothing here talks to the network; walkthrough.py drives it with
// page.evaluate. Elements live under one fixed root with pointer-events: none, so the console
// underneath stays usable.
(() => {
  if (window.__demo) return;
  const css = `
[data-testid=plaintext-key] { filter: blur(7px) !important; }
#__demo-root { position: fixed; inset: 0; pointer-events: none; z-index: 2147483000;
  font-family: ui-sans-serif, system-ui, "Segoe UI", Roboto, "Noto Sans", "Noto Sans Arabic", sans-serif; }
#__demo-root * { box-sizing: border-box; }
#__demo-cap { position: absolute; left: 24px; right: 24px; bottom: 20px; display: none;
  background: rgba(12, 16, 28, 0.92); color: #f4f6fb; border-radius: 12px; padding: 14px 20px 16px;
  box-shadow: 0 10px 30px rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.08); }
#__demo-cap .row { display: flex; align-items: baseline; gap: 12px; }
#__demo-cap .stage { font: 600 12px/1 ui-monospace, SFMono-Regular, Menlo, monospace; letter-spacing: .08em;
  text-transform: uppercase; color: #0c101c; background: #7dd3fc; border-radius: 6px; padding: 5px 8px; }
#__demo-cap .chapter { font-size: 15px; font-weight: 650; color: #cbd5e1; }
#__demo-cap .line { margin-top: 6px; font-size: 21px; line-height: 1.35; font-weight: 500; }
#__demo-cap .note { margin-top: 6px; font-size: 14px; color: #fbbf24; }
#__demo-cap .clock { margin-left: auto; font: 500 13px ui-monospace, monospace; color: #94a3b8; }
#__demo-cursor { position: absolute; width: 22px; height: 22px; margin: -11px 0 0 -11px; border-radius: 50%;
  background: rgba(250, 204, 21, 0.55); border: 2px solid rgba(161, 98, 7, 0.9); display: none;
  transition: transform .08s ease-out; }
#__demo-cursor.down { transform: scale(0.7); }
.__demo-ripple { position: absolute; width: 44px; height: 44px; margin: -22px 0 0 -22px; border-radius: 50%;
  border: 3px solid rgba(250, 204, 21, 0.9); animation: __demo-ripple .6s ease-out forwards; }
@keyframes __demo-ripple { from { transform: scale(.3); opacity: 1 } to { transform: scale(1.6); opacity: 0 } }
#__demo-ring { position: absolute; display: none; border: 3px solid #f59e0b; border-radius: 10px;
  box-shadow: 0 0 0 6px rgba(245, 158, 11, 0.18); transition: all .25s ease; }
#__demo-panel { position: absolute; inset: 0; display: none; background: rgba(8, 11, 20, 0.86);
  backdrop-filter: blur(2px); padding: 36px 40px 150px; color: #e5e7eb; }
#__demo-panel .card { height: 100%; background: #0f1524; border: 1px solid #22304a; border-radius: 14px;
  display: flex; flex-direction: column; overflow: hidden; }
#__demo-panel .head { display: flex; align-items: center; gap: 12px; padding: 14px 20px;
  border-bottom: 1px solid #22304a; }
#__demo-panel .head .t { font-size: 18px; font-weight: 650; }
#__demo-panel .head .s { font-size: 14px; color: #94a3b8; }
#__demo-panel .badge { font: 600 12px ui-monospace, monospace; padding: 4px 8px; border-radius: 6px;
  background: #1e293b; color: #cbd5e1; }
#__demo-panel .badge.ok { background: #14532d; color: #bbf7d0; }
#__demo-panel .badge.warn { background: #713f12; color: #fde68a; }
#__demo-panel .badge.bad { background: #7f1d1d; color: #fecaca; }
#__demo-panel .cols { flex: 1; display: grid; grid-template-columns: 1fr 1fr; min-height: 0; }
#__demo-panel .col { padding: 16px 20px; overflow: hidden; display: flex; flex-direction: column; gap: 10px; min-height: 0; }
#__demo-panel .col + .col { border-left: 1px solid #22304a; }
#__demo-panel h4 { margin: 0; font-size: 13px; letter-spacing: .06em; text-transform: uppercase; color: #7dd3fc; }
#__demo-panel pre { margin: 0; white-space: pre-wrap; word-break: break-word; font: 14.5px/1.45 ui-monospace,
  SFMono-Regular, Menlo, Consolas, monospace; color: #e2e8f0; background: #0b1020; border: 1px solid #1e293b;
  border-radius: 8px; padding: 10px 12px; overflow: hidden; }
#__demo-panel table { border-collapse: collapse; font: 14px/1.4 ui-monospace, monospace; }
#__demo-panel td { padding: 3px 14px 3px 0; vertical-align: top; }
#__demo-panel td.k { color: #93c5fd; white-space: nowrap; }
#__demo-panel .answer { font-size: 17px; line-height: 1.5; color: #f8fafc; background: #111a2e;
  border-left: 4px solid #38bdf8; padding: 10px 14px; border-radius: 6px; }
#__demo-panel .answer[dir=rtl] { border-left: 0; border-right: 4px solid #38bdf8; }
#__demo-panel .muted { color: #94a3b8; font-size: 14px; }
#__demo-panel mark { background: #854d0e; color: #fef3c7; border-radius: 3px; padding: 0 2px; }
#__demo-panel .spin { display: inline-block; width: 14px; height: 14px; border: 2px solid #475569;
  border-top-color: #7dd3fc; border-radius: 50%; animation: __demo-spin 1s linear infinite; vertical-align: -2px; }
@keyframes __demo-spin { to { transform: rotate(360deg) } }
#__demo-panel .tbar { display: flex; height: 26px; border-radius: 6px; overflow: hidden; background: #1e293b;
  font: 600 12px/26px ui-monospace, monospace; }
#__demo-panel .tbar span { white-space: nowrap; overflow: hidden; text-overflow: clip; padding: 0 6px; color: #0b1020;
  border-right: 1px solid #0b1020; min-width: 2px; }
#__demo-panel .tlegend { font: 13px ui-monospace, monospace; color: #cbd5e1; }
#__demo-term { position: absolute; inset: 0; display: none; background: rgba(8, 11, 20, 0.86); padding: 48px 80px 160px; }
#__demo-term .win { height: 100%; background: #272822; border-radius: 12px; border: 1px solid #3a3b33;
  display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 20px 50px rgba(0,0,0,.45); }
#__demo-term .bar { height: 38px; background: #3a3b33; display: flex; align-items: center; gap: 8px; padding: 0 14px;
  color: #cfcfc2; font: 600 14px ui-sans-serif, system-ui, sans-serif; }
#__demo-term .bar i { width: 12px; height: 12px; border-radius: 50%; display: inline-block; background: #f92672; }
#__demo-term .bar i + i { background: #e6db74; } #__demo-term .bar i + i + i { background: #a6e22e; }
#__demo-term pre { flex: 1; margin: 0; padding: 18px 22px; color: #f8f8f2; overflow: hidden; white-space: pre-wrap;
  word-break: break-word; font: 20px/1.45 "Liberation Mono", ui-monospace, Menlo, Consolas, monospace; }
#__demo-term .cmd { color: #a6e22e; } #__demo-term .cur { background: #f8f8f2; color: #272822; }
#__demo-card { position: absolute; inset: 0; display: none; background: #0b1020; color: #f8fafc;
  align-items: center; justify-content: center; text-align: center; }
#__demo-card .big { font-size: 64px; font-weight: 750; letter-spacing: -0.02em; }
#__demo-card .sub { margin-top: 18px; font-size: 26px; color: #cbd5e1; max-width: 1400px; line-height: 1.4; }
#__demo-card .stages { margin-top: 36px; display: flex; gap: 14px; justify-content: center; flex-wrap: wrap; }
#__demo-card .stages span { font: 600 18px ui-monospace, monospace; padding: 8px 14px; border-radius: 8px;
  background: #13213b; color: #7dd3fc; border: 1px solid #1e3a5f; }
#__demo-card .foot { margin-top: 40px; font-size: 18px; color: #94a3b8; }
`;
  const esc = (s) =>
    String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  let root = null;
  let clockTimer = null;
  const el = (id) => root && root.querySelector('#' + id);

  function mount() {
    if (root || !document.body) return;
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);
    root = document.createElement('div');
    root.id = '__demo-root';
    root.setAttribute('aria-hidden', 'true');
    root.innerHTML =
      '<div id="__demo-card"></div><div id="__demo-panel"></div><div id="__demo-term"></div><div id="__demo-ring"></div>' +
      '<div id="__demo-cap"></div><div id="__demo-cursor"></div>';
    document.body.appendChild(root);
    const cur = el('__demo-cursor');
    document.addEventListener('mousemove', (e) => {
      cur.style.display = 'block';
      cur.style.left = e.clientX + 'px';
      cur.style.top = e.clientY + 'px';
    }, true);
    document.addEventListener('mousedown', (e) => {
      cur.classList.add('down');
      const r = document.createElement('div');
      r.className = '__demo-ripple';
      r.style.left = e.clientX + 'px';
      r.style.top = e.clientY + 'px';
      root.appendChild(r);
      setTimeout(() => r.remove(), 700);
    }, true);
    document.addEventListener('mouseup', () => cur.classList.remove('down'), true);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();

  // The native <dialog> of the console sits in the top layer, above any z-index: while one is
  // open, the caption moves into it so it stays visible.
  function host() {
    const open = [...document.querySelectorAll('dialog[open]')].pop();
    return open || document.body;
  }
  function follow() {
    const h = host();
    if (root && root.parentNode !== h) h.appendChild(root);
  }
  setInterval(follow, 150);

  window.__demo = {
    caption(stage, chapter, line, note, clockFrom) {
      mount();
      follow();
      const c = el('__demo-cap');
      clearInterval(clockTimer);
      c.innerHTML =
        '<div class="row">' + (stage ? '<span class="stage">' + esc(stage) + '</span>' : '') +
        '<span class="chapter">' + esc(chapter) + '</span><span class="clock"></span></div>' +
        '<div class="line">' + esc(line) + '</div>' + (note ? '<div class="note">' + esc(note) + '</div>' : '');
      c.style.display = 'block';
      if (clockFrom) {
        const t0 = Date.now();
        const clock = c.querySelector('.clock');
        clockTimer = setInterval(() => {
          const s = Math.round((Date.now() - t0) / 1000);
          clock.textContent = 'real time ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
        }, 500);
      }
    },
    hideCaption() {
      clearInterval(clockTimer);
      if (el('__demo-cap')) el('__demo-cap').style.display = 'none';
    },
    ring(x, y, w, h) {
      mount();
      const r = el('__demo-ring');
      Object.assign(r.style, { display: 'block', left: x - 6 + 'px', top: y - 6 + 'px', width: w + 12 + 'px', height: h + 12 + 'px' });
    },
    unring() {
      if (el('__demo-ring')) el('__demo-ring').style.display = 'none';
    },
    // The customer app panel: html built by walkthrough.py from escaped parts.
    panel(html) {
      mount();
      const p = el('__demo-panel');
      p.innerHTML = html;
      p.style.display = 'block';
    },
    hidePanel() {
      if (el('__demo-panel')) el('__demo-panel').style.display = 'none';
      if (el('__demo-term')) el('__demo-term').style.display = 'none';
    },
    // A terminal card typed out: lines starting with "$ " are typed as commands, the others
    // printed at once. Resolves when the typing ends.
    terminal(title, lines, cps) {
      mount();
      if (el('__demo-panel')) el('__demo-panel').style.display = 'none';
      const t = el('__demo-term');
      t.innerHTML = '<div class="win"><div class="bar"><i></i><i></i><i></i><span>' + esc(title) +
        '</span></div><pre></pre></div>';
      t.style.display = 'block';
      const pre = t.querySelector('pre');
      const delay = 1000 / Math.max(1, cps || 28);
      const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
      return (async () => {
        for (const line of lines) {
          if (line.startsWith('$ ')) {
            const span = document.createElement('span');
            span.className = 'cmd';
            pre.appendChild(span);
            span.textContent = '$ ';
            for (const ch of line.slice(2)) {
              span.textContent += ch;
              await sleep(delay);
            }
            pre.appendChild(document.createTextNode('\n'));
            await sleep(400);
          } else {
            pre.appendChild(document.createTextNode(line + '\n'));
            await sleep(60);
          }
          pre.scrollTop = pre.scrollHeight;
        }
      })();
    },
    card(html) {
      mount();
      const c = el('__demo-card');
      c.innerHTML = html;
      c.style.display = 'flex';
    },
    hideCard() {
      if (el('__demo-card')) el('__demo-card').style.display = 'none';
    },
    esc,
  };
})();
