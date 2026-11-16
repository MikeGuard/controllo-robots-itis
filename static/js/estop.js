/*
 * Emergency stop — one implementation for every admin page.
 *
 *  - abort() ALWAYS stops every robot. A per-robot abort must be requested explicitly.
 *  - It only reports success when the server confirmed it; otherwise a persistent banner with Retry stays up.
 *  - SPACE (capture phase, so nothing can swallow it):
 *       focus NOT in a text field/editor -> always aborts, never scrolls, never clicks a focused button
 *       focus IN a text field/editor     -> types a space normally, UNLESS a robot is running, then it aborts
 */
(function () {
  let cfg = { getToken: () => '', isActive: () => true, onResult: () => {} };
  let banner = null;

  function showBanner(msg, retry) {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'estopFailBanner';
      banner.setAttribute('role', 'alert');
      banner.className = 'estop-banner';
      document.body.appendChild(banner);
    }
    banner.innerHTML = '';
    const t = document.createElement('span'); t.textContent = msg; banner.appendChild(t);
    const b = document.createElement('button'); b.textContent = 'Retry stop'; b.className = 'btn btn-danger';
    b.onclick = retry; banner.appendChild(b);
    banner.hidden = false;
  }

  async function abort(source, model) {
    const url = model ? `/api/abort?model=${encodeURIComponent(model)}` : '/api/abort';
    try {
      const res = await fetch(url, { method: 'POST', headers: { Authorization: `Bearer ${cfg.getToken()}` } });
      if (!res.ok) {
        let d = `HTTP ${res.status}`; try { d = (await res.json()).detail || d; } catch (_) {}
        throw new Error(d);
      }
      if (banner) banner.hidden = true;
      cfg.onResult({ ok: true, source, model: model || null });
      return true;
    } catch (e) {
      showBanner(`EMERGENCY STOP NOT CONFIRMED (${e.message}). Robots may still be moving.`, () => abort(source, model));
      cfg.onResult({ ok: false, source, model: model || null, error: e.message });
      return false;
    }
  }

  function isTypingTarget(el) {
    if (!el) return false;
    const tag = (el.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable === true;
  }

  function onKeyDown(e) {
    if (!(e.code === 'Space' || e.key === ' ' || e.key === 'Spacebar')) return;
    if (!cfg.isActive()) return;                       // not signed in / login screen: nothing to stop
    const running = window.RobotStore ? RobotStore.anyExecuting() : false;
    if (isTypingTarget(document.activeElement) && !running) return;   // a person typing a space
    e.preventDefault(); e.stopPropagation();           // never scroll, never click a focused control
    if (e.repeat) return;                              // holding the key fires one stop, not a flood
    if (document.activeElement && document.activeElement.tagName === 'BUTTON') document.activeElement.blur();
    abort('SPACE key');
  }

  function onKeyUp(e) {
    if ((e.code === 'Space' || e.key === ' ') && cfg.isActive() &&
        (!isTypingTarget(document.activeElement) || (window.RobotStore && RobotStore.anyExecuting()))) e.preventDefault();
  }

  window.EStop = {
    install(opts) {
      cfg = { ...cfg, ...opts };
      window.addEventListener('keydown', onKeyDown, { capture: true, passive: false });
      window.addEventListener('keyup', onKeyUp, { capture: true, passive: false });
    },
    abort,
    _isTypingTarget: isTypingTarget,
  };
})();
