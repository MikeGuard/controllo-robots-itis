/*
 * RobotStore — the ONLY place the dashboard learns robot / run state.
 * Source: GET /api/state (derived on the server from the execution engine + reachability,
 * with stale DB rows already reconciled). Nothing else may keep its own "is running" flag.
 *
 *   RobotStore.get()                -> { robots: { ur: {...}, niryo: {...} }, loaded }
 *   RobotStore.refresh()            -> Promise<state>   (coalesces concurrent calls)
 *   RobotStore.subscribe(fn)        -> unsubscribe fn;  fn(state, prev) on every change
 *   RobotStore.anyExecuting()/anyHalted()/executingModel()
 */
(function () {
  const blank = (m) => ({ model: m, label: m === 'ur' ? 'UR3 Arm' : 'Niryo Ned', ip: '', reachable: false, status: 'offline',
                          executing: false, submission_id: null, emergency_halted: false, halt_reason: '' });
  let state = { robots: { ur: blank('ur'), niryo: blank('niryo') }, loaded: false, error: null };
  const subs = new Set();
  let inflight = null, tokenFn = () => '';

  function emit(prev) { subs.forEach(fn => { try { fn(state, prev); } catch (e) { console.error(e); } }); }

  async function refresh() {
    if (inflight) return inflight;
    inflight = (async () => {
      const prev = state;
      try {
        const res = await fetch('/api/state', { headers: { Authorization: `Bearer ${tokenFn()}` }, cache: 'no-store' });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        state = { robots: { ur: { ...blank('ur'), ...data.robots.ur }, niryo: { ...blank('niryo'), ...data.robots.niryo } }, loaded: true, error: null };
      } catch (e) {
        // Link to the server lost: keep the last known robot state but say so; never invent "idle".
        state = { ...prev, error: e.message };
      } finally { inflight = null; }
      emit(prev);
      return state;
    })();
    return inflight;
  }

  window.RobotStore = {
    configure(opts) { tokenFn = opts.getToken; },
    get: () => state,
    refresh,
    subscribe(fn) { subs.add(fn); return () => subs.delete(fn); },
    anyExecuting: () => state.robots.ur.executing || state.robots.niryo.executing,
    anyHalted: () => state.robots.ur.emergency_halted || state.robots.niryo.emergency_halted,
    executingModel: () => (state.robots.ur.executing ? 'ur' : state.robots.niryo.executing ? 'niryo' : null),
  };
})();
