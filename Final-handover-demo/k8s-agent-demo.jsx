import { useState, useEffect, useRef, useCallback } from "react";

// ─── Service topology data ─────────────────────────────────────────────────

const W = 112, H = 54;

const SERVICES = [
  { id: "loadgenerator",  name: "Load Generator",  lang: "Python",     x: 266, y: 12,  critical: false },
  { id: "frontendproxy",  name: "Frontend Proxy",  lang: "Envoy",      x: 266, y: 94,  critical: false },
  { id: "checkout",       name: "Checkout",        lang: "Go",         x: 22,  y: 196, critical: true  },
  { id: "cart",           name: "Cart",            lang: ".NET",       x: 152, y: 196, critical: true  },
  { id: "imageprovider",  name: "Image Provider",  lang: "nginx",      x: 266, y: 196, critical: false },
  { id: "adservice",      name: "Ad Service",      lang: "Java",       x: 392, y: 196, critical: false },
  { id: "recommendation", name: "Recommend",       lang: "Python",     x: 508, y: 196, critical: false },
  { id: "payment",        name: "Payment",         lang: "JavaScript", x: 22,  y: 308, critical: true  },
  { id: "currency",       name: "Currency",        lang: "C++",        x: 152, y: 308, critical: false },
  { id: "productcatalog", name: "Product Catalog", lang: "Go",         x: 266, y: 308, critical: true  },
  { id: "shipping",       name: "Shipping",        lang: "Rust",       x: 392, y: 308, critical: false },
  { id: "email",          name: "Email",           lang: "Ruby",       x: 508, y: 308, critical: false },
];

const EDGES = [
  ["loadgenerator","frontendproxy"],
  ["frontendproxy","checkout"], ["frontendproxy","cart"],
  ["frontendproxy","imageprovider"], ["frontendproxy","adservice"],
  ["frontendproxy","recommendation"], ["frontendproxy","productcatalog"],
  ["checkout","payment"], ["checkout","currency"],
  ["checkout","productcatalog"], ["checkout","shipping"], ["checkout","email"],
  ["cart","payment"],
];

const FAULTS = [
  { id: "disk_pressure", label: "💾  Disk Pressure",   desc: "imageprovider fills node disk" },
  { id: "cpu_spike",     label: "📈  CPU Spike",        desc: "Pod CPU throttling" },
  { id: "pod_crash",     label: "💥  Crash Loop",       desc: "CrashLoopBackOff" },
  { id: "high_latency",  label: "🐌  High Latency",     desc: "Network latency injection" },
];

// ─── Agent scripts ─────────────────────────────────────────────────────────

