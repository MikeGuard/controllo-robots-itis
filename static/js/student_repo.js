/*
 * Student version history: the same branch / commit structure as the instructor's Code Studio,
 * scoped to the student's own projects (the server enforces that).
 *
 *   working copy = what you edit and Save on main (what the instructor sees live)
 *   commit       = a saved version; a commit on `main` is a submission to your instructor's queue
 *   branch       = a separate line of commits to experiment on; the instructor's queue ignores it
 *
 * "Unsaved" always means: different from S.base, the exact files the editor was loaded from or last
 * saved / committed. Viewing an older commit loads ALL of its files; nothing is overwritten until you
 * Save (restores it into your working copy) or Commit.
 *
 * Page hooks used (student.html): projectState, fileBuffer, replaceProjectFiles, saveProjectToStorage,
 * updateUnsavedIndicators, showSaveStatus, notify.
 */
(function () {
  'use strict';

  const C = window.Common;
  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;

  // commitId: 'wc' (working copy) | number | null (nothing committed and no working copy)
  const S = { owner: null, project: null, branch: 'main', branches: [], commits: [], commitId: 'wc', workingCopy: null, base: null, seq: 0 };

  const owner = () => (typeof currentStudentUser !== 'undefined' && currentStudentUser ? currentStudentUser.username : null);
  const api = (path, opts) => C.apiJson(path, currentAuthToken, opts);
  const repoUrl = () => `/api/repos/${enc(S.owner)}/${enc(S.project)}`;

  // What the editor holds right now (the open file's text included).
  function current() { fileBuffer.capture(); return C.fileSet(projectState.files, projectState.entry_point); }
  const versionSet = (v) => C.fileSet(v.files, C.resolveEntry(v.files, v.entry_point));

  const isOpen = () => !!S.project;
  const hasCommit = () => typeof S.commitId === 'number';
  const onWorkingCopy = () => S.branch === 'main' && !hasCommit();
  const isDirty = () => isOpen() && !C.sameFileSet(current(), S.base);
  const changedFiles = () => (isOpen() ? C.changedFiles(S.base, current().files) : new Set());
  const removedFiles = () => (isOpen() && S.base ? Object.keys(S.base.files).filter((n) => !(n in projectState.files)) : []);
  const currentCommit = () => S.commits.find((c) => c.id === S.commitId) || null;
  function confirmDiscard(what = 'continue') {
    if (!isDirty()) return true;
    return confirm(`You have ${onWorkingCopy() ? 'unsaved' : 'uncommitted'} changes in the editor. Discard them and ${what}?`);
  }

  function applyVersion(v) {
    S.base = versionSet(v);
    replaceProjectFiles(v.files, v.entry_point);       // never captures: the old editor text belongs to the old version
    render();
  }

  async function fetchVersion(id) {
    return id === 'wc' ? api(`${repoUrl()}/working-copy`) : api(`${repoUrl()}/commits/${id}`);
  }

  // Called right after a project was loaded into the editor (or on login): the editor shows the saved
  // working copy, so that is the baseline. Branches and commits are read in the background.
  async function sync() {
    const o = owner(), p = projectState.current_project;
    if (!o || !p || !currentAuthToken) { S.project = null; render(); return; }
    const seq = ++S.seq;
    Object.assign(S, { owner: o, project: p, branch: 'main', commitId: 'wc', commits: [], branches: [{ name: 'main', commits: 0 }], workingCopy: null });
    S.base = current();
    render();
    try {
      const [detail, list] = await Promise.all([api(repoUrl()), api(`${repoUrl()}/commits?branch=main`)]);
      if (seq !== S.seq) return;
      S.branches = detail.branches; S.commits = list.commits; S.workingCopy = detail.working_copy;
      render();
    } catch (_) {
      // 404 = the project has no commits and no saved working copy yet: still usable, just empty history
      if (seq === S.seq) render();
    }
  }

  // Picking the version you are already on (with unsaved changes) reloads it: that is "discard my changes".
  async function selectCommit(id) {
    closeAll();
    const same = id === S.commitId;
    if (same && !isDirty()) return;
    if (!confirmDiscard(same ? 'reload this version' : 'open the other version')) return;
    const seq = ++S.seq;
    try {
      const v = await fetchVersion(id);
      if (seq !== S.seq) return;
      S.commitId = id;
      applyVersion(v);
      if (id === 'wc') saveProjectToStorage();
    } catch (e) { notify('Could not load that version: ' + e.message); }
  }

  async function selectBranch(name) {
    closeAll();
    if (name === S.branch || !confirmDiscard('switch branch')) return;
    const seq = ++S.seq;
    try {
      const [list, detail] = await Promise.all([api(`${repoUrl()}/commits?branch=${enc(name)}`).then((r) => r.commits), api(repoUrl())]);
      const br = detail.branches.find((b) => b.name === name);
      const target = name === 'main' ? 'wc' : (list[0] ? list[0].id : (br && br.head_id));
      const v = target != null ? await fetchVersion(target) : null;
      if (seq !== S.seq) return;
      S.branch = name; S.branches = detail.branches; S.commits = list; S.workingCopy = detail.working_copy; S.commitId = target ?? null;
      if (v) applyVersion(v);
      else { S.base = C.fileSet({}, null); render(); }      // empty branch: the editor's files are its first commit
      if (name === 'main') saveProjectToStorage();
    } catch (e) { notify('Could not switch branch: ' + e.message); }
  }

  async function refresh() {
    if (!isOpen()) return;
    const project = S.project, branch = S.branch;
    try {
      const [detail, list] = await Promise.all([api(repoUrl()), api(`${repoUrl()}/commits?branch=${enc(branch)}`)]);
      if (S.project !== project || S.branch !== branch) return;
      S.branches = detail.branches; S.workingCopy = detail.working_copy; S.commits = list.commits; render();
    } catch (_) { /* keep what we have */ }
  }

  // The editor keeps showing exactly what you branch from, edits included. The branch starts at the commit
  // you are viewing (or your latest commit), so anything that differs from it shows as uncommitted.
  async function newBranch() {
    closeAll();
    if (!isOpen()) return;
    const base = hasCommit() ? S.commitId : (S.commits[0] ? S.commits[0].id : null);
    const from = hasCommit() ? `commit #${S.commitId}` : (S.branch === 'main' ? 'your working copy' : 'the files in the editor');
    const raw = prompt(`Name for the new branch (letters, digits, - _ . /):\nIt starts from ${from}. Main and your instructor's queue are not changed.`, '');
    if (!raw || !raw.trim()) return;
    const name = raw.trim();
    try {
      await api(`${repoUrl()}/branches`, { method: 'POST', body: JSON.stringify({ name, from_commit_id: base }) });
      const [detail, baseVersion] = await Promise.all([api(repoUrl()), base != null ? fetchVersion(base) : Promise.resolve(null)]);
      S.seq++;                                            // a version still loading must not land on the new branch
      Object.assign(S, { branches: detail.branches, branch: name, commits: [], commitId: base, workingCopy: detail.working_copy });
      S.base = baseVersion ? versionSet(baseVersion) : C.fileSet({}, null);
      render();
      showSaveStatus(`On new branch "${name}"`, true);
    } catch (e) { notify('Could not create branch: ' + e.message); }
  }

  /* ----- commit dialog ----- */
  function openCommit() {
    if (!isOpen()) { notify('Open a project first.'); return; }
    closeAll();
    const dirty = isDirty();
    $('srCommitTarget').textContent = `${S.project} on ${S.branch}`;
    const notes = [];
    if (S.branch === 'main') notes.push('A commit on main is sent to your instructor as a new iteration and becomes your saved working copy.');
    else notes.push(`Branch "${S.branch}" is private to you and your instructor; it is not queued for the robot.`);
    if (!dirty && hasCommit()) notes.push('Nothing changed since this commit.');
    const ch = [...changedFiles()], rm = removedFiles();
    if (ch.length) notes.push(`Changed: ${ch.slice(0, 6).join(', ')}${ch.length > 6 ? ', …' : ''}.`);
    if (rm.length) notes.push(`Deleted: ${rm.slice(0, 6).join(', ')}${rm.length > 6 ? ', …' : ''}.`);
    $('srCommitNote').textContent = notes.join(' ');
    $('srCommitOk').disabled = !dirty && hasCommit();
    $('srCommitMessage').value = '';
    $('srCommitDialog').classList.remove('hidden');
    $('srCommitMessage').focus();
  }
  const closeCommit = () => $('srCommitDialog').classList.add('hidden');

  async function confirmCommit() {
    const msg = $('srCommitMessage').value.trim();
    if (!msg) { notify('Write a short commit note describing the change.'); $('srCommitMessage').focus(); return; }
    const sent = current();                           // exactly these files; later edits stay "uncommitted"
    const project = S.project, branch = S.branch;
    closeCommit();
    try {
      const res = await api(`${repoUrl()}/commits`, { method: 'POST', body: JSON.stringify({
        branch, parent_id: hasCommit() ? S.commitId : null, files: sent.files, entry_point: sent.entry,
        message: msg, robot_model: projectState.robot_model || (typeof currentRobotModel !== 'undefined' ? currentRobotModel : 'ur') }) });
      if (S.project !== project || S.branch !== branch) { showSaveStatus(`Committed #${res.commit_id} to ${branch}`, true); return; }
      S.base = sent;
      // On main the server also wrote these files into your working copy, so you stay on the working copy.
      S.commitId = branch === 'main' ? 'wc' : res.commit_id;
      if (branch === 'main') saveProjectToStorage();
      render();
      await refresh();
      showSaveStatus(branch === 'main' ? `Committed #${res.commit_id}: sent to your instructor` : `Committed #${res.commit_id} to ${branch}`, true);
    } catch (e) {
      notify('Commit failed, nothing was recorded: ' + e.message);
      showSaveStatus('Commit failed', false);
    }
  }

  /* ----- rendering ----- */
  function hint(dirty) {
    if (S.branch !== 'main') return dirty ? `Commit to keep these changes on "${S.branch}" (main is not affected).` : 'Experiment freely: main and your instructor’s queue are not affected.';
    if (hasCommit()) return 'Viewing an older version: "Restore this version" copies it into your working copy.';
    return dirty ? 'Unsaved changes: press Save (Ctrl+S) to keep them.' : 'All changes saved. Commit records a version and sends it to your instructor.';
  }

  // Cheap refresh for every edit: unsaved badge, hint, per-file dots and the Save button.
  function refreshDirty() {
    const dirty = isDirty();
    const d = $('srDirty');
    if (d) { d.classList.toggle('hidden', !dirty); d.textContent = onWorkingCopy() ? 'Unsaved changes' : 'Uncommitted changes'; }
    const h = $('srContext') && $('srContext').querySelector('.ctx-hint');
    if (h) h.textContent = hint(dirty);
    if (typeof updateUnsavedIndicators === 'function') updateUnsavedIndicators();
  }

  function render() {
    if (!$('srBar')) return;
    const open = isOpen(), c = currentCommit();
    $('srBranchLabel').textContent = S.branch;
    $('srCommitLabel').textContent = hasCommit() ? `#${S.commitId}` : (S.commitId === 'wc' ? 'Working copy' : 'No commits');
    $('srStatus').innerHTML = c ? C.statusBadge(c.status) : '';
    const line = $('srContext');
    if (!open) { line.textContent = ''; refreshDirty(); return; }
    let v;
    if (c) v = `commit #${c.id} by ${C.esc(c.author)}, ${C.esc(C.stamp(c.submitted_at))}${c.message ? ` — “${C.esc(c.message)}”` : ''}`;
    else if (hasCommit()) v = `commit #${S.commitId}`;
    else if (S.commitId === 'wc') v = 'your working copy';
    else v = 'no commits yet';
    line.innerHTML = `<span class="ctx-path">${C.esc(S.project)}</span> <span class="ctx-sep">›</span> branch <strong>${C.esc(S.branch)}</strong> <span class="ctx-sep">›</span> ${v}<span class="ctx-hint"></span>`;
    refreshDirty();
  }
  let editTimer = null;
  function markEdited() { clearTimeout(editTimer); editTimer = setTimeout(refreshDirty, 150); }

  function renderBranches() {
    $('srBranchList').innerHTML = S.branches.map((b) => `<button class="popover-item" role="option" aria-selected="${b.name === S.branch}" data-sbranch="${C.esc(b.name)}">
      <span class="pi-main"><strong>${C.esc(b.name)}</strong><span class="cell-sub">${b.commits} commit${b.commits === 1 ? '' : 's'}${b.name === 'main' ? ' · default · goes to your instructor' : ''}</span></span></button>`).join('');
  }
  function renderCommits() {
    const dirty = isDirty();
    const sub = (selected, text) => (selected && dirty ? 'Click to discard your unsaved changes' : text);
    let html = '';
    if (S.branch === 'main') {
      html += `<button class="popover-item" role="option" aria-selected="${S.commitId === 'wc'}" data-scommit="wc"><span class="pi-main"><strong>Working copy</strong><span class="cell-sub">${sub(S.commitId === 'wc', 'What you are editing and saving now')}</span></span><span class="pi-side"><span class="badge">Not committed</span></span></button>`;
    }
    html += S.commits.map((c, i) => `<button class="popover-item" role="option" aria-selected="${c.id === S.commitId}" data-scommit="${c.id}">
      <span class="pi-main"><strong>#${c.id}${i === 0 ? ' · latest' : ''}</strong> ${C.esc(c.message || 'No commit note')}
        <span class="cell-sub">${sub(c.id === S.commitId, `${C.esc(c.author)} · ${C.esc(C.stamp(c.submitted_at))} · ${c.files_count} file${c.files_count === 1 ? '' : 's'}`)}</span></span>
      <span class="pi-side">${C.statusBadge(c.status)}</span></button>`).join('');
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
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closeAll(); if ($('srCommitDialog')) closeCommit(); }
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && e.target && e.target.id === 'srCommitMessage') { e.preventDefault(); confirmCommit(); }
    });
  }
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);

  window.StudentRepo = {
    state: S, sync, selectCommit, selectBranch, newBranch, openCommit, closeCommit, confirmCommit, toggle, markEdited, refresh,
    isDirty, isOpen, onWorkingCopy, changedFiles, confirmDiscard,
    // the working copy on the server now holds `set` (default: what the editor holds): it is the new baseline
    rebaseline(set) { S.base = set || current(); if (S.branch === 'main' && !hasCommit()) S.commitId = 'wc'; render(); refresh(); },
    // Save succeeded: the editor (restored commit or edits) is now the working copy on main
    markSaved(set) { S.commitId = 'wc'; S.base = C.fileSet(set.files, set.entry); render(); refresh(); },
    // the project folder was renamed on the server (history moves with it); edits in the editor are kept
    renamed(name) { S.project = name; render(); refresh(); },
  };
})();
