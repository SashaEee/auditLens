/* global React, ReactDOM */
const { useState, useEffect, useRef, useMemo, useCallback, createContext, useContext } = React;

// ─── Constants ────────────────────────────────────────────────────────────────
const CAT_LABELS = {
  deposit:"Вклады", credit:"Кредиты", mortgage:"Ипотека",
  card_credit:"Кредитные карты", card_debit:"Дебетовые карты",
  auto_loan:"Автокредиты", metals:"Драгметаллы", other:"Прочее",
};
// Темы жалоб (категории отзывов) — перевод ключей классификатора на русский
const TOPIC_LABELS = {
  fees:"Комиссии", rate_change:"Изменение ставки", app_bugs:"Сбои приложения",
  support:"Поддержка", card_block:"Блокировка карты", credit_terms:"Условия кредита",
  deposit_terms:"Условия вклада", atm:"Банкоматы", transfers:"Переводы",
  interest_rate:"Процентная ставка", loan_approval:"Одобрение кредита",
  branch_service:"Обслуживание в отделении", online_bank:"Онлайн-банк",
  premium:"Премиум-обслуживание", bonus_program:"Бонусы и кешбэк",
  documents:"Документы и справки", fraud:"Мошенничество", partner:"Партнёрские услуги",
};
const TL = t => TOPIC_LABELS[t] || t;
const LOWER_IS_BETTER = new Set(["credit","mortgage","card_credit","auto_loan"]);
const CATS_ORDER = ["deposit","credit","mortgage","card_credit","card_debit","auto_loan","metals"];
const QUICK = [
  {eb:"01 · Депозиты", t:"Сравни предложения по вкладам, выдели топ-5 и позицию Сбера."},
  {eb:"02 · Риски",    t:"Какие основные жалобы у клиентов Сбербанка? Где подводные камни?"},
  {eb:"03 · Ипотека",  t:"Сравни ипотечные ставки между Сбером и рынком, выдели программы с господдержкой."},
  {eb:"04 · Динамика", t:"Покажи изменения условий за последние 7 дней — что выросло, что упало."},
];

// ─── Helpers ──────────────────────────────────────────────────────────────────
const pct  = (v,d=2) => v==null ? "—" : `${parseFloat(v).toFixed(d)}%`;
const signed = (v,d=2) => { if(v==null)return "—"; const n=parseFloat(v); return(n>0?"+":"")+n.toFixed(d); };
const fmtNum = n => n==null ? "—" : parseInt(n).toLocaleString("ru");
// Safe render helper for unknown-type values (JSONB columns etc.)
const str = v => v==null ? "" : typeof v==="object" ? JSON.stringify(v) : String(v);
const fmtDate = s => {
  if(!s) return "—";
  try { return new Date(s).toLocaleString("ru",{day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}); }
  catch { return String(s).slice(0,16); }
};
const fmtAmount = (min,max) => {
  const f=n=>{if(!n)return null;n=parseFloat(n);if(n>=1e6)return`${+(n/1e6).toFixed(1)} млн`;if(n>=1e3)return`${Math.round(n/1e3)} тыс.`;return String(Math.round(n));};
  const[a,b]=[f(min),f(max)];
  if(a&&b)return`${a} — ${b} ₽`;if(a)return`от ${a} ₽`;if(b)return`до ${b} ₽`;return "—";
};
const fmtTerm = (min,max) => {
  const f=m=>{if(!m)return null;m=parseInt(m);if(m%12===0&&m>=12){const y=m/12;return`${y} ${y===1?"год":y<5?"года":"лет"}`;}return`${m} мес.`;};
  const[a,b]=[f(min),f(max)];if(a&&b&&a!==b)return`${a} — ${b}`;return a||b||"—";
};

// ─── API ──────────────────────────────────────────────────────────────────────
const apiFetch = (path) => fetch(path).then(r=>{if(!r.ok)throw new Error(`${r.status} ${r.statusText}`);return r.json();});
const apiPost  = (path,body) => fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).then(r=>{if(!r.ok)throw new Error(`${r.status}`);return r.json();});
const apiDel   = (path) => fetch(path,{method:"DELETE"}).then(r=>r.json()).catch(()=>{});

// ─── Context ──────────────────────────────────────────────────────────────────
const ThemeCtx = createContext({theme:"light",setTheme:()=>{}});
function ThemeProvider({children}){
  const [theme,setTheme]=useState(()=>{try{return localStorage.getItem("auditlens-theme")||"light";}catch{return"light";}});
  useEffect(()=>{document.documentElement.classList.toggle("dark",theme==="dark");try{localStorage.setItem("auditlens-theme",theme);}catch{}},[theme]);
  return <ThemeCtx.Provider value={{theme,setTheme}}>{children}</ThemeCtx.Provider>;
}
const useTheme = () => useContext(ThemeCtx);
const BanksCtx = createContext([]);
const useBanks = () => useContext(BanksCtx);

// ─── Icons ────────────────────────────────────────────────────────────────────
const Ic = {
  grid:    p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" {...p}><rect x="3" y="3" width="7.5" height="7.5" rx="1"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1"/></svg>,
  market:  p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M3 17l6-6 4 4 8-9"/><path d="M21 6h-5"/><path d="M21 6v5"/></svg>,
  scale:   p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M12 4v16"/><path d="M5 8h14"/><path d="M5 8l-2 6a4 4 0 008 0z"/><path d="M19 8l-2 6a4 4 0 008 0z"/></svg>,
  msg:     p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8z"/></svg>,
  spark:   p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M12 3l1.9 5.5L19 10l-5.1 1.5L12 17l-1.9-5.5L5 10l5.1-1.5z"/></svg>,
  bank:    p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M3 21h18"/><path d="M5 21V10"/><path d="M9 21V10"/><path d="M15 21V10"/><path d="M19 21V10"/><path d="M3 10l9-6 9 6"/></svg>,
  src:     p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v6c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 11v6c0 1.7 4 3 9 3s9-1.3 9-3v-6"/></svg>,
  shield:  p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M12 2l8 4v6c0 5-3.5 9.3-8 10-4.5-.7-8-5-8-10V6z"/></svg>,
  search:  p=><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg>,
  sun:     p=><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>,
  moon:    p=><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/></svg>,
  refresh: p=><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M3 12a9 9 0 0115.5-6.3L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 01-15.5 6.3L3 16"/><path d="M3 21v-5h5"/></svg>,
  send:    p=><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M22 2L11 13"/><path d="M22 2l-7 20-4-9-9-4z"/></svg>,
  arrow_up:p=><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M7 17L17 7"/><path d="M7 7h10v10"/></svg>,
  arrow_dn:p=><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M7 7l10 10"/><path d="M17 7v10H7"/></svg>,
  ext:     p=><svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M7 17L17 7"/><path d="M7 7h10v10"/></svg>,
  alert:   p=><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>,
  menu:    p=><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" {...p}><path d="M3 6h18M3 12h18M3 18h18"/></svg>,
  check:   p=><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" {...p}><path d="M20 6L9 17l-5-5"/></svg>,
};

// ─── Primitives ───────────────────────────────────────────────────────────────
function Spark({data,w=84,h=22,color="currentColor",area=true}){
  if(!data||!data.length)return null;
  const min=Math.min(...data),max=Math.max(...data),span=max-min||1;
  const pts=data.map((v,i)=>{const x=(i/(data.length-1))*(w-2)+1,y=h-2-((v-min)/span)*(h-4);return[x,y];});
  const d=pts.map((p,i)=>(i===0?`M${p[0]},${p[1]}`:`L${p[0]},${p[1]}`)).join(" ");
  const aD=`${d} L${pts[pts.length-1][0]},${h} L${pts[0][0]},${h} Z`;
  return <svg className="spark" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden>
    {area&&<path d={aD} fill={color} opacity=".10"/>}
    <path d={d} fill="none" stroke={color} strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"/>
  </svg>;
}

function HBars({rows,max,fmt=v=>v}){
  if(!rows||!rows.length)return null;
  const m=max||Math.max(...rows.map(r=>r.value||0))||1;
  return <div style={{display:"flex",flexDirection:"column",gap:10}}>
    {rows.map(r=>(
      <div key={r.label} style={{display:"grid",gridTemplateColumns:"140px 1fr 56px",gap:14,alignItems:"center"}}>
        <div style={{fontSize:13,color:"var(--ink-2)",whiteSpace:"nowrap",overflow:"hidden",textOverflow:"ellipsis"}}>{r.label}</div>
        <div className="bar" style={{background:"var(--paper-2)"}}>
          <i style={{width:`${((r.value||0)/m)*100}%`,background:r.color||"var(--ink)"}}/>
        </div>
        <div className="mono tnum" style={{fontSize:12,color:"var(--ink-2)",textAlign:"right"}}>{fmt(r.value)}</div>
      </div>
    ))}
  </div>;
}

function BankAvatar({slug="",name="",isSber=false}){
  const letter=(name||slug||"?").charAt(0).toUpperCase();
  return <div style={{width:28,height:28,borderRadius:6,background:isSber?"var(--accent)":"var(--paper-2)",color:isSber?"#fff":"var(--ink-2)",border:"1px solid "+(isSber?"var(--accent)":"var(--hair-2)"),display:"grid",placeItems:"center",fontWeight:600,fontSize:12,fontFamily:"'JetBrains Mono',monospace",flexShrink:0}}>{letter}</div>;
}