const DISK_SCRIPT = [
  { ms:400,  type:"agent",  text:"Incident received — node disk pressure on ip-10-0-1-45" },
  { ms:800,  type:"agent",  text:"Checkout p99 latency degrading: 120ms → 890ms" },
  { ms:1100, type:"agent",  text:"Spawning specialist subagents in parallel..." },
  { ms:1350, type:"spawn",  text:"▸ cloudwatch-agent  starting metric investigation" },
  { ms:1500, type:"spawn",  text:"▸ kubectl-agent      starting cluster state scan" },
  { ms:1650, type:"spawn",  text:"▸ otel-agent         starting trace analysis" },
  { ms:2300, type:"tool",   tool:"kubectl",    cmd:"get nodes",                              result:"ip-10-0-1-45  Ready  DiskPressure=True  87% disk" },
  { ms:2900, type:"tool",   tool:"cloudwatch", cmd:"GetMetricData node_filesystem_utilization", result:"15m trend: 71% → 79% → 87%  (rising fast)" },
  { ms:3500, type:"tool",   tool:"otel",       cmd:"GetMetricData checkoutservice.latency.p99",  result:"890ms  (baseline 120ms)  —  7.4× degradation" },
  { ms:4100, type:"hypo",   text:"Hypothesis 1 — OTel Collector emptyDir buffer overflow" },
  { ms:4700, type:"tool",   tool:"kubectl",    cmd:"describe pod otelcol-0 -n otel-demo",    result:"emptyDir: 2.1GB / 5.0GB (42%)  within limits" },
  { ms:5300, type:"tool",   tool:"cloudwatch", cmd:"GetMetricData otelcol_exporter_bytes",   result:"12.3 MB/min  (avg 11.8)  — normal range" },
  { ms:5900, type:"reject", text:"✗  Hypothesis 1 REJECTED — OTel Collector is healthy" },
  { ms:6300, type:"agent",  text:"Re-planning… checking imageprovider" },
  { ms:6800, type:"hypo",   text:"Hypothesis 2 — imageprovider nginx verbose logging" },
  { ms:7300, type:"tool",   tool:"kubectl",    cmd:"describe deploy/imageprovider -n otel-demo", result:"NGINX_LOG_LEVEL=debug  ← MISCONFIGURATION" },
  { ms:7900, type:"tool",   tool:"cloudwatch", cmd:"GetMetricData container_fs_writes{imageprovider}", result:"340.2 MB/8min  —  12× above baseline (28 MB)" },
  { ms:8500, type:"confirm",text:"✓  Root cause: imageprovider NGINX_LOG_LEVEL=debug" },
  { ms:9100, type:"tool",   tool:"kubectl",    cmd:"get pods -n otel-demo -o custom-columns=NAME,PRIORITY",
    result:"checkoutservice   payment-critical  (1000000)  PROTECTED\npaymentservice    payment-critical  (1000000)  PROTECTED\ncartservice       payment-critical  (1000000)  PROTECTED\nimageprovider     infrastructure    (900000)   CANDIDATE\nadservice         background        (100000)   CANDIDATE\nloadgenerator     background        (100000)   CANDIDATE" },
  { ms:9900, type:"approval" },
];

const RESOLUTION_SCRIPT = [
  { ms:400,  type:"agent", text:"Approval received. Executing evictions..." },
  { ms:900,  type:"tool",  tool:"kubectl", cmd:'delete pod loadgenerator-7d4f9-xxx -n otel-demo',  result:'pod "loadgenerator-7d4f9-xxx" deleted ✓' },
  { ms:1500, type:"tool",  tool:"kubectl", cmd:'delete pod imageprovider-5c8a2-xxx -n otel-demo',  result:'pod "imageprovider-5c8a2-xxx" deleted ✓' },
  { ms:2100, type:"tool",  tool:"kubectl", cmd:'delete pod adservice-3f7b1-xxx -n otel-demo',      result:'pod "adservice-3f7b1-xxx" deleted ✓' },
  { ms:2700, type:"tool",  tool:"cloudwatch", cmd:"GetMetricData node_filesystem_utilization",     result:"87% → 61%  (↓ 26%)  pressure relieved ✓" },
  { ms:3300, type:"tool",  tool:"cloudwatch", cmd:"GetMetricData checkoutservice.latency.p99",     result:"890ms → 118ms  back to baseline ✓" },
  { ms:3900, type:"tool",  tool:"memory",     cmd:"store incident#2026-05-15-disk-pressure",       result:"saved: imageprovider debug logging → disk pressure" },
  { ms:4500, type:"resolved", text:"✓  Incident resolved  |  Node: 61%  |  Checkout p99: 118ms" },
];

// ─── Helpers ───────────────────────────────────────────────────────────────

const cx = s => s.x + W / 2;
const cy = s => s.y + H / 2;

const healthColor = { healthy:"#00e87a", warning:"#ffaa00", critical:"#ff2244", evicted:"#334" };
const healthGlow  = { healthy:"#00e87a33", warning:"#ffaa0033", critical:"#ff224466", evicted:"transparent" };

