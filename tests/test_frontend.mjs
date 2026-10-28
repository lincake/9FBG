import { createRequire } from "node:module";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
const require = createRequire(import.meta.url);
const ui = require("../frontend/app.js");
const frame = (id, time = 1e9) => ({ frame_id: id, wavelength_nm: Array.from({length:512},(_,i)=>1510+i*.03), intensity: Array.from({length:512},(_,i)=>100+i), host_rx_monotonic_ns: time, time_quality: "host_receive_only" });

// Identity follows the assigned optical ID, including missing and shuffled results.
const peaks = ui.normalizePeaks([{id:"FBG9",valid:true,center_nm:1540},{id:"FBG2",valid:false,stale:true,age_ms:80},{id:"FBG1",valid:true,center_nm:1515}]);
assert.equal(peaks.length,9); assert.equal(peaks[0].center_nm,1515); assert.equal(peaks[8].center_nm,1540);
assert.equal(peaks[2].id,"FBG3"); assert.equal(peaks[2].valid,false);
assert.equal(ui.normalizePeaks([{valid:true,center_nm:1520}]).filter(p=>p.valid).length,0,"Missing IDs cannot be assigned by list order");

// Old values are explicit and cannot be presented as a current valid point.
assert.equal(ui.quality({valid:true,stale:true,age_ms:80,failure_reason:"fit_failed"}).text,"旧值");
assert.match(ui.quality({valid:false,stale:true,age_ms:80}).reason,/80 ms/);

// Initialization and zeroing need both connection and current unloaded confirmation.
assert.equal(ui.gates({running:true,tracker:{}},false,false,false).reference,false);
assert.equal(ui.gates({running:true,tracker:{}},true,false,false).reference,true);
const pressing = ui.gates({running:true,tracker:{reference_version:1,reference_frozen:true}},true,true,false);
assert.equal(pressing.reference,false); assert.equal(pressing.start,false); assert.equal(pressing.end,true);
assert.equal(ui.gates({running:false,requested_mode:"replay",tracker:{}},true,false,false).connect,false);
assert.equal(ui.captureDisplayActive({experiment:{running:true}}),true);
assert.equal(ui.captureDisplayActive({recording:true}),true);
assert.equal(ui.captureDisplayActive({experiment:{running:false,session_id:"finished-history"},recording:false,session:{closed:false}}),false,"Historical session objects do not imply current acquisition");