// Полноэкранная заглушка для пустой БД с CTA-кнопкой запуска всех источников.
function EmptyOverviewCta(){
  const[running,setRunning]=useState(false);
  const[started,setStarted]=useState(false);
  const[err,setErr]=useState(null);
  const[sources,setSources]=useState([]);
  const[progress,setProgress]=useState({offers:0,banks:0,reviews:0,runs_total:0,runs_ok:0,runs_failed:0});

  useEffect(()=>{
    apiFetch("/api/sources").then(d=>setSources((d&&d.configured)||[])).catch(()=>{});
  },[]);

  // После старта — опрашиваем summary+sources каждые 3с;
  // как только в БД появляются данные, OverviewPage перерендерится сам
  // (родитель пересмотрит isEmpty при следующем mount/перезагрузке).
  useEffect(()=>{
    if(!started)return;
    const tick=async()=>{
      try{
        const[summary,src]=await Promise.all([
          apiFetch("/api/summary"),
          apiFetch("/api/sources"),
        ]);
        const runs=(src&&src.runs)||[];
        setProgress({
          offers: summary.offers||0,
          banks:  summary.banks||0,
          reviews:summary.reviews||0,
          runs_total: runs.length,
          runs_ok:    runs.filter(r=>r.status==="ok").length,
          runs_failed:runs.filter(r=>r.status==="failed").length,
        });
        // Если в БД появились данные — перезагружаем страницу,
        // чтобы родительский OverviewPage показал нормальный дашборд.
        if((summary.offers||0)>0||(summary.banks||0)>0){
          setTimeout(()=>window.location.reload(),800);
        }
      }catch{}
    };
    tick();
    const id=setInterval(tick,3000);
    return ()=>clearInterval(id);
  },[started]);

  const startAll=async()=>{
    setRunning(true);setErr(null);
    try{
      await apiPost("/api/ingest/run-all",{});
      setStarted(true);
    }catch(e){setErr(e.message||"Не удалось запустить");}
    setRunning(false);
  };

  const totalTargets=sources.reduce((s,c)=>s+(c.targets||[]).length,0);

  return <div className="fade-in" style={{padding:"40px 0"}}>
    <header style={{marginBottom:32}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Bank Audit Platform</div>
      <h1 className="t-display" style={{maxWidth:"22ch",marginBottom:14}}>
        База пуста — нужно <em style={{fontStyle:"italic",color:"var(--accent)"}}>собрать данные</em>
      </h1>
      <p className="lede" style={{maxWidth:"60ch"}}>
        Запустите парсинг всех настроенных источников. Сбор идёт в фоне, прогресс
        и история отображаются на странице «Источники». Это безопасно повторно —
        одинаковые снимки не дублируются (идемпотентность по sha256).
      </p>
    </header>

    <section className="surface" style={{padding:"32px 36px",marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:14}}>Готово к запуску</div>
      <div style={{display:"flex",gap:32,alignItems:"flex-end",flexWrap:"wrap",marginBottom:24}}>
        <div className="hero-metric">
          <div className="num"><em>{sources.length||"—"}</em></div>
          <div className="mono tnum" style={{fontSize:12,color:"var(--ink-3)",marginTop:4}}>
            настроенных источников
          </div>
        </div>
        <div className="hero-metric">
          <div className="num"><em>{totalTargets||"—"}</em></div>
          <div className="mono tnum" style={{fontSize:12,color:"var(--ink-3)",marginTop:4}}>
            целей сбора
          </div>
        </div>
      </div>

      {!started?<>
        <button className="btn" disabled={running} onClick={startAll}
          style={{background:"var(--accent)",color:"#fff",borderColor:"var(--accent)",
                  fontSize:14,padding:"12px 22px"}}>
          <Ic.refresh/> {running?"Запускаем…":"Запустить весь сбор"}
        </button>
        {err&&<p style={{color:"var(--neg)",fontSize:13,marginTop:10}}>{err}</p>}
        {sources.length===0&&<p style={{color:"var(--ink-3)",fontSize:12,marginTop:8}}>
          Список источников не загрузился (возможно, бэкенд старой версии — перезапустите FastAPI).
          Кнопка всё равно работает: бэк сам читает <code>config/sources.yaml</code>.
        </p>}
      </>:<div style={{padding:"14px 18px",background:"var(--paper-2)",border:"1px solid var(--hair)",borderRadius:8}}>
        <div style={{fontWeight:500,marginBottom:8,color:"var(--pos)"}}>✓ Сбор запущен — обновляется автоматически</div>
        <div style={{display:"flex",gap:24,flexWrap:"wrap",marginBottom:8}}>
          <div><span className="mono tnum" style={{fontWeight:500}}>{fmtNum(progress.offers)}</span> <span className="t-cap" style={{fontSize:11}}>предложений</span></div>
          <div><span className="mono tnum" style={{fontWeight:500}}>{fmtNum(progress.banks)}</span> <span className="t-cap" style={{fontSize:11}}>банков</span></div>
          <div><span className="mono tnum" style={{fontWeight:500}}>{fmtNum(progress.reviews)}</span> <span className="t-cap" style={{fontSize:11}}>отзывов</span></div>
          <div style={{borderLeft:"1px solid var(--hair)",paddingLeft:24}}>
            <span className="mono tnum" style={{fontWeight:500,color:"var(--pos)"}}>{progress.runs_ok}</span>
            {" / "}<span className="mono tnum">{progress.runs_total}</span>
            {progress.runs_failed>0&&<> · <span className="mono tnum" style={{color:"var(--neg)"}}>{progress.runs_failed} ошибок</span></>}
            <span className="t-cap" style={{fontSize:11}}> запусков</span>
          </div>
        </div>
        <p style={{fontSize:12,color:"var(--ink-3)",marginBottom:0}}>
          Раздел <strong>Источники</strong> покажет прогресс по каждому target'у и капчи (если появятся).
        </p>
      </div>}
    </section>

    {sources.length>0&&<section className="surface" style={{padding:"22px 24px"}}>
      <div className="eyebrow" style={{marginBottom:12}}>Будут запущены</div>
      <table>
        <thead><tr>
          <th>Источник</th><th>Сборщик</th><th className="right">Целей</th>
        </tr></thead>
        <tbody>
          {sources.map(s=>(
            <tr key={s.name}>
              <td className="mono" style={{fontWeight:500}}>{s.name}</td>
              <td><span className="badge">{s.collector}</span></td>
              <td className="right mono tnum">{(s.targets||[]).length}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>}
  </div>;
}

function EmptyState({text="Данных нет"}){
  return <div style={{padding:"64px 24px",textAlign:"center"}}>
    <div style={{display:"inline-flex",width:48,height:48,borderRadius:8,background:"var(--paper-2)",border:"1px solid var(--hair)",alignItems:"center",justifyContent:"center",marginBottom:12,color:"var(--ink-3)"}}>
      <Ic.search width="20" height="20"/>
    </div>
    <div style={{fontWeight:500,marginBottom:4}}>Ничего не найдено</div>
    <div className="t-cap" style={{maxWidth:"42ch",margin:"0 auto"}}>{text}</div>
  </div>;
}

function Skel({w="100%",h=16,style={}}){
  return <div className="skel" style={{width:w,height:h,...style}}/>;
}

function LoadingPage(){
  return <div className="fade-in" style={{display:"flex",flexDirection:"column",gap:16,paddingTop:8}}>
    <Skel h={28} w="45%"/>
    <Skel h={15} w="65%"/>
    <div style={{display:"grid",gridTemplateColumns:"7fr 5fr",gap:18,marginTop:8}}>
      <Skel h={140}/>
      <Skel h={140}/>
    </div>
    <Skel h={220}/>
    <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:18}}>
      <Skel h={180}/>
      <Skel h={180}/>
    </div>
  </div>;
}

function ErrState({msg}){
  return <div style={{padding:"64px 24px",textAlign:"center"}}>
    <div style={{fontSize:28,marginBottom:12,color:"var(--neg)"}}>⚠</div>
    <div style={{fontWeight:500,marginBottom:4}}>Ошибка загрузки</div>
    <div className="t-cap" style={{maxWidth:"42ch",margin:"0 auto"}}>{msg}</div>
  </div>;
}

function StatRow({label,value,delta,sub,warn,neg}){
  return <div style={{display:"flex",alignItems:"baseline",justifyContent:"space-between",gap:14}}>
    <div className="t-cap" style={{fontSize:12.5,color:"var(--ink-3)"}}>{label}</div>
    <div style={{textAlign:"right"}}>
      <div className="mono tnum" style={{fontSize:18,fontWeight:500,color:neg?"var(--neg)":warn?"var(--warn)":"var(--ink)"}}>{value}</div>
      {(delta||sub)&&<div className="t-cap" style={{fontSize:11,color:"var(--ink-3)"}}>{delta||sub}</div>}
    </div>
  </div>;
}

function PositionBar({value,median,max}){
  if(value==null)return <span className="mono" style={{color:"var(--ink-4)"}}>—</span>;
  const vals=[value,median,max].filter(v=>v!=null).map(parseFloat);
  const lo=Math.min(...vals)*0.96,hi=Math.max(...vals)*1.04;
  const pos=v=>((parseFloat(v)-lo)/(hi-lo))*100;
  return <div style={{position:"relative",height:18,minWidth:140}}>
    <div style={{position:"absolute",left:0,right:0,top:8,height:2,background:"var(--hair)",borderRadius:1}}/>
    {median!=null&&<div title={`Медиана ${pct(median)}`} style={{position:"absolute",left:`${pos(median)}%`,top:5,width:8,height:8,borderRadius:"50%",background:"var(--ink-4)",transform:"translateX(-50%)"}}/>}
    <div title={`Сбер ${pct(value)}`} style={{position:"absolute",left:`${pos(value)}%`,top:2,width:14,height:14,borderRadius:"50%",background:"var(--accent)",transform:"translateX(-50%)",boxShadow:"0 0 0 3px var(--surface)"}}/>
  </div>;
}

// ─── Markdown renderer ────────────────────────────────────────────────────────
// Trust tier для visual differentiation (academic-style):
//  t1 (high ≥0.85)  — обычный supscript
//  t2 (mid 0.55-)   — supscript с dotted underline
//  t3 (low <0.55)   — supscript янтарного цвета (warn)
function trustTier(score){
  const v=Number(score)||0;
  if(v>=0.85)return 1;
  if(v>=0.55)return 2;
  return 3;
}

function renderMD(text, sources, charts){
  if(!text) return null;
  const chartsArr = Array.isArray(charts) ? charts : [];
  const srcByN={};
  if(Array.isArray(sources)){for(const s of sources){if(s&&s.n!=null)srcByN[s.n]=s;}}
  const escAttr=(v)=>(v==null?"":String(v).replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;"));
  const renderCitation=(n)=>{
    const s=srcByN[n];
    if(!s||!s.url){
      // Невалидная цитата — quiet якорь (не открывает в новой вкладке)
      return `<sup><a href="#src-${n}" class="cite cite-anchor" data-cite="${n}">${n}</a></sup>`;
    }
    const tier=trustTier(s.trust_score);
    return `<sup><a href="${escAttr(s.url)}" target="_blank" rel="noopener noreferrer" `
         + `class="cite cite-t${tier}" data-cite="${n}">${n}</a></sup>`;
  };
  const inlineHTML=(s)=>s
    // markdown-ссылки [текст](url) → <a>. ДО citation-замены и emphasis.
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer" class="md-link">$1</a>')
    .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>")
    // __жирный__ (подчёркивания) — только на границах слова. NB: JS \w НЕ включает
    // кириллицу, поэтому класс слова задаём явно (иначе ломается имя_атрибута).
    .replace(/(^|[^A-Za-zА-Яа-яЁё0-9_])__([^_]+?)__(?![A-Za-zА-Яа-яЁё0-9])/g,'$1<strong>$2</strong>')
    .replace(/\*(.+?)\*/g,"<em>$1</em>")
    // _курсив_ (подчёркивания) — только на границах слова (с кириллицей)
    .replace(/(^|[^A-Za-zА-Яа-яЁё0-9_])_([^_]+?)_(?![A-Za-zА-Яа-яЁё0-9])/g,'$1<em>$2</em>')
    .replace(/`([^`]+)`/g,"<code class=\"md-code\">$1</code>")
    .replace(/~~(.+?)~~/g,"<s>$1</s>")
    .replace(/\[(\d{1,3})\]/g,(_,n)=>renderCitation(parseInt(n,10)))
    // Тонкий пробел между подряд идущими [N] чтобы они не сливались визуально
    // ([6][10][5] вместо «61015»)
    .replace(/<\/sup><sup>/g,"</sup> <sup>")
    .replace(/(расхождение[^.,;\n]*?)(\d+(?:[.,]\d+)?\s*(?:п\.п\.|пп|%))/gi,
             '<span class="dr-conflict">$1$2</span>')
    .replace(/⚠\s*(КОНФЛИКТ|РАСХОЖДЕНИЕ|ПРОТИВОРЕЧИЕ)([^.\n]{0,80})/gi,
             '<span class="dr-conflict">$1$2</span>');

  const lines=text.split("\n");
  let out=[],inTable=false,tableHead=[],tableRows=[],listBuf=[],listOrdered=false,bqBuf=[];
  // Slugify для anchor id заголовков (используется TOC)
  const slug=(s)=>String(s).toLowerCase()
    .replace(/[^а-яёa-z0-9\s]/gu,"").trim().replace(/\s+/g,"-").slice(0,50);
  const flushTable=()=>{
    if(!inTable)return;
    // Обёртка с горизонтальным скроллом: на сравнении 4+ банков (колонки=банки)
    // таблица раньше сплющивалась/обрезалась. Теперь контейнер скроллится.
    out.push(<div key={"tw"+out.length} className="dr-table-wrap" style={{overflowX:"auto",maxWidth:"100%"}}>
      <table style={{minWidth: tableHead.length>3 ? 640 : undefined}}>
      <thead><tr>{tableHead.map((h,i)=><th key={i} dangerouslySetInnerHTML={{__html:inlineHTML(h)}}/>)}</tr></thead>
      <tbody>{tableRows.map((row,i)=><tr key={i}>{row.map((c,j)=><td key={j} dangerouslySetInnerHTML={{__html:inlineHTML(c)}}/>)}</tr>)}</tbody>
    </table></div>);
    inTable=false;tableHead=[];tableRows=[];
  };
  const flushList=()=>{
    if(!listBuf.length)return;
    const Tag=listOrdered?"ol":"ul";
    out.push(<Tag key={"l"+out.length}>{listBuf.map((it,i)=><li key={i} dangerouslySetInnerHTML={{__html:inlineHTML(it)}}/>)}</Tag>);
    listBuf=[];
  };
  const flushQuote=()=>{
    if(!bqBuf.length)return;
    out.push(<blockquote key={"q"+out.length} className="dr-quote"
      dangerouslySetInnerHTML={{__html:bqBuf.map(inlineHTML).join("<br/>")}}/>);
    bqBuf=[];
  };
  lines.forEach((ln,idx)=>{
    // Inline-chart marker: [[CHART:N]] вставляет ChartCanvas прямо в поток
    // markdown'а. Используется в demo и backend-generated отчётах когда
    // нужно показать график между секциями, а не в конце.
    const chm = /^\s*\[\[CHART:(\d+)\]\]\s*$/.exec(ln);
    if(chm){
      flushList(); flushTable();
      const ci = parseInt(chm[1], 10);
      const spec = chartsArr[ci];
      if(spec){
        out.push(<div key={"ch"+idx} className="dr-chart-inline">
          <ChartCanvas spec={spec}/>
        </div>);
      }
      return;
    }
    // Цитата (> ...) — рендерим как blockquote (напр. «Источник дословно»).
    const bqm=/^>\s?(.*)$/.exec(ln);
    if(bqm){flushList();flushTable();bqBuf.push(bqm[1]);return;}
    flushQuote();   // любая не-цитатная строка завершает blockquote
    if(ln.startsWith("|")){
      const cells=ln.split("|").map(c=>c.trim()).filter((_,i,a)=>i>0&&i<a.length-1);
      if(/^[-:\s|]+$/.test(ln.replace(/\|/g,"")))return;
      flushList();
      if(!inTable){inTable=true;tableHead=cells;}else tableRows.push(cells);
      return;
    }else if(inTable)flushTable();

    const h4m=/^#{4,} (.+)/.exec(ln);
    const h3m=/^### (.+)/.exec(ln);
    const h2m=/^## (.+)/.exec(ln);
    const h1m=/^# (.+)/.exec(ln);
    // Заголовки рендерятся семантическими h1/h2/h3 — стили приходят из CSS .dr-doc-main
    if(h4m){flushList();out.push(<p key={idx} className="dr-doc-h4" dangerouslySetInnerHTML={{__html:inlineHTML(h4m[1])}}/>);return;}
    if(h3m){flushList();const t=h3m[1];out.push(<h3 key={idx} id={"h-"+slug(t)} dangerouslySetInnerHTML={{__html:inlineHTML(t)}}/>);return;}
    if(h2m){flushList();const t=h2m[1];out.push(<h2 key={idx} id={"h-"+slug(t)} dangerouslySetInnerHTML={{__html:inlineHTML(t)}}/>);return;}
    if(h1m){flushList();const t=h1m[1];out.push(<h1 key={idx} id={"h-"+slug(t)} dangerouslySetInnerHTML={{__html:inlineHTML(t)}}/>);return;}

    if(/^---+$/.test(ln.trim())){flushList();out.push(<hr key={idx}/>);return;}

    const olm=/^\d+\. (.+)/.exec(ln);
    if(olm){
      if(listBuf.length&&!listOrdered)flushList();
      listOrdered=true;
      listBuf.push(olm[1]);
      return;
    }
    if(/^[*\-•] /.test(ln)){
      if(listBuf.length&&listOrdered)flushList();
      listOrdered=false;
      listBuf.push(ln.slice(2));
      return;
    }
    flushList();
    if(!ln.trim())return;
    out.push(<p key={idx} dangerouslySetInnerHTML={{__html:inlineHTML(ln)}}/>);
  });
  flushTable();flushList();flushQuote();
  return out;
}

// ─── OVERVIEW PAGE ────────────────────────────────────────────────────────────
function OverviewPage(){
  const[data,setData]=useState(null);
  const[loading,setLoading]=useState(true);
  const[err,setErr]=useState(null);

  useEffect(()=>{
    setLoading(true);
    Promise.all([
      apiFetch("/api/summary"),
      apiFetch("/api/sber-vs-market"),
      apiFetch("/api/reviews/topics"),
      apiFetch("/api/quality"),
    ]).then(([summary,svm,topics,quality])=>{
      // Aggregate topics by topic name
      const topicMap={};
      (topics||[]).forEach(t=>{
        if(!topicMap[t.topic])topicMap[t.topic]={topic:t.topic,n:0,total_n:0,total_r:0};
        topicMap[t.topic].n+=parseInt(t.n)||0;
        topicMap[t.topic].total_n+=parseInt(t.n)||0;
        topicMap[t.topic].total_r+=(parseFloat(t.avg_rating)||0)*(parseInt(t.n)||0);
      });
      const aggTopics=Object.values(topicMap).map(t=>({
        topic:t.topic, n:t.n,
        avg_rating:t.total_n>0?t.total_r/t.total_n:0,
      })).sort((a,b)=>b.n-a.n);
      setData({summary,svm,topics:aggTopics,quality});
      setLoading(false);
    }).catch(e=>{setErr(e.message);setLoading(false);});
  },[]);

  if(loading)return <LoadingPage/>;
  if(err)return <ErrState msg={err}/>;
  const{summary,svm,topics,quality}=data;

  // Compute avg delta for hero
  const validDeltas=(svm||[]).filter(r=>r.sber_vs_median_pp!=null).map(r=>parseFloat(r.sber_vs_median_pp));
  const avgDelta=validDeltas.length?validDeltas.reduce((a,b)=>a+b,0)/validDeltas.length:null;
  const depositRow=(svm||[]).find(r=>r.category==="deposit");
  const flagsTotal=(summary.flags_err||0)+(summary.flags_warn||0);
  const now=new Date();
  const timeStr=now.toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})+" МСК";
  const issueNum=now.getWeek?now.getWeek():Math.ceil((now-new Date(now.getFullYear(),0,1))/(7*86400000));

  // База пуста — показываем CTA-блок с кнопкой запуска вместо обычного дашборда
  const isEmpty=(summary.offers||0)===0&&(summary.banks||0)===0&&(summary.reviews||0)===0;
  if(isEmpty)return <EmptyOverviewCta/>;

  return <div className="fade-in">
    <header style={{marginBottom:32}}>
      <div className="eyebrow-row">
        <div className="eyebrow">Issue {issueNum} · {now.toLocaleDateString("ru",{month:"long",year:"numeric"})} · Розничный рынок</div>
        <div className="mono tnum" style={{fontSize:11.5,color:"var(--ink-3)"}}>
          обновлено {timeStr} · {fmtNum(summary.offers)} предложений · {fmtNum(summary.banks)} банков
        </div>
      </div>
      <h1 className="t-display" style={{maxWidth:"24ch",marginBottom:14}}>
        Аналитика банковского рынка — позиция Сбера vs&nbsp;<em style={{fontStyle:"italic",color:"var(--accent)"}}>конкуренты</em>
      </h1>
      <p className="lede">
        Еженедельная сводка для службы внутреннего аудита: сравнение позиции Сбербанка
        с медианой и максимумом рынка, динамика условий и ключевые риск-сигналы.
      </p>
    </header>

    <section className="row row-7-5" style={{marginBottom:32}}>
      <div className="surface" style={{padding:"28px 32px"}}>
        <div className="eyebrow" style={{marginBottom:14}}>Главное · Сбер vs медиана рынка</div>
        <div style={{display:"flex",alignItems:"flex-end",gap:36,flexWrap:"wrap"}}>
          <div className="hero-metric">
            <div className="num"><em>{avgDelta!=null?signed(avgDelta):"—"}</em></div>
            <div className="mono tnum" style={{fontSize:12,color:"var(--ink-3)",marginTop:4}}>п.п. средневзвешенно по категориям</div>
          </div>
          <div style={{flex:1,minWidth:220,paddingBottom:8}}>
            <div className="t-cap" style={{marginBottom:14,maxWidth:"36ch"}}>
              {depositRow?`Вклады: Сбер ${pct(depositRow.sber_max)} · медиана ${pct(depositRow.market_median)} · лидер ${pct(depositRow.market_max)}`:"Данные по категориям загружены из базы."}
            </div>
            {depositRow&&<div style={{display:"flex",alignItems:"center",gap:14,flexWrap:"wrap"}}>
              <div>
                <div className="mono tnum" style={{fontSize:13,fontWeight:500}}>{pct(depositRow.sber_max)}</div>
                <div className="t-cap" style={{fontSize:11}}>Сбер · вклады макс.</div>
              </div>
              <div style={{width:1,height:28,background:"var(--hair)"}}/>
              <div>
                <div className="mono tnum" style={{fontSize:13,fontWeight:500}}>{pct(depositRow.market_median)}</div>
                <div className="t-cap" style={{fontSize:11}}>Рынок · медиана</div>
              </div>
              <div style={{width:1,height:28,background:"var(--hair)"}}/>
              <div>
                <div className="mono tnum" style={{fontSize:13,fontWeight:500}}>{pct(depositRow.market_max)}</div>
                <div className="t-cap" style={{fontSize:11}}>Лидер рынка</div>
              </div>
            </div>}
          </div>
        </div>
      </div>

      <div className="surface" style={{padding:"22px 24px",display:"flex",flexDirection:"column",gap:14}}>
        <div className="eyebrow">Состояние данных</div>
        <StatRow label="Активных предложений" value={fmtNum(summary.offers)}/>
        <StatRow label="Отзывов в анализе" value={fmtNum(summary.reviews)}/>
        <StatRow label="Изменений условий (7 дн.)" value={fmtNum(summary.changes)} warn={summary.changes>50}/>
        <StatRow label="Флаги качества" value={flagsTotal}
          sub={`${summary.flags_err||0} ош. · ${summary.flags_warn||0} предупр.`}
          neg={(summary.flags_err||0)>0}/>
        {summary.last_run&&<div className="t-cap" style={{fontSize:11,color:"var(--ink-4)",paddingTop:4}}>
          Последний сбор: {fmtDate(summary.last_run)}
        </div>}
        <div style={{marginTop:"auto",paddingTop:14,borderTop:"1px solid var(--hair)"}}>
          <button className="btn btn-ghost btn-sm" style={{width:"100%",justifyContent:"space-between"}}
            onClick={()=>window.location.hash="sources"}>
            Открыть журнал сборов <Ic.ext/>
          </button>
        </div>
      </div>
    </section>

    <section style={{marginBottom:36}}>
      <div className="eyebrow-row">
        <div>
          <div className="eyebrow" style={{marginBottom:6}}>§ 02 · Сравнение по категориям</div>
          <h2 className="t-h">Где Сбер сильнее, где отстаёт</h2>
        </div>
      </div>
      <div className="surface" style={{overflow:"hidden"}}>
        {(!svm||!svm.length)?<EmptyState text="Нет данных для сравнения. Запустите сбор данных."/>:
        <table>
          <thead><tr>
            <th style={{width:"24%"}}>Категория</th>
            <th className="right">Сбер макс.</th>
            <th className="right">Медиана</th>
            <th className="right">Лидер</th>
            <th>Позиция</th>
            <th className="right">Δ к медиане</th>
          </tr></thead>
          <tbody>
            {(svm||[]).map(r=>{
              const delta=r.sber_vs_median_pp!=null?parseFloat(r.sber_vs_median_pp):null;
              const lib=LOWER_IS_BETTER.has(r.category);
              const isPos=delta!=null?(lib?delta>0:delta>0):null;
              const catN=(summary.categories||[]).find(c=>c.category===r.category);
              return <tr key={r.category}>
                <td>
                  <div style={{fontWeight:500}}>{CAT_LABELS[r.category]||r.category}</div>
                  <div className="t-cap" style={{fontSize:11.5}}>{catN?fmtNum(catN.n):"—"} предложений</div>
                </td>
                <td className="right mono tnum" style={{fontWeight:500}}>{pct(r.sber_max)}</td>
                <td className="right mono tnum" style={{color:"var(--ink-2)"}}>{pct(r.market_median)}</td>
                <td className="right mono tnum" style={{color:"var(--ink-2)"}}>{pct(r.market_max)}</td>
                <td><PositionBar value={r.sber_max} median={r.market_median} max={r.market_max}/></td>
                <td className="right">
                  {delta==null?<span className="mono" style={{color:"var(--ink-4)"}}>—</span>:
                    <span className={`delta ${isPos?"pos":"neg"}`}>
                      {isPos?<Ic.arrow_up/>:<Ic.arrow_dn/>}
                      {signed(delta)} п.п.
                    </span>}
                </td>
              </tr>;
            })}
          </tbody>
        </table>}
      </div>
    </section>

    <section className="row row-2" style={{marginBottom:36}}>
      <div>
        <div className="eyebrow" style={{marginBottom:14}}>§ 03 · Сигналы качества</div>
        <div style={{display:"flex",flexDirection:"column",gap:10}}>
          {!(quality?.flags?.length)?
            <div className="surface-flat" style={{padding:"18px 20px",color:"var(--ink-3)",fontSize:13}}>
              <Ic.check style={{display:"inline",marginRight:8,color:"var(--pos)"}}/> Активных флагов нет
            </div>:
            (quality.flags||[]).slice(0,4).map((f,i)=>(
              <div key={i} className={`alert ${f.severity==="error"?"error":""}`}>
                <div className="a-icon"><Ic.alert/></div>
                <div style={{flex:1,minWidth:0}}>
                  <h4>{str(f.code).replace(/_/g," ")}</h4>
                  <p>{str(f.detail)}</p>
                  <div className="mono tnum" style={{fontSize:10.5,color:"var(--ink-4)",marginTop:4}}>{fmtDate(f.created_at)}</div>
                </div>
              </div>
            ))}
        </div>
      </div>
      <div>
        <div className="eyebrow" style={{marginBottom:14}}>§ 04 · Топ темы жалоб</div>
        <div className="surface" style={{padding:22}}>
          {!(topics?.length)?<div style={{color:"var(--ink-3)",fontSize:13,padding:"8px 0"}}>Нет данных по темам отзывов</div>:
          <HBars
            rows={topics.slice(0,6).map(t=>({
              label:TL(t.topic),value:t.n,
              color:t.avg_rating<2.5?"var(--accent)":"var(--ink-3)",
            }))}
            fmt={v=>fmtNum(v)}
          />}
          <div style={{marginTop:18,paddingTop:14,borderTop:"1px solid var(--hair)",display:"flex",alignItems:"center",justifyContent:"space-between"}}>
            <div className="t-cap">Красным — темы со средней оценкой ниже 2.5</div>
            <button className="btn btn-ghost btn-sm" onClick={()=>window.location.hash="reviews"}>К отзывам <Ic.ext/></button>
          </div>
        </div>
      </div>
    </section>
  </div>;
}

// ─── MARKET PAGE ──────────────────────────────────────────────────────────────
function MarketPage(){
  const[cat,setCat]=useState("deposit");
  const[q,setQ]=useState("");
  const[offers,setOffers]=useState([]);
  const[catStats,setCatStats]=useState([]);
  const[loading,setLoading]=useState(true);
  const[err,setErr]=useState(null);

  // Load category stats once
  useEffect(()=>{
    apiFetch("/api/market/categories").then(setCatStats).catch(()=>{});
  },[]);

  // Load offers on category change
  useEffect(()=>{
    setLoading(true);setErr(null);
    apiFetch(`/api/market?category=${cat}&limit=100`)
      .then(d=>{setOffers(d||[]);setLoading(false);})
      .catch(e=>{setErr(e.message);setLoading(false);});
  },[cat]);

  const showRate=!["card_debit","metals"].includes(cat);
  const filtered=(offers||[]).filter(r=>!q||
    (r.bank_name||"").toLowerCase().includes(q.toLowerCase())||
    (r.title||"").toLowerCase().includes(q.toLowerCase()));
  const bestRate=filtered.length&&showRate?Math.max(...filtered.map(r=>parseFloat(r.rate_pct)||0)):0;

  // Build tabs from catStats + CATS_ORDER
  const tabs=CATS_ORDER.map(id=>{
    const s=catStats.find(c=>c.category===id);
    return{id,label:CAT_LABELS[id]||id,n:s?s.total:null};
  });

  return <div className="fade-in">
    <header style={{marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Рынок · {loading?"…":filtered.length} предложений</div>
      <h1 className="t-h" style={{marginBottom:6}}>Действующие условия по категориям</h1>
      <p className="t-cap" style={{maxWidth:"68ch"}}>
        Снимок с агрегаторов sravni.ru и banki.ru. Идемпотентность по (банк, категория, external_id). Изменения хранятся как SCD2.
      </p>
    </header>

    <div className="filter-row">
      <div className="tab-row">
        {tabs.slice(0,5).map(c=>(
          <button key={c.id} className={`tab ${cat===c.id?"active":""}`} onClick={()=>setCat(c.id)}>
            {c.label}{c.n!=null&&<span className="n">{c.n}</span>}
          </button>
        ))}
        <select className="select" value={cat} onChange={e=>setCat(e.target.value)} style={{height:26,fontSize:12.5,marginLeft:4}}>
          {tabs.map(c=><option key={c.id} value={c.id}>{c.label}</option>)}
        </select>
      </div>
      <div className="search-wrap">
        <Ic.search/>
        <input className="input" placeholder="Поиск по банку или продукту…" value={q} onChange={e=>setQ(e.target.value)}/>
      </div>
    </div>

    <div className="surface" style={{overflow:"hidden"}}>
      {loading?<div style={{padding:32}}><Skel h={40}/><div style={{height:12}}/><Skel h={40}/><div style={{height:12}}/><Skel h={40}/></div>:
       err?<ErrState msg={err}/>:
       filtered.length===0?<EmptyState text="Нет предложений. Возможно, источник ещё не обновлялся."/>:
      <table>
        <thead><tr>
          <th style={{width:"22%"}}>Банк</th>
          <th>Продукт</th>
          {showRate&&<th className="right">Ставка</th>}
          {showRate&&<th>Vs лидер</th>}
          <th>Сумма</th>
          <th>Срок</th>
          <th></th>
        </tr></thead>
        <tbody>
          {filtered.map((r,i)=>{
            const isSber=!!r.is_sber;
            const rate=parseFloat(r.rate_pct);
            return <tr key={r.offer_id||i} className={isSber?"is-sber":""}>
              <td>
                <div style={{display:"flex",alignItems:"center",gap:10}}>
                  <BankAvatar slug={r.bank_slug} name={r.bank_name} isSber={isSber}/>
                  <div>
                    <div style={{fontWeight:500}}>{r.bank_name||r.bank_slug}</div>
                    {isSber&&<div className="t-cap" style={{fontSize:10.5,color:"var(--accent)",fontFamily:"'JetBrains Mono',monospace",letterSpacing:".06em"}}>СБЕР · ОБЪЕКТ АУДИТА</div>}
                  </div>
                </div>
              </td>
              <td>
                {r.url?<a href={r.url} target="_blank" rel="noopener" style={{color:"var(--ink)",borderBottom:"1px solid var(--hair-2)"}}>{r.title}</a>
                  :<span>{r.title}</span>}
              </td>
              {showRate&&<td className="right mono tnum" style={{fontWeight:500,fontSize:14}}>
                {r.rate_pct!=null?pct(r.rate_pct):"—"}
              </td>}
              {showRate&&<td>
                {bestRate>0&&rate>0?<div style={{display:"flex",alignItems:"center",gap:8}}>
                  <div className="bar" style={{flex:1,maxWidth:80}}>
                    <i style={{width:`${(rate/bestRate)*100}%`,background:isSber?"var(--accent)":"var(--ink-3)"}}/>
                  </div>
                  <span className="mono tnum" style={{fontSize:11,color:"var(--ink-3)"}}>{Math.round((rate/bestRate)*100)}%</span>
                </div>:<span className="mono" style={{color:"var(--ink-4)"}}>—</span>}
              </td>}
              <td className="mono tnum" style={{color:"var(--ink-2)",fontSize:12.5}}>{fmtAmount(r.amount_min,r.amount_max)}</td>
              <td className="mono tnum" style={{color:"var(--ink-2)",fontSize:12.5}}>{fmtTerm(r.term_months_min,r.term_months_max)}</td>
              <td className="right">
                {r.url&&<a href={r.url} target="_blank" rel="noopener" className="icon-btn" aria-label="Открыть"><Ic.ext/></a>}
              </td>
            </tr>;
          })}
        </tbody>
      </table>}
    </div>
  </div>;
}

// ─── SBER VS MARKET PAGE ──────────────────────────────────────────────────────
function SberPage(){
  const[data,setData]=useState(null);
  const[loading,setLoading]=useState(true);
  const[err,setErr]=useState(null);

  useEffect(()=>{
    setLoading(true);
    Promise.all([
      apiFetch("/api/sber-vs-market"),
      apiFetch("/api/sber-vs-market/top"),
    ]).then(([svm,top])=>{setData({svm,top});setLoading(false);})
      .catch(e=>{setErr(e.message);setLoading(false);});
  },[]);

  if(loading)return <LoadingPage/>;
  if(err)return <ErrState msg={err}/>;
  const{svm,top}=data;
  const withData=(svm||[]).filter(r=>r.sber_max!=null);
  const depositTop=(top||[]).filter(r=>r.category==="deposit").sort((a,b)=>parseFloat(b.rate_pct||0)-parseFloat(a.rate_pct||0));

  return <div className="fade-in">
    <header style={{marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Сбер / Рынок · разбор</div>
      <h1 className="t-h" style={{marginBottom:6}}>Позиция Сбербанка по категориям</h1>
      <p className="t-cap" style={{maxWidth:"68ch"}}>
        Дельта рассчитывается между лучшей ставкой Сбера и медианой рынка. Для кредитных продуктов меньше = лучше для клиента.
      </p>
    </header>

    {!withData.length?<EmptyState text="Нет данных для сравнения. Запустите сбор данных из раздела Источники."/>:<>
    <div className="row" style={{gridTemplateColumns:"repeat(auto-fit,minmax(260px,1fr))",marginBottom:28}}>
      {withData.map(r=>{
        const delta=r.sber_vs_median_pp!=null?parseFloat(r.sber_vs_median_pp):null;
        const lib=LOWER_IS_BETTER.has(r.category);
        const isPos=delta!=null?(lib?delta>0:delta>0):null;
        const barW=r.market_max?Math.min(100,(parseFloat(r.sber_max||0)/parseFloat(r.market_max))*100):50;
        return <div key={r.category} className="surface" style={{padding:"22px 24px"}}>
          <div className="eyebrow" style={{marginBottom:10}}>{CAT_LABELS[r.category]||r.category}</div>
          <div style={{display:"flex",alignItems:"baseline",gap:8,marginBottom:14}}>
            <div className="serif" style={{fontSize:44,lineHeight:1,color:isPos?"var(--pos)":"var(--accent)"}}>
              {delta!=null?signed(delta):"—"}
            </div>
            <div className="mono tnum" style={{fontSize:11,color:"var(--ink-3)"}}>п.п.</div>
          </div>
          <div className="bar accent" style={{marginBottom:12}}>
            <i style={{width:`${barW}%`,background:isPos?"var(--pos)":"var(--accent)"}}/>
          </div>
          <div style={{display:"flex",justifyContent:"space-between",fontSize:12.5,padding:"4px 0"}}>
            <span style={{color:"var(--ink-3)"}}>Сбер макс.</span>
            <span className="mono tnum" style={{fontWeight:500}}>{pct(r.sber_max)}</span>
          </div>
          <div style={{display:"flex",justifyContent:"space-between",fontSize:12.5,padding:"4px 0",borderTop:"1px solid var(--hair)"}}>
            <span style={{color:"var(--ink-3)"}}>Медиана рынка</span>
            <span className="mono tnum" style={{color:"var(--ink-2)"}}>{pct(r.market_median)}</span>
          </div>
          <div style={{display:"flex",justifyContent:"space-between",fontSize:12.5,padding:"4px 0",borderTop:"1px solid var(--hair)"}}>
            <span style={{color:"var(--ink-3)"}}>Лидер рынка</span>
            <span className="mono tnum" style={{color:"var(--ink-2)"}}>{pct(r.market_max)}</span>
          </div>
        </div>;
      })}
    </div>

    {depositTop.length>0&&<div className="surface" style={{overflow:"hidden"}}>
      <div style={{padding:"20px 24px",borderBottom:"1px solid var(--hair)"}}>
        <div className="eyebrow" style={{marginBottom:4}}>Топ предложений по доходности · вклады</div>
        <div className="t-cap">Сбер выделен и подсвечен. Сортировка по убыванию ставки.</div>
      </div>
      <table>
        <thead><tr>
          <th className="right" style={{width:"6%"}}>№</th>
          <th>Банк</th><th>Продукт</th>
          <th className="right">Ставка</th>
          <th>Срок</th>
        </tr></thead>
        <tbody>
          {depositTop.map((r,i)=>{
            const isSber=!!r.is_sber;
            return <tr key={i} className={isSber?"is-sber":""}>
              <td className="right mono tnum" style={{color:"var(--ink-3)",fontSize:12}}>{String(i+1).padStart(2,"0")}</td>
              <td><div style={{display:"flex",alignItems:"center",gap:10}}>
                <BankAvatar slug={r.bank_slug} name={r.bank_name} isSber={isSber}/>
                <span style={{fontWeight:500}}>{r.bank_name}</span>
              </div></td>
              <td>{r.title}</td>
              <td className="right mono tnum" style={{fontWeight:500}}>{pct(r.rate_pct)}</td>
              <td className="mono tnum" style={{color:"var(--ink-2)",fontSize:12.5}}>{fmtTerm(r.term_months_min,null)}</td>
            </tr>;
          })}
        </tbody>
      </table>
    </div>}
    </>}
  </div>;
}

// ─── REVIEWS PAGE ─────────────────────────────────────────────────────────────
function ReviewsPage(){
  const banks=useBanks();
  const[bankSlug,setBankSlug]=useState("");
  const[sent,setSent]=useState("all");
  const[topics,setTopics]=useState([]);
  const[sentiment,setSentiment]=useState([]);
  const[reviews,setReviews]=useState([]);
  const[loading,setLoading]=useState(true);
  const[rvLoading,setRvLoading]=useState(false);

  // Initial load: topics + sentiment
  useEffect(()=>{
    Promise.all([
      apiFetch("/api/reviews/topics"),
      apiFetch("/api/reviews/sentiment"),
    ]).then(([t,s])=>{
      const topicMap={};
      (t||[]).forEach(r=>{
        if(!topicMap[r.topic])topicMap[r.topic]={topic:r.topic,n:0,total_r:0,total_n:0};
        topicMap[r.topic].n+=parseInt(r.n)||0;
        topicMap[r.topic].total_r+=(parseFloat(r.avg_rating)||0)*(parseInt(r.n)||0);
        topicMap[r.topic].total_n+=parseInt(r.n)||0;
      });
      const agg=Object.values(topicMap).map(x=>({topic:x.topic,n:x.n,avg_rating:x.total_n>0?x.total_r/x.total_n:0})).sort((a,b)=>b.n-a.n);
      setTopics(agg);
      setSentiment(s||[]);
      setLoading(false);
    }).catch(()=>setLoading(false));
  },[]);

  // Load reviews on filter change
  useEffect(()=>{
    setRvLoading(true);
    const qs=bankSlug?`?bank_slug=${bankSlug}&limit=50`:"?limit=50";
    apiFetch(`/api/reviews/list${qs}`)
      .then(d=>{setReviews(d||[]);setRvLoading(false);})
      .catch(()=>setRvLoading(false));
  },[bankSlug]);

  const filteredReviews=sent==="all"?reviews:reviews.filter(r=>r.sentiment===sent);

  return <div className="fade-in">
    <header style={{marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Отзывы · {rvLoading?"…":filteredReviews.length} записей</div>
      <h1 className="t-h" style={{marginBottom:6}}>Голос клиента и темы жалоб</h1>
      <p className="t-cap" style={{maxWidth:"68ch"}}>
        Тональность размечена по словарям. Темы извлекаются по ключевым словам.
      </p>
    </header>

    <div className="filter-row">
      <select className="select" value={bankSlug} onChange={e=>setBankSlug(e.target.value)}>
        <option value="">Все банки</option>
        {banks.map(b=><option key={b.slug} value={b.slug}>{b.name}</option>)}
      </select>
      <div className="tab-row">
        {[["all","Все"],["neg","Негатив"],["neu","Нейтрально"],["pos","Позитив"]].map(([k,l])=>(
          <button key={k} className={`tab ${sent===k?"active":""}`} onClick={()=>setSent(k)}>{l}</button>
        ))}
      </div>
    </div>

    {!loading&&<div className="row row-2" style={{marginBottom:28}}>
      <div className="surface" style={{padding:22}}>
        <div className="eyebrow" style={{marginBottom:14}}>Темы жалоб (все банки)</div>
        {!topics.length?<div style={{color:"var(--ink-3)",fontSize:13}}>Нет данных</div>:
        <HBars
          rows={topics.slice(0,8).map(t=>({
            label:TL(t.topic),value:t.n,
            color:t.avg_rating<2.5?"var(--accent)":"var(--ink-3)",
          }))}
          fmt={v=>fmtNum(v)}
        />}
      </div>
      <div className="surface" style={{padding:22}}>
        <div className="eyebrow" style={{marginBottom:14}}>Тональность по банкам</div>
        {!sentiment.length?<div style={{color:"var(--ink-3)",fontSize:13}}>Нет данных</div>:
        <table style={{margin:"-4px"}}>
          <thead><tr><th>Банк</th><th className="right">Негатив %</th><th className="right">Всего</th></tr></thead>
          <tbody>
            {sentiment.slice(0,8).map((s,i)=>{
              const negPct=s.neg_pct!=null?Math.round(parseFloat(s.neg_pct)):null;
              return <tr key={i}>
                <td style={{fontWeight:500}}>{s.bank_name||s.name||"—"}</td>
                <td className="right">
                  {negPct!=null?<span className={`badge ${negPct>40?"neg":negPct>25?"warn":"pos"}`}>{negPct}%</span>:<span className="mono" style={{color:"var(--ink-4)"}}>—</span>}
                </td>
                <td className="right mono tnum" style={{color:"var(--ink-3)"}}>{fmtNum(s.total||s.total_reviews)}</td>
              </tr>;
            })}
          </tbody>
        </table>}
      </div>
    </div>}

    <div className="surface" style={{padding:"20px 28px"}}>
      <div className="eyebrow" style={{marginBottom:6}}>Лента отзывов</div>
      {rvLoading?<div style={{paddingTop:16}}><Skel h={80}/><div style={{height:8}}/><Skel h={80}/></div>:
       filteredReviews.length===0?<EmptyState text="Нет отзывов по выбранным фильтрам"/>:
       filteredReviews.map(r=>{
         const rating=Math.round(parseFloat(r.rating)||0);
         const isSber=!!r.is_sber;
         return <article key={r.review_id} className="review">
           <div className="review-head">
             <BankAvatar slug={r.bank_slug} name={r.bank_name} isSber={isSber}/>
             <div style={{fontWeight:500}}>{r.bank_name||r.bank_slug}</div>
             <span className={`badge ${r.sentiment==="neg"?"neg":r.sentiment==="pos"?"pos":""}`}>
               <span className="dot"/>
               {r.sentiment==="neg"?"негатив":r.sentiment==="pos"?"позитив":"нейтрально"}
             </span>
             {r.source&&<span className="badge">{r.source}</span>}
             <div style={{marginLeft:"auto",display:"flex",gap:4}}>
               {[1,2,3,4,5].map(i=>(
                 <span key={i} style={{width:8,height:8,borderRadius:1,background:i<=rating?"var(--ink)":"var(--hair-2)"}}/>
               ))}
             </div>
           </div>
           {r.title&&<div style={{fontWeight:500,fontSize:14.5,marginBottom:6}}>{r.title}</div>}
           <p className="review-text">{r.text_short||r.text}</p>
           <div className="review-meta">{fmtDate(r.posted_at)}</div>
         </article>;
       })}
    </div>
  </div>;
}

// ─── AI PAGE ──────────────────────────────────────────────────────────────────
// ─── Editorial helpers ─────────────────────────────────────────────────────
// Trust marks академического стиля (без цветовых dots). Пара символов
// в serif-шрифте: ●●○ для visual difference без цветового шума.
function TrustMarks({score}){
  const v=Number(score)||0;
  const tier = v>=0.85 ? "h" : v>=0.55 ? "m" : "l";
  const marks = v>=0.85 ? "●●●" : v>=0.55 ? "●●○" : v>0 ? "●○○" : "○○○";
  return <span className={`dr-trust-marks dr-trust-marks-${tier}`}
               title={`trust ${v.toFixed(2)}`}>{marks}</span>;
}

const SOURCE_KIND_LABELS = {
  bank_official: "Официальный сайт",
  regulator:     "Регулятор",
  government:    "Госструктура",
  legal_db:      "Юр. база",
  aggregator:    "Агрегатор",
  press:         "Пресса",
  analyst:       "Аналитика",
  forum:         "Форум",
  blog:          "Блог",
  sponsored:     "Реклама"
};
// Палитра графиков — 4 цвета editorial palette, без gradients
const SOURCE_KIND_COLORS = {
  bank_official: "var(--ink)",
  regulator:     "var(--ink)",
  aggregator:    "var(--ink-2)",
  press:         "var(--ink-2)",
  analyst:       "var(--ink-2)",
  forum:         "var(--ink-3)",
  blog:          "var(--ink-3)",
  sponsored:     "var(--warn)"
};
const formatRelDate=(iso)=>{
  if(!iso)return "";
  try{
    const d=new Date(iso);
    const diffH=(Date.now()-d.getTime())/3600000;
    if(diffH<1) return "только что";
    if(diffH<24) return `${Math.floor(diffH)} ч`;
    const diffD=Math.floor(diffH/24);
    if(diffD<30) return `${diffD} дн`;
    return d.toLocaleDateString("ru-RU",{year:"numeric",month:"short",day:"numeric"});
  }catch{return "";}
};
const domainOf=(url)=>{try{return new URL(url).hostname.replace(/^www\./,"");}catch{return "";}};

// ─── Citation tooltip — appears on hover with 200ms delay.
//     Premium: показываем не только метаданные, но и реальный excerpt
//     из источника — аудитор видит ТОЧНУЮ фразу которую видел synthesizer.
//     Это reproducibility-сигнал: цитата проверяема не «открой URL и читай
//     всё», а «вот точный фрагмент». ────────────────────────────────────
function CitationTooltip({source, anchor}){
  if(!source||!anchor)return null;
  const r = anchor.getBoundingClientRect();
  const excerpts = source.excerpts || [];
  // Высота зависит от наличия excerpts (с ними панель больше)
  const hasExcerpt = excerpts.length > 0;
  const estHeight = hasExcerpt ? 220 : 130;
  const above = r.top > estHeight + 20;
  const style = {
    left: Math.min(window.innerWidth - 440, Math.max(12, r.left - 180)),
    top: above ? r.top - 10 - estHeight : r.bottom + 10,
  };
  const kindLabel = SOURCE_KIND_LABELS[source.source_kind] || source.source_kind || "—";
  // Берём наиболее информативный excerpt — самый длинный
  const bestExcerpt = excerpts.length
    ? excerpts.reduce((a,b)=>a.length>=b.length?a:b)
    : null;
  return <div className="cite-tooltip show" style={style}>
    <div className="cite-tooltip-head">
      <span>[{source.n}] · {kindLabel}</span>
      {source.fetched_at && <span>{formatRelDate(source.fetched_at)}</span>}
    </div>
    {hasExcerpt
      ? <div className="cite-tooltip-body">«{bestExcerpt.slice(0,360)}{bestExcerpt.length>360?"…":""}»</div>
      : <div className="cite-tooltip-body" style={{opacity:.6}}>{source.bank_name || "—"}</div>}
    <div className="cite-tooltip-foot">
      {source.bank_name && <span>{source.bank_name} · </span>}
      <span>{domainOf(source.url)}</span>
      {source.headings_path && <span> · {source.headings_path.split(" > ").slice(-2).join(" › ")}</span>}
    </div>
  </div>;
}

// ─── Process trace (collapsed by default) ─────────────────────────────────
// Какой фазе принадлежит reasoning-стадия — панель «Ход мысли» активна ТОЛЬКО
// пока её стадия == текущей фазе (иначе conductor «вечно размышляет», а analyst
// не виден). Единый источник правды для ThinkingPanel.
const STAGE_PHASE = {conductor:"planning", analyst:"synthesizing",
                     critic:"synthesizing", repair:"synthesizing"};
const STAGE_ORDER = ["conductor", "analyst", "critic", "repair"];
const PHASE_LABELS = {
  planning:        "Планирование",
  discovery:       "Discovery источников",
  research:        "Сбор данных",
  synthesizing:    "Синтез отчёта",
  agent_iter_1:    "Уточнение (итерация 1)",
  agent_iter_2:    "Уточнение (итерация 2)",
  second_pass:     "Дополнительный pass",
  merging:         "Финальная сборка",
  post_processing: "Проверка и графики",
  verifying:       "Проверка чисел",
  charting:        "Графики",
};

// Длительные стадии где LLM думает молча — нужно явно показать
// «жив, идёт стадия X, обычно занимает Y секунд». Estimate'ы выровнены
// с реальными timeout'ами на бэке.
const LONG_STAGE_HINT = {
  synthesizing:    {estimate: 35, note: "LLM пишет первый драфт отчёта по собранным данным"},
  agent_iter_1:    {estimate: 75, note: "Ищет недостающее: 4 поисковых запроса параллельно + ингест найденных страниц"},
  agent_iter_2:    {estimate: 75, note: "Вторая итерация — уточняет оставшиеся пробелы"},
  merging:         {estimate: 90, note: "Финальная сборка: сливает черновик + все дополнения в один полный отчёт (5–8 тыс. символов)"},
  post_processing: {estimate: 25, note: "Параллельно: верификация чисел, генерация графиков, cross-validation цитат"},
  second_pass:     {estimate: 60, note: "Дополнительный pass — поднимает чанки которые synthesizer пропустил"},
};

// ─── PDF export button — premium A4 PDF через server-side Chromium.
//     Показывается только когда отчёт готов (>500 chars). Использует
//     меньшее визуальное вес чтобы не отвлекать от чтения, но всегда виден.
// Экспорт ПОЛНОЙ матрицы (CSV + JSON) — машиночитаемый артефакт со всем
// контекстом каждой клетки (значение/условия/сегмент/цитата/ступени/конфликт).
// «Полная картина без воды» для самостоятельной сверки аудитором (item 58).
function MatrixExportButton({matrix, question, streaming}){
  if(!matrix || !matrix.rows || !matrix.rows.length) return null;
  const csvCell = (c)=>{
    if(!c) return "";
    if(c.state==="no_data") return "нет данных (источник не прочитан)";
    if(c.state==="not_disclosed") return "не раскрыто";
    let s = `${c.value||""} ${c.unit||""}`.trim();
    const q = [];
    if(c.conditions&&c.conditions.length) q.push("условия: "+c.conditions.join("; "));
    if(c.qualifications) q.push(c.qualifications);
    if(c.exceptions&&c.exceptions.length) q.push("исключения: "+c.exceptions.join("; "));
    if(q.length) s += " ["+q.join(" — ")+"]";
    if(c.ladder&&c.ladder.length) s += " {ступени: "+c.ladder.map(m=>`${m.value}${m.unit||""}${m.conditions&&m.conditions.length?"("+m.conditions.join(";")+")":""}`).join(" / ")+"}";
    if(c.source_idx) s += ` [${c.source_idx}]`;
    if(c.conflict) s += " ⚠конфликт";
    return s;
  };
  const dl = (content, mime, ext)=>{
    const blob = new Blob([content], {type:mime});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `audit-matrix-${Date.now().toString(36)}.${ext}`;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  };
  const toCSV = ()=>{
    const esc = (v)=>`"${String(v==null?"":v).replace(/"/g,'""')}"`;
    const head = ["Параметр","core", ...matrix.banks.map(b=>b.name)];
    const lines = [head.map(esc).join(",")];
    for(const r of matrix.rows){
      const byBank = {}; (r.cells||[]).forEach(c=>byBank[c.bank]=c);
      lines.push([r.attribute, r.is_core?"да":"", ...matrix.banks.map(b=>csvCell(byBank[b.slug]))].map(esc).join(","));
    }
    dl("﻿"+lines.join("\n"), "text/csv;charset=utf-8", "csv");
  };
  const toJSON = ()=> dl(JSON.stringify({question, ...matrix}, null, 2), "application/json", "json");
  return <span className="dr-matrix-export" style={{display:"inline-flex",gap:6}}>
    <button className="btn-ghost" disabled={streaming} onClick={toCSV} title="Полная матрица в CSV (со всеми условиями и цитатами)">⬇ Матрица CSV</button>
    <button className="btn-ghost" disabled={streaming} onClick={toJSON} title="Полная матрица в JSON">JSON</button>
  </span>;
}

