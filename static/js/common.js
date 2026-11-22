/*
 * Helpers shared by both pages (student.html + student_repo.js, instructor.html + repo.js).
 * Plain browser script, no build step: everything is on window.Common (and window.CommonTest for node tests).
 *
 *   text     esc(s)  notify(msg)  stamp(iso)  relTime(iso)  statusBadge(status)
 *   http     apiJson(path, token, opts)              JSON in/out; throws Error(<server detail>)
 *   files    languageFor(name)  cleanFileName(raw, opts)  resolveEntry(files, wanted)
 *            fileSet(files, entry)  sameFileSet(a, b)  changedFiles(base, files)  markModified(root, changed)
 *   editor   createFileBuffer({ editor, state, onEdit, onLoad })   see below
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

  const api = {
    esc, notify, stamp, relTime, statusBadge, STATUS, apiJson,
    ALLOWED_EXTENSIONS, languageFor, cleanFileName, resolveEntry, fileSet, sameFileSet, changedFiles, markModified,
    createFileBuffer,
  };
  if (typeof window !== 'undefined') window.Common = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