// Imports retain frame ID, timestamp and raw samples; no fabricated data or padding.
const imported = ui.parseReplay(JSON.stringify({frames:[frame("real-32")]}));
assert.equal(imported[0].frame_id,"real-32"); assert.equal(imported[0].host_rx_monotonic_ns,1e9); assert.equal(imported[0].intensity[10],110);
assert.equal(ui.parseReplay(JSON.stringify({type:"metadata"})+"\n"+JSON.stringify(frame("real-33"))).length,1);
assert.throws(()=>ui.parseReplay(JSON.stringify({...frame("bad"),intensity:[1,2]})),/512/);
assert.throws(()=>ui.parseReplay(JSON.stringify({...frame("bad"),frame_id:null})),/frame_id/);
assert.throws(()=>ui.parseReplay(JSON.stringify({...frame("bad"),host_rx_monotonic_ns:null})),/采集接收时间/);
assert.throws(()=>ui.parseReplay(""),/文件为空/);
assert.equal(ui.escapeHTML('<img src=x onerror="alert(1)">'),"&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
assert.deepEqual(ui.windowBounds({id:"FBG1",left_nm:1521.2,right_nm:1522.8}),[1521.2,1522.8]);
assert.equal(ui.windowBounds({left_nm:20,right_nm:10}),null);
const methods = ui.comparisonRows({algorithms:{"gaussian-1":{center_definition:"高斯中心",summary:{valid_rate:.98,processing_mean_ms:1.2}}}});
assert.equal(methods[0].valid_rate,.98); assert.equal(methods[0].method,"gaussian-1"); assert.equal(methods[0].processing_mean_ms,1.2);
const deviceInfo=ui.devicePresentation({running:true,devices:[{connected:true,hardware_timestamp_available:false,wavelength_axis:{source:"厂商导出标定轴，SDK不提供实时波长读回",axis_artifact_device_id:"F1520808",uniform:false,step_min_nm:.157,step_max_nm:.185,physical_device_identity_verified:false},sdk_status_raw:{integration:20,sensor_mode:1,interval_ms:40,trigger_frequency_hz:100,effective_integration_us:20.8,timing_settings_readback_verified:false}},{connected:true,hardware_timestamp_available:false,port_identity:{port:"COM4"},firmware:"1.0.3"}],force:{force_values:[0,0,0,0,0,0],preprocessing:{software_tare_applied:false,hardware_zero_command_sent:false}}});
assert.match(deviceInfo.integration,/20.0 μs.*请求/);assert.match(deviceInfo.sensorMode,/HDR 1/);
assert.match(deviceInfo.readInterval,/40.0 ms.*25.00 Hz 请求/);assert.match(deviceInfo.trigger,/100.0 Hz.*触发请求/);
assert.match(deviceInfo.timingReadback,/20.800 μs.*未由 SDK 实时读回验证/);
assert.match(deviceInfo.axisSource,/不提供实时波长读回/);assert.match(deviceInfo.axisIdentity,/尚未验证/);
assert.match(deviceInfo.tare,/未应用软件去皮/);assert.match(deviceInfo.clock,/未提供共同硬件时间戳/);

// The interface has all diagnostic/control targets and no device connect side effect at startup.
const root = fileURLToPath(new URL("../frontend/",import.meta.url));
const html = readFileSync(root+"index.html","utf8");
const releaseVersion = JSON.parse(readFileSync(new URL("./fixtures/history/VERSION.web.json", import.meta.url), "utf8")).version;
const pulseEntry = html.match(/<a\b[^>]*\bid="pulsePageBtn"[^>]*>/)?.[0];
assert.ok(pulseEntry, "The pulse workspace has an explicit navigation entry");
const pulseEntryURL = new URL(pulseEntry.match(/\bhref="([^"]+)"/)[1], "http://127.0.0.1");
assert.equal(pulseEntryURL.pathname, "/pulse.html");
assert.equal(pulseEntryURL.searchParams.get("version"), releaseVersion, "Pulse navigation uses the current delivered version to avoid the cached learning page");
assert.match(pulseEntry, /\btarget="_blank"/);
assert.match(pulseEntry, /\brel="noopener"/);
for(const id of ["spectrumChart","peakTable","forceGrid","timeQuality","integrationUs","sampleRateHz","px6dConnectBtn","px6dDisconnectBtn","forceZeroBtn","forceUnloadedCheck","spectrumRangeApply"]) assert.ok(html.includes(`id="${id}"`),id);
const appSource=readFileSync(root+"app.js","utf8");
for(const id of ["driftChart","driftEmpty","driftLegend","forceChart","torqueChart","forceRangeMode","forceLimit","torqueLimit","forceRangeApply","forceRangeReset","forceRangeStatus","referenceControls","offlineControls","unloadedCheck","referenceBtn","pressStartBtn","pressEndBtn","replayFile","fileSummary","compareBtn","comparePanel","compareResults"]) {
  assert.ok(!html.includes(`id="${id}"`),`${id} is removed from the DOM`);
  assert.ok(!appSource.includes(`"${id}"`),`${id} has no runtime DOM dependency`);
}
for(const identifier of ["class History", "forcePoints", "forceLastKey", "drawForceCharts", "drawDrift"]) assert.ok(!appSource.includes(identifier),`${identifier} is retired with its time chart`);
assert.deepEqual(Array.from(html.matchAll(/<canvas\b[^>]*id="([^"]+)"/g),match=>match[1]),["spectrumChart","expWavelengthChart"],"Raw spectrum and display-only wavelength drift share the main display");
const initialCalibration=html.match(/<section\b[^>]*class="panel initial-calibration-panel"[^>]*>([\s\S]*?)<\/section>/);
assert.ok(initialCalibration,"The first unloaded calibration is a primary card");
assert.ok(html.indexOf('class="page-heading"')<html.indexOf('class="panel initial-calibration-panel"'));
const liveDisplayStart=html.search(/<section\b[^>]*\bid="liveDisplayPanel"/);
assert.ok(liveDisplayStart>=0,"The live spectrum and six-axis values share one display card");
let sectionDepth=0, liveDisplayEnd=-1;
for(const tag of html.slice(liveDisplayStart).matchAll(/<section\b[^>]*>|<\/section>/g)) {
  sectionDepth+=tag[0].startsWith("</")?-1:1;
  if(sectionDepth===0){liveDisplayEnd=liveDisplayStart+tag.index+tag[0].length;break;}
}
assert.ok(liveDisplayEnd>liveDisplayStart,"The live display card closes before later workspaces");
const liveDisplay=html.slice(liveDisplayStart,liveDisplayEnd);
assert.ok(initialCalibration.index+initialCalibration[0].length<liveDisplayStart,"Initial unloaded calibration stays outside and before the live display");
assert.ok(liveDisplayEnd<html.indexOf('id="notice"')&&html.indexOf('id="notice"')<html.indexOf('id="experimentWorkspace"'),"Live display comes before notices and the acquisition workspace");
assert.ok(liveDisplay.includes('class="live-display-columns"'),"Spectrum and six-axis display have a shared column layout");
for(const panel of ["spectrum-panel","force-panel"]) {
  const pattern=new RegExp(`class="[^\"]*\\b${panel}\\b[^\"]*"`,"g");
  assert.equal(Array.from(html.matchAll(pattern)).length,1,`${panel} is moved without duplicating it`);
  assert.ok(liveDisplay.includes(panel),`${panel} belongs to the live display card`);
}
assert.ok(liveDisplay.indexOf("spectrum-panel")<liveDisplay.indexOf("force-panel"),"Optical display precedes six-axis values");
assert.ok(liveDisplay.indexOf("force-panel")<liveDisplay.indexOf('class="metric-grid"'),"All four live metrics sit below the two displays");
for(const id of ["validCount","receiveHz","processingMs","referenceVersion"]) assert.ok(liveDisplay.includes(`id="${id}"`),`${id} remains with the live display`);
assert.ok(liveDisplay.indexOf('class="force-panel"') < liveDisplay.indexOf('class="display-wavelength-panel"'), "The demo drift row sits below the original optical/force row");
assert.ok(liveDisplay.indexOf('class="display-wavelength-panel"') < liveDisplay.indexOf('class="metric-grid"'), "The drift preview is part of the frozen upper display");
assert.ok(!readFileSync(root+"experiment.js","utf8").includes('expWavelengthChart'), "The acquisition workspace no longer owns or feeds the demonstration chart");
const domIds=Array.from(html.matchAll(/\bid="([^"]+)"/g),match=>match[1]);
assert.equal(new Set(domIds).size,domIds.length,"Moving live panels cannot duplicate any DOM identity");
assert.ok(!initialCalibration[1].includes('id="experimentWorkspace"')&&!liveDisplay.includes('id="experimentWorkspace"'),"Calibration, live display and acquisition remain separate workspaces");
for(const id of ["forceZeroBtn","forceUnloadedCheck","forceZeroReset","forceDisplayZeroStatus"]) {
  assert.ok(initialCalibration[1].includes(`id="${id}"`),`${id} is in the initial calibration card`);
  assert.equal(Array.from(html.matchAll(new RegExp(`id="${id}"`,"g"))).length,1,`${id} has no duplicate control`);
}
assert.match(html,/<html lang="zh-CN">/); assert.ok(!html.includes("https://"),"No CDN dependency");
assert.ok(html.indexOf(`src="/display.js?version=${releaseVersion}"`) < html.indexOf(`src="/app.js?version=${releaseVersion}"`),"The pure display module loads before the browser entry point");
assert.ok(html.indexOf(`src="/wavelength_chart.js?version=${releaseVersion}"`) >= 0 && html.indexOf(`src="/wavelength_chart.js?version=${releaseVersion}"`) < html.indexOf(`src="/experiment.js?version=${releaseVersion}"`),"The waveform module is available before the acquisition workspace mounts");
assert.ok(html.indexOf(`src="/preferences.js?version=${releaseVersion}"`) < html.indexOf(`src="/display.js?version=${releaseVersion}"`),"Saved setting bindings are available before both workspaces load");
assert.ok(html.includes('id="settingsStatus"'));
assert.ok(html.indexOf(`src="/gui_priority.js?version=${releaseVersion}"`) >= 0 && html.indexOf(`src="/gui_priority.js?version=${releaseVersion}"`) < html.indexOf(`src="/experiment.js?version=${releaseVersion}"`), "Shared pulse priority is available before either background workspace mounts");
assert.ok(!html.includes('value="replay"'),"Retired replay workflow is absent from the UI");
// Exercise the browser entry point with an inert DOM and API. No instrument is involved.
const elements = new Map();
const listeners = new Map();
const renderCounts = { spectrum: 0, peaks: 0, force: 0 }, windowListeners = new Map(), documentListeners = new Map();
let driftDraws=0, mountedDrift;
const driftContext = new Proxy({clearRect(){driftDraws++;}}, {get(target,key) {return key in target ? target[key] : ()=>{};}});
let spectrumBounds = { width: 1000, height: 284 };
const context2d = new Proxy({clearRect(){renderCounts.spectrum++;}}, {get(target,key) {return key in target ? target[key] : ()=>{};}});
for(const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
  const id = match[1], tag = match[0];
  let value = /\bvalue="([^"]*)"/.exec(tag)?.[1] || (id==="sourceMode"?"hardware":id.endsWith("RangeMode")?"auto":"");
  let disabled = /\bdisabled\b/.test(tag);
  let markup = "";
  elements.set(id,{id,textContent:"",hidden:false,style:{},open:false,dataset:{uiVersion:/\bdata-ui-version="([^"]+)"/.exec(tag)?.[1]},
    get innerHTML(){return markup;},set innerHTML(next){markup=next;if(id==="peakTable")renderCounts.peaks++;if(id==="forceGrid")renderCounts.force++;
      if(id==="expWavelengthLegend") for(const match of next.matchAll(/id="([^"]+)"/g)) elements.set(match[1],{textContent:""});},
    get value(){return value;},set value(next){value=String(next);},checked:/\bchecked\b/.test(tag),
    get disabled(){return disabled;},set disabled(next){disabled=Boolean(next);},
    classList:{toggle(){},add(){}},addEventListener(type,callback){listeners.set(`${id}:${type}`,callback);},
    getBoundingClientRect(){return {width:id==="spectrumChart"?spectrumBounds.width:1000,height:id==="spectrumChart"?spectrumBounds.height:180,left:0,top:0};},
    getContext(){return id==="expWavelengthChart"?driftContext:context2d;},scrollIntoView(){}});
}
const calls=[];
const lowerStates=[];
let priorityFrozen = false;
const priorityListeners = new Set();
const changePriority = frozen => { priorityFrozen = frozen; for (const callback of priorityListeners) callback({ frozen }); };
const priorityController = { isFrozen: () => priorityFrozen, subscribe(callback) { priorityListeners.add(callback); return () => priorityListeners.delete(callback); }, requestResume() { changePriority(false); } };
const preferenceBindings=new Map(), preferenceSaves=[];
let intervalPoll;
let intervalStaleness;
let failStateRequests=false;
let pendingStateRequests=false;
let pendingStateSignal=null;
let resolvePendingState;
let clockNow=Date.now();
let timerID=0;
const timers=new Map();
class TestDate extends Date { static now(){return clockNow;} }
let browserState={product:"9FBG",version:"0.2.0",running:true,mode:"replay",frame:frame("test-fixture"),metrics:{new_frame_hz:null,receive_hz:12,new_frame_rate_reason:"设备未提供可验证的新帧身份"},tracker:{state:"tracking",reference_version:1,display_stale:true,display_age_ms:1500,peaks:[{id:"FBG1",valid:false,stale:true,age_ms:1500,center_nm:1512,failure_reason:"fit_failed"}]},session:{closed:false,counts:{raw_spectra:20}}};
const forceFrame=(id=1,time=1e9)=>({force_values:[60,20,40,10,50,30],axes:["Mz","Fy","Mx","Fx","My","Fz"],units:["N·m","N","N·m","N","N·m","N"],frame_id:id,stream_epoch:"px6d-real-stream",host_rx_monotonic_ns:time,age_ms:0,valid:true});
const posts=()=>calls.filter(call=>call.options?.method==="POST");
const browser = vm.createContext({document:{getElementById(id){assert.ok(elements.has(id),`DOM target ${id}`);return elements.get(id);},querySelector(){return {};},addEventListener(type,callback){documentListeners.set(type,callback);}},
  NineFBGGuiPriority: { shared: () => priorityController },
  NineFBGExperiment:{render(input){lowerStates.push(input == null ? null : structuredClone(input));}},
  NineFBGPreferences:{bind(section,mapping,afterRestore){preferenceBindings.set(section,{mapping,afterRestore});return Promise.resolve();},save(section,patch){preferenceSaves.push({section,patch:JSON.parse(JSON.stringify(patch))});return Promise.resolve();}},
  fetch:async(path,options)=>{
    calls.push({path,options});
    if(path==="/api/state") {
      if(failStateRequests) throw new TypeError("fixture service unavailable");
      if(pendingStateRequests) {
        pendingStateSignal=options.signal;
        return new Promise((resolve,reject)=>{
          resolvePendingState = snapshot => resolve({ ok: true, json: async () => structuredClone(snapshot) });
          options.signal.addEventListener("abort",()=>reject(new Error("fixture request aborted")),{once:true});
        });
      }
      return {ok:true,json:async()=>structuredClone(browserState)};
    }
    assert.equal(options?.method,"POST","Every mutation must be explicit");
    const payload=JSON.parse(options.body);
    if(path==="/api/connect") browserState={...browserState,running:true,mode:payload.mode,recording:payload.record,session:null,force:null,force_connected:false,tracker:{state:"idle",candidates:[],peaks:[]}};
    if(path==="/api/force/connect") browserState={...browserState,force_connected:true,force:forceFrame()};
    if(path==="/api/force/disconnect") browserState={...browserState,force_connected:false,force:null};
    if(path==="/api/reference") browserState={...browserState,tracker:{...browserState.tracker,reference_pending:true}};
    return {ok:true,json:async()=>({ok:true})};
  },
  addEventListener(type,callback){const prior=windowListeners.get(type);windowListeners.set(type,prior ? (...args)=>{prior(...args);callback(...args);} : callback);},
  setTimeout(callback,ms){const id=++timerID;timers.set(id,{callback,ms});return id;},
  clearTimeout(id){timers.delete(id);},
  setInterval(callback,ms){assert.equal(ms,100);if(callback.name==="poll") intervalPoll=callback;else if(callback.name==="refreshStaleness") intervalStaleness=callback;else assert.fail("Unknown background interval");},
  Date:TestDate,AbortController,console,devicePixelRatio:1});
