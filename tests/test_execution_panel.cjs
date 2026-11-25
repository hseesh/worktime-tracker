const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const pages = ['web/index.html', 'web/mobile.html'].map(p => fs.readFileSync(path.join(root,p),'utf8'));
function component(html) {
  const start = html.indexOf('// Execution panel:');
  assert.ok(start >= 0);
  return html.slice(start, html.indexOf('\n};', start) + 4);
}
assert.equal(component(pages[0]), component(pages[1]), 'desktop and mobile share the same component');
const context = {};
vm.createContext(context);
vm.runInContext(component(pages[0]) + '\nthis.Panel = ExecutionPanel;', context);
function panel(stats, activity = {}) {
  const definition = context.Panel;
  const instance = {stats, activity, ...definition.data()};
  for (const [k,v] of Object.entries(definition.methods)) instance[k] = v.bind(instance);
  for (const [k,v] of Object.entries(definition.computed)) Object.defineProperty(instance,k,{get:v.bind(instance)});
  return instance;
}
let ui = panel({sources:{codex:{count:8,seconds:1187,missing:0}},tasks:[]});
assert.equal(ui.duration(1187), '19m 47s');
assert.equal(ui.duration(0), '0s');
assert.equal(ui.duration(3601), '1h 0m 1s');
assert.equal(ui.project('D:\\Data\\unity\\P1-c\\Assets'), 'P1-c / Assets');
assert.equal(ui.project('/'), 'No project');
assert.equal(ui.title({title:'Codex session 12345678'}), 'Untitled task');
assert.equal(ui.title({title:'Create a 3D model'}), 'Create a 3D model');
assert.equal(ui.title({title:''}), 'Untitled task');
assert.equal(ui.sources[1].count, 0);
assert.equal(ui.countLabel('codex',1), '1 completed turn');
assert.equal(ui.countLabel('devin',2), '2 model requests');
assert.equal(ui.detail({source:'devin',count:2,missing:1}), '2 model requests · 1 without timing');
ui = panel({sources:{},tasks:Array.from({length:15}, (_,i)=>({source:i%2?'codex':'devin',seconds:i}))});
assert.equal(ui.visibleTasks.length,10);
assert.equal(ui.visibleTasks[0].seconds,14);
ui.filter='codex';
assert.equal(ui.filteredTasks.length,7);
assert.ok(ui.visibleTasks.every(t=>t.source==='codex'));
assert.equal(ui.tabs[0].count,15);
const activity = {projects:[{project:'P1-c/Assets',sessions:2,messages:3,duration:600}],msg_dist:{user:3},tool_kinds:{execute:4},agent_modes:{auto:2},backend_types:{desktop:2}};
const combined = panel({sources:{},tasks:[]},activity);
assert.equal(combined.activityProjects.length,1);
assert.equal(combined.activityGroups.length,4);
assert.equal(combined.hasActivity,1);
combined.filter='devin';
assert.equal(combined.activityProjects[0].sessions,2, 'task filtering does not misrepresent legacy activity scope');
assert.equal(panel({}).hasActivity,0);
ui.filter='no-source';
assert.equal(ui.visibleTasks.length,0);
for (const [i,html] of pages.entries()) {
  for (const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) {
    if (!match[1].trim()) continue;
    const result = spawnSync(process.execPath,['--check','--input-type=module'],{input:match[1],encoding:'utf8'});
    assert.equal(result.status,0,result.stderr);
  }
  assert.ok(html.includes('components: { ExecutionPanel }'));
  assert.ok(!html.includes('0 missing'));
  assert.ok(!html.includes("Today's Tasks"), 'obsolete session-span task heading removed');
  assert.ok(!/v-for="[^"]*(?:devin_activity|activity)\.titles/.test(html), 'obsolete task lists removed');
  assert.ok(html.includes('<execution-panel'), 'execution tasks retained');
  assert.ok(!html.includes('AI Session Activity (Today)'), 'no duplicate activity card');
  assert.ok(html.includes('Activity details') && html.includes('Session span'), 'legacy data merged with distinct timing label');
  assert.ok(html.includes(':activity='), 'activity data passed into merged panel');
}
console.log('PASS: execution panel formatting, filters, top-10, missing data, shared templates and page syntax');
