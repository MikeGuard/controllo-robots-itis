// Run: node tests/tree.test.js   (pure helper from static/js/repo.js; no browser needed)
const assert = require('assert');
global.window = {}; global.document = { readyState: 'loading', addEventListener() {}, getElementById() { return null; } };
require('../static/js/repo.js');
const { buildTree } = window.RepoTree;
const names = (n) => n.children.map((c) => (c.dir ? c.name + '/' : c.name));

let t = buildTree(['main.py']);
assert.deepStrictEqual(names(t), ['main.py']);                                   // single file

t = buildTree(['main.py', 'helper.py', 'lib/util.py', 'lib/__init__.py', 'lib/sub/deep.py', 'data/cfg.json']);
assert.deepStrictEqual(names(t), ['data/', 'lib/', 'helper.py', 'main.py']);     // folders first, then files
const lib = t.children.find((c) => c.name === 'lib');
assert.deepStrictEqual(names(lib), ['sub/', '__init__.py', 'util.py']);
assert.strictEqual(lib.children[0].children[0].path, 'lib/sub/deep.py');         // full path preserved for open/rename/delete

t = buildTree(['a', 'a/b.py']);                                                  // file and folder sharing a name
assert.strictEqual(t.children.length, 2);
console.log('tree tests passed');