function PdfExportButton({question, report, sources, verification, claimCheck, streaming, charts, ranking, insights, gaps}){
  const [busy, setBusy] = useState(false);
  const handle = async () => {
    if(busy || streaming) return;
    setBusy(true);
    try {
      const auditId = `${Date.now().toString(36)}`;
      const resp = await fetch("/api/ai/export-pdf", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          question: question,
          report_md: report,
          sources: (sources || []).map(s => ({
            n: s.n, url: s.url, bank_name: s.bank_name, title: s.title,
            source_kind: s.source_kind, trust_score: s.trust_score,
            fetched_at: s.fetched_at, headings_path: s.headings_path,
            // Передаём дословную выдержку — чтобы в PDF под источником была
            // та же цитата-доказательство, что в тултипе UI (item 62).
            excerpts: s.excerpts,
          })),
          meta: {
            audit_id: auditId,
            verified: claimCheck?.verified || 0,
            // unverified может прийти массивом ({claim,issue}) ИЛИ числом (старый
            // формат) — считаем количество устойчиво в обоих случаях.
            unverified: Array.isArray(verification?.unverified)
              ? verification.unverified.length
              : (verification?.unverified || 0),
          },
          // Передаём verification отдельно — PDF рендерит его как styled-секцию
          // (то же что VerificationBanner в UI), а не как сырой markdown.
          verification: verification ? {
            unverified: (Array.isArray(verification.unverified)
              ? verification.unverified : []).map(u => ({
                claim: u.claim, issue: u.issue
              })),
          } : null,
          // Графики — передаём specs как они пришли через SSE, бэкенд
          // отрендерит их в PDF тем же Chart.js через offscreen Chromium.
          charts: charts || [],
          // Богатые виджеты UI — раньше терялись при экспорте. Теперь шлём их
          // в PDF (рейтинг-карточки, инсайты, пробелы, claim-check).
          ranking: ranking || null,
          insights: insights || [],
          gaps: gaps || null,
          claim_check: claimCheck ? {
            verified: claimCheck.verified || 0,
            dropped: claimCheck.dropped || 0,
          } : null,
        }),
      });
      if(!resp.ok) {
        const err = await resp.text();
        alert(`PDF generation failed: ${err.slice(0,200)}`);
        return;
      }
      const blob = await resp.blob();
      const url  = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `auditlens_${auditId}.pdf`;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(()=>URL.revokeObjectURL(url), 4000);
    } catch(e) {
      alert(`Ошибка экспорта: ${e.message}`);
    } finally { setBusy(false); }
  };
  return <button className="btn-export" onClick={handle}
                 disabled={busy || streaming}
                 title={streaming ? "Дождитесь окончания генерации отчёта" : "Скачать отчёт в PDF"}>
    {busy ? <>
      <span className="btn-export-spinner"/>
      <span>Готовим PDF…</span>
    </> : streaming ? <>
      <span style={{opacity:.5}}>·</span>
      <span>Скачать PDF</span>
    </> : <>
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
        <path d="M8 1v9m0 0L4.5 6.5M8 10l3.5-3.5M2 11.5V13a1 1 0 001 1h10a1 1 0 001-1v-1.5"
              stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
              strokeLinejoin="round"/>
      </svg>
      <span>Скачать PDF</span>
    </>}
  </button>;
}