vm.runInContext(readFileSync(root+"display.js","utf8"),browser);
let mountedForceZero;
const OriginalDisplayZero = browser.NineFBGDisplay.DisplayZero;
browser.NineFBGDisplay.DisplayZero = class extends OriginalDisplayZero { constructor() { super(); mountedForceZero = this; } };
vm.runInContext(readFileSync(root+"wavelength_chart.js","utf8"),browser);
const realChartMount=browser.NineFBGWavelengthChart.mount;
browser.NineFBGWavelengthChart.mount=options=>{mountedDrift=realChartMount(options);return mountedDrift;};
vm.runInContext(appSource,browser);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const fire=async(id,type="click",event={})=>{assert.ok(listeners.has(`${id}:${type}`),`${id} has a real ${type} listener`);await listeners.get(`${id}:${type}`)(event);await flush();};
const poll=async()=>{await intervalPoll();await flush();};
const forceCards=()=>Array.from(elements.get("forceGrid").innerHTML.matchAll(/<div class="force-axis[^"]*"><span>([^<]+)<\/span><strong class="force-value">([^<]+)<\/strong><small>([^<]+)<\/small>/g),match=>({axis:match[1],value:match[2],unit:match[3]}));
await flush();
assert.equal(elements.get("version").textContent, releaseVersion);
assert.equal(elements.get("footerVersion").textContent, releaseVersion);
assert.equal(elements.get("serviceVersion").textContent, "服务 0.2.0");
assert.equal(elements.get("serviceVersion").hidden, false, "The running service is disclosed separately from the loaded UI");
browserState = { ...browserState, version: "0.10.2" }; await poll();
assert.equal(elements.get("version").textContent, releaseVersion);
assert.equal(elements.get("footerVersion").textContent, releaseVersion);
assert.equal(elements.get("serviceVersion").textContent, "服务 0.10.2");
assert.equal(elements.get("footerServiceVersion").textContent, " · 服务 0.10.2");
browserState = { ...browserState, version: releaseVersion }; await poll();
assert.equal(elements.get("serviceVersion").hidden, true, "Matching UI and service versions need no duplicate caption");
assert.equal(elements.get("footerServiceVersion").hidden, true);
assert.equal(typeof intervalPoll,"function");
assert.equal(typeof intervalStaleness,"function");
assert.notEqual(intervalPoll,intervalStaleness,"Freshness checking runs independently of an in-flight state request");
assert.ok(calls.every(call=>call.path==="/api/state"&&!call.options?.method),"Page loading only reads state and never connects hardware");
assert.deepEqual(Array.from(preferenceBindings.keys()),["connection","display"]);
assert.deepEqual(Object.keys(preferenceBindings.get("connection").mapping),["source_mode","integration_us","sample_rate_hz"]);
assert.deepEqual(Object.keys(preferenceBindings.get("display").mapping),["show_windows","spectrum_mode","spectrum_x_min","spectrum_x_max","spectrum_y_min","spectrum_y_max"]);
assert.deepEqual(Array.from(preferenceBindings.values()).flatMap(({mapping})=>Object.values(mapping).filter(field=>field.type==="checkbox").map(field=>field.id)),["showWindows"],"Only tracking-window visibility is persisted; unloaded confirmation, display zero and reference state are excluded");
assert.equal(elements.get("newFrameHz").textContent,"未知");
assert.match(elements.get("frameTime").textContent,/旧帧.*1500 ms/);
assert.match(elements.get("peakTable").innerHTML,/旧值/);
assert.equal(elements.get("validCount").textContent,"0 / 9");
assert.equal(elements.get("sessionState").textContent,"连续保存 · 20 帧");
assert.ok(!listeners.has("referenceBtn:click")&&!listeners.has("compareBtn:click"),"Retired actions have no listeners");

// The primary connect button requests only the optical source and display mode.
browserState={...browserState,running:false,force:null,force_connected:false,mode:"hardware"};
await poll();
assert.equal(elements.get("integrationUs").value,"20");
assert.equal(elements.get("sampleRateHz").value,"25");
assert.equal(elements.get("connectBtn").disabled,false);
await fire("connectBtn");
assert.equal(posts().at(-1).path,"/api/connect");
assert.deepEqual(JSON.parse(posts().at(-1).options.body),{mode:"hardware",record:false,connect_force:false,integration_us:20,sample_rate_hz:25});
assert.equal(elements.get("sessionState").textContent,"仅实时显示 · 未记录数据");
assert.equal(elements.get("storageIntegrity").textContent,"显示模式 · 不写采集文件");
assert.equal(elements.get("px6dConnectBtn").disabled,false);
await fire("px6dConnectBtn");
assert.equal(posts().at(-1).path,"/api/force/connect");
assert.deepEqual(JSON.parse(posts().at(-1).options.body),{});
assert.equal(elements.get("px6dConnectBtn").disabled,true);
assert.equal(elements.get("px6dDisconnectBtn").disabled,false);
assert.deepEqual(forceCards(),[
  {axis:"Fx",value:"10.000",unit:"N"},{axis:"Fy",value:"20.000",unit:"N"},{axis:"Fz",value:"30.000",unit:"N"},
  {axis:"Mx",value:"40.00000",unit:"N·m"},{axis:"My",value:"50.00000",unit:"N·m"},{axis:"Mz",value:"60.00000",unit:"N·m"}
],"All six displayed identities and units follow source axes, never list position");
assert.equal(elements.get("forceZeroBtn").disabled,true,"Unloaded confirmation is mandatory");
assert.match(elements.get("syncState").textContent,/六轴实时.*未配对/);

// Any malformed or old force input invalidates every force card and the zero gate.
for(const bad of [
  {...forceFrame(),units:["N","N","N","N","N","N"]},
  {...forceFrame(),axes:["Fx","Fx","Fz","Mx","My","Mz"]},
  {...forceFrame(),force_values:[60,20,40,10,50,Infinity]},
  {...forceFrame(),age_ms:251},
  {...forceFrame(),valid:false}
]) {
  browserState={...browserState,force:bad};await poll();
  assert.equal(forceCards().filter(card=>card.value==="—").length,6,"An invalid six-axis packet must not display partial current values");
  assert.equal(elements.get("forceZeroBtn").disabled,true);
  assert.match(elements.get("syncState").textContent,/六轴无效/);
}
browserState={...browserState,force:forceFrame()};await poll();

// Zeroing explicitly requests the existing optical reference API; six-axis
// offsets stay in browser display state without device, training or file APIs.
elements.get("forceUnloadedCheck").checked=true;await fire("forceUnloadedCheck","change");
assert.equal(elements.get("forceZeroBtn").disabled,false);
const zeroPostStart=posts().length;
const originalForce=JSON.stringify(browserState.force);
await fire("forceZeroBtn");
assert.deepEqual(posts().slice(zeroPostStart).map(call=>call.path),["/api/reference"]);
assert.deepEqual(JSON.parse(posts().at(-1).options.body),{unloaded_confirmed:true});
assert.equal(JSON.stringify(browserState.force),originalForce,"Display zeroing preserves server raw force values");
assert.match(elements.get("forceDisplayZeroStatus").textContent,/1\/12/);
assert.equal(elements.get("forceUnloadedCheck").checked,false);
assert.equal(elements.get("forceZeroBtn").disabled,true,"No second reference can be started while the zero is pending");
await poll();
assert.match(elements.get("forceDisplayZeroStatus").textContent,/1\/12/,"Repeated state polling cannot create zero samples");
for(let index=1;index<12;index++) {
  browserState={...browserState,force:forceFrame(index*20+1,1e9+index*400e6)};
  await poll();
}
assert.match(elements.get("forceDisplayZeroStatus").textContent,/显示零点已生效.*第 1 版/);
assert.ok(forceCards().every(card=>Number(card.value)===0));
assert.match(elements.get("forceGrid").innerHTML,/原始 10\.000 N/);
assert.match(elements.get("forceGrid").innerHTML,/显示零点 10\.000 N/);
assert.deepEqual(posts().slice(zeroPostStart).map(call=>call.path),["/api/reference"],"Collecting zero frames never writes measurements or sends another hardware command");
await fire("forceZeroReset");
assert.equal(forceCards()[0].value,"10.000");
assert.equal(posts().length,zeroPostStart+1,"Reset is also a pure display action");

// A running experiment owns its per-round baseline, including when a fresh
// state changes between the initial click and its final reference request.
elements.get("forceUnloadedCheck").checked=true;
browserState={...browserState,experiment:{running:true}};
const sessionPostStart=posts().length;
await fire("forceZeroBtn");
assert.equal(posts().length,sessionPostStart,"The initial control cannot update references during a point session");
assert.equal(elements.get("forceZeroBtn").disabled,true);
assert.equal(elements.get("forceUnloadedCheck").disabled,true);
assert.equal(elements.get("forceUnloadedCheck").checked,false,"Session start consumes the old initial unloaded confirmation");
assert.match(elements.get("initialCalibrationNote").textContent,/本轮无载标定.*首次标定已禁用/);
assert.match(elements.get("notice").textContent,/本轮无载标定/);
browserState={...browserState,experiment:{running:false}};await poll();
assert.equal(elements.get("forceUnloadedCheck").disabled,false);
assert.equal(elements.get("forceZeroBtn").disabled,true,"Ending a session requires a new explicit confirmation");

// Display range validation cannot change data, processing parameters, or APIs.
const rangePostStart=posts().length;
elements.get("spectrumRangeMode").value="manual";await fire("spectrumRangeMode","change");
elements.get("spectrumXMin").value="1515";elements.get("spectrumXMax").value="1514";
elements.get("spectrumYMin").value="0";elements.get("spectrumYMax").value="1000";
await fire("spectrumRangeApply");assert.match(elements.get("notice").textContent,/波长显示范围.*上限/);
elements.get("spectrumXMin").value="1500";elements.get("spectrumXMax").value="1514";
await fire("spectrumRangeApply");assert.match(elements.get("notice").textContent,/实际标定轴内/);
elements.get("spectrumXMin").value="1511";elements.get("spectrumXMax").value="1514";
const opticalBefore=JSON.stringify(browserState.frame);
await fire("spectrumRangeApply");
assert.match(elements.get("spectrumDisplayStatus").textContent,/手动显示范围.*点在显示范围外/);
assert.equal(JSON.stringify(browserState.frame),opticalBefore);
assert.deepEqual(preferenceSaves.at(-1),{section:"display",patch:{spectrum_mode:"manual",spectrum_x_min:1511,spectrum_x_max:1514,spectrum_y_min:0,spectrum_y_max:1000}});
assert.equal(forceCards()[0].value,"10.000","Removing time charts preserves the actual six-axis numeric values");
assert.equal(posts().length,rangePostStart);

// Independent PX6D disconnect retains the optical stream and never restarts it.
await fire("px6dDisconnectBtn");
assert.equal(posts().at(-1).path,"/api/force/disconnect");
assert.equal(browserState.running,true);
assert.equal(forceCards().filter(card=>card.value==="—").length,6);
assert.equal(elements.get("px6dConnectBtn").disabled,false);
await fire("px6dConnectBtn");

// A slow state API must not leave fresh-looking values on screen. Exercise
// the watchdog while fetch is unresolved, before invoking its abort deadline.
browserState={...browserState,force:forceFrame(301,7e9),tracker:{state:"tracking",reference_version:1,display_stale:false,display_age_ms:0,peaks:Array.from({length:9},(_,index)=>({id:`FBG${index+1}`,valid:true,stale:false,center_nm:1511+index,drift_nm:0}))}};
await poll();
elements.get("forceUnloadedCheck").checked=true;await fire("forceUnloadedCheck","change");
assert.equal(elements.get("forceZeroBtn").disabled,false);
assert.equal(elements.get("validCount").textContent,"9 / 9");
const hungRequestPostCount=posts().length;
pendingStateRequests=true;
const hungPoll=intervalPoll();await flush();
assert.ok(pendingStateSignal,"The state request carries its own AbortController signal");
assert.equal(pendingStateSignal.aborted,false);
clockNow+=251;intervalStaleness();
assert.equal(forceCards().filter(card=>card.value==="—").length,6,"The independent watchdog expires six-axis values while fetch is pending");
assert.equal(elements.get("forceZeroBtn").disabled,true);
assert.equal(elements.get("validCount").textContent,"9 / 9","The optical frame has its separate one-second freshness threshold");
clockNow+=750;intervalStaleness();
assert.equal(elements.get("validCount").textContent,"0 / 9");
assert.match(elements.get("frameTime").textContent,/旧帧.*超过 1 秒/);
assert.equal(elements.get("serverStatus").textContent,"本机服务已连接","Freshness expires before fetch succeeds or fails");
assert.equal(pendingStateSignal.aborted,false);
const deadline=Array.from(timers.values()).find(timer=>timer.ms===1500);
assert.ok(deadline,"State fetch has a 1500 ms abort deadline");
clockNow+=499;deadline.callback();await hungPoll;await flush();
assert.equal(pendingStateSignal.aborted,true);
assert.equal(elements.get("serverStatus").textContent,"本机服务连接中断");
assert.equal(posts().length,hungRequestPostCount,"Timeout and watchdog do not send device operations");
pendingStateRequests=false;await poll();
assert.equal(elements.get("serverStatus").textContent,"本机服务已连接","Polling can recover after an aborted request");

// A service interruption makes all force and optical results invalid locally;
// the page must never auto-connect or preserve an enabled calibration control.
elements.get("forceUnloadedCheck").checked=true;await fire("forceUnloadedCheck","change");
const failurePostCount=posts().length;
failStateRequests=true;await poll();
assert.equal(elements.get("serverStatus").textContent,"本机服务连接中断");
assert.equal(elements.get("validCount").textContent,"0 / 9");
assert.equal(forceCards().filter(card=>card.value==="—").length,6);
assert.equal(elements.get("forceZeroBtn").disabled,true);
assert.match(elements.get("frameTime").textContent,/旧帧.*服务连接中断/);
assert.match(elements.get("notice").textContent,/不能作为当前有效测量/);
assert.equal(posts().length,failurePostCount,"A failed state request cannot reconnect devices");
assert.ok(posts().every(call=>["/api/connect","/api/force/connect","/api/force/disconnect","/api/reference"].includes(call.path)),"This display workflow contains no hardware zero, training or file API");

// Restoring settings only changes controls. A saved manual range is held until
// an actual 512-point calibration axis can validate it; bad combinations fall
// back to the full spectrum without starting devices or requesting a reference.
const restorePreference=(section,values)=>{
  const {mapping,afterRestore}=preferenceBindings.get(section);
  for(const [key,value] of Object.entries(values)) {
    const target=elements.get(mapping[key].id);
    if(mapping[key].type==="checkbox") target.checked=value===true;
    else target.value=value==null?"":String(value);
  }
  afterRestore(values);
};
failStateRequests=false;
browserState={...browserState,running:false,frame:null,force:null,tracker:{},experiment:{running:false}};await poll();
const restorePosts=posts().length, restoreSaves=preferenceSaves.length;
restorePreference("connection",{source_mode:"replay",integration_us:40,sample_rate_hz:50});
assert.equal(elements.get("integrationUs").value,"40");assert.equal(elements.get("sampleRateHz").value,"50");
assert.equal(elements.get("sourceMode").value,"hardware","Historical replay preference falls back to the supported actual device UI");
assert.equal(elements.get("connectBtn").disabled,false,"Restored connection settings still require an explicit connect action");
restorePreference("display",{show_windows:false,spectrum_mode:"manual",spectrum_x_min:1511,spectrum_x_max:1514,spectrum_y_min:0,spectrum_y_max:1000});
assert.equal(elements.get("showWindows").checked,false,"Tracking-window visibility is restored as a display preference");
assert.match(elements.get("spectrumDisplayStatus").textContent,/等待实际标定轴核验/);
browserState={...browserState,running:true,mode:"hardware",frame:frame("restore-real-axis"),force:forceFrame(401),force_connected:true,tracker:{display_stale:false}};await poll();
assert.match(elements.get("spectrumDisplayStatus").textContent,/手动显示范围/);
assert.equal(elements.get("forceUnloadedCheck").checked,false);
assert.equal(elements.get("forceZeroBtn").disabled,true,"Restored settings never authorize unloaded calibration");
restorePreference("display",{spectrum_mode:"manual",spectrum_x_min:1490,spectrum_x_max:1600,spectrum_y_min:0,spectrum_y_max:1000});
assert.match(elements.get("notice").textContent,/当前标定轴不符.*完整光谱/);
assert.match(elements.get("spectrumDisplayStatus").textContent,/完整波长轴 \/ 自动强度/);
restorePreference("display",{spectrum_mode:"manual",spectrum_x_min:1514,spectrum_x_max:1511,spectrum_y_min:0,spectrum_y_max:1000});
assert.match(elements.get("notice").textContent,/已保存的光谱范围无效.*完整光谱/);
assert.match(elements.get("spectrumDisplayStatus").textContent,/完整波长轴 \/ 自动强度/);
assert.equal(posts().length,restorePosts,"Setting restoration never performs a device or reference operation");
assert.equal(preferenceSaves.length,restoreSaves,"Restoration and automatic axis readback never write preferences");
await fire("spectrumRangeReset");
assert.equal(preferenceSaves.at(-1).section,"display");
assert.equal(preferenceSaves.at(-1).patch.spectrum_mode,"auto","The full-spectrum reset persists automatic mode");
assert.equal(posts().length,restorePosts);
// Repeated snapshots of one optical frame do not redraw its canvas or peak
// table. Source, view, reference and freshness changes remain independently live.
assert.ok(html.includes(`id="version" data-ui-version="${releaseVersion}">${releaseVersion}<`));
const stableFrame = { ...frame("render-once", 9e9), stream_epoch: "render-stream" };
const stablePeaks = Array.from({length:9},(_,index)=>({id:`FBG${index+1}`,valid:true,stale:false,center_nm:1511+index,drift_nm:0,amplitude:1,background:100,fit_x:[1511+index,1511.01+index],fit_y:[100,101]}));
elements.get("showWindows").checked=true;
browserState={...browserState,version:"0.13.5",running:true,mode:"hardware",frame:stableFrame,force:forceFrame(501),force_connected:true,tracker:{state:"tracking",stream_epoch:"render-stream",reference_version:1,display_stale:false,display_age_ms:0,peaks:stablePeaks,windows:[{id:"FBG1",low_nm:1510,high_nm:1512}]}};
await poll();
let painted={...renderCounts};
await poll();
assert.equal(renderCounts.spectrum,painted.spectrum,"The identical received frame is not redrawn at every 100 ms poll");
assert.equal(renderCounts.peaks,painted.peaks,"The identical nine-peak table is not replaced at every poll");
assert.ok(renderCounts.force>painted.force,"Six-axis display continues to process every state update");
browserState={...browserState,force:{...forceFrame(502),force_values:[60,20,40,10,50,31]}};await poll();
assert.equal(forceCards()[2].value,"31.000");
assert.equal(renderCounts.spectrum,painted.spectrum,"A force-only update cannot repaint an unchanged optical frame");
assert.equal(renderCounts.peaks,painted.peaks);
browserState={...browserState,frame:{...browserState.frame,frame_id:"render-next"}};await poll();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"A new source frame ID repaints");
assert.equal(renderCounts.peaks,painted.peaks+1);
painted={...renderCounts};
browserState={...browserState,frame:{...browserState.frame,stream_epoch:"reconnected-stream"}};await poll();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Reconnection cannot reuse an earlier stream's render key");
assert.equal(renderCounts.peaks,painted.peaks+1);
painted={...renderCounts};
browserState={...browserState,tracker:{...browserState.tracker,reference_version:2}};await poll();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"A reference update repaints even before the next raw frame");
assert.equal(renderCounts.peaks,painted.peaks+1);
painted={...renderCounts};
browserState={...browserState,tracker:{...browserState.tracker,windows:[{id:"FBG1",low_nm:1510.5,high_nm:1512.5}]}};await poll();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Changed tracking-window bounds repaint the same frame");
painted={...renderCounts};
elements.get("showWindows").checked=false;await fire("showWindows","change");
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Window visibility changes repaint immediately");
await poll();assert.equal(renderCounts.spectrum,painted.spectrum+1,"The next poll does not repeat an event-triggered repaint");
painted={...renderCounts};
elements.get("spectrumRangeMode").value="manual";
elements.get("spectrumXMin").value="1511";elements.get("spectrumXMax").value="1514";
elements.get("spectrumYMin").value="0";elements.get("spectrumYMax").value="1000";
await fire("spectrumRangeApply");
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Manual display range application repaints the same frame");
assert.match(elements.get("spectrumDisplayStatus").textContent,/手动显示范围/);
await poll();assert.equal(renderCounts.spectrum,painted.spectrum+1);
painted={...renderCounts};
await fire("spectrumRangeReset");
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Restoring the full calibrated axis repaints immediately");
painted={...renderCounts};
spectrumBounds={width:1200,height:300};windowListeners.get("resize")();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Resizing repaints the cached frame with new geometry");
windowListeners.get("resize")();assert.equal(renderCounts.spectrum,painted.spectrum+1,"A duplicate resize notification does not clear the canvas again");
painted={...renderCounts};
browser.devicePixelRatio=2;windowListeners.get("resize")();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Display pixel-ratio changes update the canvas");
painted={...renderCounts};
clockNow+=1001;intervalStaleness();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"The independent watchdog repaints stale spectrum immediately without a new source ID");
assert.equal(renderCounts.peaks,painted.peaks+1,"The watchdog cannot leave an unchanged-frame peak table green");
assert.equal(elements.get("validCount").textContent,"0 / 9");
assert.match(elements.get("spectrumDisplayStatus").textContent,/旧帧/);
painted={...renderCounts};
await poll();
assert.equal(renderCounts.spectrum,painted.spectrum+1,"Restored fresh state repaints even when the source ID is unchanged");
assert.equal(renderCounts.peaks,painted.peaks+1);
assert.equal(elements.get("validCount").textContent,"9 / 9");
assert.deepEqual(browserState.frame, {...stableFrame,frame_id:"render-next",stream_epoch:"reconnected-stream"},"Rendering optimization preserves the received 512-point spectrum");

