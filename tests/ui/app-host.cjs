// Minimal DOM host for exercising the real application; no browser or network.
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const staticDir = path.join(__dirname, '../../app/miniapp/static');
const tick = () => new Promise(resolve => setImmediate(resolve));
const jsonReply = payload => ({ok:true,json:async () => structuredClone(payload)});
function deferred() {
  let resolve,reject;
  const promise = new Promise((yes,no) => { resolve=yes; reject=no; });
  return {promise,resolve,reject};
}

async function application(access = {role:'reviewer',can_write:false}, preview = false, options = {}) {
  const listeners = new Map();
  const nodes = new Map();
  function node(name) {
    if (!nodes.has(name)) {
      const attrs = {};
      nodes.set(name, {
        innerHTML:'',textContent:'',dataset:{},isConnected:true,value:'',hidden:false,disabled:false,
        hasAttribute:key => Object.hasOwn(attrs,key),
        getAttribute:key => attrs[key] ?? null,
        setAttribute(key,value) { attrs[key]=value; },removeAttribute(key) { delete attrs[key]; },
        addEventListener:(kind,handler) => listeners.set(name+':'+kind,handler),
        querySelector:() => null,querySelectorAll:() => [],replaceChildren() {},append() {},focus() {},
        close() { this.open=false; },showModal() { this.open=true; },
      });
    }
    return nodes.get(name);
  }
  const nav = ['overview','content','work','reports','business'].map(view => {
    const control = node('nav-'+view); control.dataset.view=view; return control;
  });
  const document = {
    body:Object.assign(node('body'),{dataset:{mode:preview?'preview':'live'}}),
    querySelector:node,querySelectorAll:selector => selector==='nav [data-view]' ? nav : [],
    addEventListener:(kind,handler) => listeners.set('document:'+kind,handler),
  };
  const data = {access,businesses:[{id:'one',name:'Компания'}],business:{id:'one',name:'Компания',
    timezone:'Europe/Moscow',profile_version:1,profile_confirmed:true,channel_id:'@example'},
    counts:{},sources:[],posts:[]};
  options.configure?.(data);
  const requests=[];
  function defaultResponder(url) {
    if (url.includes('/posts?')) return jsonReply({items:[],more:false,total:0});
    if (url.endsWith('/history')) return jsonReply({items:[]});
    if (url.endsWith('/control')) return jsonReply({business:data.business,jobs:[],operations:[]});
    if (url.endsWith('/report')) return jsonReply({series:[],detail:''});
    return jsonReply(data);
  }
  let responder = options.respond || defaultResponder;
  const window = {addEventListener() {},scrollTo() {},
    Telegram:{WebApp:{initData:'test-only',ready() {},expand() {},onEvent() {},isVersionAtLeast:() => false}},
    TirazhDemoStore:{read:() => ({data:null}),write:() => true},
    TirazhHTTP:{request:async (url,requestOptions) => { requests.push({url,options:requestOptions}); return responder(url,requestOptions); }},
  };
  const location = {hash:''};
  const context = vm.createContext({window,document,location,history:{replaceState(_state,_unused,hash) { location.hash=hash; }},
    structuredClone,URLSearchParams,URL,crypto:{randomUUID:() => 'test-id'},Option:function(text,value) { return {text,value}; },
    getComputedStyle:() => ({getPropertyValue:() => '#ffffff'}),matchMedia:() => ({matches:true}),
    setTimeout:() => 1,clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync(path.join(staticDir,'access.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(staticDir,'app.js'),'utf8'),context);
  await tick();
  return {context,nodes,listeners,requests,data,nav,defaultResponder,
    setResponder:next => { responder=next; },run:code => vm.runInContext(code,context)};
}
module.exports = {application,jsonReply,deferred,tick};
