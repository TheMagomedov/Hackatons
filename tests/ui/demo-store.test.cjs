const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../app/miniapp/static/demo-store.js'),'utf8');
function setup(mode='preview', options={}) {
 const values=new Map();let reads=0,writes=0;
 const context=vm.createContext({window:{}, document:{body:{dataset:{mode}}},localStorage:{
  getItem:key=>{reads++;if(options.block)throw Error('blocked');return values.get(key)||null;},
  setItem:(key,value)=>{writes++;if(options.block||options.full)throw Error('quota');values.set(key,value);},
  removeItem:key=>{writes++;if(options.block)throw Error('blocked');values.delete(key);},
 }});
 vm.runInContext(source,context);
 return {store:context.window.TirazhDemoStore,values,counts:()=>({reads,writes})};
}
function data() {return {workspaces:[{business:{id:'one',name:'Компания',timezone:'Europe/Moscow',profile_version:1,
 profile_confirmed:true,profile:{description:'Описание',tone:'Спокойно',facts:[{text:'Факт'}]},channel_id:'@channel'},
 sources:[{id:'source',name:'Описание',kind:'text'}],posts:[{id:'post',business_id:'one',text:'Текст',status:'review',revision:1,
 profile_version:1,created_at:'2026-11-13T00:00:00Z',channel_id:'@channel',scheduled_at:null}]}],jobs:[],conversations:[],assets:[],selectedBusiness:'one'};}
test('roundtrip stores only bounded demo fields, drops credential-like extras',()=>{
 const {store}=setup();const input=data();input.initData='secret';input.workspaces[0].business.token='secret';
 assert.equal(store.write(input),true);const result=store.read();assert.equal(result.issue,null);
 assert.equal(result.data.selectedBusiness,'one');assert.equal(result.data.workspaces[0].posts[0].text,'Текст');
 assert.equal(JSON.stringify(result.data).includes('secret'),false);
});
test('connected Mini App never reads, writes or clears local demo storage',()=>{
 const {store,counts}=setup('live');assert.equal(store.read().data,null);assert.equal(store.write(data()),false);
 assert.equal(store.clear(),false);assert.deepEqual(counts(),{reads:0,writes:0});
});
test('malformed or oversized storage falls back without executing injected content',()=>{
 const {store,values}=setup();for(const value of ['{bad',JSON.stringify({version:99}), 'x'.repeat(4_000_001)]) {
  values.set('tirazh.preview.v1',value);assert.equal(store.read().data,null);assert.ok(store.read().issue);
 }
});
test('company ownership and duplicate IDs are validated',()=>{
 const {store}=setup();let input=data();input.workspaces[0].posts[0].business_id='another';assert.equal(store.write(input),false);
 input=data();input.workspaces.push(structuredClone(input.workspaces[0]));assert.equal(store.write(input),false);
 input=data();input.assets=[{id:'a',business_id:'foreign',data_url:'data:image/png;base64,AAAA'}];assert.equal(store.write(input),false);
});
test('stored media accepts only bounded PNG/JPEG data URLs',()=>{
 const {store}=setup();const input=data();input.assets=[{id:'a',business_id:'one',data_url:'https://attacker.invalid/image.png'}];
 assert.equal(store.write(input),false);input.assets[0].data_url='data:image/svg+xml;base64,AAAA';assert.equal(store.write(input),false);
 input.assets[0].data_url='data:image/png;base64,AAAA';assert.equal(store.write(input),true);
});
test('blocked or full storage is reported; reset removes only demo key',()=>{
 assert.equal(setup('preview',{block:true}).store.read().data,null);
 assert.equal(setup('preview',{full:true}).store.write(data()),false);
 const {store,values}=setup();values.set('unrelated','keep');assert.equal(store.write(data()),true);assert.equal(store.clear(),true);
 assert.equal(values.has('tirazh.preview.v1'),false);assert.equal(values.get('unrelated'),'keep');
});
