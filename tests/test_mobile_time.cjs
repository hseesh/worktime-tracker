const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(require('node:path').join(__dirname,'../web/mobile.html'),'utf8');
const script=[...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m=>m[1]).find(s=>s.includes('function mobileSyncStatus'));
let definition,mounted,unmounted,timer,cleared=false,requests=[];
const events={};
const rows=[
 {device_id:'A',date:'2026-11-25',tag:'Work',seconds:3600,updated_at:'2026-11-25T14:41:00Z'},
 {device_id:'B',date:'2026-11-25',tag:'Work',seconds:1800,updated_at:'2026-11-25T22:20:00+08:00'},
 {device_id:'B',date:'2026-11-25',tag:'Indie',seconds:600,updated_at:'2026-11-25T14:30:00Z'},
 {device_id:'B',date:'2026-11-25',tag:'Idle',seconds:900,updated_at:'2026-11-25T15:00:00Z'}];
const context={console,Date,Set,Promise,Number,Math,encodeURIComponent,
 document:{hidden:false,addEventListener:(k,v)=>events[k]=v,removeEventListener:k=>delete events[k]},
 window:{addEventListener:(k,v)=>events[k]=v,removeEventListener:k=>delete events[k]},
 setInterval:(fn,ms)=>{assert.equal(ms,60000);timer=fn;return 1},clearInterval:()=>cleared=true,
 Vue:{ref:value=>({value}),computed:fn=>({get value(){return fn()}}),createApp:d=>{definition=d;return {mount(){}}},onMounted:fn=>mounted=fn,onUnmounted:fn=>unmounted=fn},
 fetch:async url=>{requests.push(url);return {ok:true,json:async()=>url.includes('/tag_time_records_cloud?')?rows:[]}}};
context.self=context;
vm.createContext(context);vm.runInContext(script,context);
assert.equal(context.mobileSyncStatus(rows).syncedDevices,2);
assert.equal(context.mobileSyncStatus(rows).cloudUpdatedAt,'2026-11-25T14:41:00.000Z');
assert.equal(context.mobileSyncStatus([]).cloudUpdatedAt,null);
const ui=definition.setup();mounted();
(async()=>{
 for(let i=0;i<10;i++) await new Promise(setImmediate);
 assert.equal(ui.error.value,'');
 assert.equal(ui.data.value.totalSeconds,6000);
 assert.equal(ui.data.value.workSeconds,5400);
 assert.equal(ui.data.value.indieSeconds,600);
 assert.equal(ui.data.value.syncedDevices,2);
 assert.ok(requests.every(url=>!url.includes('device_id=eq.')));
 const before=requests.length;context.document.hidden=true;timer();assert.equal(requests.length,before);
 context.document.hidden=false;events.visibilitychange();
 for(let i=0;i<10;i++) await new Promise(setImmediate);
 assert.ok(requests.length>before);
 assert.ok(events.pageshow);
 unmounted();assert.ok(cleared);assert.equal(Object.keys(events).length,0);
 console.log('PASS: mobile sums both devices, excludes Idle, reports upload freshness, refreshes on visibility, and cleans up timer');
})().catch(e=>{console.error(e);process.exitCode=1});
