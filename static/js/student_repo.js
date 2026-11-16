/*
 * Student version history: the same branch / commit structure as the instructor's Code Studio,
 * scoped to the student's own projects (the server enforces that).
 *
 *   working copy = what you edit and Save (what the instructor sees live)
 *   commit       = a saved version; a commit on `main` is a submission to your instructor's queue
 *   branch       = a separate line of commits to experiment on; the instructor's queue ignores it
 *
 * Viewing an older commit loads ALL of its files into the editor. Nothing is overwritten until you Save
 * (restores it into your working copy) or Commit.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const enc = encodeURIComponent;
  const STATUS = { pending: 'Pending', running: 'Running', completed: 'Completed', failed: 'Failed', aborted: 'Aborted', rejected: 'Rejected' };
  const badge = (st) => `<span class="badge" data-status="${STATUS[st] ? st : 'offline'}">${STATUS[st] || esc(st || '—')}</span>`;
  const stamp = (iso) => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }); };

  // commitId: 'wc' (working copy) | number | null (nothing committed and no working copy)
  const S = { owner: null, project: null, branch: 'main', branches: [], commits: [], commitId: 'wc', workingCopy: null, baseline: '', seq: 0 };

  const owner = () => (typeof currentStudentUser !== 'undefined' && currentStudentUser ? currentStudentUser.username : null);
  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${currentAuthToken}`, ...(opts.headers || {}) } });
    if (!res.ok) {
      let d = `HTTP ${res.status}`;
      try { const j = await res.json(); d = j.detail || (j.errors || []).join('; ') || d; } catch (_) {}
      throw new Error(d);
    }
    return res.json();
  }
  const repoUrl = () => `/api/repos/${enc(S.owner)}/${enc(S.project)}`;

  function capture() { if (editor && projectState.active_file) projectState.files[projectState.active_file] = editor.getValue(); }
  function snapshot() {
    const f = projectState.files || {}, sorted = {};
    Object.keys(f).sort().forEach((k) => { sorted[k] = f[k]; });
    return JSON.stringify({ f: sorted, e: projectState.entry_point });
  }
  const isOpen = () => !!S.project;
  const isDirty = () => { if (!isOpen()) return false; capture(); return snapshot() !== S.baseline; };
  const hasCommit = () => typeof S.commitId === 'number';
  const currentCommit = () => S.commits.find((c) => c.id === S.commitId) || null;
  const confirmDiscard = () => !isDirty() || confirm('You have unsaved changes in the editor. Discard them and continue?');

  function applyVersion(v) {
    const files = { ...v.files }, names = Object.keys(files);
    projectState.files = files;
    projectState.entry_point = (v.entry_point in files) ? v.entry_point : (names.includes('main.py') ? 'main.py' : names[0]);
    projectState.active_file = projectState.entry_point;
    if (editor) { editor.setValue(files[projectState.active_file] || ''); setTimeout(() => editor.layout(), 40); }
    S.baseline = snapshot();
    renderEditorTabs();
    renderProjectExplorer();
    render();
  }

  async function fetchVersion(id) {
    return id === 'wc' ? api(`${repoUrl()}/working-copy`) : api(`${repoUrl()}/commits/${id}`);
  }

  // Called when the project shown in the editor changes (or on login): reads its branches and commits.
  async function sync(opts = {}) {
    const o = owner(), p = projectState.current_project;
    if (!o || !p || !currentAuthToken) { S.project = null; render(); return; }
    const seq = ++S.seq;
    try {
      S.owner = o; S.project = p;
      const detail = await api(repoUrl());
      const want = (opts.branch || 'main').toLowerCase();
      const br = detail.branches.find((b) => b.name.toLowerCase() === want) || detail.branches[0];
      const list = (await api(`${repoUrl()}/commits?branch=${enc(br.name)}`)).commits;
      if (seq !== S.seq) return;
      S.branches = detail.branches; S.branch = br.name; S.commits = list; S.workingCopy = detail.working_copy;
      S.commitId = opts.commitId ?? 'wc';
      S.baseline = snapshot();            // the editor already shows the working copy
      render();
    } catch (e) {
      // 404 = project has no commits and no saved working copy yet: still usable, just empty history
      if (seq === S.seq) { S.branches = [{ name: 'main', commits: 0 }]; S.branch = 'main'; S.commits = []; S.workingCopy = null; S.commitId = 'wc'; S.baseline = snapshot(); render(); }
    }
  }

  async function selectCommit(id) {
    closeAll();
    if (id === S.commitId || !confirmDiscard()) return;
    const seq = ++S.seq;
    try {
      const v = await fetchVersion(id);
      if (seq !== S.seq) return;
      S.commitId = id;
      applyVersion(v);
    } catch (e) { notify('Could not load that version: ' + e.message); }
  }

  async function selectBranch(name) {
    closeAll();
    if (name === S.branch || !confirmDiscard()) return;
    const seq = ++S.seq;
    try {
      const list = (await api(`${repoUrl()}/commits?branch=${enc(name)}`)).commits;
      const detail = await api(repoUrl());
      const br = detail.branches.find((b) => b.name === name);
      const target = name === 'main' ? 'wc' : (list[0] ? list[0].id : (br && br.head_id));
      let v = null;
      if (target != null) v = await fetchVersion(target);
      if (seq !== S.seq) return;
      S.branch = name; S.branches = detail.branches; S.commits = list; S.workingCopy = detail.working_copy; S.commitId = target ?? null;
      if (v) applyVersion(v); else render();
    } catch (e) { notify('Could not switch branch: ' + e.message); }
  }

  async function refresh() {
    if (!isOpen()) return;
    try {
      const [detail, list] = await Promise.all([api(repoUrl()), api(`${repoUrl()}/commits?branch=${enc(S.branch)}`)]);
      S.branches = detail.branches; S.workingCopy = detail.working_copy; S.commits = list.commits; render();
    } catch (_) { /* keep what we have */ }
  }

  async function newBranch() {
    closeAll();
    if (!isOpen()) return;
    const base = hasCommit() ? S.commitId : (S.commits[0] ? S.commits[0].id : null);
    const name = prompt(`New branch name (starts from ${base ? '#' + base : 'your latest commit'}):`, '');
    if (!name || !name.trim()) return;
    capture();
    const carry = isDirty() ? { files: { ...projectState.files }, entry: projectState.entry_point } : null;
    try {
      await api(`${repoUrl()}/branches`, { method: 'POST', body: JSON.stringify({ name: name.trim(), from_commit_id: base }) });
      const detail = await api(repoUrl());
      S.branches = detail.branches; S.branch = name.trim(); S.commits = []; S.commitId = base;
      if (base != null && !carry) applyVersion(await fetchVersion(base));
      else if (carry) { projectState.files = carry.files; projectState.entry_point = carry.entry; render(); }
      else render();
      showSaveStatus(`On new branch "${name.trim()}"`, true);
    } catch (e) { notify('Could not create branch: ' + e.message); }
  }

  /* ----- commit dialog ----- */
  function openCommit() {
    if (!isOpen()) { notify('Open a project first.'); return; }
    closeAll();
    const dirty = isDirty();
    $('srCommitTarget').textContent = `${S.project} on ${S.branch}`;
    const notes = [];
    if (S.branch === 'main') notes.push('A commit on main is sent to your instructor as a new iteration.');
    else notes.push('This branch is private to you and your instructor; it is not queued for the robot.');
    if (!dirty && hasCommit()) notes.push('No changes since the selected commit.');
    $('srCommitNote').textContent = notes.join(' ');
    $('srCommitOk').disabled = !dirty && hasCommit();
    $('srCommitMessage').value = '';
    $('srCommitDialog').classList.remove('hidden');
    $('srCommitMessage').focus();
  }
  const closeCommit = () => $('srCommitDialog').classList.add('hidden');
  async function confirmCommit() {
    const msg = $('srCommitMessage').value.trim();
    if (!msg) { notify('Write a short commit note describing the change.'); return; }
    closeCommit();
    capture();
    try {
      const entry = projectState.entry_point;
      const res = await api(`${repoUrl()}/commits`, { method: 'POST', body: JSON.stringify({
        branch: S.branch, parent_id: hasCommit() ? S.commitId : null, files: projectState.files, entry_point: entry,
        message: msg, robot_model: projectState.robot_model || (typeof currentRobotModel !== 'undefined' ? currentRobotModel : 'ur') }) });
      S.commitId = res.commit_id; S.baseline = snapshot();
      if (S.branch === 'main') { try { saveProjectToStorage(); } catch (_) {} }
      await refresh();
      showSaveStatus(`Committed #${res.commit_id} to ${S.branch}`, true);
    } catch (e) { notify('Commit failed: ' + e.message); }
  }

  /* ----- rendering ----- */
  function render() {
    if (!$('srBar')) return;
    const open = isOpen(), c = currentCommit();
    $('srBranchLabel').textContent = S.branch;
    $('srCommitLabel').textContent = hasCommit() ? `#${S.commitId}` : (S.commitId === 'wc' ? 'Working copy' : 'No commits');
    $('srStatus').innerHTML = c ? badge(c.status) : (S.commitId === 'wc' ? '<span class="badge">Not committed</span>' : '');
    $('srDirty').classList.toggle('hidden', !isDirty());
    const line = $('srContext');
    if (!open) { line.textContent = ''; return; }
    let v;
    if (c) v = `commit #${c.id} by ${esc(c.author)}, ${esc(stamp(c.submitted_at))}${c.message ? ` — “${esc(c.message)}”` : ''}`;
    else if (S.commitId === 'wc') v = 'your working copy';
    else v = 'no commits yet';
    const hint = hasCommit() ? 'Viewing an older version: Save restores it into your working copy.' : 'Save keeps your working copy; Commit records a version.';
    line.innerHTML = `<span class="ctx-path">${esc(S.project)}</span> <span class="ctx-sep">›</span> branch <strong>${esc(S.branch)}</strong> <span class="ctx-sep">›</span> ${v}<span class="ctx-hint">${hint}</span>`;
  }
  let editTimer = null;
  function markEdited() { clearTimeout(editTimer); editTimer = setTimeout(() => { const d = $('srDirty'); if (d) d.classList.toggle('hidden', !isDirty()); }, 150); }

  function renderBranches() {
    $('srBranchList').innerHTML = S.branches.map((b) => `<button class="popover-item" role="option" aria-selected="${b.name === S.branch}" data-sbranch="${esc(b.name)}">
      <span class="pi-main"><strong>${esc(b.name)}</strong><span class="cell-sub">${b.commits} commit${b.commits === 1 ? '' : 's'}${b.name === 'main' ? ' · default · goes to your instructor' : ''}</span></span></button>`).join('');
  }
  function renderCommits() {
    let html = '';
    if (S.branch === 'main') {
      html += `<button class="popover-item" role="option" aria-selected="${S.commitId === 'wc'}" data-scommit="wc"><span class="pi-main"><strong>Working copy</strong><span class="cell-sub">What you are editing and saving now</span></span><span class="pi-side"><span class="badge">Not committed</span></span></button>`;
    }
    html += S.commits.map((c, i) => `<button class="popover-item" role="option" aria-selected="${c.id === S.commitId}" data-scommit="${c.id}">
      <span class="pi-main"><strong>#${c.id}${i === 0 ? ' · latest' : ''}</strong> ${esc(c.message || 'No commit note')}
        <span class="cell-sub">${esc(c.author)} · ${esc(stamp(c.submitted_at))} · ${c.files_count} file${c.files_count === 1 ? '' : 's'}</span></span>
      <span class="pi-side">${badge(c.status)}</span></button>`).join('');
    $('srCommitList').innerHTML = html || '<div class="popover-empty">No commits on this branch yet.</div>';
  }

  const PANELS = { branch: ['srBranchPanel', 'srBranchBtn'], commit: ['srCommitPanel', 'srCommitBtn'] };
  function closeAll() { Object.values(PANELS).forEach(([p, b]) => { const el = $(p); if (el) el.classList.add('hidden'); const bt = $(b); if (bt) bt.setAttribute('aria-expanded', 'false'); }); }
  async function toggle(name, ev) {
    if (ev) ev.stopPropagation();
    const [pid, bid] = PANELS[name];
    const was = !$(pid).classList.contains('hidden');
    closeAll();
    if (was) return;
    if (name === 'branch') renderBranches(); else { renderCommits(); refresh().then(renderCommits); }
    $(pid).classList.remove('hidden'); $(bid).setAttribute('aria-expanded', 'true');
  }

  function init() {
    document.addEventListener('click', (e) => {
      const item = e.target.closest && e.target.closest('.popover-item');
      if (item && item.dataset.sbranch) { selectBranch(item.dataset.sbranch); return; }
      if (item && item.dataset.scommit) { selectCommit(item.dataset.scommit === 'wc' ? 'wc' : Number(item.dataset.scommit)); return; }
      if (!(e.target.closest && (e.target.closest('.popover') || e.target.closest('.menu-btn')))) closeAll();
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closeAll(); if ($('srCommitDialog')) closeCommit(); } });
  }
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);

  window.StudentRepo = { state: S, sync, selectCommit, selectBranch, newBranch, openCommit, closeCommit, confirmCommit, toggle, markEdited, refresh,
                         // after Save / live sync the working copy is the new baseline
                         rebaseline() { S.baseline = snapshot(); if (S.branch === 'main' && !hasCommit()) S.commitId = 'wc'; render(); refresh(); },
                         isDirty, isOpen };
})();