// ─── Outline preview — TOC до текста, чтобы пользователь сразу видел план
//     отчёта. Минималистично: пронумерованные секции в 2-3 колонки. ────────
function OutlinePreview({sections}){
  if(!sections || !sections.length) return null;
  return <div className="dr-outline-preview">
    <span className="eyebrow">План отчёта</span>
    <ol>
      {sections.map((s,i)=>(
        <li key={i} className="pending">{s.title || s.kind}</li>
      ))}
    </ol>
  </div>;
}

// ─── Agent loop progress panel — итеративный agent самонаходит пробелы.
//     Главная фича DeepResearch'а: пользователь видит «iter 1: ищет
//     комиссии Альфы → нашёл +5 источников → iter 2: уточняет тарифы». ─────
function AgentPanel({iterations}){
  if(!iterations || !iterations.length) return null;
  const isRunning = iterations.some(it=>it.status==="running");
  return <div className={`dr-agent ${isRunning?"dr-agent-running":""}`}>
    <div className="dr-agent-head">
      <span className="dr-agent-mark">Iterative Agent</span>
      <span className="dr-agent-title">Уточнение отчёта по найденным пробелам</span>
      <span className="dr-agent-iter">
        итерация <b>{iterations.length}</b>{iterations.length<2 ? " / 2" : ""}
      </span>
    </div>
    <ul className="dr-agent-list">
      {iterations.flatMap((it,ii)=>(it.gaps||[]).map((g,gi)=>(
        <li key={`${ii}-${gi}`} className="dr-agent-gap">
          <span className="dr-agent-gap-arrow">{ii+1}.{gi+1}</span>
          <span className="dr-agent-gap-what">{g.what || g.query}</span>
          <span className="dr-agent-gap-q">{g.query}</span>
        </li>
      )))}
    </ul>
  </div>;
}

// ─── Claim-check meta-row: «12 фактов верифицировано · 7 отфильтровано».
//     Trust-сигнал — pipeline защитил аудитора от N галлюцинаций. ─────────
// ─── Ход размышления — живой стрим reasoning_content модели. Заполняет тихие
//     окна (planning/synthesizing/critic): reasoning приходит на 2-4с раньше
//     ответа и течёт инкрементально. text — СЫРОЙ ход мысли (на англ.), выводим
//     plain pre-wrap (НЕ markdown: XSS + мусорная разметка). Когда стадия
//     «додумала» (active=false) — сворачиваем в «Ход мысли · Nс». ───────────
function ThinkingPanel({text, stage, active}){
  const [open,setOpen]=useState(true);
  const ref=useRef(null), startRef=useRef(null), endRef=useRef(null);
  if(text && startRef.current==null) startRef.current=Date.now();
  useEffect(()=>{ const b=ref.current; if(b && (b.scrollHeight-b.scrollTop-b.clientHeight)<40) b.scrollTop=b.scrollHeight; },[text,open]);
  useEffect(()=>{ if(!active){ if(startRef.current&&!endRef.current) endRef.current=Date.now(); setOpen(false); } },[active]);
  if(!text) return null;
  const secs=startRef.current?Math.max(1,Math.round(((endRef.current||Date.now())-startRef.current)/1000)):0;
  const L={conductor:"Дирижёр размышляет",analyst:"Аналитик размышляет",critic:"Критик проверяет",repair:"Дорабатываю отчёт"};
  const head=active?(L[stage]||"Размышляю"):("Ход мысли · "+secs+"с");
  return (
    <div className={"dr-think"+(active?" dr-think-active":"")}>
      <div className="dr-think-head" onClick={()=>setOpen(o=>!o)}>
        {active&&<span className="dr-stage-pulse"/>}
        <span className="dr-think-label">{head}</span>
        <span className="dr-think-badge">EN · технический ход мысли</span>
        <span className="dr-think-toggle">{open?"▾":"▸"}</span>
      </div>
      {open&&<div className="dr-think-body" ref={ref}>{text}{active&&<span className="dr-think-caret"/>}</div>}
    </div>
  );
}

// ─── Модуль «asking» — clarification-воронка. Кликабельные варианты (single/
//     multi) + «другое» + free-text. Один экран, скип всегда доступен. ───────
function ClarifyCard({msg, onSubmit, onSkip}){
  const qs=msg.questions||[];
  const [sel,setSel]=useState({});
  const get=(id)=>sel[id]||{vals:[],other:"",otherOn:false};
  const toggle=(qq,label)=>setSel(s=>{
    const cur=get(qq.id); let vals=(cur.vals||[]).slice();
    if(qq.type==="single") vals=[label];
    else vals=vals.includes(label)?vals.filter(v=>v!==label):[...vals,label];
    return {...s,[qq.id]:{...cur,vals}};
  });
  const setText=(qq,txt)=>setSel(s=>({...s,[qq.id]:{...get(qq.id),vals:txt?[txt]:[]}}));
  const setOther=(qq,txt)=>setSel(s=>({...s,[qq.id]:{...get(qq.id),other:txt}}));
  const toggleOther=(qq)=>setSel(s=>{const c=get(qq.id);return {...s,[qq.id]:{...c,otherOn:!c.otherOn}};});
  const isAns=(qq)=>{const c=get(qq.id);return (c.vals&&c.vals.length)||(c.otherOn&&(c.other||"").trim());};
  const answered=qs.filter(isAns).length;
  const submit=()=>{
    const answers=qs.map(qq=>{const c=get(qq.id);
      return {question:qq.question, selected:(c.vals||[]).filter(Boolean), other:(c.otherOn?(c.other||"").trim():"")};
    }).filter(a=>a.selected.length||a.other);
    onSubmit(msg.question,msg.forceDeep,answers);
  };
  return <div className="clarify-card fade-in">
    <div className="clarify-head">
      <span className="dr-stage-pulse"/>
      <span className="eyebrow" style={{color:"var(--accent)"}}>Уточнение запроса · {qs.length} вопр.</span>
      <button className="clarify-x" onClick={()=>onSkip(msg.question,msg.forceDeep)} aria-label="Пропустить">✕</button>
    </div>
    <div className="clarify-sub">Ответьте, чтобы отчёт попал точно в цель — это займёт ~15 секунд.</div>
    {qs.map((qq,qi)=>{
      const c=get(qq.id);
      return <div className="clarify-q" key={qi}>
        <div className="clarify-q-t">{qq.question}</div>
        {qq.type==="text"
          ? <input className="clarify-input" placeholder="свой ответ…"
                   value={(c.vals&&c.vals[0])||""} onChange={e=>setText(qq,e.target.value)}/>
          : <div className="clarify-chips">
              {(qq.options||[]).map((o,oi)=>{
                const on=(c.vals||[]).includes(o.label);
                return <span key={oi} className={"clarify-chip "+qq.type+(on?" on":"")}
                             onClick={()=>toggle(qq,o.label)} title={o.hint||""}>
                  <span className="clarify-box">{on&&<Ic.check/>}</span>{o.label}
                  {o.recommended&&<span className="clarify-rec">реком.</span>}
                </span>;
              })}
              {qq.allow_other&&<span className={"clarify-chip dashed"+(c.otherOn?" on":"")}
                onClick={()=>toggleOther(qq)}>Другое…</span>}
            </div>}
        {qq.allow_other&&qq.type!=="text"&&c.otherOn&&
          <input className="clarify-input" style={{marginTop:"8px"}} placeholder="свой вариант"
                 value={c.other} onChange={e=>setOther(qq,e.target.value)}/>}
      </div>;
    })}
    <div className="clarify-foot">
      <span className="clarify-count">отвечено {answered} / {qs.length}</span>
      <button className="clarify-skip-btn" onClick={()=>onSkip(msg.question,msg.forceDeep)}>Пропустить</button>
      <button className="clarify-go" onClick={submit}>Уточнить и запустить ↗</button>
    </div>
  </div>;
}

// ─── Stage status — prominent banner для долгих LLM-этапов.
//     Показывает что pipeline жив, что именно делает прямо сейчас и
//     примерно сколько ждать. Прогресс-индикатор по времени (для merging
//     ещё показывает накопленные символы итогового отчёта). ───────────────
function StageStatusBanner({stage, currentPhase, mode}){
  // currentPhase — текущая phase из event'а; stage — последний stage_status
  // event если он был. Объединяем чтобы показать максимум данных.
  const longHint = LONG_STAGE_HINT[currentPhase] || LONG_STAGE_HINT[stage?.stage];
  const label   = stage?.label || PHASE_LABELS[currentPhase] || currentPhase;
  const detail  = stage?.detail || longHint?.note || "";
  const est     = stage?.estimate_s || longHint?.estimate || 30;
  const srvElapsed = stage?.progress_elapsed;
  // Локальный таймер: тикаем раз в секунду, чтобы счётчик ШЁЛ плавно между
  // серверными апдейтами (~2s). Сбрасываем при смене стадии; если сервер
  // прислал большее elapsed — подтягиваемся к нему (источник правды — сервер).
  const [localEl,setLocalEl]=useState(0);
  const stageKey=(stage?.stage||currentPhase||"")+"";
  const keyRef=useRef(stageKey);
  useEffect(()=>{ if(keyRef.current!==stageKey){ keyRef.current=stageKey; setLocalEl(srvElapsed||0); } },[stageKey]);
  useEffect(()=>{ if(srvElapsed!=null) setLocalEl(e=>Math.max(e,srvElapsed)); },[srvElapsed]);
  useEffect(()=>{ const id=setInterval(()=>setLocalEl(e=>e+1),1000); return ()=>clearInterval(id); },[]);
  // Хуки вызваны безусловно (правило hooks) — ранний выход уже после них.
  if(!stage && !currentPhase) return null;
  if(mode!=="deep") return null;
  // Если currentPhase не «длинный этап» и нет stage — не показываем баннер.
  if(!longHint && !stage) return null;
  // Фазы с собственными живыми панелями НЕ дублируем баннером:
  //   planning/synthesizing → ThinkingPanel (ход мысли), research → AgentsPanel.
  // Баннер остаётся только для тихих стадий без live-UI (merging/post_processing/
  // agent_iter и т.п.), где иначе «висит».
  if(["planning", "research", "synthesizing"].includes(currentPhase)) return null;
  // Таймер показываем для любой стадии с оценкой времени (estimate_s>0) либо
  // если сервер уже прислал elapsed. Иначе — «идёт» без чисел.
  const timed   = (stage?.estimate_s||0) > 0 || srvElapsed != null;
  const elapsed = timed ? localEl : null;
  // Прогресс-bar: elapsed / est (cap 100%), только при est>0.
  const pct = (elapsed != null && est > 0) ? Math.min(100, Math.round((elapsed/est)*100)) : null;
  return <div className="dr-stage-banner">
    <div className="dr-stage-line">
      <span className="dr-stage-pulse"/>
      <span className="dr-stage-label">{label}</span>
      <span className="dr-stage-est">
        {elapsed != null ? `${elapsed}s` : "идёт"} / ~{est}s
      </span>
    </div>
    {detail && <div className="dr-stage-detail">{detail}</div>}
    {pct != null && <div className="dr-stage-bar">
      <div className="dr-stage-bar-fill" style={{width:`${pct}%`}}/>
    </div>}
  </div>;
}