const LANG_COLOR = {
  "Go":"#00acd7","Python":"#3776ab",".NET":"#512bd4","Java":"#f89820",
  "JavaScript":"#f0db4f","nginx":"#009639","Envoy":"#ac57f0","C++":"#00599c",
  "Rust":"#ce4a1a","Ruby":"#cc342d",
};

function initHealth() {
  const h={}, l={};
  SERVICES.forEach(s=>{
    h[s.id]="healthy";
    l[s.id] = s.critical ? 80+Math.floor(Math.random()*50) : 12+Math.floor(Math.random()*30);
  });
  return {h,l};
}

// ─── Component ─────────────────────────────────────────────────────────────

export default function App() {
  const [{h:health,l:latency}, setHl] = useState(initHealth);
  const [ctxMenu, setCtxMenu]   = useState(null);
  const [incident, setIncident] = useState(null);
  const [phase, setPhase]       = useState("idle"); // idle|investigating|awaiting|resolving|resolved
  const [logs, setLogs]         = useState([]);
  const [showApproval, setShowApproval] = useState(false);
  const logRef = useRef(null);
  const timers = useRef([]);

  // Auto-scroll logs
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [logs]);

  // Click away closes context menu
  useEffect(() => {
    const close = () => setCtxMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, []);

  const clearTimers = () => { timers.current.forEach(clearTimeout); timers.current=[]; };

  const addLog = (entry) => setLogs(p => [...p, { ...entry, id: Math.random() }]);

  const injectFault = (faultId) => {
    const svcId = ctxMenu.serviceId;
    setCtxMenu(null);
    setIncident({ svcId, faultId });
    setHl(({h,l}) => ({
      h:{ ...h, [svcId]:"critical" },
      l:{ ...l, [svcId]: faultId==="high_latency"?2400:9999 }
    }));
    if (faultId==="disk_pressure") {
      timers.current.push(setTimeout(()=>{
        setHl(({h,l})=>({
          h:{...h, checkout:"warning", payment:"warning"},
          l:{...l, checkout:890, payment:720}
        }));
      }, 2200));
    }
  };

  const handleSolve = () => {
    if (!incident || phase!=="idle") return;
    setPhase("investigating");
    setLogs([]);
    setShowApproval(false);
    const script = incident.faultId==="disk_pressure" ? DISK_SCRIPT : DISK_SCRIPT;
    script.forEach(({ms,...entry})=>{
      const t = setTimeout(()=>{
        if (entry.type==="approval") { setShowApproval(true); setPhase("awaiting"); }
        else addLog(entry);
      }, ms);
      timers.current.push(t);
    });
  };

  const handleApprove = () => {
    setShowApproval(false);
    setPhase("resolving");
    setHl(({h,l})=>({
      h:{...h, loadgenerator:"evicted", imageprovider:"evicted", adservice:"evicted"},
      l
    }));
    RESOLUTION_SCRIPT.forEach(({ms,...entry})=>{
      const t = setTimeout(()=>{
        if (entry.type==="resolved"){
          setPhase("resolved");
          setHl(({h,l})=>({
            h:{...h, checkout:"healthy", payment:"healthy"},
            l:{...l, checkout:118, payment:95}
          }));
        }
        addLog(entry);
      }, ms);
      timers.current.push(t);
    });
  };

  const handleReset = () => {
    clearTimers();
    const {h,l} = initHealth();
    setHl({h,l});
    setIncident(null);
    setPhase("idle");
    setLogs([]);
    setShowApproval(false);
  };

  // ─── Render ──────────────────────────────────────────────────────────────

  const svcMap = Object.fromEntries(SERVICES.map(s=>[s.id,s]));

  return (
    <div style={{background:"#07090f",color:"#c8d8e8",fontFamily:"'SF Mono','Fira Code',monospace",
      height:"100vh",display:"flex",flexDirection:"column",overflow:"hidden"}}>

      {/* ── Header ── */}
      <div style={{background:"#0b0f1a",borderBottom:"1px solid #1c2a3a",padding:"0 20px",
        height:52,display:"flex",alignItems:"center",justifyContent:"space-between",flexShrink:0}}>
        <div style={{display:"flex",alignItems:"center",gap:14}}>
          <div style={{width:8,height:8,borderRadius:"50%",background:"#00e87a",
            boxShadow:"0 0 8px #00e87a"}} />
          <span style={{color:"#7ab8d4",fontSize:11,letterSpacing:2,textTransform:"uppercase"}}>
            otel-demo-prod
          </span>
          <span style={{color:"#2a3a4a",margin:"0 4px"}}>|</span>
          <span style={{color:"#4a6070",fontSize:11}}>EKS ap-southeast-2</span>
        </div>
        <div style={{display:"flex",alignItems:"center",gap:10}}>
          <span style={{color:"#4a6070",fontSize:11,letterSpacing:1}}>
            {SERVICES.filter(s=>health[s.id]==="healthy").length}/{SERVICES.length} healthy
          </span>
          {incident && (
            <button onClick={handleSolve} disabled={phase!=="idle"}
              style={{background: phase!=="idle"?"#0f1a24":"#003388",
                border:"1px solid",borderColor:phase!=="idle"?"#1c2a3a":"#0055cc",
                color:phase!=="idle"?"#334":"#88bbff",padding:"6px 16px",
                borderRadius:4,cursor:phase!=="idle"?"not-allowed":"pointer",
                fontSize:12,letterSpacing:1,
                boxShadow:phase==="idle"?"0 0 12px #0044aa44":"none",
                transition:"all .2s"}}>
              {phase==="idle"?"⚡ SOLVE WITH AI":
               phase==="investigating"?"⏳ INVESTIGATING...":
               phase==="awaiting"?"⏸ AWAITING APPROVAL":
               phase==="resolving"?"⚙ RESOLVING...":"✓ RESOLVED"}
            </button>
          )}
          <button onClick={handleReset}
            style={{background:"transparent",border:"1px solid #1c2a3a",
              color:"#4a6070",padding:"6px 12px",borderRadius:4,cursor:"pointer",fontSize:11}}>
            RESET
          </button>
        </div>
      </div>

      {/* ── Incident banner ── */}
      {incident && health[incident.svcId]==="critical" && phase==="idle" && (
        <div style={{background:"#1a0810",borderBottom:"1px solid #ff224444",
          padding:"8px 20px",display:"flex",alignItems:"center",gap:12,flexShrink:0}}>
          <span style={{color:"#ff2244",fontSize:11,letterSpacing:2}}>⚠ INCIDENT</span>
          <span style={{color:"#ff6688",fontSize:12}}>
            Node disk pressure detected — checkout latency degrading — click SOLVE to begin investigation
          </span>
        </div>
      )}

      {/* ── Main ── */}
      <div style={{display:"flex",flex:1,overflow:"hidden"}}>

        {/* ── Topology panel ── */}
        <div style={{flex:"0 0 62%",display:"flex",flexDirection:"column",
          borderRight:"1px solid #1c2a3a",overflow:"hidden"}}>
          <div style={{padding:"10px 16px",borderBottom:"1px solid #111a24",
            display:"flex",justifyContent:"space-between",alignItems:"center"}}>
            <span style={{fontSize:10,letterSpacing:2,color:"#4a6070",textTransform:"uppercase"}}>
              Service Topology — OpenTelemetry Astronomy Shop
            </span>
            <span style={{fontSize:10,color:"#2a3a4a"}}>right-click any service to inject fault</span>
          </div>

          <div style={{flex:1,position:"relative",overflow:"hidden",padding:"8px"}}>
            {/* SVG connection lines */}
            <svg style={{position:"absolute",inset:0,width:"100%",height:"100%",pointerEvents:"none"}}>
              {EDGES.map(([a,b])=>{
                const sa=svcMap[a], sb=svcMap[b];
                if(!sa||!sb) return null;
                const isCriticalEdge = (a==="checkout"||b==="checkout")&&(a==="payment"||b==="payment"||a==="productcatalog"||b==="productcatalog");
                const anyDegraded = health[a]==="critical"||health[b]==="critical"||health[a]==="warning"||health[b]==="warning";
                const anyEvicted = health[a]==="evicted"||health[b]==="evicted";
                return (
                  <line key={`${a}-${b}`}
                    x1={cx(sa)+8} y1={cy(sa)+8} x2={cx(sb)+8} y2={cy(sb)+8}
                    stroke={anyEvicted?"#1a2030":anyDegraded?"#ff4466":isCriticalEdge?"#00aa55":"#1e3048"}
                    strokeWidth={isCriticalEdge?1.5:1}
                    strokeDasharray={anyEvicted?"4,4":"none"}
                    opacity={anyEvicted?0.3:0.7} />
                );
              })}
            </svg>

            {/* Service cards */}
            {SERVICES.map(svc => {
              const h = health[svc.id] || "healthy";
              const lat = latency[svc.id] || 0;
              const evicted = h==="evicted";
              return (
                <div key={svc.id}
                  onContextMenu={e=>{ e.preventDefault(); if(!evicted) setCtxMenu({x:e.clientX,y:e.clientY,serviceId:svc.id}); }}
                  style={{
                    position:"absolute", left:svc.x+8, top:svc.y+8, width:W, height:H,
                    background: evicted?"#0a0e14":"#0d1622",
                    border:`1px solid ${evicted?"#1a2030":healthColor[h]}`,
                    borderRadius:6,padding:"7px 9px",cursor:evicted?"default":"context-menu",
                    opacity:evicted?0.35:1,
                    boxShadow:evicted?"none":`0 0 ${h==="healthy"?6:12}px ${healthGlow[h]}`,
                    transition:"all .4s",userSelect:"none",
                  }}>
                  <div style={{display:"flex",alignItems:"center",gap:5,marginBottom:3}}>
                    <div style={{width:6,height:6,borderRadius:"50%",flexShrink:0,
                      background:healthColor[h],
                      animation:h==="critical"?"pulse-red 1s infinite":
                                h==="warning"?"pulse-amber 1.5s infinite":"none"}} />
                    <span style={{fontSize:10,fontWeight:600,color:evicted?"#2a3a4a":h==="healthy"?"#a0c0d4":"#e0e8f0",
                      whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis",maxWidth:86}}>
                      {svc.name}
                    </span>
                  </div>
                  <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
                    <span style={{fontSize:9,color:LANG_COLOR[svc.lang]||"#5a7a8a"}}>
                      {svc.lang}
                    </span>
                    <span style={{fontSize:9,color:evicted?"#2a3a4a":
                      lat>500?"#ff4466":lat>200?"#ffaa00":"#4a7a5a"}}>
                      {evicted?"evicted":lat>999?"—":lat+"ms"}
                    </span>
                  </div>
                  {svc.critical && !evicted && (
                    <div style={{position:"absolute",top:3,right:5,
                      fontSize:8,color:h==="healthy"?"#00aa55":"#ff6688",letterSpacing:0.5}}>
                      CRITICAL
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          {/* Legend */}
          <div style={{padding:"8px 16px",borderTop:"1px solid #111a24",
            display:"flex",gap:16,alignItems:"center"}}>
            {Object.entries(healthColor).map(([k,v])=>(
              <div key={k} style={{display:"flex",alignItems:"center",gap:5}}>
                <div style={{width:6,height:6,borderRadius:"50%",background:v}} />
                <span style={{fontSize:9,color:"#4a6070",textTransform:"capitalize"}}>{k}</span>
              </div>
            ))}
            <span style={{marginLeft:"auto",fontSize:9,color:"#2a3a4a"}}>
              OTel Astronomy Shop — 12 services
            </span>
          </div>
        </div>

        {/* ── Agent panel ── */}
        <div style={{flex:1,display:"flex",flexDirection:"column",background:"#060810"}}>
          <div style={{padding:"10px 16px",borderBottom:"1px solid #111a24",
            display:"flex",alignItems:"center",gap:8}}>
            <div style={{width:6,height:6,borderRadius:"50%",
              background:phase==="idle"?"#2a3a4a":phase==="resolved"?"#00e87a":"#00aaff",
              boxShadow:phase!=="idle"?`0 0 8px ${phase==="resolved"?"#00e87a":"#00aaff"}`:"none"}} />
            <span style={{fontSize:10,letterSpacing:2,color:"#4a6070",textTransform:"uppercase"}}>
              Agent Activity
            </span>
            {phase!=="idle" && (
              <span style={{marginLeft:"auto",fontSize:9,
                color:phase==="resolved"?"#00aa55":phase==="awaiting"?"#ffaa00":"#00aaff",
                letterSpacing:1}}>
                {phase.toUpperCase()}
              </span>
            )}
          </div>

          {/* Log stream */}
          <div ref={logRef} style={{flex:1,overflowY:"auto",padding:"12px 14px",
            display:"flex",flexDirection:"column",gap:4}}>
            {phase==="idle" && (
              <div style={{margin:"auto",textAlign:"center",color:"#1e2e3e"}}>
                <div style={{fontSize:32,marginBottom:12}}>⬡</div>
                <div style={{fontSize:11,letterSpacing:1}}>NO ACTIVE INVESTIGATION</div>
                <div style={{fontSize:10,marginTop:6,color:"#1a2830"}}>
                  Right-click a service to inject a fault
                </div>
              </div>
            )}
            {logs.map(log => <LogLine key={log.id} log={log} />)}

            {/* Approval card */}
            {showApproval && (
              <div style={{margin:"8px 0",background:"#0c1520",
                border:"1px solid #ffaa0066",borderRadius:6,padding:14}}>
                <div style={{color:"#ffaa00",fontSize:11,letterSpacing:1,marginBottom:8}}>
                  ⏸  AWAITING APPROVAL
                </div>
                <div style={{fontSize:10,color:"#7a9ab0",marginBottom:10,lineHeight:1.7}}>
                  <div><span style={{color:"#4a6070"}}>Root cause: </span>
                    <span style={{color:"#c0d8e8"}}>imageprovider NGINX_LOG_LEVEL=debug</span></div>
                  <div><span style={{color:"#4a6070"}}>Node disk:   </span>
                    <span style={{color:"#ff6688"}}>87%</span></div>
                  <div><span style={{color:"#4a6070"}}>Checkout p99:</span>
                    <span style={{color:"#ff6688"}}> 890ms</span></div>
                  <div style={{marginTop:8,paddingTop:8,borderTop:"1px solid #1c2a3a"}}>
                    <div style={{color:"#4a6070",marginBottom:4}}>Evict (lowest priority first):</div>
                    {["loadgenerator","imageprovider","adservice"].map(id=>(
                      <div key={id} style={{paddingLeft:8,color:"#ff8888"}}>
                        — {id}
                      </div>
                    ))}
                    <div style={{marginTop:6,color:"#4a6070"}}>Protected:</div>
                    {["checkoutservice","paymentservice","cartservice"].map(id=>(
                      <div key={id} style={{paddingLeft:8,color:"#00aa55"}}>
                        ✓ {id}
                      </div>
                    ))}
                  </div>
                  <div style={{marginTop:8,color:"#7a7a3a",fontSize:9}}>
                    ⚠ Product browsing unavailable. Payments continue normally.
                  </div>
                </div>
                <div style={{display:"flex",gap:8}}>
                  <button onClick={handleApprove}
                    style={{flex:1,background:"#003322",border:"1px solid #00aa55",
                      color:"#00e87a",padding:"8px 0",borderRadius:4,cursor:"pointer",
                      fontSize:11,letterSpacing:1}}>
                    ✓ APPROVE
                  </button>
                  <button onClick={()=>{setShowApproval(false);setPhase("idle");}}
                    style={{flex:1,background:"#1a0810",border:"1px solid #ff224466",
                      color:"#ff6688",padding:"8px 0",borderRadius:4,cursor:"pointer",
                      fontSize:11,letterSpacing:1}}>
                    ✗ DENY
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* ── Context menu ── */}
      {ctxMenu && (
        <div style={{position:"fixed",left:ctxMenu.x,top:ctxMenu.y,zIndex:1000,
          background:"#0d1622",border:"1px solid #1c2a3a",borderRadius:6,
          overflow:"hidden",boxShadow:"0 8px 32px #00000088",minWidth:200}}>
          <div style={{padding:"8px 12px",borderBottom:"1px solid #1c2a3a",
            fontSize:9,color:"#4a6070",letterSpacing:1,textTransform:"uppercase"}}>
            Inject Fault — {ctxMenu.serviceId}
          </div>
          {FAULTS.map(f=>(
            <div key={f.id}
              onClick={e=>{ e.stopPropagation(); injectFault(f.id); }}
              style={{padding:"9px 12px",cursor:"pointer",fontSize:11,
                display:"flex",flexDirection:"column",gap:2,
                borderBottom:"1px solid #0d1622"}}
              onMouseEnter={e=>e.currentTarget.style.background="#1a2a3a"}
              onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
              <span style={{color:"#c0d8e8"}}>{f.label}</span>
              <span style={{fontSize:9,color:"#4a6070"}}>{f.desc}</span>
            </div>
          ))}
        </div>
      )}

      <style>{`
        @keyframes pulse-red   { 0%,100%{box-shadow:0 0 4px #ff2244} 50%{box-shadow:0 0 10px #ff2244} }
        @keyframes pulse-amber { 0%,100%{box-shadow:0 0 4px #ffaa00} 50%{box-shadow:0 0 8px #ffaa00} }
        ::-webkit-scrollbar { width:4px }
        ::-webkit-scrollbar-track { background:#070910 }
        ::-webkit-scrollbar-thumb { background:#1c2a3a; border-radius:2px }
      `}</style>
    </div>
  );
}

// ─── Log line component ────────────────────────────────────────────────────

function LogLine({ log }) {
  const TOOL_COLOR = {
    kubectl:"#00d4ff", cloudwatch:"#ff9933", otel:"#aa88ff",
    memory:"#00aa55", default:"#7ab8d4"
  };

  const base = { fontSize:10, lineHeight:1.6, fontFamily:"'SF Mono','Fira Code',monospace" };

  if (log.type === "tool") {
    const color = TOOL_COLOR[log.tool] || TOOL_COLOR.default;
    return (
      <div style={{...base}}>
        <div>
          <span style={{color:"#2a4a5a"}}>$ </span>
          <span style={{color}}>[{log.tool}]</span>
          <span style={{color:"#7a9ab0"}}> {log.cmd}</span>
        </div>
        {log.result && log.result.split("\n").map((line,i)=>(
          <div key={i} style={{paddingLeft:14,color:"#4a6a7a"}}>→ {line}</div>
        ))}
      </div>
    );
  }

  const typeStyle = {
    agent:   { color:"#7ab8d4" },
    spawn:   { color:"#5a8a9a" },
    hypo:    { color:"#aaaa44", fontStyle:"italic" },
    reject:  { color:"#ff6644" },
    confirm: { color:"#00cc66" },
    resolved:{ color:"#00e87a", fontWeight:600 },
  };

  const prefix = {
    agent:"  ◆ ", spawn:"  ", hypo:"  ? ",
    reject:"  ✗ ", confirm:"  ✓ ", resolved:"  ✓ ",
  };

  return (
    <div style={{...base,...(typeStyle[log.type]||{})}}>
      <span style={{color:"#2a3a4a"}}>{prefix[log.type]||"  "}</span>
      {log.text}
    </div>
  );
}