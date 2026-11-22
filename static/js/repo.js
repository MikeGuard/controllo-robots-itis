/*
 * Repository browser for Code Studio.
 *
 *   project  = a folder with a file tree (nested folders supported)
 *   commit   = an iteration (what students submit; what admins commit)
 *   branch   = a named line of commits; every project has `main`
 *
 * The selector (project / branch / commit) decides what the file tree and editor show, and Edit, Run and
 * Simulate always act on that selection. Run sends exactly the selected commit: if the editor has
 * uncommitted changes they are committed first, so nothing else can be executed by accident.
 *
 * Server API: /api/repos/...  (see app.py). This file only talks to that API and to the existing studio
 * globals (adminProjectState, studioEditor, studioBuffer, ...). Shared helpers come from static/js/common.js.
 *
 * "Uncommitted" always means: different from S.base, the exact files of the version that was loaded or last committed.
 */
(function () {
  'use strict';

  const C = window.Common;
  const $ = (id) => document.getElementById(id);
  const esc = C.esc;
  const enc = encodeURIComponent;
  const badge = C.statusBadge;

  /* ---------- pure helpers (also used by tests/tree.test.js) ---------- */

  // paths -> nested tree. Folders first, then files, each alphabetically.
  function buildTree(paths) {
    const root = { name: '', path: '', dir: true, children: [] };
    for (const p of [...paths].sort()) {
      const parts = p.split('/');
      let node = root;
      parts.forEach((part, i) => {
        const last = i === parts.length - 1;
        const path = parts.slice(0, i + 1).join('/');
        let child = node.children.find((c) => c.name === part && c.dir === !last);
        if (!child) { child = { name: part, path, dir: !last, children: [] }; node.children.push(child); }
        node = child;
      });
    }
    const order = (n) => { n.children.sort((a, b) => (a.dir === b.dir ? a.name.localeCompare(b.name) : a.dir ? -1 : 1)); n.children.forEach(order); };
    order(root);
    return root;
  }

  const relTime = C.relTime;
  const stamp = C.stamp;

  /* ---------- state ---------- */

  const S = {
    repos: [], owner: null, project: null, adminOwned: false,
    branch: 'main', branches: [], commits: [], commitId: null,   // commitId: number | 'wc' (working copy) | null (nothing committed yet)
    workingCopy: null, base: null, seq: 0, collapsed: new Set(), editTimer: null, model: 'ur',
  };

  const api = (path, opts) => C.apiJson(path, currentAdminToken, opts);
  const repoUrl = (o, p) => `/api/repos/${enc(o)}/${enc(p)}`;

  // Editor text -> the file it was loaded from (studioBuffer, see Common.createFileBuffer).
  function capture() { if (typeof studioBuffer !== 'undefined') studioBuffer.capture(); }
  function current() { capture(); return C.fileSet(adminProjectState.files, adminProjectState.entry_point); }
  const versionSet = (v) => C.fileSet(v.files, C.resolveEntry(v.files, v.entry_point));
  function isOpen() { return !!S.project; }
  function isDirty() { return isOpen() && !C.sameFileSet(current(), S.base); }
  const changedFiles = () => (isOpen() ? C.changedFiles(S.base, current().files) : new Set());
  const removedFiles = () => (isOpen() && S.base ? Object.keys(S.base.files).filter((n) => !(n in adminProjectState.files)) : []);
  const hasCommit = () => typeof S.commitId === 'number';
  const currentCommit = () => S.commits.find((c) => c.id === S.commitId) || null;

  function confirmDiscard(what = 'continue') {
    if (!isDirty()) return true;
    return confirm(`You have uncommitted changes in the editor. Discard them and ${what}?`);
  }

  /* ---------- loading ---------- */

  function applyVersion(v) {
    S.base = versionSet(v);
    adminProjectState.files = { ...v.files };
    adminProjectState.entry_point = S.base.entry;
    adminProjectState.current_project = S.project;
    adminProjectState.owner = S.owner;
    adminProjectState.is_student_project = !S.adminOwned;
    adminProjectState.submission_id = hasCommit() ? S.commitId : null;
    adminProjectState.robot_model = v.robot_model || S.model || 'ur';
    selectedSubId = hasCommit() ? S.commitId : null;
    S.model = adminProjectState.robot_model;
    const sel = $('creatorModelSelect'); if (sel) sel.value = S.model === 'niryo' ? 'niryo' : 'ur';
    if (typeof studioEditor !== 'undefined' && studioEditor) {
      studioEditor.updateOptions({ readOnly: false });
      setTimeout(() => studioEditor.layout(), 40);
    }
    studioBuffer.load(S.base.entry);                    // never captures: the old text belongs to the old version
    studioRenderFileTabs();
    renderTree();
    renderHeader();
  }

  async function fetchVersion(id) {
    if (id === 'wc') return api(`${repoUrl(S.owner, S.project)}/working-copy`);
    return api(`${repoUrl(S.owner, S.project)}/commits/${id}`);
  }

  async function loadRepos() {
    try { S.repos = (await api('/api/repos')).repos; } catch (e) { notify('Could not load projects: ' + e.message); }
    renderPicker();
    return S.repos;
  }

  // Open a project at a branch/commit. Nothing changes on screen unless every request succeeds.
  async function open(owner, project, opts = {}) {
    if (!(opts.force || confirmDiscard())) return false;
    const seq = ++S.seq;
    try {
      const detail = await api(repoUrl(owner, project));
      const want = (opts.branch || 'main').toLowerCase();
      const br = detail.branches.find((b) => b.name.toLowerCase() === want) || detail.branches[0];
      const list = (await api(`${repoUrl(owner, project)}/commits?branch=${enc(br.name)}`)).commits;
      let target = opts.commitId ?? (list[0] ? list[0].id : (br.head_id ?? (detail.working_copy ? 'wc' : null)));
      let version = null;
      if (target !== null) {
        const next = { owner, project }; const keep = { o: S.owner, p: S.project };
        S.owner = next.owner; S.project = next.project;            // fetchVersion reads these
        try { version = await fetchVersion(target); } finally { S.owner = keep.o; S.project = keep.p; }
      }
      if (seq !== S.seq) return false;                              // a newer open() superseded this one
      S.owner = owner; S.project = project; S.adminOwned = !!detail.is_admin_owned;
      S.branches = detail.branches; S.branch = br.name; S.commits = list; S.workingCopy = detail.working_copy;
      S.model = detail.robot_model || 'ur'; S.commitId = target;
      if (!version) {                                                // empty project: blank editor, nothing committed yet
        const ip = S.model === 'niryo' ? niryoIp : urIp;
        version = { files: { 'main.py': (S.model === 'niryo' ? CREATOR_TEMPLATES.niryo_default(ip) : CREATOR_TEMPLATES.default(ip)) }, entry_point: 'main.py', robot_model: S.model };
      }
      applyVersion(version);
      return true;
    } catch (e) {
      if (seq === S.seq) { notify(`Could not open "${project}": ${e.message}`); renderHeader(); }
      return false;
    }
  }

  // Picking the version you are already on (with uncommitted changes) reloads it: that is "discard my changes".
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
    } catch (e) { notify('Could not load that commit: ' + e.message); }
  }

  async function selectBranch(name) {
    closeAll();
    if (name === S.branch) return;
    await open(S.owner, S.project, { branch: name });
  }

  async function refreshCommits() {
    if (!isOpen()) return;
    const owner = S.owner, project = S.project, branch = S.branch;
    try {
      const [detail, list] = await Promise.all([
        api(repoUrl(owner, project)),
        api(`${repoUrl(owner, project)}/commits?branch=${enc(branch)}`),
      ]);
      if (S.owner !== owner || S.project !== project || S.branch !== branch) return;   // moved on meanwhile
      S.branches = detail.branches; S.workingCopy = detail.working_copy; S.commits = list.commits;
      renderHeader();
    } catch (_) { /* keep what we have */ }
  }
  let softTimer = null;
  function refreshSoon() { clearTimeout(softTimer); softTimer = setTimeout(() => { if (isOpen()) refreshCommits(); }, 400); }

  /* ---------- actions ---------- */

  function defaultFiles() {
    const model = $('creatorModelSelect') ? $('creatorModelSelect').value : 'ur';
    const ip = model === 'niryo' ? niryoIp : urIp;
    return { model, files: { 'main.py': model === 'niryo' ? CREATOR_TEMPLATES.niryo_default(ip) : CREATOR_TEMPLATES.default(ip) } };
  }

  async function newProject(presetFiles) {
    closeAll();
    if (!confirmDiscard()) return;
    const name = prompt('Name for the new project:');
    if (!name || !name.trim()) return;
    const clean = name.trim().replace(/[^a-zA-Z0-9_-]/g, '_');
    const d = defaultFiles();
    try {
      await api('/api/repos', { method: 'POST', body: JSON.stringify({ project: clean, robot_model: d.model, files: presetFiles || d.files }) });
      await loadRepos();
      await open(currentAdminUser || 'admin', clean, { force: true });
      showActionToast(`Created project "${clean}".`);
    } catch (e) { notify('Could not create project: ' + e.message); }
  }

  // The editor keeps showing exactly what you branch from (uncommitted edits, or the student's working copy,
  // included). The branch starts at the selected commit, or main's latest one; whatever differs from that
  // commit shows as uncommitted on the new branch.
  async function newBranch() {
    closeAll();
    if (!isOpen()) return;
    const base = hasCommit() ? S.commitId : null;
    const from = base ? `commit #${base}` : (S.commitId === 'wc' ? `${S.owner}'s working copy` : `the latest commit on main`);
    const raw = prompt(`New branch name (letters, digits, - _ . /):\nIt starts from ${from}; the files in the editor come along. Main and the student are not changed.`, '');
    if (!raw || !raw.trim()) return;
    const name = raw.trim();
    const owner = S.owner, project = S.project;
    try {
      const res = await api(`${repoUrl(owner, project)}/branches`, { method: 'POST', body: JSON.stringify({ name, from_commit_id: base }) });
      const [detail, list, baseVersion] = await Promise.all([
        api(repoUrl(owner, project)),
        api(`${repoUrl(owner, project)}/commits?branch=${enc(name)}`),
        res.base_id != null ? fetchVersion(res.base_id) : Promise.resolve(null),
      ]);
      if (S.owner !== owner || S.project !== project) return;
      S.seq++;                                           // a version still loading must not land on the new branch
      Object.assign(S, { branches: detail.branches, branch: name, commits: list.commits, workingCopy: detail.working_copy, commitId: res.base_id ?? null });
      S.base = baseVersion ? versionSet(baseVersion) : C.fileSet({}, null);
      adminProjectState.submission_id = selectedSubId = hasCommit() ? S.commitId : null;
      studioRenderFileTabs(); renderTree(); renderHeader();
      showActionToast(`Switched to new branch "${name}".`);
    } catch (e) { notify('Could not create branch: ' + e.message); }
  }

  function openCommit() {
    if (!isOpen()) { notify('Open a project first.'); return; }
    closeAll();
    const dirty = isDirty();
    $('commitDialogTarget').textContent = `${S.owner}/${S.project} on ${S.branch}`;
    const notes = [];
    if (!dirty) notes.push('No changes since the selected version.');
    const ch = [...changedFiles()], rm = removedFiles();
    if (ch.length) notes.push(`Changed: ${ch.slice(0, 6).join(', ')}${ch.length > 6 ? ', …' : ''}.`);
    if (rm.length) notes.push(`Deleted: ${rm.slice(0, 6).join(', ')}${rm.length > 6 ? ', …' : ''}.`);
    if (hasCommit() && currentCommit() && currentCommit().id !== (S.commits[0] || {}).id) notes.push(`You are on an older commit (#${S.commitId}); the new commit is based on it.`);
    if (!S.adminOwned && S.branch === 'main') notes.push(`This also updates ${S.owner}'s working copy, so the student sees it.`);
    if (S.branch !== 'main') notes.push('This branch is separate: the student is not affected.');
    $('commitDialogNote').textContent = notes.join(' ');
    $('commitDialogOk').disabled = !dirty && hasCommit();
    $('commitMessage').value = '';
    $('commitDialog').classList.remove('hidden');
    $('commitMessage').focus();
  }
  function closeCommit() { $('commitDialog').classList.add('hidden'); }
  async function confirmCommit() {
    const msg = $('commitMessage').value.trim() || 'Instructor edit';
    closeCommit();
    await commit(msg);
  }

  // Creates a commit from the editor. Returns the new commit id, or null if it failed.
  // Exactly the files sent become the baseline: anything typed while the request runs stays "uncommitted".
  async function commit(message) {
    const sent = current();
    const owner = S.owner, project = S.project, branch = S.branch;
    try {
      const res = await api(`${repoUrl(owner, project)}/commits`, {
        method: 'POST',
        body: JSON.stringify({ branch, parent_id: hasCommit() ? S.commitId : null, files: sent.files,
                               entry_point: sent.entry, message, robot_model: $('creatorModelSelect') ? $('creatorModelSelect').value : S.model }),
      });
      if (S.owner === owner && S.project === project && S.branch === branch) {
        S.commitId = res.commit_id; S.base = sent;
        selectedSubId = res.commit_id; adminProjectState.submission_id = res.commit_id;
      }
      await refreshCommits();
      renderHeader();
      if (typeof fetchSubmissions === 'function') fetchSubmissions();
      showActionToast(`Committed #${res.commit_id} to ${branch}${res.synced_to_student ? ` and synced to ${owner}` : ''}.`);
      return res.commit_id;
    } catch (e) {
      notify('Commit failed, nothing was recorded: ' + e.message);
      return null;
    }
  }

  // Run = the selected commit, exactly. Uncommitted editor changes are committed first (after asking).
  async function runSelected() {
    if (!hasAdminPermission('can_execute')) { notify('Missing permission: can_execute.'); return; }
    if (!isOpen()) { notify('Open a project first.'); return; }
    let runId = S.commitId;
    if (isDirty() || !hasCommit()) {
      const why = !hasCommit() ? 'There is no commit to run yet.' : 'The editor has uncommitted changes.';
      if (!confirm(`${why}\nRun executes an exact commit, so your changes will be committed to "${S.branch}" first. Continue?`)) return;
      runId = await commit(!hasCommit() ? 'Initial commit' : 'Run from Studio');
      if (runId === null) return;
    }
    if (typeof fetchSubmissions === 'function') await fetchSubmissions();
    const model = $('creatorModelSelect') ? $('creatorModelSelect').value : S.model;
    await quickRunSubmission(runId, null, model);
    refreshSoon();
  }

  async function renameProject() {
    if (!isOpen()) return;
    if (!S.adminOwned) { notify('Student projects cannot be renamed here; it would break the link to the student.'); return; }
    const name = prompt(`Rename "${S.project}" to:`, S.project);
    if (!name || !name.trim() || name.trim() === S.project) return;
    if (!confirmDiscard()) return;
    try {
      const r = await api(`${repoUrl(S.owner, S.project)}/rename`, { method: 'POST', body: JSON.stringify({ new_name: name.trim() }) });
      await loadRepos();
      await open(S.owner, r.project, { force: true });
      showActionToast(`Renamed to "${r.project}".`);
    } catch (e) { notify('Rename failed: ' + e.message); }
  }

  async function deleteProject() {
    if (!isOpen()) return;
    try {
      if (S.branch !== 'main') {
        const n = (S.branches.find((b) => b.name === S.branch) || {}).commits || 0;
        if (!confirm(`Delete branch "${S.branch}" and its ${n} commit${n === 1 ? '' : 's'}?\n\nThis cannot be undone. The main branch and the student's working copy are not affected.`)) return;
        await api(`${repoUrl(S.owner, S.project)}/branches/${enc(S.branch)}`, { method: 'DELETE' });
        await open(S.owner, S.project, { force: true });
        showActionToast('Branch deleted.');
      } else if (S.adminOwned) {
        if (!confirm(`Permanently delete the project "${S.project}" with all its branches and commits?\n\nThis cannot be undone.`)) return;
        await api(repoUrl(S.owner, S.project), { method: 'DELETE' });
        S.project = null; await loadRepos();
        const first = S.repos[0]; if (first) await open(first.owner, first.project, { force: true }); else renderHeader();
        showActionToast('Project deleted.');
      } else {
        notify("Student projects can't be deleted here. Switch to a branch to delete that instead.");
      }
      if (typeof fetchSubmissions === 'function') fetchSubmissions();
    } catch (e) { notify('Delete failed: ' + e.message); }
  }

  /* ---------- rendering ---------- */

  function renderHeader() {
    const open = isOpen();
    const c = currentCommit();
    $('repoPickerLabel').textContent = open ? `${S.owner} / ${S.project}` : 'Select a project';
    $('branchLabel').textContent = S.branch;
    $('commitLabel').textContent = hasCommit() ? `#${S.commitId}` : (S.commitId === 'wc' ? 'Working copy' : 'No commits');
    const st = $('studioCommitStatus');
    st.innerHTML = c ? badge(c.status) : (S.commitId === 'wc' ? '<span class="badge">Not committed</span>' : '');
    const names = Object.keys(adminProjectState.files || {});
    const folder = $('studioFolderName'); if (folder) folder.textContent = open ? `${S.owner}/${S.project}@${S.branch}` : '';
    const np = $('creatorProgName'); if (np && open) np.value = S.project;
    const cnt = $('studioTabFilesCountBadge'); if (cnt) cnt.textContent = names.length;
    const fb = $('adminTabFilesBadge'); if (fb) fb.textContent = `${names.length} file${names.length === 1 ? '' : 's'}`;
    const foot = $('studioFooterStats'); if (foot) foot.textContent = `${names.length} file${names.length === 1 ? '' : 's'} • Entry: ${adminProjectState.entry_point}`;

    const line = $('studioContextLine');
    if (!open) { line.textContent = 'Pick a project to start. Edit, Run and Simulate act on the branch and commit you select here.'; updateDirtyUI(); return; }
    let version;
    if (c) version = `commit #${c.id} by ${esc(c.author)}, ${esc(stamp(c.submitted_at))}${c.message ? ` — “${esc(c.message)}”` : ''}`;
    else if (hasCommit()) version = `starts from #${S.commitId} (no commits on this branch yet)`;
    else if (S.commitId === 'wc') version = `the working copy (not committed yet)`;
    else version = 'no commits yet';
    line.innerHTML = `<span class="ctx-path">${esc(S.owner)} / ${esc(S.project)}</span> <span class="ctx-sep">›</span> branch <strong>${esc(S.branch)}</strong> <span class="ctx-sep">›</span> ${version}<span class="ctx-hint"></span>`;
    const bs = $('studioBottomStatus'); if (bs) bs.textContent = open ? `${S.owner}/${S.project} • ${S.branch}` : 'No project open';
    updateDirtyUI();
  }

  // Cheap refresh after every edit: badge, hint, per-file dots and the Commit button's emphasis.
  function updateDirtyUI() {
    const dirty = isDirty();
    const d = $('studioDirtyBadge'); if (d) d.classList.toggle('hidden', !dirty);
    const line = $('studioContextLine'); const hint = line && line.querySelector('.ctx-hint');
    if (hint) hint.textContent = dirty ? 'Uncommitted edits: Simulate uses the editor; Run commits them first (Ctrl+S to commit).' : 'Edit, Run and Simulate use this version.';
    const changed = changedFiles();
    C.markModified($('studioFileTabs'), changed);
    C.markModified($('studioFilesList'), changed);
    const btn = $('btnCommitStudio'); if (btn) btn.classList.toggle('btn-primary', dirty);
  }
  function markEdited() {
    if (!isOpen()) return;
    clearTimeout(S.editTimer);
    S.editTimer = setTimeout(updateDirtyUI, 120);
  }

  function renderPicker() {
    const list = $('repoPickerList'); if (!list) return;
    const q = ($('repoSearch') ? $('repoSearch').value : '').toLowerCase().trim();
    const repos = S.repos.filter((r) => !q || `${r.owner} ${r.project}`.toLowerCase().includes(q));
    if (!repos.length) { list.innerHTML = '<div class="popover-empty">No projects match.</div>'; return; }
    const byOwner = new Map();
    repos.forEach((r) => { if (!byOwner.has(r.owner)) byOwner.set(r.owner, []); byOwner.get(r.owner).push(r); });
    let html = '';
    for (const [owner, rs] of byOwner) {
      html += `<div class="popover-group">${esc(owner)}</div>`;
      for (const r of rs) {
        const cur = isOpen() && r.owner.toLowerCase() === S.owner.toLowerCase() && r.project.toLowerCase() === S.project.toLowerCase();
        html += `<button class="popover-item" role="option" aria-selected="${cur}" data-repo="${esc(r.owner)}\u0001${esc(r.project)}">
          <span class="pi-main"><strong>${esc(r.project)}</strong><span class="cell-sub">${r.commits} commit${r.commits === 1 ? '' : 's'} · ${r.branches.length} branch${r.branches.length === 1 ? '' : 'es'}${r.last_activity ? ' · ' + esc(relTime(r.last_activity)) : ''}</span></span>
          <span class="pi-side"><span class="badge">${r.robot_model === 'niryo' ? 'Niryo' : 'UR3'}</span>${r.head ? badge(r.head.status) : '<span class="badge">No commits</span>'}</span></button>`;
      }
    }
    list.innerHTML = html;
  }

  function renderBranches() {
    $('branchList').innerHTML = S.branches.map((b) => `<button class="popover-item" role="option" aria-selected="${b.name === S.branch}" data-branch="${esc(b.name)}">
      <span class="pi-main"><strong>${esc(b.name)}</strong><span class="cell-sub">${b.commits} commit${b.commits === 1 ? '' : 's'}${b.name === 'main' ? ' · default' : (b.created_by ? ' · by ' + esc(b.created_by) : '')}</span></span></button>`).join('') ||
      '<div class="popover-empty">No branches.</div>';
  }

  function renderCommits() {
    const dirty = isDirty();
    const sub = (selected, text) => (selected && dirty ? 'Click to discard your uncommitted changes' : text);
    let html = '';
    if (S.workingCopy && S.branch === 'main') {
      html += `<button class="popover-item" role="option" aria-selected="${S.commitId === 'wc'}" data-commit="wc"><span class="pi-main"><strong>Working copy</strong><span class="cell-sub">${sub(S.commitId === 'wc', `What ${esc(S.owner)} currently has open (${S.workingCopy.files_count} file${S.workingCopy.files_count === 1 ? '' : 's'}), not committed`)}</span></span><span class="pi-side"><span class="badge">Not committed</span></span></button>`;
    }
    html += S.commits.map((c, i) => `<button class="popover-item" role="option" aria-selected="${c.id === S.commitId}" data-commit="${c.id}">
      <span class="pi-main"><strong>#${c.id}${i === 0 ? ' · latest' : ''}</strong> ${esc(c.message || 'No commit note')}
        <span class="cell-sub">${sub(c.id === S.commitId, `${esc(c.author)} · ${esc(stamp(c.submitted_at))} · ${c.files_count} file${c.files_count === 1 ? '' : 's'} · ${c.loc} lines`)}</span></span>
      <span class="pi-side">${badge(c.status)}</span></button>`).join('');
    $('commitList').innerHTML = html || '<div class="popover-empty">No commits on this branch yet.</div>';
  }

  /* ----- file tree ----- */
  function renderTree() {
    const host = $('studioFilesList'); if (!host) return;
    const files = adminProjectState.files || {};
    const entry = adminProjectState.entry_point, active = adminProjectState.active_file;
    const changed = changedFiles();
    const tree = buildTree(Object.keys(files));
    const row = (n, depth) => {
      const pad = `padding-left:${8 + depth * 14}px`;
      if (n.dir) {
        const closed = S.collapsed.has(n.path);
        return `<div class="tree-row tree-dir" style="${pad}" data-dir="${esc(n.path)}"><span class="tree-caret" aria-hidden="true">${closed ? '▸' : '▾'}</span><span class="tree-name">${esc(n.name)}/</span></div>` +
          (closed ? '' : n.children.map((c) => row(c, depth + 1)).join(''));
      }
      const isEntry = n.path === entry;
      return `<div class="tree-row tree-file ${n.path === active ? 'is-active' : ''} ${changed.has(n.path) ? 'is-modified' : ''}" style="${pad}" data-file="${esc(n.path)}" title="${esc(n.path)}" aria-current="${n.path === active}">
        <span class="tree-caret"></span><span class="tree-name">${esc(n.name)}</span><span class="file-dot" style="margin-left:6px" title="Uncommitted changes" aria-label="uncommitted changes"></span>${isEntry ? '<span class="badge" style="margin-left:6px" title="Runs when you Simulate or Run">entry</span>' : ''}
        <span class="tree-actions">${!isEntry && n.path.endsWith('.py') ? `<button type="button" class="btn btn-sm btn-ghost" data-act="entry" data-path="${esc(n.path)}">Set entry</button>` : ''}
          <button type="button" class="btn btn-sm btn-ghost" data-act="rename" data-path="${esc(n.path)}">Rename</button>
          <button type="button" class="btn btn-sm btn-ghost btn-quiet-danger" data-act="delete" data-path="${esc(n.path)}">Delete</button></span></div>`;
    };
    host.innerHTML = tree.children.map((c) => row(c, 0)).join('');
    const foot = $('studioFooterStats'); const names = Object.keys(files);
    if (foot) foot.textContent = `${names.length} file${names.length === 1 ? '' : 's'} • Entry: ${entry}`;
  }

  /* ---------- popovers ---------- */
  const PANELS = { repoPicker: ['repoPickerPanel', 'repoPickerBtn'], branch: ['branchPanel', 'branchBtn'], commit: ['commitPanel', 'commitBtn'] };
  function closeAll() {
    Object.values(PANELS).forEach(([p, b]) => { const el = $(p); if (el) el.classList.add('hidden'); const bt = $(b); if (bt) bt.setAttribute('aria-expanded', 'false'); });
  }
  async function toggle(name, ev) {
    if (ev) ev.stopPropagation();
    const [pid, bid] = PANELS[name];
    const wasOpen = !$(pid).classList.contains('hidden');
    closeAll();
    if (wasOpen) return;
    if (name === 'repoPicker') { renderPicker(); loadRepos(); setTimeout(() => $('repoSearch') && $('repoSearch').focus(), 20); }
    if (name === 'branch') renderBranches();
    if (name === 'commit') { renderCommits(); refreshCommits().then(renderCommits); }
    $(pid).classList.remove('hidden');
    $(bid).setAttribute('aria-expanded', 'true');
  }

  function init() {
    document.addEventListener('click', (e) => {
      const item = e.target.closest && e.target.closest('.popover-item');
      if (item) {
        if (item.dataset.repo) { const [o, p] = item.dataset.repo.split('\u0001'); closeAll(); open(o, p); }
        else if (item.dataset.branch) selectBranch(item.dataset.branch);
        else if (item.dataset.commit) selectCommit(item.dataset.commit === 'wc' ? 'wc' : Number(item.dataset.commit));
        return;
      }
      const tree = e.target.closest && (e.target.closest('#studioFilesList') || e.target.closest('#studioFileTabs'));
      if (tree) {
        const act = e.target.closest('[data-act]');
        if (act && act.dataset.path) {
          e.stopPropagation();
          const path = act.dataset.path;
          if (act.dataset.act === 'entry') studioSetEntryPoint(path);
          else if (act.dataset.act === 'rename') studioRenameFile(path);
          else if (act.dataset.act === 'delete') studioDeleteFile(path);
          return;
        }
        const dir = e.target.closest('[data-dir]');
        if (dir) { const p = dir.dataset.dir; S.collapsed.has(p) ? S.collapsed.delete(p) : S.collapsed.add(p); renderTree(); return; }
        const f = e.target.closest('[data-file]');
        if (f) { studioSwitchFile(f.dataset.file); return; }
      }
      if (!(e.target.closest && (e.target.closest('.popover') || e.target.closest('.menu-btn')))) closeAll();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closeAll(); closeCommit(); }
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && e.target && e.target.id === 'commitMessage') { e.preventDefault(); confirmCommit(); }
    });
  }
  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);

  window.RepoTree = { buildTree };
  window.RepoUI = {
    state: S, open, loadRepos, selectCommit, selectBranch, newProject, newBranch, openCommit, closeCommit, confirmCommit, commit,
    runSelected, renameProject, deleteProject, renderHeader, renderTree, renderPicker, markEdited, refreshSoon, refreshCommits,
    isDirty, isOpen, toggle, closeAll, capture, changedFiles, confirmDiscard,
  };
})();