function ClaimCheckRow({claimCheck, verification, sourcesCount}){
  const cc = claimCheck || {};
  const ver = verification || {};
  const verified = cc.verified || 0;
  const dropped  = cc.dropped  || 0;
  const unver    = (ver.unverified||[]).length;
  // Не рендерим строку если совсем нет сигналов (start of stream)
  if(verified===0 && dropped===0 && unver===0 && !sourcesCount) return null;
  return <div className="dr-meta-row">
    {!!sourcesCount && <span className="dr-meta-pill">
      <span className="dot"/>{sourcesCount} источн.
    </span>}
    {(verified>0 || dropped>0) && <span className="dr-meta-pill ok">
      <span className="dot"/><b>{verified}</b> фактов верифицировано
    </span>}
    {dropped>0 && <span className="dr-meta-pill warn">
      <span className="dot"/><b>{dropped}</b> отфильтровано
        <span style={{color:"var(--ink-4)",marginLeft:6}}>(защита от галлюцинаций)</span>
    </span>}
    {unver>0 && <span className="dr-meta-pill warn">
      <span className="dot"/><b>{unver}</b> требуют ручной проверки
    </span>}
  </div>;
}

// ─── Панель параллельных агентов — живые карточки во время волны сбора.
//     Статус каждого (ждёт/ищет/читает/обдумывает/готов) движется по РЕАЛЬНЫМ
//     событиям agent_tool_call (не самоползущий бар). Показывается только в фазе
//     research; после неё итог берёт на себя ProcessTrace. ────────────────────
function _agentStatus(st){
  if(!st || !st.status || st.status==="pending") return {t:"ждёт", c:"var(--ink-4)", run:false};
  if(st.status==="done")  return {t:"готов", c:"var(--pos,#3fb950)", run:false};
  if(st.status==="error") return {t:"ошибка", c:"var(--warn)", run:false};
  const lt=st.live_tool;
  if(lt==="web_search"||lt==="semantic_search") return {t:"ищет", c:"var(--accent)", run:true};
  if(lt==="read_url") return {t:`читает · ${st.n_reads||0} стр`, c:"var(--accent)", run:true};
  if(lt==="run_sql") return {t:"запрос к БД", c:"var(--accent)", run:true};
  if(st.live_phase==="think") return {t:"обдумывает", c:"var(--accent)", run:true};
  return {t:"работает", c:"var(--accent)", run:true};
}
function AgentsPanel({plan, stepStates, phase}){
  if(!plan || !plan.length || phase!=="research") return null;
  return <div className="dr-agents">
    <div className="dr-agents-head"><span className="eyebrow">Агенты · {plan.length} параллельно</span></div>
    {plan.map((step,i)=>{
      const st=stepStates?.[step.n];
      const s=_agentStatus(st);
      return <div key={i} className={"dr-agent-card"+(s.run?" dr-agent-card-run":"")}>
        <span className="dr-agent-dot" style={{background:s.c}}/>
        <span className="dr-agent-name">{step.title}</span>
        {st?.model && <span className="dr-agent-model mono">{st.model}</span>}
        <span className="dr-agent-status mono" style={{color:s.c}}>{s.t}</span>
      </div>;
    })}
  </div>;
}

function ProcessTrace({plan, stepStates, phase, mode, sources, verification}){
  const[expanded,setExpanded]=useState(false);
  if(!plan || !plan.length){
    if(phase && mode==="deep"){
      return <div className="dr-phase-banner">{(PHASE_LABELS[phase]||phase)}…</div>;
    }
    return null;
  }
  const states = Object.values(stepStates||{});
  const done    = states.filter(s=>s?.status==="done").length;
  const errored = states.filter(s=>s?.status==="error").length;
  const total   = plan.length;
  const running = phase && phase!=="done" && (done<total || phase!=="charting");
  const sourcesN = (sources||[]).length;
  const unverif  = (verification?.unverified||[]).length;

  if(!expanded){
    return <div className="dr-trace">
      <span className="dr-trace-stat">Process: <strong>{done}/{total}</strong> шагов</span>
      <span className="dr-trace-stat">·  <strong>{sourcesN}</strong> источников</span>
      {errored>0 && <span className="dr-trace-stat">·  <strong style={{color:"var(--warn)"}}>{errored} ошибок</strong></span>}
      {verification && <span className="dr-trace-stat">· <strong style={{color:unverif?"var(--warn)":"var(--pos)"}}>{unverif?`${unverif} unverified`:"verified"}</strong></span>}
      {running && <span className="dr-trace-stat" style={{color:"var(--accent)"}}>·  {(PHASE_LABELS[phase]||phase)}…</span>}
      <span className="dr-trace-toggle" onClick={()=>setExpanded(true)}>expand</span>
    </div>;
  }
  return <div className="dr-trace dr-trace-expanded">
    <div style={{display:"flex",alignItems:"center",gap:14,flexWrap:"wrap"}}>
      <span className="dr-trace-stat">Process: <strong>{done}/{total}</strong> шагов</span>
      <span className="dr-trace-stat">· <strong>{sourcesN}</strong> источников</span>
      {running && <span className="dr-trace-stat" style={{color:"var(--accent)"}}>· {(PHASE_LABELS[phase]||phase)}…</span>}
      <span className="dr-trace-toggle" onClick={()=>setExpanded(false)}>collapse</span>
    </div>
    {plan.map((step,i)=>{
      const st=stepStates?.[step.n];
      const status=st?.status||"pending";
      // Step-grouping: virtual N (TR-/AG-/G-/E-) визуально отличается
      const sn = String(step.n||"");
      const grpCls = sn.startsWith("TR-") ? " dr-trace-row-tr"
                  : sn.startsWith("AG")    ? " dr-trace-row-ag"
                  : sn.startsWith("G-")    ? " dr-trace-row-g"
                  : sn.startsWith("E-")    ? " dr-trace-row-e"
                  : "";
      const numLabel = sn.startsWith("TR-") ? "💬"
                     : sn.startsWith("AG")  ? "🤖"
                     : sn.startsWith("G-")  ? "↻"
                     : sn.startsWith("E-")  ? "+"
                     : step.n;
      return <div key={i} className={`dr-trace-row dr-trace-row-${status}${grpCls}`}>
        <span className="dr-trace-num">{numLabel}</span>
        <span className="dr-trace-title">{step.title}</span>
        <span className="dr-trace-tool">{step.tool}</span>
        <span className="dr-trace-found">
          {(()=>{
            if(status==="running") return "…";
            if(st?.error) return "ошибка";
            const f = st?.found, u = st?.used;
            if(f==null && u==null) return "";
            if(f && u && u>f) return `+${f} нов. / ${u} всего`;
            if(f) return `+${f} ист.`;
            if(u) return `${u} (повтор)`;
            return "0 ист.";
          })()}
        </span>
      </div>;
    })}
  </div>;
}

// ─── Coverage banner — minimal single-line ────────────────────────────────
function CoverageBanner({coverage}){
  if(!coverage)return null;
  const{total_sources,high_trust,mid_trust,low_trust,warning}=coverage;
  const tone = warning ? "warn" : (high_trust>=2 ? "ok" : "");
  return <div className={`dr-coverage${tone?" dr-coverage-"+tone:""}`}>
    <span><strong>{total_sources}</strong> источников</span>
    <span><strong>{high_trust}</strong> высокий trust</span>
    <span><strong>{mid_trust}</strong> средний</span>
    {low_trust>0 && <span><strong>{low_trust}</strong> низкий</span>}
    {warning && <div className="dr-coverage-warning">{warning}</div>}
  </div>;
}

// ─── Verification banner — quiet ──────────────────────────────────────────
function VerificationBanner({verification}){
  if(!verification)return null;
  const u=verification.unverified||[];
  if(!u.length){
    return <div className="dr-verify dr-verify-ok">
      Numeric verification — все числовые утверждения подтверждены источниками.
    </div>;
  }
  return <div className="dr-verify dr-verify-warn">
    <div className="dr-verify-head">{u.length} утверждений требуют ручной проверки</div>
    <ul className="dr-verify-list">
      {u.map((it,i)=><li key={i}><strong>«{it.claim}»</strong> — {it.issue}</li>)}
    </ul>
  </div>;
}

// ─── Ranking widget — v2 §5c: рейтинг субъектов как first-class артефакт ──
function RankingWidget({ranking}){
  if(!ranking || !ranking.entries || ranking.entries.length===0) return null;
  const entries = [...ranking.entries].sort((a,b)=>(a.rank||99)-(b.rank||99));
  return <div className="dr-ranking">
    <div className="dr-ranking-head">
      <span className="dr-ranking-title">🏆 Рейтинг</span>
      {ranking.criterion && <span className="dr-ranking-criterion">{ranking.criterion}</span>}
    </div>
    <ol className="dr-ranking-list">
      {entries.map((e,i)=>{
        const cites = (e.evidence_ns||[]).map(n=>`[${n}]`).join("");
        return <li key={i} className={`dr-ranking-row${e.data_gap?" dr-ranking-gap":""}`}>
          <span className="dr-ranking-rank">{e.rank || i+1}</span>
          <span className="dr-ranking-body">
            <span className="dr-ranking-subject">
              {e.subject_label || e.subject}
              {typeof e.score==="number" &&
                <span className="dr-ranking-score">{e.score.toLocaleString("ru")} /10</span>}
              {e.data_gap && <span className="dr-ranking-dg">недостаточно данных</span>}
            </span>
            {e.rationale && <span className="dr-ranking-rationale">{e.rationale} {cites}</span>}
          </span>
        </li>;
      })}
    </ol>
  </div>;
}

// ─── Insights widget — v2 §5c: аналитические инсайты как first-class ──────
function InsightsWidget({insights}){
  if(!insights || insights.length===0) return null;
  return <div className="dr-insights">
    <div className="dr-insights-head">
      <span className="dr-insights-title">💡 Ключевые инсайты</span>
    </div>
    <ul className="dr-insights-list">
      {insights.map((it,i)=>{
        const cites = (it.evidence_ns||[]).map(n=>`[${n}]`).join("");
        return <li key={i} className="dr-insight">
          <span className="dr-insight-headline">{it.headline} {cites}</span>
          {it.explanation && <span className="dr-insight-explain">{it.explanation}</span>}
          {it.impact && <span className="dr-insight-impact">
            <span className="dr-insight-impact-label">Влияние:</span> {it.impact}
          </span>}
        </li>;
      })}
    </ul>
  </div>;
}

// ─── Editorial chart — palette: ink-первичные, без shadow ─────────────────
let _chartIdSeq = 1;
function ChartCanvas({spec, sources}){
  const ref=useRef();
  const idRef=useRef(`chart-${_chartIdSeq++}`);
  useEffect(()=>{
    if(!ref.current||!window.Chart||!spec)return;
    const ctx=ref.current.getContext("2d");
    // Editorial palette — 5 нейтральных тонов, sequential по тёмности.
    // Никакого кричащего красного: для аудит-отчёта цвет НЕ должен
    // прочитываться как «плохо/хорошо». Лидер = самый тёмный, остальные
    // светлее. Это даёт автоматическую визуальную иерархию.
    const palette = [
      "#16181d",  // ink — primary (обычно first entity = лидер)
      "#44464d",  // ink-2
      "#707075",  // ink-3
      "#9c9ea3",  // ink-4
      "#c4c6cc",  // ink-5
    ];
    // Для doughnut пускаем по этому же sequential — самый большой сегмент
    // будет самый тёмный (это работает естественно с правильной сортировкой).
    const horizontal = spec.chartType==="horizontalBar";
    const isDoughnut = spec.chartType==="doughnut";
    const isLine     = spec.chartType==="line";
    const datasets = (spec.datasets||[]).map((d,i)=>({
      ...d,
      backgroundColor: isDoughnut ? palette
                       : isLine ? `${palette[i%palette.length]}22`   // hex+alpha=22 (13%)
                       : palette[i%palette.length],
      borderColor:     palette[i%palette.length],
      borderWidth:     isLine ? 2 : 0,
      pointRadius:     isLine ? 3 : 0,
      pointBackgroundColor: palette[i%palette.length],
      tension:         isLine ? 0.25 : 0,
    }));
    // Data-labels плагин — рисуем значения прямо на барах (premium-эстетика)
    const fmtVal = (v)=>{
      if(v==null) return "";
      if(typeof v !== "number") return String(v);
      // Тысячные разделители, до 1 знака после запятой
      return v.toLocaleString("ru-RU", {maximumFractionDigits: 1});
    };
    const dataLabelsPlugin = {
      id:"valLabels",
      afterDatasetsDraw(chart){
        if(isLine || isDoughnut) return;
        const {ctx, scales} = chart;
        chart.data.datasets.forEach((ds, dsi)=>{
          const meta = chart.getDatasetMeta(dsi);
          meta.data.forEach((bar, i)=>{
            const v = ds.data[i];
            if(v==null) return;
            ctx.save();
            ctx.font = "500 10.5px 'JetBrains Mono', monospace";
            ctx.fillStyle = "#16181d";
            ctx.textAlign = horizontal ? "left" : "center";
            ctx.textBaseline = horizontal ? "middle" : "bottom";
            const text = fmtVal(v);
            if(horizontal){
              ctx.fillText(text, bar.x + 4, bar.y);
            }else{
              ctx.fillText(text, bar.x, bar.y - 4);
            }
            ctx.restore();
          });
        });
      },
    };
    const inst = new window.Chart(ctx, {
      type: horizontal ? "bar" : (isDoughnut ? "doughnut" : isLine ? "line" : "bar"),
      data: {labels: spec.labels||[], datasets},
      plugins: [dataLabelsPlugin],
      options: {
        indexAxis: horizontal ? "y" : "x",
        responsive: true, maintainAspectRatio: false,
        animation: {duration: 280, easing: "easeOutCubic"},
        layout: { padding: {top: isDoughnut ? 4 : 16, bottom: 4, left: 4, right: horizontal ? 36 : 8} },
        plugins: {
          legend: {
            display: datasets.length>1 || isDoughnut,
            position: isDoughnut ? "right" : "bottom",
            labels: {
              font:{size:11, family:"Geist, Inter, sans-serif"},
              color:"#44464d", boxWidth:10, boxHeight:10, padding:14,
              usePointStyle: true, pointStyle: "rect",
            },
          },
          title: {
            display: !!spec.title, text: spec.title,
            font: {size:13.5, weight:"600", family:"'Source Serif 4', Georgia, serif"},
            color: "#16181d", padding: {bottom: 14},
            align: "start",
          },
          tooltip: {
            intersect: false, backgroundColor: "#16181d",
            titleFont:{size:12, weight:"500"},
            bodyFont:{size:11.5, family:"Geist, sans-serif"},
            padding: 10, cornerRadius: 4,
            callbacks: {
              label: (item)=>` ${item.dataset.label||""}: ${fmtVal(item.parsed.y ?? item.parsed.x ?? item.parsed)}`,
            },
          },
        },
        scales: isDoughnut ? {} : {
          x: {
            ticks: {font:{size:10.5, family:"Geist, sans-serif"}, color:"#707075"},
            grid: {display: !horizontal, color:"#ebebed", lineWidth: 1, drawTicks: false},
            border: {display: false},
          },
          y: {
            beginAtZero: true,
            ticks: {font:{size:10.5, family:"Geist, sans-serif"}, color:"#707075"},
            grid: {display: horizontal, color:"#ebebed", lineWidth: 1, drawTicks: false},
            border: {display: false},
          },
        },
      },
    });
    return ()=>inst.destroy();
  },[spec]);
  return <div className="dr-chart">
    <canvas ref={ref} id={idRef.current}/>
    {spec.sourceCitations&&spec.sourceCitations.length>0&&
      <div className="dr-chart-cites">
        Источники:&nbsp;
        {spec.sourceCitations.map((n,i)=>(
          <React.Fragment key={i}>
            {i>0 && " "}
            <span className="cite cite-t1">[{n}]</span>
          </React.Fragment>
        ))}
      </div>}
  </div>;
}

// ─── ToolsTimeline (для quick-mode) — без emoji, monospace lineage ────────
const TOOL_LABELS = {
  get_market_offers:    "Рынок предложений",
  get_sber_vs_market:   "Сбер vs рынок",
  get_reviews_analysis: "Анализ отзывов",
  get_review_themes:    "Темы отзывов",
  get_bank_ratings:     "Рейтинги банков",
  get_change_history:   "История изменений",
  semantic_search:      "Поиск по документам",
  fetch_official:       "Запрос к источнику",
  run_sql:              "SQL-запрос",
};

function ToolsTimeline({tools, active}){
  if(!tools||!tools.length) return null;
  return <div className="tools-tl">
    {tools.map((t,i)=>{
      const lbl = TOOL_LABELS[t] || t;
      const isLast = i===tools.length-1;
      return <span key={i} className={`tools-tl-step${active&&isLast?" tools-tl-active":""}`}>
        <span className="tools-tl-label">{lbl}</span>
        {!isLast && <span className="tools-tl-arrow">·</span>}
      </span>;
    })}
  </div>;
}

// ─── TOC — auto-extracted from rendered headings, sticky left ─────────────
function TableOfContents({contentEl, activeId, onClick}){
  const[items,setItems]=useState([]);
  useEffect(()=>{
    if(!contentEl) return;
    const update=()=>{
      const hs = Array.from(contentEl.querySelectorAll("h2,h3"));
      setItems(hs.map(h=>({
        id: h.id, text: h.textContent.trim(),
        level: Number(h.tagName.slice(1)),
      })));
    };
    update();
    // Re-scan when content changes (streaming)
    const obs = new MutationObserver(update);
    obs.observe(contentEl,{childList:true,subtree:true,characterData:true});
    return ()=>obs.disconnect();
  },[contentEl]);
  if(!items.length) return null;
  return <nav className="dr-toc">
    <div className="dr-toc-h">Содержание</div>
    <ul>
      {items.map((it,i)=>{
        const num = (it.text.match(/^(\d+)\./) || [])[1];
        const display = num ? it.text.replace(/^\d+\.\s*/,"") : it.text;
        return <li key={i} style={it.level===3?{paddingLeft:14}:null}>
          <a className={`dr-toc-link${activeId===it.id?" active":""}`}
             href={`#${it.id}`}
             onClick={(e)=>{e.preventDefault();onClick&&onClick(it.id);}}>
            {num && <span className="dr-toc-num">{num}.</span>}
            <span>{display}</span>
          </a>
        </li>;
      })}
    </ul>
  </nav>;
}

// ─── Sources rail — sticky right column with bidirectional binding ────────
function SourcesRail({sources, activeN, onHover, onClick}){
  if(!sources||!sources.length)return null;
  const officialN  = sources.filter(s=>s.source_kind==="bank_official").length;
  const regulatorN = sources.filter(s=>s.source_kind==="regulator").length;
  return <aside className="dr-rail">
    <div className="dr-rail-h">
      <span>Источники · {sources.length}</span>
      {(officialN+regulatorN)>0 && <span style={{color:"var(--ink-3)"}}>{officialN+regulatorN} офиц.</span>}
    </div>
    <ul className="dr-rail-list">
      {sources.map((s,i)=>{
        const kind = s.source_kind || "unknown";
        const kindLabel = SOURCE_KIND_LABELS[kind] || kind;
        const isActive = String(activeN)===String(s.n);
        return <li key={i}>
          <a id={`src-${s.n}`} href={s.url||"#"} target="_blank" rel="noopener noreferrer"
             className={`dr-rail-item${isActive?" active":""}`}
             onMouseEnter={()=>onHover&&onHover(s.n)}
             onMouseLeave={()=>onHover&&onHover(null)}
             onClick={(e)=>{onClick&&onClick(s.n,e);}}>
            <div>
              <span className="dr-rail-num">{s.n}.</span>
              <span className="dr-rail-bank">{s.bank_name || kindLabel}</span>
            </div>
            <span className="dr-rail-domain">{domainOf(s.url)||"—"}</span>
            <div className="dr-rail-meta">
              <span>{kindLabel}</span>
              <TrustMarks score={s.trust_score}/>
              {s.fetched_at && <span>· {formatRelDate(s.fetched_at)}</span>}
            </div>
          </a>
        </li>;
      })}
    </ul>
  </aside>;
}

// Backward-compat: AIPage может рендерить SourcesRail в обоих режимах.
// Также сохраняем legacy SourcesPanel API для других мест (KnowledgePage uses SOURCE_KIND_COLORS)
function SourcesPanel({sources}){
  return <SourcesRail sources={sources}/>;
}