// Run an actual mounted capture session against inert API packets. The same
// production event handlers, polling loop and watchdog run without hardware.
browserState={...browserState,recording:false,experiment:{running:false,session_id:"finished-history"},session:{closed:true},force:forceFrame(601,12e9),tracker:{...browserState.tracker,reference_version:2,display_stale:false,display_age_ms:0}};
await poll();
assert.equal(elements.get("captureDisplayNotice").hidden,true,"A completed retained session stays live");
elements.get("forceUnloadedCheck").checked=true;await fire("forceUnloadedCheck","change");
await fire("forceZeroBtn");
for(let index=1;index<12;index++) {
  browserState={...browserState,force:forceFrame(601+index,12e9+index*400e6)};await poll();
}
assert.match(elements.get("forceDisplayZeroStatus").textContent,/显示零点已生效/);
assert.ok(forceCards().every(card=>Number(card.value)===0));
browserState={...browserState,frame:{...stableFrame,frame_id:"demo-before-capture",host_rx_monotonic_ns:19e9},tracker:{...browserState.tracker,reference_pending:false,frame_id:"demo-before-capture",host_rx_monotonic_ns:19e9,stream_epoch:stableFrame.stream_epoch,reference_nm:Array.from({length:9},(_,index)=>1511+index),config_version:"pre-capture-reference",algorithm_version:"raw-local-fit",peaks:stablePeaks.map(peak=>({...peak,value_frame_id:"demo-before-capture",drift_nm:.001}))}};await poll();
assert.equal(mountedDrift.history.points.length,1,"A valid idle source frame reaches the real mounted demonstration buffer");
assert.equal(elements.get("expWavelengthValue1").textContent,"1.00 pm");
browserState={...browserState,frame:{...stableFrame,frame_id:"capture-entry"},experiment:{running:true,session_id:"mock-capture",path:"fixture-only",counts:{raw_spectra:1},writer_queue_depth:0},metrics:{receive_hz:25,processing_ms:2,queue_depth:0},tracker:{...browserState.tracker,reference_pending:false}};
elements.get("spectrumTooltip").hidden=false;
await poll();
assert.equal(elements.get("captureDisplayNotice").hidden,false);
assert.equal(elements.get("peakDisplayStatus").hidden,false);
assert.match(elements.get("captureDisplaySnapshot").textContent,/capture-entry.*第 2 版/);
assert.equal(elements.get("frameLabel").textContent,"冻结原始帧");
assert.match(elements.get("frameTime").textContent,/冻结快照/);
assert.match(elements.get("runningBadge").innerHTML,/上方显示已冻结/);
assert.equal(elements.get("syncState").textContent,"六轴显示已冻结");
assert.equal(elements.get("spectrumTooltip").hidden,true,"A tooltip from the live frame is removed when freezing starts");
assert.equal(elements.get("spectrumRangeApply").disabled,true);
assert.equal(elements.get("spectrumRangeReset").disabled,true);
assert.equal(elements.get("showWindows").disabled,true);
const captureDriftDraws=driftDraws, captureDriftHistory=JSON.stringify(mountedDrift.history), captureDriftLegend=elements.get("expWavelengthValue1").textContent;
assert.equal(mountedDrift.frozen,true);assert.equal(elements.get("expWavelengthWindow").disabled,true);
assert.match(elements.get("expWavelengthStatus").textContent,/采集期间显示已冻结/);
const capturePaints={...renderCounts},captureLowerStart=lowerStates.length,capturePosts=posts().length;
const held={force:elements.get("forceGrid").innerHTML,peaks:elements.get("peakTable").innerHTML,frame:elements.get("frameId").textContent,rate:elements.get("receiveHz").textContent,processing:elements.get("processingMs").textContent,reference:elements.get("referenceVersion").textContent};
for(let index=1;index<=20;index++) {
  browserState={...browserState,frame:{...stableFrame,frame_id:`capture-${index}`,host_rx_monotonic_ns:20e9+index*40e6,intensity:stableFrame.intensity.map(value=>value+index)},force:{...forceFrame(700+index,20e9+index*40e6),force_values:[60,20,40,10+index,50,30]},tracker:{...browserState.tracker,frame_id:`capture-${index}`,host_rx_monotonic_ns:20e9+index*40e6,stream_epoch:stableFrame.stream_epoch,reference_version:3,config_version:"fixture-current-config",reference_history:[{version:3,reason:"无载参考更新",frame_id:`capture-${index}`}],peaks:stablePeaks.map(peak=>({...peak,value_frame_id:`capture-${index}`,drift_nm:index*.001}))},metrics:{receive_hz:25+index,processing_ms:index,queue_depth:index},experiment:{...browserState.experiment,counts:{raw_spectra:index+1},writer_queue_depth:index}};
  await poll();
}
assert.deepEqual(renderCounts,capturePaints,"20 distinct capture frames cause zero spectrum clears, peak table rewrites or force grid rewrites after the entry snapshot");
assert.equal(lowerStates.length-captureLowerStart,20,"All 20 new states reach the lower capture workspace while the demo chart is paused");
assert.equal(driftDraws,captureDriftDraws,"20 source states cannot redraw the frozen drift canvas");
assert.equal(JSON.stringify(mountedDrift.history),captureDriftHistory,"Frozen drift keeps no growing history, freshness invalidation or reference rewrites");
assert.equal(elements.get("expWavelengthValue1").textContent,captureDriftLegend,"Frozen drift legend holds its labeled snapshot");
assert.equal(lowerStates.at(-1).frame.frame_id,"capture-20");
assert.equal(lowerStates.at(-1).force.force_values[3],30);
assert.equal(elements.get("forceGrid").innerHTML,held.force);
assert.equal(elements.get("peakTable").innerHTML,held.peaks);
assert.equal(elements.get("frameId").textContent,held.frame);
assert.equal(elements.get("receiveHz").textContent,held.rate);
assert.equal(elements.get("processingMs").textContent,held.processing);
assert.equal(elements.get("referenceVersion").textContent,held.reference,"Frozen reference identity cannot silently change below the captured spectrum");
assert.equal(elements.get("configVersion").textContent,"fixture-current-config","Lower reference diagnostics remain current even while the top card holds its historical reference");
assert.match(elements.get("referenceHistory").innerHTML,/第 3 版.*capture-20/);
assert.match(elements.get("sessionState").textContent,/21 个原始光谱/);
assert.match(elements.get("pipelineQueues").textContent,/20 \/ 20 帧/);
assert.match(elements.get("storageIntegrity").textContent,/正在独立记录/);
assert.match(elements.get("captureDisplayHealth").textContent,/接收有效/);
assert.equal(posts().length,capturePosts,"Display freezing never sends an acquisition, device, training or file command");

