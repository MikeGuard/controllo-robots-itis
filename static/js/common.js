/*
 * Helpers shared by both pages (student.html + student_repo.js, instructor.html + repo.js).
 * Plain browser script, no build step: everything is on window.Common (and window.CommonTest for node tests).
 *
 *   text     esc(s)  notify(msg)  stamp(iso)  relTime(iso)  statusBadge(status)
 *   http     apiJson(path, token, opts)              JSON in/out; throws Error(<server detail>)
 *   files    languageFor(name)  cleanFileName(raw, opts)  resolveEntry(files, wanted)
 *            fileSet(files, entry)  sameFileSet(a, b)  changedFiles(base, files)  markModified(root, changed)
 *   editor   createFileBuffer({ editor, state, onEdit, onLoad })   see below
 *   logs     createLogView(el, { key, controls, ... })   bounded, throttled log pane with Auto-scroll / No spam / Last line
 */
(function () {
  'use strict';

  /* ---------- text ---------- */

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

  const STATUS = { pending: 'Pending', running: 'Running', completed: 'Completed', failed: 'Failed', aborted: 'Aborted', rejected: 'Rejected' };
  const statusBadge = (st) => `<span class="badge" data-status="${STATUS[st] ? st : 'offline'}">${STATUS[st] || esc(st || '—')}</span>`;

  const stamp = (iso) => {
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false });
  };

  function relTime(iso) {
    if (!iso) return '';
    const d = new Date(iso); if (isNaN(d)) return '';
    const s = Math.max(0, Math.floor((Date.now() - d) / 1000));
    if (s < 60) return 'just now';
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
    if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
    return d.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
  }

  // Small non-blocking toast (bottom right). Long messages stay up longer.
  function notify(msg) {
    if (typeof document === 'undefined' || !document.body) return;
    let el = document.getElementById('notifyToast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'notifyToast';
      el.setAttribute('role', 'status');
      el.className = 'fixed bottom-4 right-4 z-[100] max-w-sm px-3 py-2 rounded-lg bg-slate-800 border border-slate-600 text-xs text-slate-100 transition-opacity duration-200 opacity-0 pointer-events-none';
      el.style.whiteSpace = 'pre-line';
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.style.opacity = '1';
    clearTimeout(el._t);
    el._t = setTimeout(() => { el.style.opacity = '0'; }, String(msg).length > 90 ? 7000 : 4500);
  }

  /* ---------- http ---------- */

  async function apiJson(path, token, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}`, ...(opts.headers || {}) } });
    if (!res.ok) {
      let d = `HTTP ${res.status}`;
      try { const j = await res.json(); d = j.detail || (j.errors || []).join('; ') || d; } catch (_) {}
      throw new Error(d);
    }
    return res.json();
  }

  /* ---------- project files ---------- */

  // Same list and limits as workspace.py (ALLOWED_EXTENSIONS, MAX_PATH_DEPTH): checking here gives the
  // user the error when they name the file, instead of every later Save failing.
  const ALLOWED_EXTENSIONS = ['.py', '.txt', '.json', '.csv', '.md', '.log', '.yaml', '.yml'];
  const MAX_PATH_DEPTH = 5;

  function languageFor(name) {
    const ext = (String(name).split('.').pop() || '').toLowerCase();
    if (ext === 'json') return 'json';
    if (ext === 'yaml' || ext === 'yml') return 'yaml';
    if (['txt', 'csv', 'md', 'log'].includes(ext)) return 'plaintext';
    return 'python';
  }

  // -> { name } or { error }. `folders` allows "lib/util.py"; `defaultExt` is added when the name has no extension.
  function cleanFileName(raw, { folders = false, defaultExt = '' } = {}) {
    const text = String(raw || '').trim().replace(/\\/g, '/').replace(/^\/+/, '');
    let segs = (folders ? text.split('/') : [text.replace(/\//g, '')])
      .map((s) => s.replace(/[^a-zA-Z0-9_.-]/g, ''))
      .filter(Boolean);
    if (!segs.length) return { error: 'Enter a file name (letters, digits, _ - . only).' };
    if (segs.some((s) => s.startsWith('.'))) return { error: 'File and folder names cannot start with a dot.' };
    if (segs.some((s) => s.length > 64)) return { error: 'File and folder names are limited to 64 characters.' };
    if (segs.length > MAX_PATH_DEPTH) return { error: `Folders can be nested at most ${MAX_PATH_DEPTH - 1} levels deep.` };
    let last = segs[segs.length - 1];
    if (!last.includes('.') && defaultExt) last += defaultExt;
    segs = [...segs.slice(0, -1), last];
    const dot = last.lastIndexOf('.');
    const ext = dot > 0 ? last.slice(dot).toLowerCase() : '';
    if (!ALLOWED_EXTENSIONS.includes(ext)) {
      return { error: `${ext ? `"${ext}" files are` : 'Files without an extension are'} not allowed. Use one of: ${ALLOWED_EXTENSIONS.join(' ')}` };
    }
    return { name: segs.join('/') };
  }

  // The file a project runs: the requested one if it exists, else main.py, else the first .py, else the first file.
  // Same rule as workspace.resolve_entry_point on the server.
  function resolveEntry(files, wanted) {
    const names = Object.keys(files || {});
    if (wanted && names.includes(wanted)) return wanted;
    if (names.includes('main.py')) return 'main.py';
    return names.find((n) => n.endsWith('.py')) || names[0] || 'main.py';
  }

  // A frozen copy of a version (what is saved / committed), used to tell what changed since.
  const fileSet = (files, entry) => ({ files: { ...(files || {}) }, entry: entry || null });
  function sameFileSet(a, b) {
    if (!a || !b || a.entry !== b.entry) return false;
    const ka = Object.keys(a.files), kb = Object.keys(b.files);
    return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b.files, k) && a.files[k] === b.files[k]);
  }
  // Names of files that are new or edited compared to `base` (deleted files have no tab, so they are not listed).
  function changedFiles(base, files) {
    const out = new Set();
    if (!base) return out;
    for (const [k, v] of Object.entries(files || {})) if (!Object.prototype.hasOwnProperty.call(base.files, k) || base.files[k] !== v) out.add(k);
    return out;
  }
  // Toggles the unsaved marker on every [data-file] element under `root` (file tabs, tree rows).
  function markModified(root, changed) {
    if (!root) return;
    root.querySelectorAll('[data-file]').forEach((el) => el.classList.toggle('is-modified', changed.has(el.dataset.file)));
  }

  /* ---------- editor buffer ----------
   *
   * One Monaco editor shows one file of a project at a time. The buffer remembers WHICH file the editor text was
   * loaded from (`bound`) and only ever writes the text back into that file. It never uses "whatever
   * state.active_file is now", so changing active_file, deleting, renaming or replacing files cannot copy one
   * file's text into another (the old bug: deleting the open notes.txt wrote its text into main.py).
   *
   *   capture()           editor text -> the file it was loaded from (if that file still exists)
   *   open(name)          capture(), then show `name`                  (user switches tabs)
   *   load(name)          show `name` WITHOUT capturing first           (files were just replaced / regenerated)
   *   forget(name)        `name` is gone: the editor text belongs to nothing until the next load()
   *   rename(old, new)    the open file was renamed
   *   handleChange()      wire to editor.onDidChangeModelContent; ignores our own setValue() calls
   *   attach()            call once the editor exists (Monaco loads asynchronously)
   *
   * Rule for callers: capture() BEFORE mutating state.files, load() AFTER.
   */
  function createFileBuffer({ editor, state, onEdit, onLoad } = {}) {
    let bound = null;
    let loading = false;
    const ed = () => (typeof editor === 'function' ? editor() : editor) || null;
    const st = () => (typeof state === 'function' ? state() : state) || null;
    const has = (s, name) => !!(s && s.files && name !== null && Object.prototype.hasOwnProperty.call(s.files, name));

    function capture() {
      const e = ed(), s = st();
      if (!e || loading || !has(s, bound)) return false;
      s.files[bound] = e.getValue();
      return true;
    }

    function setLanguage(name) {
      const e = ed();
      const m = e && e.getModel && e.getModel();
      if (m && typeof monaco !== 'undefined' && monaco.editor) monaco.editor.setModelLanguage(m, languageFor(name));
    }

    function load(name) {
      const s = st();
      if (!has(s, name)) { bound = null; return false; }   // the files were replaced: the old text belongs to nothing
      s.active_file = name;
      const e = ed();
      if (e) {
        // setValue also clears Monaco's undo stack, so Ctrl+Z can never bring back another file's text.
        // Skipped only when re-showing the same file with unchanged text (keeps the cursor where it was).
        loading = true;
        try { if (name !== bound || e.getValue() !== s.files[name]) e.setValue(s.files[name] ?? ''); } finally { loading = false; }
        setLanguage(name);
        bound = name;
      } else {
        bound = null;            // attach() binds once the editor exists
      }
      if (onLoad) onLoad(name);
      return true;
    }

    function open(name) {
      if (!has(st(), name)) return false;
      capture();
      return load(name);
    }

    function forget(name) { if (bound === name) bound = null; }

    function rename(oldName, newName) {
      if (bound === oldName) { bound = newName; setLanguage(newName); if (onLoad) onLoad(newName); }
    }

    function handleChange() {
      if (loading) return;
      capture();
      if (onEdit) onEdit();
    }

    function attach() {
      const s = st();
      if (s && has(s, s.active_file)) load(s.active_file);
    }

    return { capture, open, load, forget, rename, handleChange, attach, bound: () => bound };
  }

  /* ---------- log panes ----------
   * createLogModel(opts) keeps the lines of one log pane (no DOM, tested in tests/log_view.test.js).
   * createLogView(el, opts) shows a model in a pane and adds its controls:
   *   Auto-scroll   follow the newest line (pauses while you scroll up; untick to never move)
   *   Full log      every line kept
   *   No spam       hides simulation step-by-step progress and folds repeated lines into "line  (×N)"
   *   Last line     only the newest line
   * Only the last `maxLines` lines are kept and the pane is redrawn at most every 150 ms, however fast
   * text arrives, so a program that prints in a loop can no longer freeze the page.
   */
  const LOG_MODES = { all: 'Full log', quiet: 'No spam', last: 'Last line' };
  const SPAM_LINE = /^\s*(\[STEP \d+\/\d+\]|\[SIMULATOR\] \(\d+\/\d+\))/;     // simulation playback progress
  const LOG_STAMP = /^\[\d{1,2}:\d{2}(:\d{2})?\]\s*/;                         // "[12:01:33] " ignored when folding repeats

  function createLogModel({ maxLines = 1000, maxLineChars = 2000, isSpam = (t) => SPAM_LINE.test(t), tag = null } = {}) {
    let lines = [];          // { text, tag }
    let open = false;        // the last line has not ended with a newline yet
    let dropped = 0;

    function append(text, meta) {
      if (text === null || text === undefined || text === '') return;
      const parts = String(text).replace(/\r\n?/g, '\n').split('\n');
      parts.forEach((p, i) => {
        const isLast = i === parts.length - 1;
        if (i === 0 && open && lines.length) lines[lines.length - 1].text += p;
        else if (!(isLast && p === '')) lines.push({ text: p, tag: tag ? tag(p, meta) : meta });
        // print('.', end='') in a loop builds one endless line: wrap it so the line cap still bounds the pane
        let cur = lines[lines.length - 1];
        while (cur && cur.text.length > maxLineChars) {
          const rest = cur.text.slice(maxLineChars);
          cur.text = cur.text.slice(0, maxLineChars);
          cur = { text: rest, tag: cur.tag };
          lines.push(cur);
        }
      });
      open = parts[parts.length - 1] !== '';
      if (lines.length > maxLines) {
        const n = lines.length - maxLines;
        lines.splice(0, n);
        dropped += n;
      }
    }

    function clear() { lines = []; open = false; dropped = 0; }

    // The text a pane shows in `mode`; `filter(line)` keeps only some lines (the admin's Prints/Errors tabs).
    function render(mode = 'all', filter = null) {
      const src = filter ? lines.filter(filter) : lines;
      if (mode === 'last') {
        for (let i = src.length - 1; i >= 0; i--) if (src[i].text.trim()) return src[i].text;
        return '';
      }
      const head = dropped ? `[… ${dropped} older line${dropped === 1 ? '' : 's'} not kept]\n` : '';
      if (mode !== 'quiet') return head + src.map((l) => l.text).join('\n');
      const out = [];
      let prevKey = null, reps = 0;
      src.forEach((l) => {
        if (isSpam(l.text)) return;
        const key = l.text.replace(LOG_STAMP, '');
        if (!key.trim()) {                                     // collapse runs of blank lines
          if (out.length && !out[out.length - 1].trim()) return;
          out.push(''); prevKey = null; return;
        }
        if (key === prevKey) { reps++; out[out.length - 1] = `${l.text}  (×${reps})`; return; }
        out.push(l.text); prevKey = key; reps = 1;
      });
      const last = src[src.length - 1];
      if (last && isSpam(last.text)) out.push(last.text);      // still show where a running playback is
      return head + out.join('\n');
    }

    return {
      append, clear, render,
      all: () => lines.map((l) => l.text).join('\n'),
      count: (pred) => lines.reduce((n, l) => n + (pred(l) ? 1 : 0), 0),
      size: () => lines.length,
    };
  }

  function createLogView(el, { key, controls = null, placeholder, maxLines, isSpam, tag, onRender } = {}) {
    const model = createLogModel({ maxLines, isSpam, tag });
    const storeKey = `ur.logview.${key || (el && el.id) || 'log'}`;
    const settings = { mode: 'all', follow: true };
    try { Object.assign(settings, JSON.parse(localStorage.getItem(storeKey) || '{}')); } catch (_) { }
    if (!LOG_MODES[settings.mode]) settings.mode = 'all';
    let empty = placeholder !== undefined ? placeholder : (el ? el.textContent.trim() : '');
    let filter = null, filterEmpty = '', fixed = null, timer = null, pinned = true;

    function save() { try { localStorage.setItem(storeKey, JSON.stringify(settings)); } catch (_) { } }

    function draw() {
      timer = null;
      if (!el) return;
      let text;
      if (fixed !== null) text = fixed;
      else if (!model.size()) text = empty;
      else {
        text = model.render(settings.mode, filter);
        if (!text && filter) text = filterEmpty;
      }
      if (el.textContent !== text) el.textContent = text;
      if (settings.follow && pinned) el.scrollTop = el.scrollHeight;
      if (onRender) onRender();
    }
    const schedule = () => { if (!timer) timer = setTimeout(draw, 150); };
    const now = () => { if (timer) clearTimeout(timer); draw(); };

    if (el) {
      // Following pauses while the user scrolls up to read, and resumes at the bottom.
      el.addEventListener('scroll', () => {
        if (el.clientHeight) pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
      }, { passive: true });
      // A pane filled while hidden (another tab) or resized still opens on its newest line.
      if (typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(() => { if (settings.follow && pinned) el.scrollTop = el.scrollHeight; }).observe(el);
      }
    }

    if (controls && typeof document !== 'undefined') {
      const wrap = document.createElement('span');
      wrap.className = 'logview-controls flex items-center gap-2';
      wrap.innerHTML =
        `<label class="flex items-center gap-1 text-xs text-slate-400 cursor-pointer select-none whitespace-nowrap" title="Keep the newest line in view">` +
        `<input type="checkbox" class="accent-sky-500"> Auto-scroll</label>` +
        `<select class="text-[11px] bg-slate-800 border border-slate-700 text-slate-300 rounded px-1 py-0.5" ` +
        `title="Full log: every line. No spam: hides simulation step-by-step progress and folds repeated lines. Last line: only the newest line.">` +
        Object.entries(LOG_MODES).map(([v, label]) => `<option value="${v}">${label}</option>`).join('') + `</select>`;
      const box = wrap.querySelector('input');
      const sel = wrap.querySelector('select');
      box.checked = settings.follow;
      sel.value = settings.mode;
      box.addEventListener('change', () => { settings.follow = box.checked; pinned = true; save(); now(); });
      sel.addEventListener('change', () => { settings.mode = sel.value; pinned = true; save(); now(); });
      controls.insertBefore(wrap, controls.firstChild);
    }

    return {
      append(text, meta) { model.append(text, meta); schedule(); },
      // Empties the pane; `text` (optional) is shown until new lines arrive.
      clear(text = '') { model.clear(); fixed = null; empty = text; pinned = true; now(); },
      // Show only the lines `fn(line)` accepts (null: all); `emptyText` when none match.
      setFilter(fn, emptyText = '') { filter = fn; filterEmpty = emptyText; fixed = null; pinned = true; now(); },
      // Show a fixed text instead of the log (the log keeps collecting); null goes back to the log.
      showText(text) { fixed = text; now(); },
      flush: now,
      all: model.all,
      count: model.count,
      size: model.size,
      mode: () => settings.mode,
    };
  }

  const api = {
    esc, notify, stamp, relTime, statusBadge, STATUS, apiJson,
    ALLOWED_EXTENSIONS, languageFor, cleanFileName, resolveEntry, fileSet, sameFileSet, changedFiles, markModified,
    createFileBuffer, createLogModel, createLogView, LOG_MODES,
  };
  if (typeof window !== 'undefined') window.Common = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
