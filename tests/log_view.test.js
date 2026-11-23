// Run: node tests/log_view.test.js   (static/js/common.js; no browser needed)
// The line model behind every log pane: bounded, chunk-safe, and the three display modes.
const assert = require('assert');
const C = require('../static/js/common.js');

// Chunks that split a line are joined; a trailing newline does not add an empty line.
{
  const m = C.createLogModel();
  m.append('hel'); m.append('lo\nwor'); m.append('ld\n'); m.append('next\n');
  assert.strictEqual(m.all(), 'hello\nworld\nnext');
  assert.strictEqual(m.size(), 3);
}

// Only the newest maxLines are kept, and the pane says how many were dropped.
{
  const m = C.createLogModel({ maxLines: 3 });
  for (let i = 1; i <= 10; i++) m.append(`line ${i}\n`);
  assert.strictEqual(m.size(), 3);
  assert.strictEqual(m.render('all'), '[… 7 older lines not kept]\nline 8\nline 9\nline 10');
}

// A program printing in a loop stays bounded however long it runs.
{
  const m = C.createLogModel({ maxLines: 1000 });
  for (let i = 0; i < 50000; i++) m.append(`tick ${i}\n`);
  assert.strictEqual(m.size(), 1000);
  assert.strictEqual(m.render('last'), 'tick 49999');
}

// "No spam" hides playback steps (except where a running playback is), folds repeats and blank runs.
{
  const m = C.createLogModel();
  m.append('[SIMULATOR] Starting\n\n\n\n');
  m.append('[STEP 1/3] moveJ\n[STEP 2/3] moveJ\n');
  m.append('ciao\nciao\nciao\n[12:00:01] [robot] busy\n[12:00:02] [robot] busy\n');
  m.append('[STEP 3/3] moveL\n');
  assert.strictEqual(m.render('quiet'),
    '[SIMULATOR] Starting\n\nciao  (×3)\n[12:00:02] [robot] busy  (×2)\n[STEP 3/3] moveL');
  assert.strictEqual(m.render('all').split('\n').length, 12);       // the full log still has every line
  m.append('done\n');
  assert.ok(!m.render('quiet').includes('[STEP'));
}

// The admin's "(i/n)" simulator progress lines count as spam too.
{
  const m = C.createLogModel();
  m.append('[SIMULATOR] (1/2) moveJ\n[SIMULATOR] (2/2) moveJ\n[SIMULATOR] 3D simulation completed successfully!\n');
  assert.strictEqual(m.render('quiet'), '[SIMULATOR] 3D simulation completed successfully!');
}

// "Last line" shows the newest non-blank line; filters (the admin's Prints / Errors tabs) apply first.
{
  const tag = (line, explicit) => (explicit === true || explicit === false ? explicit : line.trim() !== '' && !line.startsWith('[SIMULATOR'));
  const m = C.createLogModel({ tag });
  m.append('[SIMULATOR] go\n');
  m.append('x = 1\n');
  m.append('[DISPATCH] sent\n', false);
  m.append('\n\n');
  assert.strictEqual(m.render('last'), '[DISPATCH] sent');
  assert.strictEqual(m.render('last', (l) => l.tag), 'x = 1');
  assert.strictEqual(m.count((l) => l.tag === true), 1);
  assert.strictEqual(m.render('all', (l) => /error/i.test(l.text)), '');
  m.clear();
  assert.strictEqual(m.size(), 0);
  assert.strictEqual(m.render('all'), '');
}

// A line that never ends (print('.', end='') in a loop) is wrapped, so it is bounded like any other output.
{
  const m = C.createLogModel({ maxLines: 10, maxLineChars: 100 });
  for (let i = 0; i < 100000; i++) m.append('.');
  assert.strictEqual(m.size(), 10);
  assert.ok(m.all().length <= 10 * 101);
  m.append('x'.repeat(250) + '\nend\n');
  assert.strictEqual(m.render('last'), 'end');
}

console.log('log_view tests passed');