// Every route to the spectrum painter obeys the same display hold. Mouse
// hover cannot combine the latest raw samples with old canvas geometry.
spectrumBounds={width:1100,height:270};windowListeners.get("resize")();
browser.devicePixelRatio=3;windowListeners.get("resize")();
browser.document.hidden=true;documentListeners.get("visibilitychange")();
browser.document.hidden=false;documentListeners.get("visibilitychange")();
elements.get("expWavelengthWindow").value="120";await fire("expWavelengthWindow","change");
assert.equal(driftDraws,captureDriftDraws,"Resize, DPR, hidden recovery and window controls cannot redraw the frozen drift chart");
assert.equal(JSON.stringify(mountedDrift.history),captureDriftHistory);
elements.get("showWindows").checked=false;await fire("showWindows","change");
await fire("spectrumRangeApply");await fire("spectrumRangeReset");
restorePreference("display",{spectrum_mode:"manual",spectrum_x_min:1511,spectrum_x_max:1514,spectrum_y_min:0,spectrum_y_max:1000});
await fire("forceZeroReset");
await fire("spectrumChart","mousemove",{currentTarget:elements.get("spectrumChart"),clientX:150,clientY:80});
assert.deepEqual(renderCounts,capturePaints,"Resize, visibility, ranges, preference restoration and zero reset cannot bypass the capture hold");
assert.equal(elements.get("spectrumTooltip").hidden,true);
assert.equal(posts().length,capturePosts);