// ─── DocTocSlot: автоматическое оглавление из ближайшего .dr-doc-main ────
// Sticky левая колонка. Подписывается на MutationObserver когда контент стримится.
function DocTocSlot(){
  const ref = useRef();
  const[items,setItems]=useState([]);
  const[activeId,setActiveId]=useState(null);

  useEffect(()=>{
    if(!ref.current)return;
    // Найдём sibling .dr-doc-main в том же .dr-doc
    const slot = ref.current;
    const findMain = ()=> slot.parentElement?.querySelector(".dr-doc-main");
    const update = ()=>{
      const main = findMain();
      if(!main){setItems([]);return;}
      const hs = Array.from(main.querySelectorAll("h2,h3"));
      setItems(hs.map(h=>({
        id: h.id, text: h.textContent.trim(),
        level: Number(h.tagName.slice(1)),
      })));
    };
    update();
    const main = findMain();
    if(main){
      const obs = new MutationObserver(update);
      obs.observe(main,{childList:true,subtree:true,characterData:true});
      // Active section через scroll
      const onScroll=()=>{
        const hs = Array.from(main.querySelectorAll("h2,h3"));
        const top = window.scrollY + 110;
        let cur = null;
        for(const h of hs){
          if(h.getBoundingClientRect().top + window.scrollY <= top) cur = h.id;
        }
        setActiveId(cur);
      };
      window.addEventListener("scroll",onScroll,{passive:true});
      onScroll();
      return ()=>{obs.disconnect();window.removeEventListener("scroll",onScroll);};
    }
  },[]);

  if(!items.length) return <div className="dr-doc-toc" ref={ref}/>;

  return <div className="dr-doc-toc" ref={ref}>
    <nav className="dr-toc">
      <div className="dr-toc-h">Содержание</div>
      <ul>
        {items.map((it,i)=>{
          const num = (it.text.match(/^(\d+)\./) || [])[1];
          const display = num ? it.text.replace(/^\d+\.\s*/,"") : it.text;
          return <li key={i} style={it.level===3?{paddingLeft:14}:null}>
            <a className={`dr-toc-link${activeId===it.id?" active":""}`}
               href={`#${it.id}`}
               onClick={(e)=>{e.preventDefault();
                 document.getElementById(it.id)?.scrollIntoView({behavior:"smooth",block:"start"});
               }}>
              {num && <span className="dr-toc-num">{num}.</span>}
              <span>{display}</span>
            </a>
          </li>;
        })}
      </ul>
    </nav>
  </div>;
}

// Чтобы Sources rail-slot тоже был частью .dr-doc grid, обёртка-div
function DocRailSlot({children}){
  return <div className="dr-doc-rail">{children}</div>;
}

// ─── Keyboard shortcuts overlay (?) ────────────────────────────────────────
const KBD_SHORTCUTS = [
  {keys:["?"],          action:"Показать эту справку"},
  {keys:["/"],          action:"Фокус в поле ввода"},
  {keys:["⌘","K"],      action:"Command palette"},
  {keys:["J"],          action:"Следующая секция"},
  {keys:["K"],          action:"Предыдущая секция"},
  {keys:["G","G"],      action:"К началу отчёта"},
  {keys:["["],          action:"Предыдущая цитата"},
  {keys:["]"],          action:"Следующая цитата"},
  {keys:["Enter"],      action:"Открыть источник цитаты в новой вкладке"},
  {keys:["S"],          action:"Скрыть/показать панель источников"},
  {keys:["T"],          action:"Скрыть/показать оглавление"},
  {keys:["⌘","P"],      action:"Печать / экспорт PDF"},
  {keys:["Esc"],        action:"Закрыть окно"},
];
function KbdHelp({onClose}){
  return <div className="kbd-help" onClick={onClose}>
    <div className="kbd-help-card" onClick={(e)=>e.stopPropagation()}>
      <h3>Горячие клавиши</h3>
      {KBD_SHORTCUTS.map((row,i)=>(
        <div key={i} className="kbd-help-row">
          <span>{row.action}</span>
          <span className="kbd-help-keys">
            {row.keys.map((k,j)=><kbd key={j}>{k}</kbd>)}
          </span>
        </div>
      ))}
    </div>
  </div>;
}

function AIPage(){
  const[msgs,setMsgs]=useState([
    {role:"ai",text:"Здравствуйте. Я ИИ-аналитик AuditLens, подключён к базе предложений и отзывов. Спросите о позиции Сбера, рисках по продуктам или динамике ставок. Для глубокого исследования включите Deep Research.",tools:[]}
  ]);
  const[q,setQ]=useState("");
  const[loading,setLoading]=useState(false);
  const[deepMode,setDeepMode]=useState(false);
  const[showKbd,setShowKbd]=useState(false);
  const[hoverCite,setHoverCite]=useState(null);          // {n, anchor} для tooltip
  const[activeCite,setActiveCite]=useState(null);        // подсветка bidirectional
  const[hideRail,setHideRail]=useState(false);
  const[hideToc,setHideToc]=useState(false);
  const feedRef=useRef();
  const inputRef=useRef();
  const msgsRef=useRef(msgs);
  useEffect(()=>{msgsRef.current=msgs;},[msgs]);
  // Автоскролл «прилипает к низу» ТОЛЬКО если пользователь уже внизу. Листаешь
  // вверх — не перебиваем (раньше каждый чанк/источник утаскивал вьюпорт вниз).
  const stickRef=useRef(true);
  useEffect(()=>{
    const el=feedRef.current; if(!el) return;
    if(stickRef.current) el.scrollTop=el.scrollHeight;  // мгновенно, без рывка smooth
  },[msgs,loading]);
  useEffect(()=>{
    const el=feedRef.current; if(!el) return;
    const onScroll=()=>{ stickRef.current=(el.scrollHeight-el.scrollTop-el.clientHeight)<120; };
    el.addEventListener("scroll",onScroll,{passive:true});
    return ()=>el.removeEventListener("scroll",onScroll);
  },[]);

  // ── Citation hover tooltip + bidirectional binding ──
  useEffect(()=>{
    const onOver=(e)=>{
      const a = e.target.closest && e.target.closest(".cite[data-cite]");
      if(!a)return;
      const n = Number(a.dataset.cite);
      // Найдём latest message с sources содержащим этот N
      const msg = [...msgsRef.current].reverse().find(m=>(m.sources||[]).some(s=>s.n===n));
      const src = msg?.sources?.find(s=>s.n===n);
      if(src) setHoverCite({n, anchor:a, source:src});
      setActiveCite(n);
    };
    const onOut=(e)=>{
      const a = e.target.closest && e.target.closest(".cite[data-cite]");
      if(a){setHoverCite(null);setActiveCite(null);}
    };
    document.addEventListener("mouseover",onOver);
    document.addEventListener("mouseout",onOut);
    return ()=>{document.removeEventListener("mouseover",onOver);document.removeEventListener("mouseout",onOut);};
  },[]);

  // ── Keyboard shortcuts (J/K/G/[/]/?/Esc/S/T/⌘P/⌘K/etc) ──
  useEffect(()=>{
    const isInput=(el)=>el && (el.tagName==="INPUT" || el.tagName==="TEXTAREA" || el.isContentEditable);
    const onKey=(e)=>{
      if(e.key==="Escape"){
        if(showKbd){setShowKbd(false);return;}
      }
      if(isInput(e.target) && !(e.metaKey||e.ctrlKey)) return;
      if(e.key==="?"){e.preventDefault();setShowKbd(s=>!s);return;}
      if(e.key==="/"){e.preventDefault();inputRef.current?.focus();return;}
      if(e.key==="s"||e.key==="S"){setHideRail(v=>!v);return;}
      if(e.key==="t"||e.key==="T"){setHideToc(v=>!v);return;}
      if(e.key==="j"||e.key==="J"||e.key==="k"||e.key==="K"){
        const dir = (e.key==="j"||e.key==="J")?1:-1;
        const headings = Array.from(feedRef.current?.querySelectorAll(".dr-doc-main h1, .dr-doc-main h2, .dr-doc-main h3")||[]);
        if(!headings.length)return;
        const top = window.scrollY+90;
        const idx = headings.findIndex(h=>h.getBoundingClientRect().top+window.scrollY>top);
        const target = dir===1
          ? headings[idx===-1?headings.length-1:idx]
          : headings[Math.max(0, (idx===-1?headings.length:idx)-2)];
        target?.scrollIntoView({behavior:"smooth",block:"start"});
        return;
      }
      if(e.key==="["||e.key==="]"){
        const dir = e.key==="]"?1:-1;
        const cites = Array.from(feedRef.current?.querySelectorAll(".cite[data-cite]")||[]);
        if(!cites.length)return;
        const top = window.scrollY+100;
        const idx = cites.findIndex(c=>c.getBoundingClientRect().top+window.scrollY>top);
        const target = dir===1
          ? cites[idx===-1?cites.length-1:idx]
          : cites[Math.max(0, (idx===-1?cites.length:idx)-2)];
        target?.focus();
        target?.scrollIntoView({behavior:"smooth",block:"center"});
        return;
      }
    };
    window.addEventListener("keydown",onKey);
    return ()=>window.removeEventListener("keydown",onKey);
  },[showKbd]);

  const streamChat=async(question,history,forceDeep)=>{
    try{
      const res=await fetch("/api/ai/analyze",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({question,history,force_deep:forceDeep}),
      });
      if(!res.ok){
        const errData=await res.json().catch(()=>({detail:res.statusText}));
        setMsgs(m=>{const u=[...m];u[u.length-1]={...u[u.length-1],text:`⚠ Ошибка ${res.status}: ${errData.detail||res.statusText}`};return u;});
        return;
      }
      const reader=res.body.getReader();
      const dec=new TextDecoder();
      let buf="";
      const updateLast=(patch)=>setMsgs(m=>{const u=[...m],last=u[u.length-1];u[u.length-1]={...last,...patch(last)};return u;});
      outer: while(true){
        const{done,value}=await reader.read();
        if(done)break;
        buf+=dec.decode(value,{stream:true}).replace(/\r/g,"");
        const parts=buf.split("\n\n");
        buf=parts.pop()||"";
        for(const part of parts){
          for(const line of part.split("\n")){
            if(!line.startsWith("data: "))continue;
            try{
              const data=JSON.parse(line.slice(6));
              if(data.type==="text"&&data.chunk){
                updateLast(last=>({text:(last.text||"")+data.chunk}));
              }else if(data.type==="reasoning"){
                // Живой ход мысли LLM (delta.reasoning_content). Копим ПО СТАДИЯМ
                // (reasoningStages[stage]) — иначе таймер «Ход мысли · Nс» суммирует
                // время всех стадий. Текст — plain (НЕ markdown: сырой thinking).
                if(data.reset){
                  // Стадия ретраится (транзиент) — чистим её буфер, не задваиваем.
                  updateLast(last=>{
                    const st=data.stage||last.reasoningStage||"?";
                    return {reasoningStages:{...(last.reasoningStages||{}),[st]:""}};
                  });
                }else if(data.chunk){
                  updateLast(last=>{
                    const st=data.stage||last.reasoningStage||"?";
                    const stages={...(last.reasoningStages||{})};
                    stages[st]=(stages[st]||"")+data.chunk;
                    return {reasoningStages:stages, reasoningStage:st};
                  });
                }
              }else if(data.type==="report_replace"&&typeof data.text==="string"){
                // Final merge-pass — синтезатор объединил draft + addendum'ы в
                // один чистый отчёт. Заменяем весь body, отчёт перерендерится.
                updateLast(()=>({text:data.text, merged:true}));
              }else if(data.type==="tool_call"){
                updateLast(last=>({tools:[...(last.tools||[]),data.name]}));
              }else if(data.type==="sources"&&Array.isArray(data.sources)){
                updateLast(()=>({sources:data.sources}));
              }else if(data.type==="mode"){
                updateLast(()=>({mode:data.value}));
              }else if(data.type==="phase"){
                updateLast(()=>({phase:data.value}));
              }else if(data.type==="plan"&&Array.isArray(data.steps)){
                updateLast(()=>({plan:data.steps,stepStates:{}}));
              }else if(data.type==="step_start"){
                updateLast(last=>({
                  stepStates:{...(last.stepStates||{}),[data.n]:{status:"running",title:data.title,tool:data.tool,entity:data.entity}}
                }));
              }else if(data.type==="agent_tool_call"){
                // Живой статус агента: какой инструмент сейчас, сколько прочитано.
                updateLast(last=>({
                  stepStates:{...(last.stepStates||{}),[data.n]:{
                    ...(last.stepStates?.[data.n]||{}),
                    live_tool:data.tool, live_phase:data.phase,
                    n_reads:data.n_reads, calls:data.calls, model:data.model,
                    entity:data.entity ?? last.stepStates?.[data.n]?.entity,
                  }}
                }));
              }else if(data.type==="step_done"){
                updateLast(last=>({
                  stepStates:{...(last.stepStates||{}),[data.n]:{
                    ...(last.stepStates?.[data.n]||{}),
                    status: data.error ? "error" : "done",
                    found: data.found, used: data.used, error: data.error,
                  }}
                }));
              }else if(data.type==="coverage"){
                updateLast(()=>({coverage:data}));
              }else if(data.type==="matrix"&&data.data){
                // Полная матрица для машиночитаемого экспорта (CSV/JSON).
                updateLast(()=>({matrix:data.data}));
              }else if(data.type==="gaps"){
                updateLast(()=>({gaps:data}));
              }else if(data.type==="verification"){
                updateLast(()=>({verification:data}));
              }else if(data.type==="stage_status"){
                // Длинная стадия (merging / agent_iter / post_processing) —
                // показываем её отдельным prominent banner'ом чтобы пользователь
                // видел что pipeline жив и сколько примерно ждать.
                updateLast(()=>({stageStatus:data}));
              }else if(data.type==="merge_progress"){
                // Прогресс финальной сборки — счётчик символов, видимый юзеру
                updateLast(last=>({stageStatus:{
                  ...(last.stageStatus||{}),
                  stage:"merging",
                  label:"Финальная сборка отчёта",
                  detail:`Накоплено ${data.chars} символов, прошло ${data.elapsed_s}s`,
                  progress_chars:data.chars,
                  progress_elapsed:data.elapsed_s,
                }}));
              }else if(data.type==="claim_check"){
                // P0.2: счётчик «верифицировано/отфильтровано» — показывает
                // что pipeline защитил от N галлюцинаций. Trust-сигнал.
                updateLast(()=>({claimCheck:data}));
              }else if(data.type==="outline"&&Array.isArray(data.sections)){
                // Адаптивный outline ДО текста — TOC появляется сразу,
                // пользователь видит куда поедет отчёт.
                updateLast(()=>({outline:data.sections}));
              }else if(data.type==="agent_gaps"){
                // Iterative agent loop: сам нашёл пропуски и пошёл их искать.
                updateLast(last=>({
                  agentIters:[...(last.agentIters||[]),
                    {iteration:data.iteration, gaps:data.gaps||[], status:"running"}]
                }));
              }else if(data.type==="phase"&&typeof data.value==="string"
                       && data.value.startsWith("agent_iter_")){
                // Завершение текущей итерации — отметить как «done»
                updateLast(last=>{
                  const iters=[...(last.agentIters||[])];
                  if(iters.length){iters[iters.length-1]={...iters[iters.length-1],status:"done"};}
                  return {agentIters:iters, phase:data.value};
                });
              }else if(data.type==="chart"&&data.spec){
                updateLast(last=>({charts:[...(last.charts||[]),data.spec]}));
              }else if(data.type==="ranking"&&data.entries){
                // v2 §5c: рейтинг субъектов — first-class артефакт (replace,
                // как coverage/verification). Рендерится отдельным виджетом.
                updateLast(()=>({ranking:data}));
              }else if(data.type==="insights"&&Array.isArray(data.items)){
                updateLast(()=>({insights:data.items}));
              }else if(data.type==="done"){
                break outer;
              }
            }catch{}
          }
        }
      }
      setMsgs(m=>{
        const u=[...m],last=u[u.length-1];
        if(last.role==="ai"&&!last.text)u[u.length-1]={...last,text:"(модель не вернула текст — попробуйте переформулировать запрос)"};
        return u;
      });
    }catch(e){
      setMsgs(m=>{const u=[...m];u[u.length-1]={...u[u.length-1],text:`⚠ Ошибка соединения: ${e.message}`};return u;});
    }finally{
      setLoading(false);
    }
  };

  // Запуск research: ai-bubble + стрим. История БЕЗ clarify-сообщений.
  const runSend=(t,forceDeep)=>{
    const history=msgsRef.current
      .filter(m=>m.role==="user"||m.role==="ai")
      .map(m=>({role:m.role==="user"?"user":"assistant",content:m.text||""}));
    setLoading(true);
    setMsgs(m=>[...m,{role:"ai",text:"",tools:[]}]);
    streamChat(t,history,forceDeep);
  };
  // Точка входа: модуль «asking» — сначала clarify-воронка (если запрос неполный),
  // потом research. Fail-open: ошибка/полный запрос → сразу research.
  const send=async(txt)=>{
    const t=(txt||q).trim();
    if(!t||loading)return;
    setQ("");
    const forceDeep = deepMode ? true : null;     // null = auto-detect на бэке
    // Снимаем незакрытую clarify-карточку (если пользователь начал новый запрос,
    // не ответив на прошлую) — иначе сосуществуют две воронки.
    setMsgs(m=>[...m.filter(x=>x.role!=="clarify"),{role:"user",text:t}]);
    setLoading(true);
    let data=null;
    try{ data=await apiPost("/api/ai/clarify",{question:t,deep:!!deepMode}); }catch(e){ data=null; }
    if(!data || data.complete!==false || !(Array.isArray(data.questions)&&data.questions.length)){
      runSend(t,forceDeep);                       // воронка не нужна / ошибка → research
      return;
    }
    setLoading(false);                            // интерактивная карточка вопросов
    setMsgs(m=>[...m,{role:"clarify",question:t,forceDeep,questions:data.questions}]);
  };
  // Submit воронки: собрать обогащённый промпт (сервер) → пометить запрос → research.
  const clarifySubmit=async(srcQuestion,forceDeep,answers)=>{
    if(loading)return;
    setLoading(true);
    setMsgs(m=>m.filter(x=>x.role!=="clarify"));
    let enriched=srcQuestion;
    if(answers&&answers.length){
      try{ const r=await apiPost("/api/ai/clarify",{question:srcQuestion,answers});
           if(r&&r.enriched_question) enriched=r.enriched_question; }catch{}
    }
    if(enriched!==srcQuestion) setMsgs(m=>{const u=[...m];
      for(let i=u.length-1;i>=0;i--){ if(u[i].role==="user"){u[i]={...u[i],refined:enriched};break;} }
      return u;});
    runSend(enriched,forceDeep);
  };
  const clarifySkip=(srcQuestion,forceDeep)=>{
    setMsgs(m=>m.filter(x=>x.role!=="clarify"));
    runSend(srcQuestion,forceDeep);
  };

  return <div className="fade-in chat-shell">
    {showKbd && <KbdHelp onClose={()=>setShowKbd(false)}/>}
    {hoverCite && hoverCite.source && <CitationTooltip source={hoverCite.source} anchor={hoverCite.anchor}/>}
    <div className="chat-stream">
      <div className="chat-feed" ref={feedRef}>
        {msgs.map((m,i)=>{
          if(m.role==="clarify"){
            return <div key={i} className="chat-msg ai"><div className="chat-bubble chat-bubble-deep">
              <ClarifyCard msg={m} onSubmit={clarifySubmit} onSkip={clarifySkip}/>
            </div></div>;
          }
          if(m.mode==="deep"){
            // Editorial document layout
            const userQ = (i>0 && msgs[i-1]?.role==="user") ? msgs[i-1].text : "Аудит-отчёт";
            // Кнопка показывается когда есть СОДЕРЖАТЕЛЬНЫЙ отчёт. Порог 200 chars —
            // даже короткий стрим уже видим, и кнопка появляется почти сразу.
            // `streaming` — отключаем кнопку пока идёт активная генерация,
            // чтобы пользователь не экспортнул half-baked draft.
            const showPdfBtn = m.role==="ai" && m.text && m.text.length>200;
            const streaming  = m.role==="ai" && loading && i===msgs.length-1;
            return <div key={i} className={`chat-msg ${m.role}`}>
              <div className="dr-doc-toolbar">
                <span className="who">{m.role==="user"?"Вы · аудитор":"AuditLens · аналитический отчёт"}</span>
                {showPdfBtn &&
                  <PdfExportButton question={userQ} report={m.text}
                                   sources={m.sources||[]} verification={m.verification}
                                   claimCheck={m.claimCheck} streaming={streaming}
                                   charts={m.charts||[]} ranking={m.ranking}
                                   insights={m.insights} gaps={m.gaps}/>}
                {m.matrix && <MatrixExportButton matrix={m.matrix} question={userQ} streaming={streaming}/>}
              </div>
              <div className="chat-bubble chat-bubble-deep">
                {/* Живые карточки параллельных агентов (во время волны сбора). */}
                {loading && i===msgs.length-1 &&
                  <AgentsPanel plan={m.plan} stepStates={m.stepStates} phase={m.phase}/>}
                {/* Ход размышления — ОТДЕЛЬНАЯ панель на каждую стадию. Активна та,
                    чья стадия совпадает с текущей фазой; прочие свёрнуты «· Nс».
                    Так дирижёр не «вечно размышляет», а аналитик/критик видны. */}
                {m.reasoningStages && STAGE_ORDER.filter(s=>m.reasoningStages[s]).map(s=>
                  <ThinkingPanel key={s} stage={s} text={m.reasoningStages[s]}
                                  active={loading && i===msgs.length-1 && m.phase===STAGE_PHASE[s]}/>)}
                {/* Доисследование пробелов (gap-loop) — только во время прогона. */}
                {loading && i===msgs.length-1 && m.agentIters && m.agentIters.length>0 &&
                  <AgentPanel iterations={m.agentIters}/>}
                {/* Stage banner — только для тихих стадий без своей живой панели
                    (research→AgentsPanel, planning/synthesizing→ThinkingPanel). */}
                {loading && i===msgs.length-1 &&
                  <StageStatusBanner stage={m.stageStatus} currentPhase={m.phase}
                                      mode={m.mode}/>}
                {/* Coverage — только как предупреждение о слабом покрытии. */}
                {m.coverage?.warning && <CoverageBanner coverage={m.coverage}/>}
                {/* Process trace — пост-фактум сводка процесса под готовым отчётом. */}
                {m.plan && m.plan.length>0 && m.phase==="done" &&
                  <ProcessTrace plan={m.plan} stepStates={m.stepStates} phase={m.phase}
                                mode={m.mode} sources={m.sources} verification={m.verification}/>}
                <div className="dr-doc">
                  {!hideToc && <DocTocSlot/>}
                  <article className="dr-doc-main" ref={(el)=>{ m._mainEl=el; }}>
                    {/* Meta-row: «✓ N фактов верифицировано · X отфильтровано» */}
                    {(m.claimCheck || m.verification) &&
                      <ClaimCheckRow claimCheck={m.claimCheck}
                                      verification={m.verification}
                                      sourcesCount={(m.sources||[]).length}/>}
                    {m.role==="ai"&&!m.text&&loading?
                      <div className="typing"><i/><i/><i/></div>:
                      <>{renderMD(m.text, m.sources, m.charts)}
                        {streaming && m.text && <span className="dr-type-caret"/>}</>
                    }
                    {/* Charts-wrap внизу: только те графики, что НЕ были встроены
                        через [[CHART:N]] маркер. Если все встроены — пустой блок
                        не рендерим. */}
                    {(()=>{
                      const used = new Set((m.text||"").matchAll(/\[\[CHART:(\d+)\]\]/g));
                      const usedIdx = new Set();
                      (m.text||"").replace(/\[\[CHART:(\d+)\]\]/g,(_,n)=>{usedIdx.add(parseInt(n,10));return _;});
                      const rest = (m.charts||[]).filter((_,i)=>!usedIdx.has(i));
                      return rest.length>0 && <div className="dr-charts-wrap">
                        {rest.map((c,ci)=><ChartCanvas key={ci} spec={c}/>)}
                      </div>;
                    })()}
                    {m.ranking && <div className="dr-fade-in"><RankingWidget ranking={m.ranking}/></div>}
                    {m.insights && m.insights.length>0 && <div className="dr-fade-in"><InsightsWidget insights={m.insights}/></div>}
                    {m.verification&&<VerificationBanner verification={m.verification}/>}
                    {/* Дублирующая кнопка PDF — в конце документа, после всех
                        разделов и графиков. Нужна для длинных отчётов где
                        верхняя ушла за viewport scroll. */}
                    {showPdfBtn && !streaming &&
                      <div className="dr-doc-footer">
                        <PdfExportButton question={userQ} report={m.text}
                                         sources={m.sources||[]} verification={m.verification}
                                         claimCheck={m.claimCheck} streaming={false}
                                         charts={m.charts||[]} ranking={m.ranking}
                                         insights={m.insights} gaps={m.gaps}/>
                        <span className="dr-doc-footer-hint">
                          Готовый отчёт для аудита · нумерация страниц, источники, A4
                        </span>
                      </div>}
                  </article>
                  {!hideRail && <DocRailSlot>
                    <SourcesRail sources={m.sources||[]} activeN={activeCite}
                                  onHover={setActiveCite}/>
                  </DocRailSlot>}
                </div>
              </div>
            </div>;
          }
          // Quick mode — обычный bubble
          return <div key={i} className={`chat-msg ${m.role}`}>
            <div className="who">{m.role==="user"?"Вы · аудитор":"AuditLens AI"}</div>
            <div className="chat-bubble">
              {m.tools&&m.tools.length>0&&<ToolsTimeline tools={m.tools} active={m.role==="ai"&&!m.text&&loading}/>}
              {m.role==="ai"&&!m.text&&loading?
                <div className="typing"><i/><i/><i/></div>:
                renderMD(m.text, m.sources)
              }
              {m.sources&&m.sources.length>0&&<SourcesPanel sources={m.sources}/>}
            </div>
          </div>;
        })}
      </div>
      <div className="chat-input-wrap">
        <button className={`deep-toggle${deepMode?" deep-toggle-on":""}`}
                onClick={()=>setDeepMode(!deepMode)}
                title="Deep Research: planner → multi-step → verify → charts. Дольше (~60–120с), но даёт audit-grade отчёт с цитированием источников."
                disabled={loading}>
          Deep Research
        </button>
        <textarea ref={inputRef} className="chat-textarea" placeholder={deepMode?"Опишите задачу для глубокого исследования. Enter — отправить":"Задайте вопрос о рынке…  Enter — отправить, Shift+Enter — перенос, ?  — горячие клавиши"}
          value={q} onChange={e=>setQ(e.target.value)}
          onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}}}/>
        <button className="btn btn-primary" disabled={!q.trim()||loading} onClick={()=>send()} aria-label="Отправить">
          <Ic.send/>
        </button>
      </div>
    </div>
    <aside className="chat-side">
      <h4>Быстрые запросы</h4>
      {QUICK.map((qp,i)=>(
        <button key={i} className="qp" onClick={()=>send(qp.t)}>
          <span className="qp-eb">{qp.eb}</span>
          {qp.t}
        </button>
      ))}
      <h4 style={{marginTop:24}}>Контекст сессии</h4>
      <div className="t-cap" style={{lineHeight:1.6}}>
        Подключены источники: <span className="mono">v_offer_current</span>, <span className="mono">v_review_topics</span>, <span className="mono">v_sber_vs_market</span>.
        Глубина истории — 30 дней. Модель: Llama 3.3 70B via Fireworks AI.
      </div>
    </aside>
  </div>;
}

