// Run: node tests/file_buffer.test.js   (static/js/common.js; no browser needed)
// The fake editor behaves like Monaco where it matters: setValue() fires the change listeners synchronously,
// and getValue() inside a listener already returns the new text.
const assert = require('assert');
const C = require('../static/js/common.js');

function fakeEditor(initial = '') {
  let value = initial;
  const listeners = [];
  return {
    getValue: () => value,
    setValue(v) { value = v; listeners.forEach((fn) => fn()); },
    type(text) { value += text; listeners.forEach((fn) => fn()); },     // a user keystroke
    onDidChangeModelContent(fn) { listeners.push(fn); },
    getModel: () => null,
  };
}

function setup(files, active = 'main.py') {
  const state = { files: { ...files }, entry_point: 'main.py', active_file: active };
  const editor = fakeEditor();
  let edits = 0;
  const buf = C.createFileBuffer({ editor: () => editor, state: () => state, onEdit: () => { edits++; } });
  editor.onDidChangeModelContent(() => buf.handleChange());
  buf.attach();
  return { state, editor, buf, edits: () => edits };
}

// Deleting the open file mirrors the student page's deleteFile(): capture, delete, forget, load the entry point.
function deleteFile({ state, buf }, name) {
  buf.capture();
  delete state.files[name];
  buf.forget(name);
  if (state.entry_point === name) state.entry_point = C.resolveEntry(state.files, null);
  if (state.active_file === name) buf.load(state.entry_point);
}

/* 1. The reported bug: a text file that sorts after main.py is written, then deleted. */
for (const other of ['zeta.txt', 'notes.txt', 'a_first.txt']) {        // after and before "main.py"
  const t = setup({ 'main.py': 'print("main")\n', [other]: '' });
  assert.ok(t.buf.open(other), 'an EMPTY file must open (old switchToFile refused it)');
  assert.strictEqual(t.editor.getValue(), '');
  t.editor.type('text written into the txt file');
  assert.strictEqual(t.state.files[other], 'text written into the txt file');
  deleteFile(t, other);
  assert.deepStrictEqual(Object.keys(t.state.files), ['main.py'], 'only the deleted file is gone');
  assert.strictEqual(t.state.files['main.py'], 'print("main")\n', `main.py must not be overwritten (${other})`);
  assert.strictEqual(t.editor.getValue(), 'print("main")\n', 'editor shows main.py');
  assert.strictEqual(t.state.active_file, 'main.py');
  t.buf.capture();                                                      // e.g. Save right after deleting
  assert.strictEqual(t.state.files['main.py'], 'print("main")\n');
}

/* 2. The old ordering (set active_file first, then capture) can no longer copy text across files. */
{
  const t = setup({ 'main.py': 'M', 'notes.txt': 'N' });
  t.buf.open('notes.txt');
  t.state.active_file = 'main.py';                                      // what the old code did before switching
  t.buf.capture();
  assert.strictEqual(t.state.files['main.py'], 'M', 'capture writes only into the file the text came from');
  assert.strictEqual(t.state.files['notes.txt'], 'N');
}

/* 3. Switching keeps edits in their own file; our own setValue() calls are not user edits. */
{
  const t = setup({ 'main.py': 'M', 'helper.py': 'H' });
  t.editor.type('1');
  const editsBefore = t.edits();
  t.buf.open('helper.py');
  assert.strictEqual(t.edits(), editsBefore, 'loading a file is not an edit');
  t.editor.type('2');
  t.buf.open('main.py');
  assert.deepStrictEqual(t.state.files, { 'main.py': 'M1', 'helper.py': 'H2' });
}

/* 4. Replacing every file (version switch / project load) never writes the old text into the new set. */
{
  const t = setup({ 'main.py': 'old main', 'notes.txt': 'old notes' });
  t.buf.open('notes.txt');
  t.editor.type('!');
  t.state.files = { 'main.py': 'v2 main', 'notes.txt': 'v2 notes' };     // e.g. "Open in editor" of an older commit
  t.buf.load('main.py');
  assert.deepStrictEqual(t.state.files, { 'main.py': 'v2 main', 'notes.txt': 'v2 notes' });
}

/* 5. Rename keeps the binding; a regenerated open file is shown instead of being overwritten by the stale text. */
{
  const t = setup({ 'main.py': 'M', 'out.txt': 'old' });
  t.buf.open('out.txt');
  t.buf.capture();
  t.state.files['data.txt'] = t.state.files['out.txt']; delete t.state.files['out.txt'];
  t.state.active_file = 'data.txt'; t.buf.rename('out.txt', 'data.txt');
  t.editor.type('+');
  assert.strictEqual(t.state.files['data.txt'], 'old+');
  t.state.files['data.txt'] = 'written by the simulation';
  t.buf.load('data.txt');
  assert.strictEqual(t.editor.getValue(), 'written by the simulation');
  t.buf.open('main.py');
  assert.strictEqual(t.state.files['data.txt'], 'written by the simulation');
}

/* 6. Pure helpers shared with the server rules. */
assert.deepStrictEqual(C.cleanFileName('notes', { defaultExt: '.txt' }), { name: 'notes.txt' });
assert.deepStrictEqual(C.cleanFileName('lib/util', { folders: true, defaultExt: '.py' }), { name: 'lib/util.py' });
assert.deepStrictEqual(C.cleanFileName('lib/util.py'), { name: 'libutil.py' }, 'no folders unless allowed');
assert.ok(C.cleanFileName('readme').error, 'no extension');
assert.ok(C.cleanFileName('evil.exe').error, 'extension not allowed');
assert.ok(C.cleanFileName('.hidden.py').error);
assert.ok(C.cleanFileName('a/b/c/d/e/f.py', { folders: true }).error, 'too deep');
assert.strictEqual(C.resolveEntry({ 'a.txt': '', 'main.py': '' }, 'gone.py'), 'main.py');
assert.strictEqual(C.resolveEntry({ 'a.txt': '', 'b.py': '' }, null), 'b.py');
assert.strictEqual(C.resolveEntry({ 'a.txt': '' }, null), 'a.txt');
assert.deepStrictEqual([...C.changedFiles(C.fileSet({ a: '1', b: '2' }, 'a'), { a: '1', b: '3', c: '' })], ['b', 'c']);
assert.ok(C.sameFileSet(C.fileSet({ a: '1' }, 'a'), C.fileSet({ a: '1' }, 'a')));
assert.ok(!C.sameFileSet(C.fileSet({ a: '1' }, 'a'), C.fileSet({ a: '1', b: '' }, 'a')));
assert.strictEqual(C.esc(`<a href='x'>&"`), '&lt;a href=&#39;x&#39;&gt;&amp;&quot;');

console.log('file buffer tests passed');