// DisplayZero still sees fresh identities while painting is suspended, so
// reconnection cannot apply an old stream's six-axis offsets on resumption.
browserState={...browserState,force:{...forceFrame(801,30e9),stream_epoch:"new-force-stream"}};await poll();
assert.match(elements.get("forceDisplayZeroStatus").textContent,/上次显示零点已失效/);
assert.deepEqual(renderCounts,capturePaints);
pendingStateRequests=true;
const hungCapturePoll=intervalPoll();await flush();
assert.ok(pendingStateSignal&&!pendingStateSignal.aborted);
clockNow+=251;intervalStaleness();
assert.match(elements.get("captureDisplayHealth").textContent,/PX6D.*超过 250 ms 未更新/);
assert.deepEqual(renderCounts,capturePaints,"Force freshness expires without repainting frozen numbers");
clockNow+=750;intervalStaleness();
assert.match(elements.get("captureDisplayHealth").textContent,/光谱数据已过期/);
assert.deepEqual(renderCounts,capturePaints,"The optical watchdog updates the live warning without clearing the frozen canvas or peak table");
assert.equal(elements.get("validCount").textContent,"9 / 9","Frozen validity count is a clearly labeled snapshot, not a live status");
const captureDeadline=Array.from(timers.values()).find(timer=>timer.ms===1500);
assert.ok(captureDeadline);captureDeadline.callback();await hungCapturePoll;await flush();
assert.match(elements.get("captureDisplayHealth").textContent,/服务连接中断.*无法确认/);
assert.equal(lowerStates.at(-1),null,"An unresolved state request invalidates the lower workspace at its abort deadline");
pendingStateRequests=false;
failStateRequests=true;await poll();
assert.equal(lowerStates.at(-1),null,"Service loss immediately invalidates the lower workspace instead of holding live-looking values");
assert.match(elements.get("captureDisplayHealth").textContent,/服务连接中断.*无法确认/);
assert.deepEqual(renderCounts,capturePaints,"A network failure cannot bypass the numeric hold");
assert.equal(driftDraws,captureDriftDraws,"Staleness and network invalidation do not repaint the demonstration chart during capture");
assert.equal(JSON.stringify(mountedDrift.history),captureDriftHistory);
assert.equal(elements.get("forceZeroBtn").disabled,true);
failStateRequests=false;
browserState={...browserState,experiment:{...browserState.experiment,error:"fixture writer failure"}};await poll();
assert.match(elements.get("captureDisplayHealth").textContent,/fixture writer failure/);
assert.equal(elements.get("storageIntegrity").textContent,"fixture writer failure","Writer errors remain live during freezing");
browserState={...browserState,running:false,force_connected:false,experiment:{...browserState.experiment,error:null}};await poll();
assert.match(elements.get("captureDisplayHealth").textContent,/光谱设备未连接/);
assert.equal(elements.get("disconnectBtn").disabled,true,"Device controls still reflect the actual connection");
assert.deepEqual(renderCounts,capturePaints);