// ─── BANKS PAGE ───────────────────────────────────────────────────────────────
function BanksPage(){
  const[banks,setBanks]=useState([]);
  const[loading,setLoading]=useState(true);
  const[err,setErr]=useState(null);
  const[q,setQ]=useState("");

  useEffect(()=>{
    apiFetch("/api/banks").then(d=>{setBanks(d||[]);setLoading(false);}).catch(e=>{setErr(e.message);setLoading(false);});
  },[]);

  const filtered=(banks||[]).filter(b=>!q||(b.name||"").toLowerCase().includes(q.toLowerCase())||(b.slug||"").toLowerCase().includes(q.toLowerCase()));
  const sorted=[...filtered].sort((a,b)=>(b.total_reviews||0)-(a.total_reviews||0));

  if(loading)return <LoadingPage/>;
  if(err)return <ErrState msg={err}/>;

  return <div className="fade-in">
    <header style={{marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Банки · {banks.length} организаций</div>
      <h1 className="t-h" style={{marginBottom:6}}>Рейтинги и репутация</h1>
      <p className="t-cap" style={{maxWidth:"68ch"}}>Агрегировано с banki.ru — средние оценки, объёмы отзывов, доля решённых обращений.</p>
    </header>
    <div className="filter-row">
      <div className="search-wrap">
        <Ic.search/>
        <input className="input" placeholder="Поиск банка…" value={q} onChange={e=>setQ(e.target.value)}/>
      </div>
    </div>
    <div className="surface" style={{overflow:"hidden"}}>
      {!sorted.length?<EmptyState text="Нет данных о банках. Запустите сбор данных."/>:
      <table>
        <thead><tr>
          <th style={{width:"6%"}} className="right">№</th>
          <th>Банк</th>
          <th className="right">Ср. оценка</th>
          <th>Распределение</th>
          <th className="right">Отзывов</th>
          <th className="right">Решено</th>
        </tr></thead>
        <tbody>
          {sorted.map((b,idx)=>{
            const grade=parseFloat(b.avg_grade)||0;
            const solved=parseFloat(b.solved_pct)||0;
            return <tr key={b.bank_id||b.slug} className={b.is_sber?"is-sber":""}>
              <td className="right mono tnum" style={{color:"var(--ink-3)",fontSize:12}}>{String(idx+1).padStart(2,"0")}</td>
              <td>
                <div style={{display:"flex",alignItems:"center",gap:12}}>
                  <BankAvatar slug={b.slug} name={b.name} isSber={b.is_sber}/>
                  <div>
                    <div style={{fontWeight:500}}>{b.name||b.slug}</div>
                    <div className="mono" style={{fontSize:11,color:"var(--ink-3)"}}>{b.slug}</div>
                  </div>
                </div>
              </td>
              <td className="right">
                <span className="serif" style={{fontSize:22,fontWeight:400,color:grade>=4?"var(--pos)":grade>=3.5?"var(--warn)":"var(--neg)"}}>
                  {grade>0?grade.toFixed(2):"—"}
                </span>
              </td>
              <td>
                {grade>0?<div style={{display:"flex",gap:2,height:6,maxWidth:160}}>
                  <div style={{flex:Math.round(grade*18),background:"var(--pos)",borderRadius:2}}/>
                  <div style={{flex:Math.round((5-grade)*15),background:"var(--accent)",borderRadius:2}}/>
                </div>:<span style={{color:"var(--ink-4)",fontSize:12}}>нет данных</span>}
              </td>
              <td className="right mono tnum">{fmtNum(b.total_reviews)}</td>
              <td className="right mono tnum" style={{color:"var(--ink-2)"}}>{solved>0?`${solved}%`:"—"}</td>
            </tr>;
          })}
        </tbody>
      </table>}
    </div>
  </div>;
}

// ─── SOURCES PAGE ─────────────────────────────────────────────────────────────
function AlertsStatusBar(){
  const[s,setS]=useState(null);
  const[busy,setBusy]=useState("");
  const[msg,setMsg]=useState("");
  const load=()=>apiFetch("/api/alerts/status").then(setS).catch(()=>{});
  useEffect(()=>{load();},[]);
  const testLogin=async()=>{
    setBusy("login");setMsg("");
    try{const r=await apiPost("/api/alerts/test-login",{});
      setMsg(r.ok?"✓ SMTP-логин прошёл":`✗ ${r.error||"ошибка"}`);
    }catch(e){setMsg("✗ "+(e.message||"network"));}
    setBusy("");
  };
  const sendTest=async()=>{
    setBusy("send");setMsg("");
    try{const r=await apiPost("/api/alerts/send-test",{});
      setMsg(r.ok?"✓ Тестовое письмо отправлено":"✗ Ошибка отправки — см. серверные логи");
    }catch(e){setMsg("✗ "+(e.message||"network"));}
    setBusy("");
  };
  const runNow=async()=>{
    setBusy("run");setMsg("");
    try{const r=await apiPost("/api/alerts/run-now",{});
      setMsg(`Прогон: sent=${r.sent}, ${r.skipped||r.error||"ok"}`);
    }catch(e){setMsg("✗ "+(e.message||"network"));}
    setBusy("");
  };
  if(!s) return null;
  return <div className="card" style={{padding:"12px 16px",marginBottom:12,display:"flex",alignItems:"center",gap:12,flexWrap:"wrap"}}>
    <div style={{minWidth:0}}>
      <div style={{fontSize:12,textTransform:"uppercase",letterSpacing:.6,color:"var(--ink-2)"}}>Email-алерты</div>
      <div style={{fontSize:13}}>
        {s.configured?<span style={{color:"var(--pos)"}}>● настроено</span>
                     :<span style={{color:"var(--ink-2)"}}>○ не настроено (заполните SMTP_* в .env)</span>}
        {s.configured&&<span style={{color:"var(--ink-2)",marginLeft:8}}>{s.from} → {s.to}</span>}
      </div>
    </div>
    <div style={{display:"flex",gap:6,marginLeft:"auto",flexWrap:"wrap"}}>
      <button className="btn btn-ghost btn-sm" disabled={!!busy||!s.configured} onClick={testLogin}>
        {busy==="login"?"…":"Проверить логин"}
      </button>
      <button className="btn btn-ghost btn-sm" disabled={!!busy||!s.configured} onClick={sendTest}>
        {busy==="send"?"…":"Тестовое письмо"}
      </button>
      <button className="btn btn-ghost btn-sm" disabled={!!busy||!s.configured} onClick={runNow}>
        {busy==="run"?"…":"Запустить прогон"}
      </button>
    </div>
    {msg&&<div style={{flexBasis:"100%",fontSize:12,color:"var(--ink-2)"}}>{msg}</div>}
  </div>;
}

function SourcesPage(){
  const[data,setData]=useState({runs:[],captcha_pending:[],configured:[]});
  const[loading,setLoading]=useState(true);
  const[starting,setStarting]=useState({});
  const[runningAll,setRunningAll]=useState(false);
  const[solving,setSolving]=useState({}); // idx → "pending"|"ok"|"fail"

  const load=()=>apiFetch("/api/sources").then(d=>{setData(d||{runs:[],captcha_pending:[],configured:[]});setLoading(false);}).catch(()=>setLoading(false));
  useEffect(()=>{
    load();
    // Авто-обновление пока идут запуски: прогресс/капча появляются без ручного refresh.
    // Опрос каждые 3с — лёгкий, /api/sources читает только последние 50 запусков.
    const id=setInterval(load,3000);
    return ()=>clearInterval(id);
  },[]);

  const startIngest=async(source,target)=>{
    setStarting(s=>({...s,[source]:true}));
    try{await apiPost("/api/ingest/run",{source,target});}catch{}
    setTimeout(()=>{setStarting(s=>({...s,[source]:false}));load();},2000);
  };

  const startAll=async()=>{
    setRunningAll(true);
    try{await apiPost("/api/ingest/run-all",{});}catch{}
    setTimeout(()=>{setRunningAll(false);load();},2500);
  };

  const dismissCaptcha=async(idx)=>{
    await apiDel(`/api/captcha/${idx}`);
    setSolving(s=>{const n={...s};delete n[idx];return n;});
    load();
  };

  // Открывает капчу в headed-браузере с тем же профилем.
  // После успеха backend сам перезапускает упавший target — UI показывает это.
  const solveCaptcha=async(idx)=>{
    setSolving(s=>({...s,[idx]:"pending"}));
    try{
      const res=await apiPost(`/api/captcha/solve/${idx}`,{});
      const next=res.solved?(res.resumed?"resumed":"ok"):"fail";
      setSolving(s=>({...s,[idx]:next}));
      if(res.solved){setTimeout(()=>{load();setSolving(s=>{const n={...s};delete n[idx];return n;});},2000);}
    }catch(e){
      setSolving(s=>({...s,[idx]:"fail"}));
    }
  };

  const captchas=data.captcha_pending||[];
  const runs=data.runs||[];
  const configured=data.configured||[];

  // Все источники: настроенные в sources.yaml + те что встречались в истории.
  // Так кнопки доступны даже когда БД пуста и истории нет.
  const allSources=[...new Set([
    ...configured.map(c=>c.name),
    ...runs.map(r=>r.source),
  ])];

  return <div className="fade-in">
    <header style={{marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Источники · OpenClaw</div>
      <h1 className="t-h" style={{marginBottom:6}}>Запуски сбора и капчи</h1>
      <p className="t-cap" style={{maxWidth:"68ch"}}>
        Идемпотентный приём данных по sha256 снимка. Капча решается через кнопку ниже — откроется браузер с тем же профилем что используется для парсинга.
      </p>
    </header>

    {captchas.map((c,i)=>{
      const st=solving[i];
      return <div key={i} className="alert" style={{marginBottom:12}}>
        <div className="a-icon"><Ic.alert/></div>
        <div style={{flex:1,minWidth:0}}>
          <h4 style={{marginBottom:4}}>Требуется капча · <span className="mono">{c.source}</span></h4>
          <p style={{wordBreak:"break-all",color:"var(--ink-2)",fontSize:13,marginBottom:0}}>{c.url}</p>
          {st==="pending"&&<p style={{fontSize:12,color:"var(--pos)",marginTop:4}}>
            ⏳ Открываем браузер — решите капчу в появившемся окне…
          </p>}
          {st==="resumed"&&<p style={{fontSize:12,color:"var(--pos)",marginTop:4}}>
            ✓ Капча решена. Парсинг <span className="mono">{c.target||c.source}</span> запущен автоматически — следите за прогрессом ниже.
          </p>}
          {st==="ok"&&<p style={{fontSize:12,color:"var(--pos)",marginTop:4}}>✓ Капча решена. Перезапуск target'а недоступен (target не был зафиксирован) — нажмите кнопку источника вручную.</p>}
          {st==="fail"&&<p style={{fontSize:12,color:"var(--neg)",marginTop:4}}>✗ Время вышло или профиль не настроен. Проверьте OPENCLAW_BROWSER_PROFILE.</p>}
        </div>
        <button className="btn btn-sm" disabled={st==="pending"||st==="ok"||st==="resumed"}
          style={{background:st==="ok"||st==="resumed"?"var(--pos)":st==="fail"?"var(--neg)":undefined,color:st?"#fff":undefined}}
          onClick={()=>solveCaptcha(i)}>
          {st==="pending"?"Ожидание…":st==="resumed"?"✓ Возобновлено":st==="ok"?"Решено ✓":st==="fail"?"Повторить":"Решить капчу"}
        </button>
        <button className="btn btn-ghost btn-sm" onClick={()=>dismissCaptcha(i)}>Убрать</button>
      </div>;
    })}

    <AlertsStatusBar/>

    <div className="filter-row" style={{marginBottom:16}}>
      <button className="btn btn-sm" disabled={runningAll}
        onClick={startAll}
        style={{background:"var(--accent)",color:"#fff",borderColor:"var(--accent)"}}>
        <Ic.refresh/> {runningAll?"Запускаем…":"Запустить весь сбор"}
      </button>
      {allSources.map(src=>(
        <button key={src} className="btn btn-ghost btn-sm" disabled={!!starting[src]||runningAll}
          onClick={()=>startIngest(src,null)} title={`Запустить только ${src}`}>
          <Ic.refresh/> {src}{starting[src]?" …":""}
        </button>
      ))}
      <button className="btn btn-ghost btn-sm" onClick={load} style={{marginLeft:"auto"}}>
        <Ic.refresh/> Обновить
      </button>
    </div>
    {!runs.length&&allSources.length>0&&!loading&&<div className="alert" style={{marginBottom:16}}>
      <div className="a-icon"><Ic.alert/></div>
      <div style={{flex:1,minWidth:0}}>
        <h4 style={{marginBottom:4}}>Базы пусты — нет ни одного запуска</h4>
        <p style={{fontSize:13,color:"var(--ink-2)",marginBottom:0}}>
          Нажмите <strong>Запустить весь сбор</strong> выше, чтобы пройти по всем источникам
          ({allSources.length}) последовательно. Это может занять несколько минут.
        </p>
      </div>
    </div>}

    <div className="surface" style={{overflow:"hidden"}}>
      <div style={{padding:"16px 24px",borderBottom:"1px solid var(--hair)"}}>
        <div className="eyebrow" style={{marginBottom:2}}>История запусков</div>
      </div>
      {loading?<div style={{padding:32}}><Skel h={40}/><div style={{height:8}}/><Skel h={40}/></div>:
      !runs.length?<EmptyState text="Нет запусков в истории"/>:
      <><div style={{padding:"10px 24px",fontSize:11.5,color:"var(--ink-3)",borderBottom:"1px solid var(--hair)"}}>
        <strong>Спарсено</strong> — сколько товаров увидел адаптер. <strong>Изменилось</strong> — сколько новых
        или с обновлёнными условиями (SCD2). 0 при ненулевом «Спарсено» = идемпотентный прогон, данные не изменились.
        Снимок не меняется (sha256) → парсер не запускается, оба нуля.
      </div>
      <table>
        <thead><tr>
          <th>Источник</th><th>Цель</th><th>Статус</th>
          <th className="right">Спарсено</th>
          <th className="right">Изменилось</th>
          <th>Старт</th><th>Финиш / Ошибка</th>
        </tr></thead>
        <tbody>
          {runs.map((r,i)=>{
            const seen=r.items_seen??r.seen??0;
            const written=r.items_written??r.written??0;
            const idempotent=seen>0&&written===0;
            const fresh=written>0;
            const empty=seen===0&&written===0&&r.status==="ok";
            return <tr key={i}>
              <td className="mono" style={{fontWeight:500,fontSize:12.5}}>{r.source}</td>
              <td className="mono" style={{color:"var(--ink-2)",fontSize:12.5}}>{r.target_name}</td>
              <td>
                <span className={`badge ${r.status==="ok"?"pos":r.status==="error"||r.status==="failed"?"neg":r.status==="captcha"?"warn":""}`}>
                  <span className="dot"/>
                  {r.status==="ok"?(empty?"снимок без изменений":idempotent?"без изменений":"новые данные")
                    :r.status==="error"||r.status==="failed"?"ошибка"
                    :r.status==="captcha"?"капча":r.status||"в процессе"}
                </span>
              </td>
              <td className="right mono tnum" style={{color:seen?undefined:"var(--ink-4)"}}>{seen||"—"}</td>
              <td className="right mono tnum" style={{color:fresh?"var(--pos)":idempotent?"var(--ink-4)":undefined,fontWeight:fresh?500:400}}
                  title={idempotent?"Парсер увидел items, но условия не изменились с прошлого запуска":""}>
                {written||(idempotent?"0":"—")}
              </td>
              <td className="mono tnum" style={{color:"var(--ink-3)",fontSize:12}}>{fmtDate(r.started_at||r.started)}</td>
              <td>
                {r.error||r.err?<span style={{color:"var(--neg)",fontSize:12}}>{str(r.error||r.err)}</span>:
                  <span className="mono tnum" style={{color:"var(--ink-3)",fontSize:12}}>{fmtDate(r.finished_at||r.finished)||"—"}</span>}
              </td>
            </tr>;
          })}
        </tbody>
      </table></>}
    </div>
  </div>;
}

// ─── QUALITY PAGE ─────────────────────────────────────────────────────────────
function QualityPage(){
  const[data,setData]=useState(null);
  const[loading,setLoading]=useState(true);
  const[err,setErr]=useState(null);

  useEffect(()=>{
    apiFetch("/api/quality").then(d=>{setData(d);setLoading(false);}).catch(e=>{setErr(e.message);setLoading(false);});
  },[]);

  if(loading)return <LoadingPage/>;
  if(err)return <ErrState msg={err}/>;

  const flags=data?.flags||[];
  const errCount=flags.filter(f=>f.severity==="error").length;
  const warnCount=flags.filter(f=>f.severity==="warn").length;

  return <div className="fade-in">
    <header style={{marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Качество данных · 48&nbsp;ч</div>
      <h1 className="t-h" style={{marginBottom:6}}>Активные флаги и аномалии</h1>
      <p className="t-cap" style={{maxWidth:"68ch"}}>
        Правила: устаревшие данные, скачки ставок &gt; 25%, дубли по (банк, категория, external_id), пустые снимки.
      </p>
    </header>

    <div className="row row-3" style={{marginBottom:24}}>
      <div className="surface" style={{padding:"22px 24px"}}>
        <div className="eyebrow" style={{marginBottom:8}}>Ошибки · 24ч</div>
        <div className="serif" style={{fontSize:48,color:errCount>0?"var(--neg)":"var(--pos)",lineHeight:1}}>{errCount}</div>
        <div className="t-cap" style={{marginTop:6}}>{errCount>0?"требуют немедленного разбора":"всё в порядке"}</div>
      </div>
      <div className="surface" style={{padding:"22px 24px"}}>
        <div className="eyebrow" style={{marginBottom:8}}>Предупреждения · 24ч</div>
        <div className="serif" style={{fontSize:48,color:warnCount>0?"var(--warn)":"var(--pos)",lineHeight:1}}>{warnCount}</div>
        <div className="t-cap" style={{marginTop:6}}>{warnCount>0?"можно ставить в бэклог":"нет предупреждений"}</div>
      </div>
      <div className="surface" style={{padding:"22px 24px"}}>
        <div className="eyebrow" style={{marginBottom:8}}>Всего флагов · 48ч</div>
        <div className="serif" style={{fontSize:48,color:flags.length===0?"var(--pos)":"var(--ink)",lineHeight:1}}>{flags.length}</div>
        <div className="t-cap" style={{marginTop:6}}>в базе quality_flag</div>
      </div>
    </div>

    <div className="surface" style={{overflow:"hidden"}}>
      {!flags.length?<EmptyState text="Активных флагов качества нет — всё чисто"/>:
      <table>
        <thead><tr>
          <th>Код</th><th>Тип</th><th>Тяжесть</th><th>Детали</th><th>Когда</th>
        </tr></thead>
        <tbody>
          {flags.map((f,i)=>(
            <tr key={f.flag_id||i}>
              <td className="mono" style={{fontWeight:500,fontSize:12.5}}>{str(f.code)}</td>
              <td className="mono" style={{color:"var(--ink-2)",fontSize:12.5}}>{str(f.entity_type)}</td>
              <td>
                <span className={`badge ${f.severity==="error"?"neg":"warn"}`}>
                  <span className="dot"/>{f.severity==="error"?"ошибка":"предупр."}
                </span>
              </td>
              <td style={{maxWidth:520,fontSize:13}}>{str(f.detail)}</td>
              <td className="mono tnum" style={{color:"var(--ink-3)",fontSize:12}}>{fmtDate(f.created_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>}
    </div>
  </div>;
}

// ─── KNOWLEDGE PAGE: knowledge layer coverage + live semantic search ───────
function KnowledgePage(){
  const[coverage,setCoverage]=useState([]);
  const[recent,setRecent]=useState([]);
  const[loading,setLoading]=useState(true);
  const[query,setQuery]=useState("");
  const[searchResults,setSearchResults]=useState(null);
  const[searching,setSearching]=useState(false);
  const[bootstrapping,setBootstrapping]=useState(false);
  const[crawling,setCrawling]=useState(false);

  const load=()=>{
    Promise.all([
      apiFetch("/api/rag/coverage").catch(()=>[]),
      apiFetch("/api/sources").then(d=>(d?.runs||[])).catch(()=>[]),
    ]).then(([cov,runs])=>{
      setCoverage(Array.isArray(cov)?cov:[]);
      setRecent((runs||[]).filter(r=>r.status==="ok").slice(0,8));
      setLoading(false);
    });
  };
  useEffect(()=>{load();const id=setInterval(load,8000);return()=>clearInterval(id);},[]);

  const runSearch=async()=>{
    const q=query.trim(); if(!q)return;
    setSearching(true);setSearchResults(null);
    try{
      // вызываем chat endpoint в режиме semantic_search (но проще — создадим прямой endpoint)
      // Для MVP — вызываем AI agent с явным указанием
      const res=await apiPost("/api/rag/semantic-search",{query:q,top_k:8,trust_min:0.5});
      setSearchResults(res?.results||[]);
    }catch(e){
      setSearchResults({error:e.message});
    }finally{setSearching(false);}
  };

  const totalDocs=coverage.reduce((s,c)=>s+(Number(c.documents)||0),0);
  const totalChunks=coverage.reduce((s,c)=>s+(Number(c.chunks)||0),0);
  const banksWithData=coverage.filter(c=>(Number(c.documents)||0)>0).length;
  const banksWithoutData=coverage.length-banksWithData;

  const startBootstrap=async()=>{
    setBootstrapping(true);
    try{await apiPost("/api/rag/bootstrap-all",{});}catch{}
    setTimeout(()=>{setBootstrapping(false);load();},2000);
  };
  const startCrawl=async()=>{
    setCrawling(true);
    try{await apiPost("/api/rag/crawl-all",{});}catch{}
    setTimeout(()=>{setCrawling(false);load();},2000);
  };

  return <div className="fade-in">
    <header style={{marginBottom:24}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Knowledge layer · pgvector</div>
      <h1 className="t-h" style={{marginBottom:6}}>База знаний по банкам</h1>
      <p className="t-cap" style={{maxWidth:"68ch"}}>
        Документы официальных сайтов, ЦБ-реестра и агрегаторов.
        Для каждого фрагмента считаем семантический embedding (BGE-M3 1024d) — RAG-поиск возвращает релевантные фрагменты с trust-фильтром.
      </p>
    </header>

    {/* KPI bar */}
    <div className="k-kpi-row">
      <div className="k-kpi"><div className="k-kpi-num">{totalDocs}</div><div className="k-kpi-lbl">документов</div></div>
      <div className="k-kpi"><div className="k-kpi-num">{totalChunks}</div><div className="k-kpi-lbl">фрагментов</div></div>
      <div className="k-kpi"><div className="k-kpi-num">{banksWithData}<span className="k-kpi-frac"> / {banksWithData+banksWithoutData}</span></div><div className="k-kpi-lbl">банков с данными</div></div>
      <div className="k-kpi-actions">
        <button className="btn btn-sm" disabled={bootstrapping} onClick={startBootstrap}>
          <Ic.refresh/> {bootstrapping?"Discovery…":"Discovery sitemap"}
        </button>
        <button className="btn btn-sm" disabled={crawling} onClick={startCrawl}
                style={{background:"var(--accent)",color:"#fff",borderColor:"var(--accent)"}}>
          <Ic.refresh/> {crawling?"Запуск…":"Crawl всех банков"}
        </button>
      </div>
    </div>

    {/* Live semantic search */}
    <section className="k-section">
      <div className="k-section-head">
        <div>
          <h3 className="k-section-title">Live-поиск по базе</h3>
          <p className="t-cap">Тест семантического поиска (без LLM). Возвращает топ-фрагментов с trust-score.</p>
        </div>
      </div>
      <div className="k-search-wrap">
        <input className="k-search-input" placeholder='напр. "лимит SWIFT в Турцию", "комиссия за обслуживание карты"…'
               value={query} onChange={e=>setQuery(e.target.value)}
               onKeyDown={e=>{if(e.key==="Enter")runSearch();}}/>
        <button className="btn btn-sm" disabled={!query.trim()||searching} onClick={runSearch}
                style={{background:"var(--accent)",color:"#fff",borderColor:"var(--accent)"}}>
          {searching?"Ищу…":"Найти"}
        </button>
      </div>
      {searchResults&&Array.isArray(searchResults)&&<div className="k-search-results">
        {searchResults.length===0?
          <div className="k-empty">По запросу ничего не нашлось. Попробуйте проиндексировать больше документов.</div>:
          searchResults.map((r,i)=><div key={i} className="k-search-card" style={{"--src-accent":SOURCE_KIND_COLORS[r.source_kind]||"#737373"}}>
            <div className="k-search-card-head">
              <strong>{r.bank_name||"Источник"}</strong>
              <span className="k-search-rel">релевантность {(r.relevance*100).toFixed(0)}%</span>
              <TrustDots score={r.trust_score}/>
            </div>
            {r.headings_path&&<div className="k-search-crumbs">{r.headings_path}</div>}
            <div className="k-search-text">{r.text?.slice(0,400)}…</div>
            <a href={r.url} target="_blank" rel="noopener noreferrer" className="k-search-url">{r.url}</a>
          </div>)
        }
      </div>}
      {searchResults&&searchResults.error&&<div className="k-empty" style={{color:"var(--neg)"}}>
        Ошибка: {searchResults.error}
      </div>}
    </section>

    {/* Bank coverage table */}
    <section className="k-section">
      <div className="k-section-head">
        <h3 className="k-section-title">Покрытие по банкам</h3>
      </div>
      {loading?<div className="k-empty">Загрузка…</div>:
       coverage.length===0?<div className="k-empty">
         База ещё пустая. Нажмите «Discovery sitemap» для топ-27 банков, затем «Crawl всех банков» для индексации.
       </div>:
       <table className="k-cov-table">
         <thead><tr>
           <th>Банк</th><th>Документы</th><th>Фрагменты</th><th>Features</th>
           <th>Последний fetch</th>
         </tr></thead>
         <tbody>{coverage.map((c,i)=>(
           <tr key={i}>
             <td><strong>{c.name||c.slug}</strong> <span className="t-cap">/{c.slug}</span></td>
             <td>{c.documents||0}</td>
             <td>{c.chunks||0}</td>
             <td>{c.features||0}</td>
             <td className="t-cap">{formatRelDate(c.last_doc_fetch)}</td>
           </tr>
         ))}</tbody>
       </table>}
    </section>
  </div>;
}


// ─── SHELL ────────────────────────────────────────────────────────────────────
const NAV=[
  {id:"overview",label:"Обзор",       icon:Ic.grid,   group:"Анализ"},
  {id:"market",  label:"Рынок",       icon:Ic.market, group:"Анализ"},
  {id:"sber",    label:"Сбер / Рынок",icon:Ic.scale,  group:"Анализ"},
  {id:"reviews", label:"Отзывы",      icon:Ic.msg,    group:"Анализ"},
  {id:"ai",      label:"ИИ-аналитик", icon:Ic.spark,  group:"Анализ"},
  {id:"knowledge",label:"База знаний",icon:Ic.src,    group:"Анализ"},
  {id:"banks",   label:"Банки",       icon:Ic.bank,   group:"Данные"},
  {id:"sources", label:"Источники",   icon:Ic.src,    group:"Данные"},
  {id:"quality", label:"Качество",    icon:Ic.shield, group:"Данные"},
];
const PAGES_FN={overview:OverviewPage,market:MarketPage,sber:SberPage,reviews:ReviewsPage,ai:AIPage,knowledge:KnowledgePage,banks:BanksPage,sources:SourcesPage,quality:QualityPage};
const PAGE_LABELS={overview:["01","Обзор"],market:["02","Рынок"],sber:["03","Сбер / Рынок"],reviews:["04","Отзывы"],ai:["05","ИИ-аналитик"],knowledge:["06","База знаний"],banks:["07","Банки"],sources:["08","Источники"],quality:["09","Качество"]};

function Shell(){
  const[page,setPage]=useState(()=>location.hash?.slice(1)||"overview");
  const{theme,setTheme}=useTheme();
  const[banks,setBanks]=useState([]);
  const[qualityCount,setQualityCount]=useState(0);
  const[hasCaptcha,setHasCaptcha]=useState(false);

  // Load banks for context + sidebar badges
  useEffect(()=>{
    apiFetch("/api/banks").then(d=>{setBanks(d||[]);}).catch(()=>{});
    apiFetch("/api/quality").then(d=>{setQualityCount((d?.flags||[]).length);}).catch(()=>{});
    apiFetch("/api/sources").then(d=>{setHasCaptcha((d?.captcha_pending||[]).length>0);}).catch(()=>{});
  },[]);

  useEffect(()=>{
    const onHash=()=>setPage(location.hash?.slice(1)||"overview");
    window.addEventListener("hashchange",onHash);
    return ()=>window.removeEventListener("hashchange",onHash);
  },[]);
  useEffect(()=>{history.replaceState(null,"","#"+page);},[page]);

  const groups=useMemo(()=>{const g={};NAV.forEach(n=>{(g[n.group]=g[n.group]||[]).push(n);});return g;},[]);
  const Page=PAGES_FN[page]||OverviewPage;
  const[idx,label]=PAGE_LABELS[page]||["01","Обзор"];

  return <BanksCtx.Provider value={banks}>
    <div id="app">
      <aside className="rail">
        <div className="rail-brand">
          <div className="rail-mark">A</div>
          <div>
            <h1>AuditLens</h1>
            <small>v1.0 · Internal</small>
          </div>
        </div>
        {Object.entries(groups).map(([gr,items])=>(
          <div key={gr}>
            <div className="rail-section">{gr}</div>
            {items.map(n=>{
              const active=page===n.id;
              const allItems=NAV.filter(x=>x.group===gr);
              const num=allItems.findIndex(x=>x.id===n.id)+1+(gr==="Анализ"?0:5);
              const dot=n.id==="sources"&&hasCaptcha;
              const count=n.id==="quality"&&qualityCount>0?qualityCount:null;
              return <button key={n.id} className={`nav-item ${active?"active":""}`} onClick={()=>setPage(n.id)}>
                <span className="rail-num">{String(num).padStart(2,"0")}</span>
                <span style={{display:"inline-flex",marginRight:10,color:"var(--ink-3)"}}><n.icon/></span>
                {n.label}
                {dot&&<span className="nav-dot"/>}
                {count&&<span className="nav-count">{count}</span>}
              </button>;
            })}
          </div>
        ))}
        <div className="rail-foot">
          <div className="user-chip">
            <div className="avatar">АД</div>
            <div>
              <div className="nm">Аудитор</div>
              <div className="role">Внутренний аудит</div>
            </div>
          </div>
        </div>
      </aside>

      <div className="main">
        <div className="topbar">
          <div className="mobile-nav">
            <button className="icon-btn" aria-label="меню"><Ic.menu/></button>
          </div>
          <div className="crumb">
            <span className="crumb-idx">{idx} / 08</span>
            <span style={{color:"var(--hair-2)"}}>—</span>
            <b>{label}</b>
          </div>
          <div className="tb-spacer"/>
          <div className="tb-meta desk-only">
            <span className="live">данные актуальны</span>
            <span>{new Date().toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})} МСК</span>
            <span className="kbd">API</span>
          </div>
          <button className="icon-btn" aria-label="обновить" title="Обновить страницу" onClick={()=>setPage(p=>p)}>
            <Ic.refresh/>
          </button>
          <button className="icon-btn" aria-label="тема" onClick={()=>setTheme(theme==="dark"?"light":"dark")} title="Сменить тему">
            {theme==="dark"?<Ic.sun/>:<Ic.moon/>}
          </button>
        </div>
        <div className="content">
          <Page key={page}/>
        </div>
      </div>
    </div>
  </BanksCtx.Provider>;
}

function App(){
  return <ThemeProvider><Shell/></ThemeProvider>;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
