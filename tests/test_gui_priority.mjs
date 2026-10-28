import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";
const source=readFileSync(new URL("../frontend/gui_priority.js",import.meta.url),"utf8");
function world({broadcast=true,storage=true}={}) {
  let time=100000,sequence=0; const windows=[], values=new Map(), channels=[];
  function page() {
    const events=new Map(),timers=new Map(); const p={events,timers}; windows.push(p);
    function storageEvent(key,newValue){for(const other of windows)if(other!==p)other.events.get("storage")?.({key,newValue});}
    class Channel {
      constructor(name){this.name=name;this.closed=false;channels.push(this);}
      postMessage(data){for(const c of channels)if(c!==this&&!c.closed&&c.name===this.name)c.onmessage?.({data});}
      close(){this.closed=true;}
    }
    const localStorage=storage?{get length(){return values.size;},key(i){return [...values.keys()][i]??null;},getItem(key){return values.get(key)??null;},
      setItem(key,value){values.set(key,String(value));storageEvent(key,String(value));},removeItem(key){values.delete(key);storageEvent(key,null);}}:undefined;
    class Clock extends Date{static now(){return time;}}
    p.context=vm.createContext({Date:Clock,Math,crypto:{randomUUID:()=>"owner-"+(++sequence)},localStorage,
      ...(broadcast?{BroadcastChannel:Channel}:{}),
      setInterval(fn,ms){assert.equal(ms,1000);timers.set(++sequence,fn);return sequence;},clearInterval(id){timers.delete(id);},
      addEventListener(type,fn){events.set(type,fn);},removeEventListener(type,fn){if(events.get(type)===fn)events.delete(type);}});
    vm.runInContext(source,p.context);p.controller=p.context.NineFBGGuiPriority.shared();return p;
  }
  return {page,values,advance(ms,{skip=[]}={}){time+=ms;for(const p of windows)if(!skip.includes(p))for(const fn of [...p.timers.values()])fn();}};
}
for(const mode of [{broadcast:true,storage:true},{broadcast:false,storage:true},{broadcast:true,storage:false}]) {
  const w=world(mode),main=w.page(),pulse=w.page();const changes=[];
  main.controller.subscribe(s=>changes.push(s.frozen));
  assert.equal(main.controller.isFrozen(),false);
  pulse.controller.setActive(true);assert.equal(main.controller.isFrozen(),true);assert.equal(pulse.controller.isFrozen(),false);
  const late=w.page();assert.equal(late.controller.isFrozen(),true,"Opening the workbench while pulse owns priority must start frozen");
  w.advance(1000);w.advance(1000);assert.deepEqual(changes,[true],"Heartbeat cannot trigger repeated display rendering");
  const second=w.page();second.controller.setActive(true);pulse.controller.setActive(false);assert.equal(main.controller.isFrozen(),true,"One tab cannot release another tab's lease");
  main.controller.requestResume();assert.equal(second.controller.isActive(),false);assert.equal(main.controller.isFrozen(),false);assert.equal(late.controller.isFrozen(),false);
  assert.equal(changes.at(-1),false);
  let resumeReason;
  pulse.controller.subscribe(s=>{resumeReason=s.reason;});
  main.controller.requestResume();assert.equal(resumeReason,"resume","A hidden inactive pulse tab must also withdraw its priority preference");
  pulse.controller.setActive(true);pulse.events.get("pagehide")();assert.equal(main.controller.isFrozen(),false,"Page exit releases priority without backend work");
  pulse.controller.setActive(true);w.advance(5001,{skip:[pulse]});assert.equal(main.controller.isFrozen(),false,"A crashed pulse tab cannot freeze the workbench indefinitely");
  pulse.controller.setActive(false);pulse.controller.setActive(true);pulse.controller.dispose();assert.equal(main.controller.isFrozen(),false);assert.equal(pulse.timers.size,0);
}
{
  const w=world({broadcast:false,storage:false}),p=w.page();p.controller.setActive(true);p.controller.requestResume();assert.equal(p.controller.isActive(),false,"Missing optional browser storage remains usable");
}
console.log("gui_priority: cross-page leases, multiple owners, resume, heartbeat, expiry and storage fallbacks passed");