// Completing or aborting the authoritative capture state resumes the latest
// packet immediately, despite the retained session identity/history.
browserState={...browserState,running:true,force_connected:true,experiment:{running:false,session_id:"mock-capture",phase:"stopped"},frame:{...stableFrame,frame_id:"capture-latest-at-stop",host_rx_monotonic_ns:31e9},force:{...forceFrame(802,30.04e9),stream_epoch:"new-force-stream",force_values:[60,20,40,14,50,30]},tracker:{...browserState.tracker,frame_id:"capture-latest-at-stop",host_rx_monotonic_ns:31e9,display_stale:false,display_age_ms:0,peaks:stablePeaks.map(peak=>({...peak,value_frame_id:"capture-latest-at-stop",drift_nm:.002}))},metrics:{receive_hz:55,processing_ms:8}};await poll();
assert.equal(elements.get("captureDisplayNotice").hidden,true);
assert.equal(elements.get("peakDisplayStatus").hidden,true);
assert.equal(elements.get("frameLabel").textContent,"当前原始帧");
assert.equal(elements.get("frameId").textContent,"capture-latest-at-stop");
assert.equal(mountedDrift.frozen,false);assert.equal(elements.get("expWavelengthWindow").disabled,false);
assert.ok(driftDraws>captureDriftDraws,"Stopping the authoritative session immediately resumes the drift painter");
assert.equal(elements.get("expWavelengthWindow").value,"60","Programmatic window changes during the freeze cannot alter the held window");
assert.equal(mountedDrift.history.points.length,1,"The preview discards pre-capture history and resumes using only the latest real source frame");
assert.equal(mountedDrift.history.points[0].t,31);assert.ok(mountedDrift.history.points[0].starts.every(Boolean));
assert.equal(elements.get("expWavelengthValue1").textContent,"2.00 pm");
assert.match(elements.get("expWavelengthStatus").textContent,/九峰参考 第 3 版.*九路有效/);
assert.equal(elements.get("receiveHz").textContent,"55.0");
assert.equal(elements.get("referenceVersion").textContent,"第 3 版");
assert.equal(forceCards()[0].value,"14.000","Old force offsets invalidated during the hold cannot reappear after it");
assert.equal(renderCounts.spectrum,capturePaints.spectrum+1);
assert.equal(renderCounts.peaks,capturePaints.peaks+1);
assert.equal(renderCounts.force,capturePaints.force+1);
assert.equal(elements.get("showWindows").disabled,false);
assert.equal(elements.get("spectrumRangeReset").disabled,false);
assert.equal(posts().length,capturePosts);

browserState={...browserState,recording:true};await poll();
assert.equal(elements.get("captureDisplayNotice").hidden,false,"An explicitly active legacy raw recording also freezes the ordinary display");
const legacyPaints={...renderCounts};
browserState={...browserState,frame:{...browserState.frame,frame_id:"legacy-next"}};await poll();
assert.deepEqual(renderCounts,legacyPaints);
browserState={...browserState,recording:false};await poll();
assert.equal(elements.get("captureDisplayNotice").hidden,true);
assert.equal(elements.get("frameId").textContent,"legacy-next");
browserState={...browserState,experiment:{running:true,session_id:"same-frame-session"}};await poll();
const sameFramePaints={...renderCounts};
browserState={...browserState,experiment:{running:false,session_id:"same-frame-session",phase:"aborted"}};await poll();
assert.equal(renderCounts.spectrum,sameFramePaints.spectrum+1,"Abort resumes painting even if the latest raw frame identity matches the frozen entry");
assert.equal(renderCounts.peaks,sameFramePaints.peaks+1);
assert.equal(elements.get("captureDisplayNotice").hidden,true);

