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
for(const id of ["spectrumChart","peakTable","forceGrid","timeQuality","integrationUs","sampleRateHz","px6dConnectBtn","px6dDisconnectBtn","forceZeroBtn","forceUnloadedCheck","spectrumRangeApply"]) assert.ok(html.includes(`id="${id}"`),id);
const appSource=readFileSync(root+"app.js","utf8");
for(const id of ["driftChart","driftEmpty","driftLegend","forceChart","torqueChart","forceRangeMode","forceLimit","torqueLimit","forceRangeApply","forceRangeReset","forceRangeStatus","referenceControls","offlineControls","unloadedCheck","referenceBtn","pressStartBtn","pressEndBtn","replayFile","fileSummary","compareBtn","comparePanel","compareResults"]) {
  assert.ok(!html.includes(`id="${id}"`),`${id} is removed from the DOM`);
  assert.ok(!appSource.includes(`"${id}"`),`${id} has no runtime DOM dependency`);
}
for(const identifier of ["class History", "forcePoints", "forceLastKey", "drawForceCharts", "drawDrift"]) assert.ok(!appSource.includes(identifier),`${identifier} is retired with its time chart`);
assert.deepEqual(Array.from(html.matchAll(/<canvas\b[^>]*id="([^"]+)"/g),match=>match[1]),["spectrumChart"],"Only the raw spectrum canvas remains in the main display");
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
const domIds=Array.from(html.matchAll(/\bid="([^"]+)"/g),match=>match[1]);
assert.equal(new Set(domIds).size,domIds.length,"Moving live panels cannot duplicate any DOM identity");
assert.ok(!initialCalibration[1].includes('id="experimentWorkspace"')&&!liveDisplay.includes('id="experimentWorkspace"'),"Calibration, live display and acquisition remain separate workspaces");
for(const id of ["forceZeroBtn","forceUnloadedCheck","forceZeroReset","forceDisplayZeroStatus"]) {
  assert.ok(initialCalibration[1].includes(`id="${id}"`),`${id} is in the initial calibration card`);
  assert.equal(Array.from(html.matchAll(new RegExp(`id="${id}"`,"g"))).length,1,`${id} has no duplicate control`);
}
assert.match(html,/<html lang="zh-CN">/); assert.ok(!html.includes("https://"),"No CDN dependency");
assert.ok(html.indexOf('src="/display.js"') < html.indexOf('src="/app.js"'),"The pure display module loads before the browser entry point");
assert.ok(html.indexOf('src="/preferences.js"') < html.indexOf('src="/display.js"'),"Saved setting bindings are available before both workspaces load");
assert.ok(html.includes('id="settingsStatus"'));
assert.ok(!html.includes('value="replay"'),"Retired replay workflow is absent from the UI");
// Exercise the browser entry point with an inert DOM and API. No instrument is involved.
const elements = new Map();
const listeners = new Map();
const renderCounts = { spectrum: 0, peaks: 0, force: 0 }, windowListeners = new Map();
let spectrumBounds = { width: 1000, height: 284 };
const context2d = new Proxy({clearRect(){renderCounts.spectrum++;}}, {get(target,key) {return key in target ? target[key] : ()=>{};}});
for(const match of html.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
  const id = match[1], tag = match[0];
  let value = /\bvalue="([^"]*)"/.exec(tag)?.[1] || (id==="sourceMode"?"hardware":id.endsWith("RangeMode")?"auto":"");
  let disabled = /\bdisabled\b/.test(tag);
  let markup = "";
  elements.set(id,{id,textContent:"",hidden:false,style:{},open:false,
    get innerHTML(){return markup;},set innerHTML(next){markup=next;if(id==="peakTable")renderCounts.peaks++;if(id==="forceGrid")renderCounts.force++;},
    get value(){return value;},set value(next){value=String(next);},checked:/\bchecked\b/.test(tag),
    get disabled(){return disabled;},set disabled(next){disabled=Boolean(next);},
    classList:{toggle(){},add(){}},addEventListener(type,callback){listeners.set(`${id}:${type}`,callback);},
    getBoundingClientRect(){return {width:id==="spectrumChart"?spectrumBounds.width:1000,height:id==="spectrumChart"?spectrumBounds.height:180,left:0,top:0};},
    getContext(){return context2d;},scrollIntoView(){}});
}
const calls=[];
const preferenceBindings=new Map(), preferenceSaves=[];
let intervalPoll;
let intervalStaleness;
let failStateRequests=false;
let pendingStateRequests=false;
let pendingStateSignal=null;
let clockNow=Date.now();
let timerID=0;
const timers=new Map();
class TestDate extends Date { static now(){return clockNow;} }
let browserState={product:"9FBG",version:"0.2.0",running:true,mode:"replay",frame:frame("test-fixture"),metrics:{new_frame_hz:null,receive_hz:12,new_frame_rate_reason:"设备未提供可验证的新帧身份"},tracker:{state:"tracking",reference_version:1,display_stale:true,display_age_ms:1500,peaks:[{id:"FBG1",valid:false,stale:true,age_ms:1500,center_nm:1512,failure_reason:"fit_failed"}]},session:{closed:false,counts:{raw_spectra:20}}};
const forceFrame=(id=1,time=1e9)=>({force_values:[60,20,40,10,50,30],axes:["Mz","Fy","Mx","Fx","My","Fz"],units:["N·m","N","N·m","N","N·m","N"],frame_id:id,stream_epoch:"px6d-real-stream",host_rx_monotonic_ns:time,age_ms:0,valid:true});
const posts=()=>calls.filter(call=>call.options?.method==="POST");
const browser = vm.createContext({document:{getElementById(id){assert.ok(elements.has(id),`DOM target ${id}`);return elements.get(id);},querySelector(){return {};}},
  NineFBGPreferences:{bind(section,mapping,afterRestore){preferenceBindings.set(section,{mapping,afterRestore});return Promise.resolve();},save(section,patch){preferenceSaves.push({section,patch:JSON.parse(JSON.stringify(patch))});return Promise.resolve();}},
  fetch:async(path,options)=>{
    calls.push({path,options});
    if(path==="/api/state") {
      if(failStateRequests) throw new TypeError("fixture service unavailable");
      if(pendingStateRequests) {
        pendingStateSignal=options.signal;
        return new Promise((resolve,reject)=>options.signal.addEventListener("abort",()=>reject(new Error("fixture request aborted")),{once:true}));
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
  addEventListener(type,callback){windowListeners.set(type,callback);},
  setTimeout(callback,ms){const id=++timerID;timers.set(id,{callback,ms});return id;},
  clearTimeout(id){timers.delete(id);},
  setInterval(callback,ms){assert.equal(ms,100);if(callback.name==="poll") intervalPoll=callback;else if(callback.name==="refreshStaleness") intervalStaleness=callback;else assert.fail("Unknown background interval");},
  Date:TestDate,AbortController,console,devicePixelRatio:1});
vm.runInContext(readFileSync(root+"display.js","utf8"),browser);
vm.runInContext(appSource,browser);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const fire=async(id,type="click",event={})=>{assert.ok(listeners.has(`${id}:${type}`),`${id} has a real ${type} listener`);await listeners.get(`${id}:${type}`)(event);await flush();};
const poll=async()=>{await intervalPoll();await flush();};
const forceCards=()=>Array.from(elements.get("forceGrid").innerHTML.matchAll(/<div class="force-axis[^"]*"><span>([^<]+)<\/span><strong class="force-value">([^<]+)<\/strong><small>([^<]+)<\/small>/g),match=>({axis:match[1],value:match[2],unit:match[3]}));
await flush();
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
assert.equal(elements.get("sampleRateHz").value,"100");
assert.equal(elements.get("connectBtn").disabled,false);
await fire("connectBtn");
assert.equal(posts().at(-1).path,"/api/connect");
assert.deepEqual(JSON.parse(posts().at(-1).options.body),{mode:"hardware",record:false,connect_force:false,integration_us:20,sample_rate_hz:100});
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
assert.match(html, /id="version">0\.7\.1</);
const stableFrame = { ...frame("render-once", 9e9), stream_epoch: "render-stream" };
const stablePeaks = Array.from({length:9},(_,index)=>({id:`FBG${index+1}`,valid:true,stale:false,center_nm:1511+index,drift_nm:0,amplitude:1,background:100,fit_x:[1511+index,1511.01+index],fit_y:[100,101]}));
elements.get("showWindows").checked=true;
browserState={...browserState,version:"0.7.1",running:true,mode:"hardware",frame:stableFrame,force:forceFrame(501),force_connected:true,tracker:{state:"tracking",stream_epoch:"render-stream",reference_version:1,display_stale:false,display_age_ms:0,peaks:stablePeaks,windows:[{id:"FBG1",low_nm:1510,high_nm:1512}]}};
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
console.log("前端验证通过：0.7.1 首次标定后实时大框与唯一DOM标识、同帧去重与六轴独立刷新、参考/范围/窗口/尺寸变化立即重画、独立失效刷新、设置恢复、实际谱轴及原始数据保全。");