// Pulse priority pauses the whole background page, including state reads and
// independent freshness/render work. It has no hardware command side effects.
const priorityPostCount = posts().length, priorityCallCount = calls.length;
const priorityPaints = { ...renderCounts }, priorityLowerCount = lowerStates.length;
const priorityFrame = elements.get("frameId").textContent;
// A completed local display zero remains valid under display suspension.
const normalizeZeroFrame = (id, stamp) => browser.NineFBGDisplay.normalizeForce(forceFrame(id,stamp), { running: true, connected: true });
mountedForceZero.request(true, normalizeZeroFrame(1000,39e9));
for (let index = 1; index < 12; index++) mountedForceZero.update(normalizeZeroFrame(1000+index,39e9+index*40e6));
assert.equal(mountedForceZero.snapshot().active, true);
const completedZero = mountedForceZero.snapshot();
elements.get("forceUnloadedCheck").checked = true;
changePriority(true);
assert.equal(elements.get("forceUnloadedCheck").checked, false, "Unloaded confirmation cannot persist across an observation pause");
assert.deepEqual(mountedForceZero.snapshot(), completedZero, "Priority keeps a completed display zero and its version");
assert.equal(elements.get("guiPriorityNotice").hidden, false);
assert.match(elements.get("guiPriorityMessage").textContent, /脉搏曲线优先.*快照.*后台采集、保存与训练继续/);
assert.equal(elements.get("disconnectBtn").disabled, true);
assert.equal(elements.get("forceZeroBtn").disabled, true);
assert.equal(mountedDrift.frozen, true);
browserState = { ...browserState, frame: { ...browserState.frame, frame_id: "pulse-priority-next", host_rx_monotonic_ns: 40e9 }, force: forceFrame(900,40e9) };
clockNow += 5000;
for (let index = 0; index < 20; index++) { await intervalPoll(); intervalStaleness(); }
windowListeners.get("resize")();
await fire("disconnectBtn");
assert.equal(calls.length, priorityCallCount, "Frozen GUI sends no state reads or forced hardware operations");
assert.deepEqual(renderCounts, priorityPaints, "Spectrum, peaks and six-axis display do no background repaint");
assert.equal(lowerStates.length, priorityLowerCount, "Experiment receives no state updates during pulse priority");
assert.equal(elements.get("frameId").textContent, priorityFrame);
assert.match(elements.get("serverStatus").textContent, /本页状态刷新已暂停/, "Intentional suspension cannot masquerade as a service failure");
const resumePromise = fire("guiPriorityResume");
assert.equal(elements.get("disconnectBtn").disabled, true, "Lease release keeps controls gated until the new state response");
assert.match(elements.get("guiPriorityMessage").textContent, /等待最新设备状态/);
await resumePromise;
assert.equal(elements.get("guiPriorityNotice").hidden, true);
assert.equal(elements.get("frameId").textContent, "pulse-priority-next");
assert.equal(mountedDrift.frozen, false);
assert.equal(elements.get("disconnectBtn").disabled, false);
assert.equal(posts().length, priorityPostCount, "Resuming only obtains state; it never reconnects devices or changes references");

// An already pending state request cannot paint after pulse priority begins.
pendingStateRequests = true;
const latePriorityPoll = intervalPoll(); await flush();
const pendingPriorityPaints = { ...renderCounts }, pendingPriorityLower = lowerStates.length;
mountedForceZero.request(true, normalizeZeroFrame(1200,42e9));
assert.equal(mountedForceZero.snapshot().pending, true);
elements.get("forceUnloadedCheck").checked = true;
changePriority(true);
assert.equal(mountedForceZero.snapshot().pending, false, "Unfinished display zero cannot mix pre-pause no-load samples with later hand contact");
assert.equal(elements.get("forceUnloadedCheck").checked, false);
Array.from(timers.values()).find(timer => timer.ms === 1500).callback();
await latePriorityPoll; await flush();
assert.deepEqual(renderCounts, pendingPriorityPaints);
assert.equal(lowerStates.length, pendingPriorityLower);
assert.match(elements.get("serverStatus").textContent, /本页状态刷新已暂停/);
pendingStateRequests = false;
changePriority(false); await flush();
assert.equal(elements.get("guiPriorityNotice").hidden, true, "Automatic lease release follows the same fresh-state resume path");
assert.equal(posts().length, priorityPostCount);

// A response issued before a complete freeze/resume cycle is still old when
// it arrives after release. Keep controls gated until a newly issued read.
pendingStateRequests = true;
const crossingPriorityPoll = intervalPoll(); await flush();
const crossingPaints = { ...renderCounts }, crossingLower = lowerStates.length;
changePriority(true); changePriority(false);
pendingStateRequests = false;
resolvePendingState({ ...browserState, frame: { ...browserState.frame, frame_id: "old-request-crossed-priority" } });
await crossingPriorityPoll; await flush();
assert.deepEqual(renderCounts, crossingPaints);
assert.equal(lowerStates.length, crossingLower);
assert.equal(elements.get("guiPriorityNotice").hidden, false);
assert.equal(elements.get("disconnectBtn").disabled, true);
browserState = { ...browserState, frame: { ...browserState.frame, frame_id: "fresh-after-priority-cycle", host_rx_monotonic_ns: 43e9 } };
await poll();
assert.equal(elements.get("guiPriorityNotice").hidden, true);
assert.equal(elements.get("frameId").textContent, "fresh-after-priority-cycle");
assert.equal(posts().length, priorityPostCount);

// Fresh page mount while an acquisition is already running paints a single
// current snapshot rather than freezing the initial empty placeholder.
browserState={...browserState,experiment:{running:true,session_id:"already-running"},tracker:{...browserState.tracker,reference_version:0},frame:{...stableFrame,frame_id:"midcapture-page-load"}};
const remounted=vm.createContext({...browser,globalThis:undefined});
delete remounted.globalThis;
vm.runInContext(readFileSync(root+"display.js","utf8"),remounted);
vm.runInContext(appSource,remounted);await flush();
assert.equal(elements.get("frameId").textContent,"midcapture-page-load");
assert.equal(elements.get("captureDisplayNotice").hidden,false);
assert.match(elements.get("captureDisplaySnapshot").textContent,/九峰参考 未建立/);
assert.equal(mountedDrift.history.points.length,0,"Opening midway through capture cannot collect a hidden preview buffer");
assert.match(elements.get("expWavelengthStatus").textContent,/冻结.*等待采集会话结束后显示新帧/);
const remountPaints={...renderCounts};
browserState={...browserState,frame:{...browserState.frame,frame_id:"midcapture-next"}};await poll();
assert.deepEqual(renderCounts,remountPaints);
assert.equal(elements.get("frameId").textContent,"midcapture-page-load");
assert.equal(lowerStates.at(-1).frame.frame_id,"midcapture-next");
assert.equal(posts().length,capturePosts,"Fresh page mounting and display resumption never start or stop hardware");
changePriority(true);
const initialFrozenCalls = calls.length, initialFrozenPaints = { ...renderCounts };
const frozenMount = vm.createContext({ ...browser, globalThis: undefined });
delete frozenMount.globalThis;
vm.runInContext(readFileSync(root + "display.js", "utf8"), frozenMount);
vm.runInContext(appSource, frozenMount); await flush();
assert.equal(calls.length, initialFrozenCalls, "Opening another GUI under an existing priority lease does not begin state polling");
assert.deepEqual(renderCounts, initialFrozenPaints, "A frozen page mount cannot replace the held canvas or values");
assert.equal(elements.get("guiPriorityNotice").hidden, false);
assert.equal(elements.get("forceZeroBtn").disabled, true);
console.log("前端验证通过：当前版本 演示波长图移至原始光谱下方并由上方显示单独管理；采集期间光谱和波长图快照冻结，20个不同帧无额外光谱重画/六轴与九峰表改写且下方完整收到20帧；初次打开正在采集、结束/历史会话、旧记录、范围/尺寸/提示旁路、服务中断与独立失效警告、显示零点跨流失效及自动恢复均通过。仅仿真API和DOM，无硬件或真实数据写入。");
