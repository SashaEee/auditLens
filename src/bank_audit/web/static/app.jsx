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

// Редакторский экран приветствия ИИ-аналитика (новый дизайн)
function AiWelcome({onPick,recent,onOpenHistory,onLoadSession}){
  const me=useMe();
  return <div className="ai-welcome fade-in">
    <div className="aw-eyebrow">{me?`${greeting(me)} · ИИ-аналитик`:"ИИ-аналитик · AuditLens"}</div>
    <h1 className="aw-title">Спросите об условиях<br/>банковского рынка</h1>
    <p className="aw-lede">Сравнение тарифов, ставок и рисков по продуктам — с цитированием официальных источников и позицией Сбера. Для аудит-вывода включите <b>Deep&nbsp;Research</b>: планировщик, мульти-агентный сбор и проверка чисел.</p>
    <div className="aw-cards">
      {QUICK.map((s,i)=>(
        <button key={i} className="aw-card" onClick={()=>onPick(s.t)}>
          <span className="aw-card-eb">{s.eb}</span>
          <span className="aw-card-t">{s.t}</span>
        </button>
      ))}
    </div>
    {recent&&recent.length>0 && <div className="aw-recent">
      <div className="aw-recent-h">
        <span className="l">Продолжить</span>
        <button onClick={onOpenHistory}>Вся история
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        </button>
      </div>
      <div className="aw-recent-grid">
        {recent.slice(0,4).map(s=>(
          <button key={s.session_id} className="aw-rec" onClick={()=>onLoadSession&&onLoadSession(s.session_id)}>
            <span className="t">{s.title||"Без названия"}</span>
            <span className="m">{fmtHistTime(s.updated_at)} · {s.n_messages||0} сообщ.</span>
          </button>
        ))}
      </div>
    </div>}
    <div className="aw-conn">Подключено: <span>v_offer_current · v_review_topics · v_sber_vs_market</span> · глубина 30 дней</div>
  </div>;
}

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
// Дата публикации новости: всегда МСК и явная пометка пояса (правка аналитиков)
const fmtDateMsk = s => {
  if(!s) return "";
  try {
    const d = new Date(s);
    if(isNaN(d)) return String(s).slice(0,16);
    return d.toLocaleString("ru",{timeZone:"Europe/Moscow",day:"2-digit",month:"2-digit",
      hour:"2-digit",minute:"2-digit"}).replace(", "," ")+" МСК";
  } catch { return String(s).slice(0,16); }
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
const apiPut   = (path,body) => fetch(path,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}).then(r=>{if(!r.ok)throw new Error(`${r.status}`);return r.json();});

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

// ─── Пользователь (персонализация) ────────────────────────────────────────────
const MeCtx = createContext(null);
const useMe = () => useContext(MeCtx);
const firstName = (name) => (name||"").trim().split(/\s+/)[0] || "";
const initials = (name) => {
  const p=(name||"").trim().split(/\s+/).filter(Boolean);
  return ((p[0]?.[0]||"")+(p[1]?.[0]||"")).toUpperCase() || "А";
};
const greetWord = (tz) => {
  let h;
  try{ h=parseInt(new Intl.DateTimeFormat("ru",{hour:"numeric",hour12:false,timeZone:tz||undefined}).format(new Date())); }
  catch{ h=new Date().getHours(); }
  if(h>=5&&h<12) return "Доброе утро";
  if(h>=12&&h<18) return "Добрый день";
  if(h>=18&&h<23) return "Добрый вечер";
  return "Доброй ночи";
};
const greeting = (me) => {
  const fn=firstName(me?.name), w=greetWord(me?.timezone);
  return fn?`${w}, ${fn}`:w;
};

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
    // Сначала экранируем ВЕСЬ вход: в markdown попадает недоверенный текст
    // (LLM-пересказ жалоб клиентов, сниппеты источников) — сырой <img onerror=…>
    // иначе исполнится через dangerouslySetInnerHTML (stored XSS).
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    // markdown-ссылки [текст](url) → <a>. ДО citation-замены и emphasis.
    // URL — через escAttr: кавычка в URL иначе выламывается из href-атрибута.
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             (_,txt,url)=>`<a href="${escAttr(url)}" target="_blank" rel="noopener noreferrer" class="md-link">${txt}</a>`)
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
// ─── OVERVIEW · Утренний брифинг ─────────────────────────────────────────────
// Микрокомпоненты выпуска: числа детерминированы (бэк), LLM только формулирует.

function DeltaStrip({from,to}){
  const up=to>from;
  return <span className="bf-delta">
    <span>{from}%</span><span className="arr">→</span>
    <span style={{fontWeight:600}}>{to}%</span>
    <span className={`delta ${up?"pos":"neg"}`}>{up?<Ic.arrow_up/>:<Ic.arrow_dn/>}{signed(Math.round((to-from)*100)/100)}</span>
  </span>;
}

// ключевая ставка: ступени, не сглаживание — ставка дискретна
function RateStep({points,w=110,h=24}){
  if(!points||points.length<2)return null;
  const vals=points.map(p=>p.rate);
  const min=Math.min(...vals),max=Math.max(...vals),rng=(max-min)||1;
  const step=w/(points.length-1);
  let d=`M0 ${h-2-((vals[0]-min)/rng)*(h-6)}`;
  vals.forEach((v,i)=>{const y=h-2-((v-min)/rng)*(h-6);if(i>0)d+=` H${(i*step).toFixed(1)} V${y.toFixed(1)}`;});
  return <svg width={w} height={h} style={{display:"block"}}>
    <path d={d} fill="none" stroke="currentColor" strokeWidth="1.5"/>
  </svg>;
}

// микро-глиф матрицы рисков 3×3 (вероятность × влияние из дайджеста)
function RiskGlyph({likelihood=2,impact=2}){
  const cells=[];
  for(let r=0;r<3;r++)for(let c=0;c<3;c++){
    const active=(2-r)===(impact-1)&&c===(likelihood-1);
    cells.push(<circle key={`${r}${c}`} cx={4+c*6} cy={4+r*6} r={active?2.6:1.3}
      fill={active?"var(--sev,var(--ink-3))":"var(--hair-2)"}/>);
  }
  return <svg className="bf-glyph" width="21" height="21" viewBox="0 0 21 21"
    role="img" aria-label={`вероятность ${likelihood}/3, влияние ${impact}/3`}>
    <title>{`вероятность ${likelihood}/3 · влияние ${impact}/3`}</title>{cells}
  </svg>;
}

const BF_KIND={
  review_spike:{tag:"Риск · жалобы"},
  mass_move:{tag:"Тарифы · массовое"},
  tariff_move:{tag:"Тарифы"},
  rate_move:{tag:"Ключевая ставка"},
  news_alert:{tag:"Новость"},
  exploit:{tag:"Лазейки"},          // будущий источник соседней команды
};

function bfGoAI(prompt){
  try{sessionStorage.setItem("al-ai-prefill",prompt);}catch{}
  location.hash="ai";
}
function bfGoDrill(drill){
  if(!drill)return;
  if(drill.url){window.open(drill.url,"_blank","noopener");return;}
  try{sessionStorage.setItem(drill.page==="reviews"?"al-rv-prefilter":"al-mk-preset",
    JSON.stringify(drill.params||{}));}catch{}
  location.hash=drill.page||"overview";
}
// Всегда возвращает [начало, длина] фрагмента заголовка для оранжевого акцента —
// чтобы подсветка была на КАЖДОМ заголовке, а не только когда hot от LLM точно
// совпал. Приоритет: точный hot → без регистра → число/процент/×N → банк/ЦБ →
// последние 2 слова. hl — заголовок, hot — подсказка модели (может быть пустой).
// ─── Всплывашки поверх всего: портал в body + fixed ──────────────────────────
// Раньше попап жил внутри плитки и обрезался её контейнером (у полосы пульса
// overflow:hidden ради скруглённых углов), а hover-фильтры создавали слои,
// перекрывавшие подсказку. Портал в body снимает и обрезание, и конкуренцию
// z-index: попап физически вне всех контейнеров страницы.
function popPlace(el,{w=400,h=280,gap=12}={}){
  const r=el.getBoundingClientRect();
  // размеры окна берём с фолбэками: в некоторых встроенных вебвью innerWidth
  // приходит нулём, и без страховки попап получал отрицательную ширину
  const de=document.documentElement;
  const W=window.innerWidth||de.clientWidth||de.getBoundingClientRect().width||1280;
  const H=window.innerHeight||de.clientHeight||800;
  const M=12;
  const ww=Math.max(240,Math.min(w,W-M*2));
  let left,top,arrow;
  if(W-r.right>=ww+gap){ left=r.right+gap; top=r.top-10; arrow="left"; }
  else if(r.left>=ww+gap){ left=r.left-ww-gap; top=r.top-10; arrow="right"; }
  else if(H-r.bottom>=h+gap){ left=r.left; top=r.bottom+gap; arrow="top"; }
  else { left=r.left; top=r.top-h-gap; arrow="bottom"; }
  left=Math.min(Math.max(M,left),W-ww-M);
  top=Math.min(Math.max(M,top),H-Math.min(h,H-M*2)-M);
  return {left,top,width:ww,arrow};
}

// Текстовая подсказка [data-tip] — тоже порталом (была CSS ::after, обрезалась)
function TipLayer(){
  const[tip,setTip]=useState(null);
  useEffect(()=>{
    let cur=null;
    const show=e=>{
      const el=e.target&&e.target.closest&&e.target.closest("[data-tip]");
      if(!el||el===cur)return;
      const txt=el.getAttribute("data-tip");
      if(!txt)return;
      cur=el;
      setTip({txt,...popPlace(el,{w:320,h:120})});
    };
    const hide=e=>{
      if(!cur)return;
      if(e&&e.target&&e.target.closest&&e.target.closest("[data-tip]")===cur&&e.type!=="scroll")return;
      cur=null;setTip(null);
    };
    document.addEventListener("mouseover",show);
    document.addEventListener("mouseout",hide);
    document.addEventListener("focusin",show);
    document.addEventListener("focusout",hide);
    window.addEventListener("scroll",hide,true);
    return()=>{document.removeEventListener("mouseover",show);
      document.removeEventListener("mouseout",hide);
      document.removeEventListener("focusin",show);
      document.removeEventListener("focusout",hide);
      window.removeEventListener("scroll",hide,true);};
  },[]);
  if(!tip)return null;
  return ReactDOM.createPortal(
    <div className={"tip-pop tip-a-"+tip.arrow}
         style={{left:tip.left,top:tip.top,maxWidth:tip.width}}>{tip.txt}</div>,
    document.body);
}

// ─── «Как это посчитано» — раскрытие любой цифры брифинга ─────────────────────
// Аудитор не должен гадать, откуда взялось «×2.1»: показываем формулу словами,
// как считалась норма, на какой выборке и из какого источника.
function xpRows(kind,d){
  const R=[];
  if(kind==="review_spike"){
    if(d.week!=null&&d.baseline_week!=null)
      R.push(["Расчёт",`${d.week} жалоб за 7 дней ÷ ${d.baseline_week} — норма недели = ×${d.ratio}`]);
    if(d.baseline_week!=null)
      // ВАЖНО: это среднее по окну 14–63 дня назад (7 недель), не медиана и не
      // «прошлые 6 недель» — последние две недели в норму НЕ входят, иначе
      // всплеск разбавлял бы сам себя
      R.push(["Норма",d.base_count!=null
        ? `${d.base_count} жалоб за ${d.base_weeks} недель до этого (окно 14–63 дня назад) ÷ ${d.base_weeks} = ${d.baseline_week} в неделю`
        : `среднее за неделю по окну 14–63 дня назад — ${d.baseline_week} жалоб`]);
    if(d.prev_week!=null) R.push(["Прошлая неделя",`${d.prev_week} жалоб`]);
    // масштаб: 6.7/нед — это ОДНА тема; без общего числа цифра кажется мелкой
    if(d.week_total)
      R.push(["Масштаб",`тема — ${Math.round(100*d.week/d.week_total)}% всех жалоб на Сбер за неделю (${d.week} из ${d.week_total})`]);
    if(d.market_ratio!=null)
      R.push(["Рынок",`та же тема по рынку ×${d.market_ratio}` +
        (d.bank_specific?" — значит всплеск наш, а не отраслевой":"")]);
    R.push(["Выборка","только Сбербанк · негативные отзывы banki.ru (1–2★) · темы по ключевым словам"]);
  } else if(kind==="tariff_move"){
    if(d.from!=null&&d.to!=null)
      R.push(["Расчёт",`${d.from}% → ${d.to}% = ${d.delta>0?"+":""}${d.delta} п.п.`]);
    if(d.category) R.push(["Продукт",`${CAT_LABELS[d.category]||d.category} · ${d.title||""}`]);
    R.push(["Порог","в движения недели попадают сдвиги от 0.05 п.п."]);
    R.push(["Источник","журнал изменений тарифов (sravni.ru)"]);
  } else if(kind==="mass_move"){
    R.push(["Расчёт",`${d.n_banks} банков изменили условия за ${d.window_h||48} ч`]);
    if(d.banks) R.push(["Банки",(d.banks||[]).slice(0,6).join(", ")]);
    R.push(["Критерий","массовым считаем движение от 3 банков одной категории"]);
  } else if(kind==="rate_move"){
    if(d.current!=null) R.push(["Значение",`ключевая ставка ЦБ ${d.current}%`]);
    if(d.as_of) R.push(["На дату",String(d.as_of)]);
    R.push(["Источник","Банк России, официальная публикация"]);
  }
  return R;
}

// расшифровки плиток пульса: у каждой цифры своя формула и своя выборка
const xpDiverge=d=>[
  ["Расчёт",`${d.week} жалоб за 7 дней ÷ ${d.baseline_week} — норма недели = ×${d.ratio}`],
  ["Норма",`${d.base_count} жалоб за ${d.base_weeks} недель до этого (окно 14–63 дня назад) ÷ ${d.base_weeks}`],
  ["Рынок",d.market_ratio!=null
    ?`та же тема по всем банкам ×${d.market_ratio} — мы растём в ${d.gap} раза быстрее рынка`
    :"рыночный срез недоступен"],
  ["Почему здесь","из 22 тем показана та, где наш рост сильнее всего обгоняет рыночный"],
  ["Выборка","только Сбербанк · негативные отзывы banki.ru (1–2★)"],
];
const xpEscalation=k=>[
  ["Значение",`${pct1(k.escalation_pct)} жалоб содержат угрозу обращения в ЦБ, суд, ФАС или прокуратуру`],
  ["Как ищем","по формулировкам жалобы: «жалоба в ЦБ», «подам иск», «в прокуратуру» и подобным"],
  ["Порог","12% — принятый в инструменте уровень внимания"],
  ["Выборка",`${fmtNum(k.total||0)} жалоб за 90 дней · только Сбербанк · banki.ru`],
];
const xpWeek=(ov,k)=>[
  ["Расчёт",`${ov.week} жалоб за последние 7 дней`],
  ["Норма",ov.baseline_week!=null
    ?`${Math.round(ov.baseline_week)} в неделю — среднее по окну 14–63 дня назад`:"—"],
  ["Рынок",ov.market_ratio!=null?`по всем банкам ×${ov.market_ratio} к своей норме`:"—"],
  ["Масштаб",k.total?`корпус ${fmtNum(k.total)} жалоб за 90 дн · доля рынка ${k.market_share_pct}% · ${k.market_rank}-е место из ${k.market_banks}`:"—"],
  ["Канал","banki.ru, негативные отзывы 1–2★ — один из каналов, не все обращения"],
];
const xpOurChanges=tm=>[
  ["Значение",`${(tm.totals&&tm.totals.sber_changes_7d)||0} офферов Сбера со значимым изменением условий за 7 дней`],
  ["Значимое","изменение нестаточного условия или сдвиг ставки от 0.01 п.п."],
  ["Зачем","проверить, что изменения тарифов прошли согласование и корректно отражены"],
  ["Источник","журнал изменений условий (sravni.ru), сверка ежедневная"],
];
const xpUnclassified=u=>u?[
  ["Значение",`${u.week} жалоб из ${u.week_total} за неделю (${u.pct}%) не попали ни в одну из 22 тем`],
  ["Норма",`${u.baseline_week} в неделю по окну 14–63 дня назад`+(u.ratio!=null?` — сейчас ×${u.ratio}`:"")],
  ["Что значит","классификатор их не видит: либо инцидент вне таксономии, либо пробел в правилах"],
  ["Зачем","картина по темам неполна на эту долю — это надо знать до выводов"],
]:[];
const xpThemeUp=t=>[
  ["Расчёт",`${t.n} жалоб за 90 дней против ${Math.round(t.n/(1+(t.delta_pct||0)/100))} за предыдущие 90 → +${Math.round(t.delta_pct)}%`],
  ["Горизонт","квартал — медленные тренды, которых не видно в недельном окне"],
  ["Порог","показываем тему с ростом от 50% и не менее 30 жалоб"],
  ["Выборка","только Сбербанк · негативные отзывы banki.ru (1–2★)"],
];

// обёртка вокруг числа: пунктирное подчёркивание + карточка-расшифровка.
// Позиция выбирается по свободному месту: сбоку (не перекрывает текст вообще),
// иначе снизу/сверху — попап не должен резать строку заголовка.
function Xp({rows,children,note}){
  const ref=useRef(null);
  const[box,setBox]=useState(null);
  const show=useCallback(()=>{
    if(ref.current)setBox(popPlace(ref.current,{w:400,h:Math.min(300,80+34*((rows||[]).length))}));
  },[rows]);
  const hide=useCallback(()=>setBox(null),[]);
  useEffect(()=>{
    if(!box)return;
    const off=()=>setBox(null);
    window.addEventListener("scroll",off,true);
    window.addEventListener("resize",off);
    return()=>{window.removeEventListener("scroll",off,true);
      window.removeEventListener("resize",off);};
  },[box]);
  if(!rows||!rows.length)return children;
  return <span className="xp" tabIndex={0} ref={ref}
      onMouseEnter={show} onMouseLeave={hide} onFocus={show} onBlur={hide}>
    {children}
    {box&&ReactDOM.createPortal(
      <div className={"xp-pop xp-a-"+box.arrow} role="tooltip"
           style={{left:box.left,top:box.top,width:box.width}}>
        <span className="xp-h">как это посчитано</span>
        {rows.map(([k,v],i)=><span key={i} className="xp-row">
          <span className="xp-k">{k}</span><span className="xp-v">{v}</span></span>)}
        {note&&<span className="xp-note">{note}</span>}
      </div>, document.body)}
  </span>;
}

// «−22 ко вчера» под числом плитки: носитель смысла — изменение, а не уровень.
// Сравниваются снапшоты выпусков (см. _digest_delta на бэке), поэтому дрейф
// скользящего окна внутри дня сюда не попадает.
function BfDelta({v,unit,invert}){
  if(v==null||v===0)return null;
  const better=invert?v<0:v>0;      // invert=true → рост это плохо
  return <span className={"bf-t-delta "+(better?"good":"bad")}>
    {v>0?"+":"−"}{Math.abs(v)}{unit||""} ко вчера</span>;
}

// Вердикт дня, если LLM не сформулировала: одна фраза по тем же числам.
function bfVerdict(dv,esc,ovl,unc){
  const bits=[];
  if(dv&&dv.gap>=1.25)
    bits.push(`Внимание на «${(dv.short||dv.label).toLowerCase()}»: ${dv.week} жалоб при норме ${dv.baseline_week}` +
      (dv.market_ratio!=null&&dv.market_ratio<1.15?" — и это только у нас, по рынку тема ровная":""));
  else bits.push("Спокойное утро: тем с ростом сильнее рынка нет");
  if(esc!=null&&esc>=12) bits.push(`эскалации в ЦБ и суд выше порога — ${pct1(esc)} при 12%`);
  if(unc&&unc.ratio!=null&&unc.ratio>=1.3) bits.push(`жалоб вне известных тем больше обычного (${unc.week} против ${unc.baseline_week})`);
  if(bits.length===1&&ovl&&ovl.week!=null&&ovl.baseline_week!=null)
    bits.push(`всего ${ovl.week} жалоб за неделю при норме ${Math.round(ovl.baseline_week)}`);
  return bits.join(", ")+".";
}

function bfPickHot(hl,hot){
  if(!hl)return null;
  const trim=(i,len)=>{ // обрезаем хвостовую/ведущую пунктуацию у фрагмента
    while(len>0&&/[\s,.;:!?«»"'()—-]/.test(hl[i+len-1]))len--;
    while(len>0&&/[\s«»"'(—-]/.test(hl[i])){i++;len--;}
    return len>0?[i,len]:null;
  };
  if(hot){
    let i=hl.indexOf(hot);
    if(i<0)i=hl.toLowerCase().indexOf(hot.toLowerCase());
    if(i>=0)return trim(i,hot.length);
  }
  // число с единицей: ×2.8, +140%, 17.7%, «2.8 раза», 30 000 ₽
  let m=hl.match(/[×+\-]?\d[\d.,]*(?:\s?\d{3})*\s*(?:%|п\.?\s?п\.?|пп|раза?|₽|млрд|млн)?/);
  if(m){const f=m[0].replace(/\s+$/,"");if(f.length>=2){const i=hl.indexOf(f);if(i>=0)return trim(i,f.length);}}
  // банк / регулятор / продукт-бренд (только буквы, без хвостовой пунктуации)
  const b=hl.match(/Сбер[а-яё]*|ВТБ|Альфа[-а-яё]*|Газпромбанк|Т-?Банк|ЦБ\s?РФ|ЦБ|Домклик|ДОМ\.РФ/i);
  if(b)return trim(b.index,b[0].length);
  // последние 2 слова (или всё, если слово одно)
  const w=hl.trim().split(/\s+/);
  if(w.length>=2){const f=w.slice(-2).join(" ");const i=hl.lastIndexOf(f);if(i>=0)return trim(i,f.length);}
  return trim(0,hl.length);
}

function BfCard({ins,idx,lead}){
  const d=ins.data||{};
  const xp=xpRows(ins.kind,d);
  const viz=(()=>{
    if(ins.kind==="review_spike")
      return <>
        <Spark data={[d.baseline_week||0,d.prev_week||0,d.week||0]} w={64} h={20} color="var(--ink-3)"/>
        {d.ratio&&<span className="mono tnum" style={{fontSize:12,fontWeight:600}}>×{d.ratio}</span>}
        {d.geo&&<span className="mono" style={{fontSize:11,color:"var(--ink-3)"}}>{d.geo.share}% · {d.geo.city}</span>}
      </>;
    if(ins.kind==="tariff_move")return <DeltaStrip from={d.from} to={d.to}/>;
    if(ins.kind==="mass_move")
      return <span className="mono" style={{fontSize:12}}>
        <b style={{fontSize:15}}>{d.n_banks}</b> банков · {(d.banks||[]).slice(0,3).join(", ")}{(d.banks||[]).length>3?"…":""}
      </span>;
    if(ins.kind==="rate_move")
      return <><RateStep points={(d.points||[]).slice(-30)}/><span className="mono tnum" style={{fontSize:13,fontWeight:600}}>{d.current}%</span></>;
    return null;
  })();
  return <article className={`bf-card${lead?" lead":""}`} data-sev={ins.severity} style={{"--i":idx}}>
    <div className="bf-kicker">
      {(BF_KIND[ins.kind]||{tag:ins.kind}).tag}
      {ins.after_pause&&<span className="badge warn" style={{fontSize:9}}>сбор после паузы</span>}
      <RiskGlyph likelihood={ins.likelihood} impact={ins.impact}/>
    </div>
    <h3 className="bf-title">{ins.title}</h3>
    {ins.so_what&&<div className="bf-sowhat">{ins.so_what}</div>}
    {viz&&<div className="bf-viz">{viz}</div>}
    {(ins.provenance||xp.length>0)&&<div className="bf-prov">
      {xp.length>0
        ?<Xp rows={xp} note={ins.provenance}><span className="xp-link">как посчитано</span></Xp>
        :null}
      {xp.length>0&&ins.provenance?<span className="bf-prov-sep"> · </span>:null}
      {ins.provenance}
    </div>}
    <div className="bf-foot">
      <button className="bf-btn" onClick={()=>bfGoDrill(ins.drill)}>
        {ins.kind==="news_alert"?"Источник":"Разобраться"} <Ic.ext/>
      </button>
      {ins.ai_prompt&&<button className="bf-btn ai" onClick={()=>bfGoAI(ins.ai_prompt)}>✦ Спросить ИИ</button>}
    </div>
  </article>;
}

// ─── Личная полоса «Обзора» (Фаза 3) — редакторский пролог, без плашек ────────
const PL_CSS=`
.pl{margin-bottom:30px;}
.pl-top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:13px;flex-wrap:wrap;}
.pl-hi{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:500;letter-spacing:.06em;text-transform:uppercase;color:var(--accent);}
.pl-set{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--hair-2);background:var(--surface);border-radius:var(--r);
  padding:4px 10px;font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-3);transition:color .12s,border-color .12s;}
.pl-set:hover{color:var(--accent);border-color:color-mix(in oklab,var(--accent),transparent 82%);}
.pl-lede{font-family:'Source Serif 4',Georgia,serif;font-size:19px;line-height:1.56;letter-spacing:-.004em;color:var(--ink);
  text-wrap:pretty;max-width:64ch;}
.pl-nudge{font-family:'Source Serif 4',Georgia,serif;font-size:18px;line-height:1.52;color:var(--ink-3);max-width:60ch;text-wrap:pretty;}
.pl-nudge button{font-family:'Geist','Inter',sans-serif;font-size:13.5px;color:var(--accent);font-weight:500;margin-left:7px;transition:filter .12s;}
.pl-nudge button:hover{filter:brightness(1.12);}
.pl-quiet{font-family:'Source Serif 4',Georgia,serif;font-size:16.5px;color:var(--ink-3);font-style:italic;}
.pl-fy{margin-top:18px;}
.pl-fy-row{display:flex;align-items:center;gap:13px;padding:10px 3px;border-top:1px solid var(--hair);cursor:pointer;transition:background .12s;}
.pl-fy-row:last-child{border-bottom:1px solid var(--hair);}
.pl-fy-row:hover{background:color-mix(in oklab,var(--surface),transparent 30%);}
.pl-dot{width:6px;height:6px;border-radius:50%;flex:none;background:var(--ink-4);}
.pl-dot.sev-red{background:var(--neg);}
.pl-dot.sev-amber{background:var(--warn);}
.pl-dot.sev-green{background:var(--pos);}
.pl-fy-t{flex:1;min-width:0;font-size:13.5px;line-height:1.4;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.pl-fy-tag{font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--accent);
  white-space:nowrap;flex:none;transition:opacity .12s;}
.pl-fy-act{display:flex;gap:4px;align-items:center;flex:none;opacity:0;width:0;overflow:hidden;transition:opacity .14s;}
.pl-fy-row:hover .pl-fy-tag{opacity:0;}
.pl-fy-row:hover .pl-fy-act{opacity:1;width:auto;}
.pl-fy-act button{width:26px;height:26px;border-radius:6px;display:grid;place-items:center;font-size:12px;color:var(--ink-4);transition:background .12s,color .12s;}
.pl-fy-act button:hover{background:var(--paper-2);}
.pl-fy-act .ask:hover{color:var(--accent);}
.pl-fy-act .mute:hover{color:var(--ink);}
.pl-div{border:0;border-top:1px solid var(--hair);margin:28px 0 0;}
.pl-skel{height:80px;margin-bottom:30px;border-radius:var(--r-lg);
  background:linear-gradient(90deg,var(--paper-2) 25%,var(--surface) 50%,var(--paper-2) 75%);background-size:200% 100%;animation:pl-sh 1.5s ease-in-out infinite;}
@keyframes pl-sh{0%{background-position:200% 0}100%{background-position:-200% 0}}
`;
function PersonalBand(){
  const me=useMe();
  // полоса на главной — опция (prefs.personal_band_home), по умолчанию выключена:
  // основной персональный опыт живёт на странице «Для вас»
  const enabled=!!(me&&me.prefs&&me.prefs.personal_band_home);
  const[p,setP]=useState(undefined);          // undefined=грузится, null=выкл, obj=данные
  const[gone,setGone]=useState({});
  useEffect(()=>{ if(!enabled)return;
    apiFetch("/api/overview/personal").then(d=>setP(d.personal||null)).catch(()=>setP(null)); },[enabled]);
  if(!enabled) return null;
  if(p===undefined) return <div className="pl-skel"/>;
  if(p===null) return null;                    // персонализация выключена
  const items=(p.for_you||[]).filter(x=>!gone[x.title]).slice(0,3);
  const bandFb=(x,verdict)=>{ const key=x.url||x.title; if(!key)return;
    if(verdict===-1){ setGone(g=>({...g,[x.title]:1}));
      fbToast("Понял — такого будет меньше",true); }
    else fbToast("Учтём в вашей подборке",true);
    apiPost("/api/feedback",{kind:"for_you",item_key:key,verdict,
      topics:x.reason_slugs||[],payload:{title:x.title,kind_src:x.kind}}).catch(()=>{}); };
  const g=greetWord(me&&me.timezone);
  return <div className="pl">
    <style>{PL_CSS}</style>
    <div className="pl-top">
      <span className="pl-hi">{g}{p.name?", "+p.name:""}</span>
      <button className="pl-set" onClick={()=>{location.hash="profile";}} title="Персонализация">⚙ персонализация</button>
    </div>
    {p.lead ? <p className="pl-lede">{p.lead}</p>
      : !p.has_profile
        ? <p className="pl-nudge">Опишите, какие процессы и продукты вы проверяете — и эта полоса будет собираться лично под вас.<button onClick={()=>{location.hash="profile";}}>Настроить →</button></p>
        : <p className="pl-quiet">По вашим темам сегодня спокойно.</p>}
    {items.length>0 && <div className="pl-fy">
      {items.map((x,i)=>(
        <div key={i} className="pl-fy-row" onClick={()=>{ if(x.url) window.open(x.url,"_blank","noopener"); }}>
          <span className={"pl-dot sev-"+(x.severity||"amber")}/>
          <span className="pl-fy-t">{x.title}</span>
          {x.reason&&<span className="pl-fy-tag">{x.reason}</span>}
          <span className="pl-fy-act" onClick={e=>e.stopPropagation()}>
            <button className="ask" title="Спросить ИИ" onClick={()=>bfGoAI("Разбери подробно для аудита: "+(x.title||""))}>✦</button>
            <button className="ask" title="Интересно — больше такого" onClick={()=>bandFb(x,1)}><IcTUp s={11}/></button>
            <button className="mute" title="Не интересно — меньше такого" onClick={()=>bandFb(x,-1)}><IcTDn s={11}/></button>
          </span>
        </div>
      ))}
    </div>}
    <hr className="pl-div"/>
  </div>;
}

// ─── Оценки 👍/👎: два контура — рекомендации (контент) / качество (ответы ИИ) ──
const FB_CSS=`
.fb-toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,14px);z-index:400;background:var(--surface);
  border:1px solid var(--hair);border-radius:999px;box-shadow:var(--shadow-2);padding:9px 18px;
  font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--ink-2);opacity:0;transition:opacity .25s,transform .25s;
  pointer-events:none;max-width:min(88vw,500px);text-align:center;}
.fb-toast.on{opacity:1;transform:translate(-50%,0);}
.fb-toast .sp{color:var(--accent);}
.aifb{display:flex;align-items:center;gap:8px;margin-top:14px;flex-wrap:wrap;}
.aifb-l{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-4);}
.aifb button.tb{width:27px;height:27px;border-radius:7px;display:grid;place-items:center;color:var(--ink-4);
  border:1px solid transparent;transition:color .12s,background .12s,border-color .12s;}
.aifb button.tb:hover{color:var(--ink-2);background:var(--paper-2);}
.aifb button.tb.on{color:var(--accent);background:var(--accent-soft);border-color:color-mix(in oklab,var(--accent),transparent 75%);}
.aifb button.tb.on-neg{color:var(--neg);background:color-mix(in oklab,var(--neg),transparent 90%);border-color:color-mix(in oklab,var(--neg),transparent 75%);}
.aifb-why{width:100%;display:flex;flex-wrap:wrap;gap:7px;align-items:center;animation:fade-in .2s ease-out;}
.aifb-chip{font-size:11.5px;padding:5px 11px;border-radius:999px;border:1px solid var(--hair);color:var(--ink-3);transition:all .12s;}
.aifb-chip.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft);}
.aifb-inp{flex:1;min-width:170px;height:30px;padding:0 10px;font-size:12px;border:1px solid var(--hair);border-radius:8px;
  background:var(--paper);color:var(--ink);}
.aifb-send{height:30px;padding:0 13px;border-radius:8px;background:var(--accent);color:#fff;font-size:12px;font-weight:500;}
.aifb-done{font-size:11.5px;color:var(--pos);}
.shr{position:relative;display:inline-flex;}
.shr-btn{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 11px;border:1px solid var(--hair);
  border-radius:8px;background:var(--surface);font-size:12px;color:var(--ink-2);transition:color .12s,border-color .12s;}
.shr-btn:hover{color:var(--accent);border-color:color-mix(in oklab,var(--accent),transparent 75%);}
.shr-btn .n{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--accent);background:var(--accent-soft);
  border-radius:999px;padding:1px 6px;}
.shr-pop{position:absolute;top:34px;right:0;z-index:90;width:302px;background:var(--surface);border:1px solid var(--hair);
  border-radius:12px;box-shadow:var(--shadow-2);padding:10px;animation:fade-in .15s ease-out;text-align:left;}
.shr-h{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--ink-4);margin:2px 2px 8px;}
.shr-row{display:flex;align-items:center;gap:9px;width:100%;padding:7px 8px;border-radius:8px;font-size:12.5px;
  color:var(--ink-2);text-align:left;transition:background .12s;}
.shr-row:hover{background:var(--paper-2);}
.shr-row .ava{width:24px;height:24px;border-radius:50%;background:var(--accent-soft);color:var(--accent);
  display:grid;place-items:center;font-size:10px;font-weight:600;flex:none;}
.shr-row .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.shr-row .st{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--ink-4);flex:none;}
.shr-row.on .st{color:var(--pos);}
.shr-row.on:hover .st{color:var(--neg);}
.shr-div{border-top:1px solid var(--hair);margin:8px 0;}
.shr-q{width:100%;height:30px;padding:0 10px;font-size:12px;border:1px solid var(--hair);border-radius:8px;
  background:var(--paper);color:var(--ink);margin-bottom:6px;}
.shr-list{max-height:210px;overflow:auto;}
.shr-empty{font-size:12px;color:var(--ink-4);padding:10px;text-align:center;}
.shr-foot{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--ink-4);margin-top:8px;
  line-height:1.5;padding:0 2px;}
.shr-owner{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--ink-3);
  border:1px solid var(--hair);border-radius:999px;padding:3px 10px;}
`;
function fbToast(text,sparkle){
  try{
    const el=document.createElement("div"); el.className="fb-toast";
    if(sparkle){const s=document.createElement("span");s.className="sp";s.textContent="✦ ";el.appendChild(s);}
    el.appendChild(document.createTextNode(text));
    document.body.appendChild(el);
    requestAnimationFrame(()=>el.classList.add("on"));
    setTimeout(()=>{el.classList.remove("on");setTimeout(()=>el.remove(),300);},2600);
  }catch{}
}
const IcTUp=({s=13})=><svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M7 10v11"/><path d="M7 11l4-8.3c1-.4 2.5.2 2.5 1.8V9h4.9c1.2 0 2.1 1.1 1.9 2.3l-1.2 6.9c-.2 1-1 1.7-2 1.7H7"/></svg>;
const IcTDn=({s=13})=><svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><g transform="rotate(180 12 12)"><path d="M7 10v11"/><path d="M7 11l4-8.3c1-.4 2.5.2 2.5 1.8V9h4.9c1.2 0 2.1 1.1 1.9 2.3l-1.2 6.9c-.2 1-1 1.7-2 1.7H7"/></g></svg>;

// панель оценки под ответом ИИ-аналитика: 👍 = удачно (учит и рекомендации),
// 👎 = причины+комментарий → команде на разбор
const AIFB_REASONS=[["offtopic","не по делу"],["shallow","мало конкретики"],
                    ["wrong","ошибка в данных"],["long","слишком длинно"]];
function AiFbBar({q,text,sessionId,mode,fbMap}){
  const key="s"+(sessionId||0)+":"+fyHash((q||"")+"|"+(text||"").slice(0,180));
  const[v,setV]=useState(0);
  useEffect(()=>{ if(fbMap&&fbMap[key]) setV(fbMap[key]); },[fbMap,key]);
  const[open,setOpen]=useState(false);
  const[rs,setRs]=useState({});
  const[comment,setComment]=useState("");
  const[sent,setSent]=useState(false);
  const post=(verdict,extra)=>apiPost("/api/feedback",{kind:"ai_answer",item_key:key,verdict,
    payload:{question:(q||"").slice(0,300),mode:mode||"quick",session_id:sessionId,...(extra||{})}}).catch(()=>{});
  const like=()=>{const nv=v===1?0:1;setV(nv);setOpen(false);post(1);
    if(nv===1)fbToast("Спасибо! Удачный ответ — учтём и в ваших рекомендациях",true);};
  const dislike=()=>{const nv=v===-1?0:-1;setV(nv);setSent(false);
    if(nv===-1){setOpen(true);post(-1);}else{setOpen(false);post(-1);}};
  const send=()=>{post(-1,{reasons:Object.keys(rs).filter(k=>rs[k]),comment:comment.slice(0,300)});
    setSent(true);setOpen(false);fbToast("Спасибо — команда разберёт этот ответ");};
  return <div className="aifb" onClick={e=>e.stopPropagation()}>
    <span className="aifb-l">Оценить ответ</span>
    <button className={"tb"+(v===1?" on":"")} title="Полезный ответ" onClick={like}><IcTUp/></button>
    <button className={"tb"+(v===-1?" on-neg":"")} title="Плохой ответ — команда разберёт" onClick={dislike}><IcTDn/></button>
    {sent&&v===-1&&<span className="aifb-done">отправлено — разберём ✓</span>}
    {open&&v===-1&&<div className="aifb-why">
      {AIFB_REASONS.map(([k,l])=><button key={k} className={"aifb-chip"+(rs[k]?" on":"")}
        onClick={()=>setRs(r=>({...r,[k]:!r[k]}))}>{l}</button>)}
      <input className="aifb-inp" placeholder="что не так? (необязательно)" value={comment}
        onChange={e=>setComment(e.target.value)} onKeyDown={e=>{if(e.key==="Enter")send();}}/>
      <button className="aifb-send" onClick={send}>Отправить</button>
    </div>}
  </div>;
}

// шеринг отчёта (Фаза 5): владелец открывает доступ всем или адресно, с отзывом
const IcShare=()=><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4"/></svg>;
function ShareButton({reportId}){
  const[open,setOpen]=useState(false);
  const[users,setUsers]=useState(null);
  const[shares,setShares]=useState(null);
  const[q,setQ]=useState("");
  const ref=useRef(null);
  const load=()=>{
    apiFetch("/api/users").then(d=>setUsers(d.users||[])).catch(()=>setUsers([]));
    apiFetch(`/api/reports/${reportId}/shares`).then(d=>setShares(d.shares||[])).catch(()=>setShares([]));
  };
  useEffect(()=>{ if(open)load(); },[open,reportId]); // eslint-disable-line
  useEffect(()=>{ if(!open)return;
    const onDoc=(e)=>{ if(ref.current&&!ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown",onDoc);
    return ()=>document.removeEventListener("mousedown",onDoc);
  },[open]);
  const shareTo=async(username,label)=>{ try{
      await apiPost(`/api/reports/${reportId}/share`,{shared_with:username});
      fbToast(username?"Отчёт доступен: "+label:"Отчёт открыт всем пользователям AuditLens",true);
      load();
    }catch{ fbToast("Не удалось поделиться"); } };
  const revoke=async(shareId)=>{ try{ await apiPost(`/api/shares/${shareId}/revoke`,{}); load(); }catch{} };
  const activeAll=(shares||[]).find(s=>!s.shared_with);
  const byUser={}; (shares||[]).forEach(s=>{ if(s.shared_with) byUser[s.shared_with]=s; });
  const n=(shares||[]).length;
  const flt=(users||[]).filter(u=>!q||((u.display_name||u.username).toLowerCase().includes(q.toLowerCase())));
  return <span className="shr" ref={ref}>
    <button className="shr-btn" onClick={()=>setOpen(o=>!o)} title="Поделиться отчётом с коллегами">
      <IcShare/>Поделиться{n>0&&<span className="n">{n}</span>}
    </button>
    {open&&<div className="shr-pop" onClick={e=>e.stopPropagation()}>
      <div className="shr-h">Доступ к отчёту</div>
      <button className={"shr-row all"+(activeAll?" on":"")}
              title={activeAll?"Клик — закрыть общий доступ":"Открыть отчёт всем пользователям"}
              onClick={()=>activeAll?revoke(activeAll.share_id):shareTo(null,null)}>
        <span className="ava">✦</span>
        <span className="nm">Всем пользователям AuditLens</span>
        <span className="st">{activeAll?"✓ открыт":"открыть"}</span>
      </button>
      <div className="shr-div"/>
      <input className="shr-q" placeholder="Найти коллегу…" value={q} onChange={e=>setQ(e.target.value)}/>
      <div className="shr-list">
        {users===null?<div className="shr-empty">Загрузка…</div>
          :flt.length===0?<div className="shr-empty">{q?"Не найдено":"Коллеги появятся здесь после первого входа в инструмент"}</div>
          :flt.map(u=>{ const s=byUser[u.username]; const label=u.display_name||u.username;
            return <button key={u.username} className={"shr-row"+(s?" on":"")}
                title={s?"Клик — отозвать доступ":"Дать доступ"}
                onClick={()=>s?revoke(s.share_id):shareTo(u.username,label)}>
              <span className="ava">{initials(label)}</span>
              <span className="nm">{label}</span>
              <span className="st">{s?"✓ доступ":"дать доступ"}</span>
            </button>; })}
      </div>
      <div className="shr-foot">Коллеги найдут отчёт в истории (⌘K) → Отчёты → «Поделились со мной»</div>
    </div>}
  </span>;
}

// кольцо «Сила персонализации» (профиль)
function PfRing({score}){
  const r=26,c=2*Math.PI*r;
  return <svg width="72" height="72" viewBox="0 0 64 64" aria-hidden="true">
    <circle cx="32" cy="32" r={r} fill="none" stroke="var(--hair)" strokeWidth="5"/>
    <circle cx="32" cy="32" r={r} fill="none" stroke="var(--accent)" strokeWidth="5" strokeLinecap="round"
      strokeDasharray={c} strokeDashoffset={c*(1-Math.min(score,100)/100)} transform="rotate(-90 32 32)"
      style={{transition:"stroke-dashoffset .6s ease"}}/>
    <text x="32" y="37" textAnchor="middle" fontSize="14" fontWeight="600" fill="var(--ink)"
      fontFamily="'Geist','Inter',sans-serif">{score}%</text>
  </svg>;
}

// ─── «Общий / Для вас»: сегмент-переключатель + персональный разворот ─────────
const OVSEG_CSS=`
.ovseg{position:relative;display:inline-flex;padding:3px;background:var(--paper-2);border:1px solid var(--hair);border-radius:10px;user-select:none;}
.ovseg-thumb{position:absolute;top:3px;left:3px;height:calc(100% - 6px);width:104px;background:var(--surface);border-radius:7px;
  box-shadow:var(--shadow-1);transition:transform .18s cubic-bezier(.3,.7,.4,1);}
.ovseg.fy .ovseg-thumb{transform:translateX(104px);}
.ovseg button{position:relative;z-index:1;width:104px;height:26px;display:inline-flex;align-items:center;justify-content:center;gap:6px;
  font-size:12.5px;color:var(--ink-3);border-radius:7px;transition:color .15s;}
.ovseg button.on{color:var(--ink);font-weight:500;}
.ovseg .sp{color:var(--accent);font-size:11px;line-height:1;}
.ovseg-wrap{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);}
.fy-seg-mob{display:none;margin-bottom:18px;}
/* 960px — тот же брейкпоинт, что у .desk-only: без «мёртвой зоны» 901-960 */
@media(max-width:960px){.fy-seg-mob{display:flex;justify-content:center;}}
`;
function OvSeg({page}){
  const go=(p)=>{ if(p===page)return; try{localStorage.setItem("al-ov-mode",p);}catch{} location.hash=p; };
  return <div className={"ovseg"+(page==="foryou"?" fy":"")} role="tablist" aria-label="Режим обзора">
    <style>{OVSEG_CSS}</style>
    <span className="ovseg-thumb"/>
    <button className={page==="overview"?"on":""} onClick={()=>go("overview")}>Общий</button>
    <button className={page==="foryou"?"on":""} onClick={()=>go("foryou")}><span className="sp">✦</span>Для вас</button>
  </div>;
}

const FY_SRC={cbr_press:"ЦБ РФ",cbr_news:"ЦБ РФ",banki_news:"Банки.ру",frankmedia:"Frank Media",
  tg_cbr:"ЦБ · Telegram",tg_banksta:"Банкста",tg_cyberpolice:"Киберполиция",tg_frankmedia:"Frank Media",
  tg_kommersant:"Коммерсантъ",tg_rbc:"РБК",web_search:"веб-поиск"};
const fySrcName=(t)=>FY_SRC[t.source]||t.domain||"источник";
const fyHash=(s)=>{let h=0;for(let i=0;i<(s||"").length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h);};

// мини-спарклайн тренда (инлайновый SVG, без библиотек)
function FySpark({series,w=118,h=30}){
  const vals=(series||[]).map(p=>(p&&p.n)||0);
  if(vals.length<3) return null;
  const max=Math.max(...vals,1),min=Math.min(...vals);
  const pts=vals.map((v,i)=>[i/(vals.length-1)*w,h-3-((v-min)/((max-min)||1))*(h-8)]);
  const d=pts.map((p,i)=>(i?"L":"M")+p[0].toFixed(1)+","+p[1].toFixed(1)).join("");
  const last=pts[pts.length-1];
  return <svg className="spark" width={w} height={h} viewBox={"0 0 "+w+" "+h} aria-hidden="true">
    <path d={d+"L"+w.toFixed(1)+","+(h-1)+"L0,"+(h-1)+"Z"} fill="var(--accent-soft)" opacity=".5"/>
    <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.4" strokeLinejoin="round" strokeLinecap="round"/>
    <circle cx={last[0]} cy={last[1]} r="2.2" fill="var(--accent)"/>
  </svg>;
}

const FY_CSS=`
.fy-head{margin-bottom:4px;}
.fy-ai{color:var(--accent);}
.fy-lede{font-family:'Source Serif 4',Georgia,serif;font-size:18.5px;line-height:1.56;color:var(--ink-2);max-width:66ch;text-wrap:pretty;}
.fy-chips{display:flex;gap:7px;flex-wrap:wrap;margin-top:15px;align-items:center;}
.fy-chip{font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3);
  border:1px solid var(--hair);border-radius:999px;padding:4px 11px;}
.fy-chip.acc{color:var(--accent);border-color:color-mix(in oklab,var(--accent),transparent 75%);background:var(--accent-soft);}
.fy-tune{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-4);cursor:pointer;transition:color .12s;}
.fy-tune:hover{color:var(--accent);}
.fy-hint{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-4);}
.fy-hint a{color:var(--accent);cursor:pointer;}
.fy-sec{margin-top:30px;}
.fy-checks-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:12px;margin-top:12px;}
.fy-check{display:flex;gap:12px;align-items:flex-start;background:var(--surface);border:1px solid var(--hair);border-radius:var(--r-lg);
  padding:14px 16px;transition:border-color .15s,box-shadow .15s;}
.fy-check:hover{border-color:color-mix(in oklab,var(--accent),transparent 78%);box-shadow:var(--shadow-1);}
.fy-check .n{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--accent);padding-top:2px;flex:none;}
.fy-check .t{font-size:13.5px;font-weight:500;line-height:1.45;min-width:0;}
.fy-check .w{font-size:12px;font-weight:400;color:var(--ink-3);margin-top:4px;line-height:1.5;}
.fy-check .ask{margin-left:auto;width:28px;height:28px;border-radius:7px;display:grid;place-items:center;color:var(--ink-4);flex:none;transition:color .12s,background .12s;}
.fy-check .ask:hover{color:var(--accent);background:var(--accent-soft);}
.fy-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(225px,1fr));gap:12px;margin-top:12px;}
.fy-card{background:var(--surface);border:1px solid var(--hair);border-radius:var(--r-lg);padding:16px 18px 13px;cursor:pointer;
  transition:transform .15s,box-shadow .15s,border-color .15s;}
.fy-card:hover{transform:translateY(-2px);box-shadow:var(--shadow-2);border-color:var(--hair-2);}
.fy-card .lbl{font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-3);
  margin-bottom:9px;display:flex;justify-content:space-between;gap:8px;}
.fy-card .num{font-family:'Source Serif 4',Georgia,serif;font-size:29px;line-height:1;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;}
.fy-card .num small{font-size:12px;color:var(--ink-3);font-family:'Geist','Inter',sans-serif;}
.fy-card .delta{font-size:11.5px;font-weight:600;font-family:'Geist','Inter',sans-serif;}
.fy-card .delta.up{color:var(--neg);}
.fy-card .delta.down{color:var(--pos);}
.fy-card .spark{display:block;margin:11px 0 6px;}
.fy-card .meta{font-size:11.5px;color:var(--ink-3);line-height:1.5;}
.fy-card .meta b{color:var(--ink-2);font-weight:500;}
.fy-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:13px;margin-top:12px;grid-auto-flow:dense;}
@media(max-width:1200px){.fy-grid{grid-template-columns:repeat(2,1fr);}}
@media(max-width:560px){.fy-grid{grid-template-columns:1fr;}}
.fy-tile{position:relative;display:flex;flex-direction:column;background:var(--surface);border:1px solid var(--hair);border-radius:var(--r-lg);
  overflow:hidden;cursor:pointer;transition:transform .16s,box-shadow .16s;}
.fy-tile:hover{transform:translateY(-2px);box-shadow:var(--shadow-2);}
.fy-tile.hero{grid-column:span 2;grid-row:span 2;}
@media(max-width:560px){.fy-tile.hero{grid-column:span 1;}}
.fy-tile .img{height:96px;background-size:cover;background-position:center;flex:none;}
.fy-tile.hero .img{flex:1;min-height:210px;height:auto;}
.fy-tile .img.ph{display:grid;place-items:center;}
.fy-tile .img.ph span{font-family:'Instrument Serif',serif;font-size:34px;color:color-mix(in oklab,var(--ink),transparent 60%);}
.fy-g0{background:linear-gradient(135deg,var(--accent-soft),color-mix(in oklab,var(--accent),var(--paper) 80%));}
.fy-g1{background:linear-gradient(135deg,var(--paper-2),color-mix(in oklab,var(--ink),var(--paper) 88%));}
.fy-g2{background:linear-gradient(160deg,color-mix(in oklab,var(--pos),var(--paper) 86%),var(--paper-2));}
.fy-g3{background:linear-gradient(150deg,color-mix(in oklab,var(--warn),var(--paper) 86%),var(--paper-2));}
.fy-g4{background:linear-gradient(140deg,color-mix(in oklab,var(--accent),var(--paper) 90%),color-mix(in oklab,var(--ink),var(--paper) 92%));}
.fy-tile .body{padding:12px 14px 13px;display:flex;flex-direction:column;gap:7px;flex:none;min-height:0;}
.fy-tile .src{display:flex;align-items:center;gap:7px;font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.05em;
  text-transform:uppercase;color:var(--ink-3);}
.fy-tile .src .dt{margin-left:auto;color:var(--ink-4);text-transform:none;letter-spacing:0;}
.fy-tile .sev{width:6px;height:6px;border-radius:50%;flex:none;}
.fy-tile .sev.red{background:var(--neg);} .fy-tile .sev.amber{background:var(--warn);} .fy-tile .sev.green{background:var(--pos);}
.fy-tile .tt{font-family:'Source Serif 4',Georgia,serif;font-size:14.5px;line-height:1.38;color:var(--ink);
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;}
.fy-tile.hero .tt{font-size:19px;}
.fy-tile .sum{font-size:12.5px;color:var(--ink-3);line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.fy-tile .why{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--accent);text-transform:uppercase;letter-spacing:.04em;}
.fy-tile .acts{position:absolute;top:8px;right:8px;display:flex;gap:5px;opacity:0;transition:opacity .15s;z-index:2;}
.fy-tile:hover .acts{opacity:1;}
.fy-tile .acts button{width:26px;height:26px;border-radius:7px;background:color-mix(in oklab,var(--paper),transparent 10%);
  backdrop-filter:blur(6px);border:1px solid var(--hair);display:grid;place-items:center;font-size:12px;color:var(--ink-2);transition:color .12s;}
.fy-tile .acts button:hover{color:var(--accent);}
.fy-tar{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;}
@media(max-width:900px){.fy-tar{grid-template-columns:1fr;}}
.fy-tar .surface{padding:15px 18px 8px;}
.fy-tar-row{display:flex;justify-content:space-between;align-items:baseline;gap:12px;padding:8px 0;border-top:1px solid var(--hair);font-size:13px;}
.fy-tar-row:first-of-type{border-top:0;}
.fy-tar-row .r{white-space:nowrap;font-size:12.5px;}
.fy-trust{margin-top:36px;padding-top:14px;border-top:1px solid var(--hair);display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;
  font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-4);}
.fy-trust a{color:var(--ink-3);cursor:pointer;transition:color .12s;}
.fy-trust a:hover{color:var(--accent);}
.fy-tile .acts button.on{color:var(--accent);border-color:color-mix(in oklab,var(--accent),transparent 65%);}
.fy-tile.liked{border-color:color-mix(in oklab,var(--accent),transparent 55%);}
.fy-tile.liked::after{content:"✓ в фокусе";position:absolute;top:8px;left:8px;z-index:2;
  font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.04em;color:var(--accent);
  background:color-mix(in oklab,var(--paper),transparent 8%);backdrop-filter:blur(4px);
  border:1px solid color-mix(in oklab,var(--accent),transparent 70%);padding:2px 7px;border-radius:999px;}
.fy-check .acts2{margin-left:auto;display:flex;gap:2px;flex:none;}
.fy-check .acts2 button{width:26px;height:26px;border-radius:7px;display:grid;place-items:center;color:var(--ink-4);
  transition:color .12s,background .12s;}
.fy-check .acts2 button:hover{color:var(--accent);background:var(--accent-soft);}
.fy-check .acts2 button.on{color:var(--accent);background:var(--accent-soft);}
.fy-pshint{margin-top:13px;display:inline-flex;align-items:center;gap:7px;cursor:pointer;
  font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:.03em;color:var(--ink-3);
  border:1px dashed color-mix(in oklab,var(--accent),transparent 65%);border-radius:999px;padding:5px 13px;
  transition:color .12s,border-color .12s;}
.fy-pshint:hover{color:var(--accent);border-color:var(--accent);}
.fy-pshint .pc{color:var(--accent);font-weight:600;}
`;

// плитка новости (Perplexity-стиль): картинка или детерминированный градиент-фолбэк
function FyTile({t,hero,fb,onFb}){
  const[imgOk,setImgOk]=useState(true);
  const src=fySrcName(t);
  const open=()=>{if(t.url)window.open(t.url,"_blank","noopener");};
  return <div className={"fy-tile"+(hero?" hero":"")+(fb===1?" liked":"")} onClick={open} role="link" tabIndex={0}
              onKeyDown={e=>{if(e.key==="Enter")open();}}>
    {t.image&&imgOk
      ?<div className="img" style={{backgroundImage:"url("+JSON.stringify(t.image)+")"}}>
         <img src={t.image} alt="" style={{display:"none"}} loading="lazy" referrerPolicy="no-referrer" onError={()=>setImgOk(false)}/>
       </div>
      :<div className={"img ph fy-g"+(fyHash(t.source||t.domain||t.title||"")%5)}><span>{(src[0]||"·").toUpperCase()}</span></div>}
    <div className="body">
      <div className="src">
        {t.severity&&<span className={"sev "+t.severity}/>}
        {src}
        {t.ts&&<span className="dt">{new Date(t.ts).toLocaleDateString("ru",{day:"numeric",month:"short"})}</span>}
      </div>
      <div className="tt">{t.title}</div>
      {hero&&t.summary?<div className="sum">{t.summary}</div>:null}
      {t.reason&&<div className="why">{t.reason}</div>}
    </div>
    <span className="acts" onClick={e=>e.stopPropagation()}>
      <button title="Разобрать с ИИ" onClick={()=>bfGoAI("Разбери подробно для внутреннего аудита Сбера: "+(t.title||""))}>✦</button>
      <button className={fb===1?"on":""} title="Интересно — больше такого" onClick={()=>onFb(t,1)}><IcTUp s={12}/></button>
      <button title="Не интересно — меньше такого" onClick={()=>onFb(t,-1)}><IcTDn s={12}/></button>
    </span>
  </div>;
}

function ForYouPage(){
  const me=useMe();
  const[p,setP]=useState(undefined);          // undefined=грузится, null=выкл/ошибка
  const[err,setErr]=useState(false);
  const[busy,setBusy]=useState(false);
  const[gone,setGone]=useState({});
  const[goneChk,setGoneChk]=useState({});
  const[fb,setFb]=useState({});               // {key: verdict} — оценки плиток
  const[cfb,setCfb]=useState({});             // оценки зацепок
  const load=()=>apiFetch("/api/overview/foryou")
    .then(d=>{setP(d.foryou||null);setErr(false);})
    .catch(()=>{setP(null);setErr(true);});
  useEffect(()=>{load();
    apiFetch("/api/feedback?kind=news").then(d=>setFb(d.items||{})).catch(()=>{});
    apiFetch("/api/feedback?kind=check").then(d=>setCfb(d.items||{})).catch(()=>{});
  },[]);
  const refresh=async()=>{ if(busy)return; setBusy(true);
    try{const d=await apiPost("/api/overview/foryou/refresh",{});setP(d.foryou||null);}catch{}
    setBusy(false); };
  // 👍/👎 на плитке: мгновенная локальная реакция + обучение весов тем/источников
  const onTileFb=(t,verdict)=>{
    const key=t.url||t.title; if(!key)return;
    const nv=(fb[key]||0)===verdict?0:verdict;
    setFb(m=>({...m,[key]:nv}));
    if(verdict===-1&&nv===-1){ setGone(g=>({...g,[t.title]:1}));
      fbToast("Понял — такого будет меньше в вашей подборке",true); }
    if(verdict===1&&nv===1) fbToast("Учтём в вашей подборке",true);
    apiPost("/api/feedback",{kind:"news",item_key:key,verdict,
      topics:t.reason_slugs||[],
      payload:{title:t.title,source:t.source,reason:t.reason}}).catch(()=>{});
  };
  const onCheckFb=(c,verdict)=>{
    const key=c.title; if(!key)return;
    const nv=(cfb[key]||0)===verdict?0:verdict;
    setCfb(m=>({...m,[key]:nv}));
    if(verdict===-1&&nv===-1){ setGoneChk(g=>({...g,[key]:1}));
      fbToast("Понял — не то. Научимся предлагать точнее",true); }
    if(verdict===1&&nv===1) fbToast("Учтём — таких зацепок будет больше",true);
    apiPost("/api/feedback",{kind:"check",item_key:key,verdict,
      payload:{title:c.title,why:c.why}}).catch(()=>{});
  };
  const goProfile=()=>{location.hash="profile";};

  if(p===undefined) return <div className="fade-in">
    <style>{FY_CSS}</style>
    <div className="fy-seg-mob"><OvSeg page="foryou"/></div>
    <div className="skel" style={{height:13,width:260,marginBottom:16,borderRadius:6}}/>
    <div className="skel" style={{height:44,width:"54%",marginBottom:10,borderRadius:8}}/>
    <div className="skel" style={{height:20,width:"68%",marginBottom:28,borderRadius:6}}/>
    <div className="fy-grid">{[0,1,2,3,4].map(i=><div key={i} className="skel" style={{height:i===0?260:150,borderRadius:10,gridColumn:i===0?"span 2":undefined,gridRow:i===0?"span 2":undefined}}/>)}</div>
  </div>;

  if(err) return <div className="fade-in">
    <style>{FY_CSS}</style><div className="fy-seg-mob"><OvSeg page="foryou"/></div>
    <ErrState msg="Не удалось собрать персональную страницу. Обновите страницу или попробуйте позже."/>
  </div>;

  if(p===null) return <div className="fade-in">
    <style>{FY_CSS}</style><div className="fy-seg-mob"><OvSeg page="foryou"/></div>
    <div style={{padding:"72px 24px",textAlign:"center",maxWidth:500,margin:"0 auto"}}>
      <div style={{fontSize:26,marginBottom:12,color:"var(--accent)"}}>✦</div>
      <div className="t-h" style={{marginBottom:8}}>Персонализация выключена</div>
      <p className="t-cap" style={{marginBottom:20,textWrap:"pretty"}}>Включите персональный дайджест — и эта страница будет собираться каждое утро под вашу зону ответственности в Сбере: направления, новости, зацепки для проверок.</p>
      <button className="btn btn-accent" onClick={async()=>{try{await apiPut("/api/me",{prefs:{personal_digest:true}});setP(undefined);load();}catch{}}}>Включить персонализацию</button>
    </div>
  </div>;

  const hl=p.headline||"Ваша повестка на сегодня";
  const hh=bfPickHot(hl,p.hot||"");
  const genAt=p.generated_at?new Date(p.generated_at):null;
  const tiles=(()=>{ const arr=(p.news||[]).filter(t=>t&&t.title&&!gone[t.title]).slice(0,8);
    const hi=arr.findIndex(t=>t.image);
    if(hi>0){const[t]=arr.splice(hi,1);arr.unshift(t);}
    return arr; })();
  const focus=p.focus||[], checks=p.checks||[];
  const tar=p.tariffs||{}, gap=tar.gap||[], moves=tar.moves||[];
  const openReviews=(c)=>{ try{sessionStorage.setItem("al-rv-prefilter",
      JSON.stringify({bank:"Сбербанк",product:c.product||""}));}catch{}
    location.hash="reviews"; };

  return <div className="fade-in">
    <style>{FY_CSS}</style>
    <div className="fy-seg-mob"><OvSeg page="foryou"/></div>

    {/* ① персональный masthead */}
    <header className="fy-head">
      <div className="eyebrow-row">
        <div className="eyebrow">Для вас · {new Date().toLocaleDateString("ru",{day:"numeric",month:"long"})} · <span className="fy-ai">✦ собрано под ваш профиль</span></div>
        <div style={{display:"flex",alignItems:"center",gap:12}}>
          {busy?
            <span className="bf-live"><span className="dot"/>пересобираю…</span>:
            genAt&&<span className="bf-stamp">обновлено {genAt.toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})}</span>}
          <button className="bf-refresh" onClick={refresh} disabled={busy} title="Пересобрать под профиль">⟳</button>
        </div>
      </div>
      <h1 className="t-display" style={{maxWidth:"26ch",marginBottom:12}}>
        {hh?<>{hl.slice(0,hh[0])}<em style={{fontStyle:"italic",color:"var(--accent)"}}>{hl.slice(hh[0],hh[0]+hh[1])}</em>{hl.slice(hh[0]+hh[1])}</>:hl}
      </h1>
      {p.lead?<p className="fy-lede">{p.lead}</p>
        :!p.has_profile?<p className="fy-lede" style={{color:"var(--ink-3)"}}>Опишите в профиле, что вы проверяете в Сбере — и каждое утро здесь будет личная сводка. <a style={{color:"var(--accent)",cursor:"pointer"}} onClick={goProfile}>Настроить →</a></p>
        :<p className="fy-lede" style={{color:"var(--ink-3)"}}>По вашим темам сегодня спокойно — ниже общая картина по вашим направлениям.</p>}
      {(p.top_topics||[]).length>0&&<div className="fy-chips">
        {p.top_topics.slice(0,5).map((t,i)=><span key={t} className={"fy-chip"+(i===0?" acc":"")}>{t}</span>)}
        <span className="fy-tune" onClick={goProfile}>настроить →</span>
      </div>}
      {(()=>{ const ps=me&&me.personalization;
        if(!ps||ps.score>=60) return null;
        const next=(ps.parts||[]).find(x=>!x.done&&x.cta);
        return <div className="fy-pshint" onClick={goProfile} title="Открыть профиль">
          ✦ персонализация <span className="pc">{ps.score}%</span> · {next?next.cta:"уточните профиль"} →
        </div>; })()}
    </header>

    {/* ② что проверить сегодня (ИИ-зацепки) */}
    {checks.filter(c=>!goneChk[c.title]).length>0&&<section className="fy-sec">
      <div className="eyebrow">Что проверить сегодня · <span className="fy-ai">✦ по сигналам дня</span></div>
      <div className="fy-checks-row">
        {checks.filter(c=>!goneChk[c.title]).map((c,i)=><div key={c.title} className="fy-check">
          <span className="n">{String(i+1).padStart(2,"0")}</span>
          <div className="t">{c.title}{c.why&&<div className="w">{c.why}</div>}</div>
          <span className="acts2">
            <button title="Составить план проверки с ИИ"
                    onClick={()=>bfGoAI("Проверка в Сбере: "+c.title+". "+(c.why||"")+" Составь детальный план аудиторской проверки по этому пункту.")}>✦</button>
            <button className={cfb[c.title]===1?"on":""} title="Полезная зацепка" onClick={()=>onCheckFb(c,1)}><IcTUp s={12}/></button>
            <button title="Не то — научимся точнее" onClick={()=>onCheckFb(c,-1)}><IcTDn s={12}/></button>
          </span>
        </div>)}
      </div>
    </section>}

    {/* ③ стат-карты направлений */}
    {focus.length>0&&<section className="fy-sec">
      <div className="eyebrow-row">
        <div className="eyebrow">Ваши направления · жалобы Сбера · 90 дней</div>
        {p.default_focus&&<span className="fy-hint">стартовый набор — <a onClick={goProfile}>уточните профиль</a></span>}
      </div>
      <div className="fy-cards">
        {focus.map(c=>{
          const st=c.stats;
          const d=st&&typeof st.delta_pct==="number"?st.delta_pct:null;
          return <div key={c.slug} className="fy-card" onClick={()=>openReviews(c)} title="Открыть в «Отзывах»">
            <div className="lbl"><span>{c.label}</span>{st&&st.market_rank?<span>#{st.market_rank} на рынке</span>:null}</div>
            {st?<div className="num tnum">{(st.total||0).toLocaleString("ru")}<small>жалоб</small>
                {d!=null&&!st.delta_low_n&&<span className={"delta "+(d>0?"up":"down")}>{d>0?"+":""}{Math.round(d)}%</span>}</div>
              :<div style={{fontSize:14,color:"var(--ink-3)",padding:"6px 0"}}>отдельного среза по продукту нет</div>}
            <FySpark series={c.trend}/>
            <div className="meta">
              {c.theme?<>горячая тема: <b>{c.theme.label}</b>{typeof c.theme.delta_pct==="number"&&c.theme.delta_pct>0?" · +"+Math.round(c.theme.delta_pct)+"%":""}</>
                :st&&st.market_share_pct!=null?<>доля рынка жалоб: {st.market_share_pct}%</>
                :<span style={{color:"var(--ink-4)"}}>клик — все отзывы</span>}
            </div>
          </div>;})}
      </div>
    </section>}

    {/* ④ новостная сетка (Perplexity-стиль) */}
    {tiles.length>0&&<section className="fy-sec">
      <div className="eyebrow-row">
        <div className="eyebrow">Новости для вас · <span className="fy-ai">✦ отобрано по профилю</span></div>
        <span className="fy-hint">👍/👎 на плитках учат подборку</span>
      </div>
      <div className="fy-grid">
        {tiles.map((t,i)=><FyTile key={t.url||t.title} t={t} hero={i===0&&!!t.image}
          fb={fb[t.url||t.title]||0} onFb={onTileFb}/>)}
      </div>
    </section>}

    {/* ⑤ тарифы: Сбер на фоне рынка */}
    {(gap.length>0||moves.length>0)&&<section className="fy-sec">
      <div className="eyebrow">Тарифы в ваших категориях{tar.key_rate!=null?" · ключевая "+tar.key_rate+"%":""}</div>
      <div className="fy-tar">
        {gap.length>0&&<div className="surface">
          <div className="t-cap" style={{marginBottom:4}}>Сбер против рынка (макс. ставка)</div>
          {gap.map(r=><div key={r.category} className="fy-tar-row">
            <span>{CAT_LABELS[r.category]||r.category}</span>
            <span className="mono tnum r">{r.sber_max!=null?(+r.sber_max).toFixed(2)+"%":"—"}
              <span style={{color:"var(--ink-4)"}}> · медиана {r.market_median!=null?(+r.market_median).toFixed(2)+"%":"—"}</span>
              {r.sber_vs_median_pp!=null&&<b style={{marginLeft:8,color:"var(--ink-2)"}}>{r.sber_vs_median_pp>0?"+":""}{(+r.sber_vs_median_pp).toFixed(2)} п.п.</b>}</span>
          </div>)}
        </div>}
        {moves.length>0&&<div className="surface">
          <div className="t-cap" style={{marginBottom:4}}>Движения за 7 дней</div>
          {moves.map((m,i)=><div key={i} className="fy-tar-row">
            <span style={{minWidth:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>
              {m.is_sber?<b style={{color:"var(--accent)"}}>Сбер</b>:m.bank}
              <span style={{color:"var(--ink-4)"}}> · {CAT_LABELS[m.category]||m.category}</span></span>
            <span className="mono tnum r">{m.from}→{m.to}
              {typeof m.delta==="number"&&<b style={{marginLeft:6,color:"var(--ink-2)"}}>{m.delta>0?"+":""}{m.delta} п.п.</b>}</span>
          </div>)}
        </div>}
      </div>
    </section>}

    {/* ⑥ подвал-доверие */}
    <footer className="fy-trust">
      <span>данные брифинга {p.digest_date||"—"} · заголовок, лид и зацепки — ИИ · 👍/👎 учат ваши рекомендации{p.feedback_used>0?" · учтено "+p.feedback_used+" ваших оценок":""}</span>
      <a onClick={goProfile}>настроить профиль →</a>
    </footer>
  </div>;
}

function OverviewPage(){
  const[dg,setDg]=useState(null);
  const[summary,setSummary]=useState(null);
  const[loading,setLoading]=useState(true);
  const[err,setErr]=useState(null);
  const[refreshBusy,setRefreshBusy]=useState(false);

  const loadDigest=()=>apiFetch("/api/overview/digest").then(d=>{setDg(d);return d;});
  useEffect(()=>{
    Promise.allSettled([loadDigest(),apiFetch("/api/summary")]).then(([d,s])=>{
      if(s.status==="fulfilled")setSummary(s.value);
      if(d.status==="rejected")setErr(String(d.reason&&d.reason.message||d.reason));
      setLoading(false);
    });
  },[]);
  // поллинг пока генерится (первый визит дня / ручной refresh)
  const refreshing=!!(dg&&dg.meta&&dg.meta.refreshing);
  useEffect(()=>{
    if(!refreshing)return;
    const id=setInterval(()=>{loadDigest().catch(()=>{});},20000);
    return()=>clearInterval(id);
  },[refreshing]);

  const manualRefresh=()=>{
    if(refreshBusy)return; setRefreshBusy(true);
    // Оптимистично включаем «обновляется»: сервер мог ещё не закоммитить
    // mark_run, и мгновенный GET вернул бы refreshing=false — поллинг не
    // стартовал бы и юзер не увидел бы новый выпуск. Поллинг сам сойдётся.
    const optimistic=()=>setDg(d=>d&&({...d,meta:{...d.meta,refreshing:true}}));
    apiPost("/api/overview/digest/refresh",{force:true})
      .then(optimistic)
      .catch(()=>{optimistic();/* 409 = уже генерится — тоже поллим */})
      .finally(()=>setRefreshBusy(false));
  };

  if(loading)return <LoadingPage/>;
  if(err&&!dg)return <ErrState msg={err}/>;

  // База пуста — CTA-блок с кнопкой запуска сбора
  const isEmpty=summary&&(summary.offers||0)===0&&(summary.banks||0)===0&&(summary.reviews||0)===0
    &&dg&&dg.meta&&dg.meta.empty;
  if(isEmpty)return <EmptyOverviewCta/>;

  // ── данные секций (любая может отсутствовать/деградировать) ──
  const sec=(dg&&dg.sections)||{};
  const P=n=>((sec[n]||{}).payload)||{};
  const ST=n=>(sec[n]||{}).status||"failed";
  const head=P("headline"), pulse=P("reviews_pulse"), tm=P("tariff_moves"),
        qo=P("quality_ops"), nw=P("news"), brief=P("reviews_brief");
  const isToday=!!(dg&&dg.meta&&dg.meta.today);
  const generating=refreshing&&(dg.meta.empty||!isToday);

  const issueDate=dg&&dg.date?new Date(dg.date+"T12:00:00"):new Date();
  const issueNum=Math.ceil((issueDate-new Date(issueDate.getFullYear(),0,0))/864e5);
  const genAt=dg&&dg.meta&&dg.meta.generated_at?new Date(dg.meta.generated_at):null;

  // пульс дня (детерминированный, живёт без LLM)
  const kr=tm.key_rate||{};
  const svmRows=tm.sber_gap||[];
  const deltas=svmRows.filter(r=>r.sber_vs_median_pp!=null).map(r=>parseFloat(r.sber_vs_median_pp));
  const avgDelta=deltas.length?deltas.reduce((a,b)=>a+b,0)/deltas.length:null;
  const ovl=pulse.overall||{};
  const flagsErr=qo.flags_err||0, flagsWarn=qo.flags_warn||0;
  const insights=head.insights||[];
  const newsGroups=nw.groups||[];
  const newsOk=(nw.sources||[]).filter(s=>s.ok).length, newsAll=(nw.sources||[]).length;
  const hl=head.headline||"", hot=head.hot||"";
  // данные плиток пульса
  const kpi=pulse.kpi||{}, esc=kpi.escalation_pct;
  const dlt=(dg&&dg.meta&&dg.meta.delta)||{};
  const dv=(pulse.diverge||[]).find(d=>d.gap!=null&&d.gap>=1.15)||null;  // ведущее расхождение
  const unc=pulse.unclassified||null;
  const up=(pulse.themes_up||[])[0]||null;
  const runsOk=(qo.runs||[]).filter(r=>r.status==="ok").length, runsAll=(qo.runs||[]).length;
  const hh=bfPickHot(hl,hot);   // [начало,длина] акцента — есть всегда, если есть заголовок
  // заголовок дня пишется по ведущему сигналу — его расчёт и раскрываем на акценте.
  // Если акцент — число, ищем сигнал, чьё значение в нём фигурирует.
  const leadIns=(()=>{
    if(!insights.length)return null;
    const frag=hh?hl.slice(hh[0],hh[0]+hh[1]):"";
    const num=(frag.match(/\d+[.,]?\d*/)||[])[0];
    if(num){
      const n=parseFloat(num.replace(",","."));
      const hit=insights.find(i=>{const d=i.data||{};
        return [d.ratio,d.week,d.delta,d.to,d.current,d.n_banks].some(v=>v!=null&&Math.abs(parseFloat(v)-n)<0.051);});
      if(hit)return hit;
    }
    return insights[0];
  })();
  const leadXp=leadIns?xpRows(leadIns.kind,leadIns.data||{}):[];

  return <div className="fade-in">
    <div className="fy-seg-mob"><OvSeg page="overview"/></div>
    {/* ⓪ ЛИЧНЫЙ СЛОЙ — опциональная полоса (prefs.personal_band_home), над передовицей */}
    <PersonalBand/>
    {/* ① MASTHEAD — передовица */}
    <header style={{marginBottom:26}}>
      <div className="eyebrow-row">
        <div className="eyebrow">
          Брифинг №{issueNum} · {issueDate.toLocaleDateString("ru",{weekday:"long",day:"numeric",month:"long"})} · розница / УВА
        </div>
        <div style={{display:"flex",alignItems:"center",gap:12}}>
          {refreshing?
            <span className="bf-live"><span className="dot"/>обновляется…</span>:
            genAt&&<span className="bf-stamp">сводка {genAt.toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})} МСК · действует до {String((dg&&dg.meta&&dg.meta.digest_hour_msk)??7).padStart(2,"0")}:00 МСК</span>}
          <button className="bf-refresh" onClick={manualRefresh} disabled={refreshBusy||refreshing}
            title="Перегенерировать выпуск">⟳</button>
        </div>
      </div>
      {generating&&!hl?
        <div>
          <div className="skel" style={{height:44,width:"58%",marginBottom:10,borderRadius:8}}/>
          <div className="skel" style={{height:44,width:"36%",marginBottom:14,borderRadius:8}}/>
          <p className="lede" style={{color:"var(--ink-3)"}}>Собираю сводку дня — первый визит за сегодня · ~1–2 мин. Цифры ниже уже живые.</p>
        </div>:
        <>
          <h1 className="t-display" style={{maxWidth:"26ch",marginBottom:12}}>
            {hh?<>{hl.slice(0,hh[0])}
              <Xp rows={leadXp} note={leadIns?leadIns.provenance:null}>
                <em style={{fontStyle:"italic",color:"var(--accent)"}}>{hl.slice(hh[0],hh[0]+hh[1])}</em>
              </Xp>
              {hl.slice(hh[0]+hh[1])}</>:hl||"Сводка дня"}
          </h1>
          {/* Вердикт дня вместо статистики генератора («3 риск-сигн · 8 новостей»
              ничего не меняли в решениях аудитора). Берём фразу от LLM, если она
              есть, иначе собираем детерминированно из тех же чисел. */}
          <p className="lede" style={{maxWidth:"70ch"}}>{head.quiet_note||bfVerdict(dv,esc,ovl,unc)}</p>
          <p className="bf-stampline">
            {kpi.as_of?`жалобы на ${fmtDateMsk(kpi.as_of)}`:"данные обновляются"}
            {tm.totals&&tm.totals.last_ok_run&&<> · тарифы на {fmtDateMsk(tm.totals.last_ok_run)}</>}
            {runsAll>0&&<> · <a href="#sources" className={runsOk<runsAll?"warn":""}>источники {runsOk}/{runsAll}</a></>}
            {flagsErr>0&&<> · <a href="#quality" className="warn">{flagsErr} ошибок качества</a></>}
            {kpi.total&&<> · корпус {fmtNum(kpi.total)} жалоб за 90 дн</>}
            {kr.current!=null&&<> · ключевая ЦБ {kr.current}%</>}
            {ST("headline")==="stale"&&<span className="bf-stale"> · ⚠ сводка за {sec.headline.stale_from}</span>}
            {ST("headline")==="degraded"&&<span className="bf-stale"> · ⚠ ИИ недоступен, сигналы детерминированные</span>}
          </p>
        </>}
    </header>

    {/* ② ПУЛЬС ДНЯ — сменный лист аудитора (без LLM).
        Отбор переработан 23.07.2026 по отзыву аудиторов «бесполезная»: рыночные
        метрики (медиана, спред) убраны — они отвечают на вопрос трейдера;
        ключевая ставка ушла в штамп. Каждая плитка = вопрос аудитора, ведёт
        туда, где с этим работают, и раскрывается попапом «как посчитано».
        Коэффициент ×N на экран не выводится: только пара «факт · норма». */}
    <section style={{marginBottom:22}}>
      <div className="bf-pulse">
        {/* ГЛАВНОЕ: тема с максимальным расхождением нашей динамики с рыночной.
            Живёт и в спокойный день — тогда честно говорит «ничего срочного» */}
        <div className={"bf-t bf-t-hero"+(dv&&dv.gap>=1.5?" alarm":dv&&dv.gap>=1.25?" attn":"")}
             onClick={dv?()=>bfGoDrill({page:"reviews",params:{theme:dv.key}}):undefined}
             style={dv?{cursor:"pointer"}:undefined}>
          <div className="bf-t-cap">Проверить сегодня
            {dv&&dv.gap>=1.25&&<span className="bf-t-chip">сильнее рынка</span>}</div>
          {dv?<>
            <Xp rows={xpDiverge(dv)} note="banki.ru · негативные отзывы 1–2★">
              <span className="bf-t-val">{dv.short||dv.label}</span>
            </Xp>
            <div className="bf-t-sub">{dv.week} жалоб · норма {dv.baseline_week}
              {dv.market_ratio!=null&&<> · по рынку {dv.market_ratio>1.1?"тоже растёт":"без роста"}</>}
              {dlt.diverge_key===dv.key&&<BfDelta v={dlt.diverge_week} invert/>}</div>
          </>:<>
            <span className="bf-t-val">Ничего срочного</span>
            <div className="bf-t-sub">проверено {(head.stats&&head.stats.checked_themes)||22} тем — превышений нет</div>
          </>}
        </div>

        {/* Регуляторный риск: доля жалоб с угрозой ЦБ/суда/ФАС */}
        <a className={"bf-t"+(esc!=null&&esc>=12?" attn":"")} href="#reviews">
          <div className="bf-t-cap">Дошло до ЦБ и суда</div>
          <Xp rows={xpEscalation(kpi)} note="banki.ru · окно 90 дней">
            <span className="bf-t-val">{esc!=null?pct1(esc):"—"}</span>
          </Xp>
          <div className="bf-t-sub">порог 12%{kpi.total?` · из ${fmtNum(kpi.total)} жалоб за 90 дн`:""}
            <BfDelta v={dlt.escalation_pct} unit=" пп" invert/></div>
        </a>

        {/* Объём недели — с нормой рядом, без коэффициента */}
        <a className="bf-t" href="#reviews">
          <div className="bf-t-cap">Жалобы · 7 дней</div>
          <Xp rows={xpWeek(ovl,kpi)} note="banki.ru · негативные отзывы 1–2★">
            <span className="bf-t-val">{ovl.week!=null?fmtNum(ovl.week):"—"}
              {ovl.baseline_week!=null&&<small> норма {Math.round(ovl.baseline_week)}</small>}</span>
          </Xp>
          <div className="bf-t-sub">banki.ru · 1–2★{kpi.market_rank?` · ${kpi.market_rank}-е место из ${kpi.market_banks}`:""}
            <BfDelta v={dlt.week} invert/></div>
        </a>

        {/* Что меняли МЫ САМИ — согласовано ли */}
        <a className="bf-t" href="#market?view=changes&bank=sberbank">
          <div className="bf-t-cap">Меняли сами</div>
          <Xp rows={xpOurChanges(tm)} note="журнал изменений условий">
            <span className="bf-t-val">{(tm.totals&&tm.totals.sber_changes_7d)!=null?fmtNum(tm.totals.sber_changes_7d):"—"}
              <small> офферов</small></span>
          </Xp>
          <div className="bf-t-sub">за 7 дней · условия продуктов Сбера
            <BfDelta v={dlt.sber_changes}/></div>
        </a>

        {/* Слепая зона: чего классификатор не видит */}
        <a className={"bf-t"+(unc&&unc.ratio>=1.3?" attn":"")} href="#reviews">
          <div className="bf-t-cap">Вне известных тем</div>
          <Xp rows={xpUnclassified(unc)} note="классификатор тем · 22 темы">
            <span className="bf-t-val">{unc&&unc.week!=null?unc.week:"—"}
              {unc&&unc.pct!=null&&<small> · {unc.pct}%</small>}</span>
          </Xp>
          <div className="bf-t-sub">{unc&&unc.ratio!=null
            ?(unc.ratio>=1.3?"выше обычного — возможен новый инцидент":"как обычно")
            :"жалобы без темы"}<BfDelta v={dlt.unclassified} invert/></div>
        </a>

        {/* Медленный тренд — то, чего не видно в недельном окне */}
        <a className="bf-t bf-t-wide" href="#reviews">
          <div className="bf-t-cap">Растёт за квартал</div>
          {up?<>
            <Xp rows={xpThemeUp(up)} note="banki.ru · окно 90 дней против предыдущих 90">
              <span className="bf-t-val">{up.short||up.label}</span>
            </Xp>
            <div className="bf-t-sub">+{Math.round(up.delta_pct)}% к прошлому кварталу · {up.n} жалоб</div>
          </>:<>
            <span className="bf-t-val">Без роста</span>
            <div className="bf-t-sub">ни одна тема не выросла заметно за квартал</div>
          </>}
        </a>
      </div>
    </section>

    {/* ③ СВОДКА ДНЯ + ④ НОВОСТИ */}
    <section className="bf-core" style={{marginBottom:30}}>
      <div>
        {insights.length?
          <div className="bf-cards">
            {insights.map((ins,i)=><BfCard key={ins.ref||i} ins={ins} idx={i} lead={i===0}/>)}
          </div>:
          generating?
            <div className="bf-cards">
              {[0,1,2].map(i=><div key={i} className="skel" style={{height:150,borderRadius:10}}/>)}
            </div>:
            <div className="surface" style={{padding:"22px 24px"}}>
              <div className="rv-radar-calm"><span className="rv-radar-check"><Ic.check/></span>
                За сутки резких сигналов не выявлено{head.stats?` · проверено ${head.stats.checked_themes} тем жалоб`:""}</div>
            </div>}
        {head.quiet_note&&<div className="bf-quiet"><span className="ok"><Ic.check/></span>{head.quiet_note}</div>}

        {/* ③b Анализ жалоб недели (LLM, reviews_brief) */}
        {brief.markdown&&<div className="surface" style={{padding:"20px 24px",marginTop:16}}>
          <div className="eyebrow-row" style={{marginBottom:12}}>
            <div className="eyebrow">Анализ жалоб недели</div>
            <div style={{display:"flex",gap:10,alignItems:"center"}}>
              {ST("reviews_brief")==="stale"&&<span className="bf-stale">за {sec.reviews_brief.stale_from}</span>}
              <button className="btn btn-ghost btn-sm" onClick={()=>location.hash="reviews"}>К отзывам <Ic.ext/></button>
            </div>
          </div>
          <div className="bf-brief">{renderMD(brief.markdown)}</div>
        </div>}
      </div>

      {/* ④ Новости для аудитора (sticky) */}
      <aside className="bf-news">
        <div className="bf-news-h">
          <div className="eyebrow" style={{marginBottom:0}}>Новости для аудитора</div>
          {newsAll>0&&<span className="bf-news-cov" title={(nw.sources||[]).map(s=>`${s.name}: ${s.ok?"ок":s.skipped_reason||"—"}`).join("\n")}>
            {newsOk}/{newsAll} ист.</span>}
        </div>
        {newsGroups.length?newsGroups.map(g=><div key={g.key}>
            <div className="bf-news-g">{g.title||g.key}</div>
            {(g.items||[]).map((it,i)=>
              <a key={i} className="bf-news-it" data-sev={it.severity} href={it.url}
                 target="_blank" rel="noopener noreferrer">
                <div className="bf-news-t">{it.title}</div>
                {(it.why||it.summary)&&<div className="bf-news-s">{it.why||it.summary}</div>}
                <div className="bf-news-m">{it.domain}{it.ts?` · ${fmtDateMsk(it.ts)}`:""}
                  {(it.products||[]).map(p=><span key={p} className="bf-chip">{PROD_RU[p]||p}</span>)}
                  <Ic.ext/></div>
              </a>)}
          </div>):
          ST("news")==="degraded"&&(nw.items_raw||[]).length?
            <div>
              <div className="bf-news-g" style={{color:"var(--warn)"}}>Без ИИ-отбора (сырая лента)</div>
              {(nw.items_raw||[]).slice(0,10).map((it,i)=>
                <a key={i} className="bf-news-it" href={it.url} target="_blank" rel="noopener noreferrer">
                  <div className="bf-news-t">{it.title}</div>
                  <div className="bf-news-m">{it.domain}<Ic.ext/></div>
                </a>)}
            </div>:
            <div className="bf-news-empty">{generating?"Собираю ленту…":"За сутки новости не собраны."}</div>}
      </aside>
    </section>

    {/* ⑤ ТАРИФНЫЕ ДВИЖЕНИЯ НЕДЕЛИ */}
    <section style={{marginBottom:26}}>
      <div className="eyebrow-row">
        <div className="eyebrow" style={{marginBottom:10}}>Тарифные движения недели</div>
        {(tm.mass_updates||[]).length>0&&
          <span className="badge warn" style={{cursor:"pointer"}} title="Открыть журнал изменений"
            onClick={()=>{const m=tm.mass_updates[0];location.hash="market?"+new URLSearchParams({cat:m.category||"",view:"changes"});}}>
            массовое движение: {tm.mass_updates.map(m=>CAT_LABELS[m.category]||m.category).join(", ")}{tm.after_pause?" · сбор после паузы":""}</span>}
      </div>
      <div className="surface" style={{overflow:"hidden"}}>
        {(tm.top_changes||[]).length?
          <table className="m-cards">
            <thead><tr><th>Банк</th><th>Продукт</th><th className="right">Было → стало</th><th className="right">Δ</th><th className="right">Когда</th></tr></thead>
            <tbody>{tm.top_changes.slice(0,10).map((c,i)=>{
              const up=c.to>c.from;
              // точный диплинк в журнал: свежие выпуски несут offer_id/change_id,
              // старые — хотя бы категорию
              const go=()=>{const sp=new URLSearchParams({cat:c.category||"",view:"changes"});
                if(c.bank_slug)sp.set("bank",c.bank_slug);
                if(c.change_id)sp.set("change",c.change_id);
                if(c.offer_id)sp.set("offer",c.offer_id);
                location.hash="market?"+sp.toString();};
              return <tr key={i} onClick={go} style={{cursor:"pointer"}} title="Открыть в журнале изменений">
                <td className="m-primary" data-label="Банк"><div style={{fontWeight:500}}>{c.bank}{c.is_sber&&<span className="badge solid" style={{marginLeft:8,fontSize:9}}>Сбер</span>}</div>
                  <div className="t-cap" style={{fontSize:11}}>{CAT_LABELS[c.category]||c.category}</div></td>
                <td data-label="Продукт" style={{fontSize:12.5,color:"var(--ink-2)"}}>{c.title}</td>
                <td className="right mono tnum" data-label="Было → стало">{c.from}% → <b>{c.to}%</b></td>
                <td className="right" data-label="Δ"><span className={`delta ${up?"pos":"neg"}`}>{up?<Ic.arrow_up/>:<Ic.arrow_dn/>}{signed(c.delta)}</span></td>
                <td className="right mono tnum" data-label="Когда" style={{fontSize:11,color:"var(--ink-3)"}}>{fmtDate(c.changed_at)}</td>
              </tr>;})}
            </tbody>
          </table>:
          <div style={{padding:"20px 24px",fontSize:13,color:"var(--ink-3)"}}>
            Изменений ставок за неделю не зафиксировано · под наблюдением {(tm.totals&&tm.totals.banks_tracked)||0} банков
            {tm.totals&&tm.totals.last_ok_run&&<> · последний сбор {fmtDate(tm.totals.last_ok_run)}</>}
          </div>}
        {(tm.top_changes||[]).length>0&&<div style={{padding:"12px 20px",borderTop:"1px solid var(--hair)",display:"flex",justifyContent:"space-between",alignItems:"center"}}>
          <span className="bf-stamp">{(tm.totals&&tm.totals.changes_7d)||0} изменений · {(tm.totals&&tm.totals.banks_changed_7d)||0} банков за 7 дн</span>
          <button className="btn btn-ghost btn-sm" onClick={()=>location.hash="market?view=changes"}>Все изменения <Ic.ext/></button>
        </div>}
      </div>
    </section>

    {/* ⑥ ПОДВАЛ ДОВЕРИЯ */}
    <div className="bf-trust">
      {qo.totals&&<span>{fmtNum(qo.totals.offers)} предложений · {qo.totals.banks} банков</span>}
      {pulse.kpi&&pulse.kpi.as_of&&<span>отзывы: {fmtNum((pulse.kpi.total||0))} за 90 дн (обн. {pulse.kpi.as_of})</span>}
      {tm.totals&&tm.totals.last_ok_run&&<span>сбор тарифов: {fmtDate(tm.totals.last_ok_run)}</span>}
      <a href="#quality">{flagsErr+flagsWarn} флаг(ов) качества</a>
      {dg&&dg.meta&&dg.meta.tokens&&dg.meta.tokens.in>0&&<span>дайджест: {Math.round((dg.meta.tokens.in+dg.meta.tokens.out)/1000)}k токенов/день · один на всех</span>}
      {qo.captcha_pending>0&&<a href="#sources">{qo.captcha_pending} капч(и) ждут решения</a>}
      <a href="#sources">Источники →</a>
    </div>
  </div>;
}

// ─── MARKET PAGE — «Позиция»: рынок × Сбер в одном развороте ─────────────────
// Три слоя: Атлас (cat=null) → рабочая область категории (Витрина/Журнал) →
// драуэр-досье оффера. Состояние зеркалится в hash (#market?cat=…&view=…):
// диплинки с Обзора шарятся ссылкой, F5 не теряет срез. Легаси-пресет
// al-mk-preset (bfGoDrill) конвертируется при маунте, URL приоритетнее.

const MK_TERMS=[["0-3","до 3 мес"],["4-6","4–6 мес"],["7-12","7–12 мес"],["13+","от года"]];
// значение сопоставимой метрики категории: ставка / ₽ в год / дни грейса
const mkGap=(v,m)=>{
  if(v==null)return "—";
  const n=parseFloat(v), sign=n>0?"+":(n<0?"−":"");
  const a=Math.abs(n);
  if(m==="fee_service")return n===0?"наравне":sign+fmtNum(Math.round(a))+" ₽/год";
  if(m==="grace_days")return n===0?"наравне":sign+Math.round(a)+" дн";
  return n===0?"наравне":sign+a.toFixed(2)+" пп";
};
const mkMetric=(v,m)=>{
  if(v==null)return "—";
  const n=parseFloat(v);
  if(m==="fee_service")return n===0?"бесплатно":fmtNum(Math.round(n))+" ₽/год";
  if(m==="grace_days")return Math.round(n)+" дн";
  return pct(n);
};
const MK_FLD={rate_pct:"ставка",amount_min:"мин. сумма",amount_max:"макс. сумма",
  term_months_min:"срок от",term_months_max:"срок до",fee_open:"комиссия открытия",
  fee_service:"обслуживание",early_withdraw:"досрочное снятие",capitalization:"капитализация",
  replenishable:"пополнение",conditions:"условия",rate_kind:"тип ставки",currency:"валюта",
  grace_days:"грейс-период",cashback_pct:"кешбэк"};
// значение поля диффа — человеком: аудитору нужны цифры, а не имена полей
const mkFldVal=(k,v)=>{
  if(v==null||v==="None"||v==="")return "—";
  const n=parseFloat(v);
  if(k==="amount_min"||k==="amount_max"||k==="fee_open"||k==="fee_service")
    return isNaN(n)?String(v).slice(0,30):fmtNum(n)+" ₽";
  if(k==="term_months_min"||k==="term_months_max")
    return isNaN(n)?String(v).slice(0,30):n+" мес";
  if(k==="grace_days")return isNaN(n)?String(v):n+" дн";
  if(k==="cashback_pct")return isNaN(n)?String(v):pct(n,1);
  if(k==="rate_pct")return isNaN(n)?String(v):pct(n);
  if(v==="true")return "да"; if(v==="false")return "нет";
  return String(v).slice(0,30);
};
// диффы без ставки → список «поле: было → стало»
const mkDiffOthers=(diff)=>{
  let d=diff;if(typeof d==="string"){try{d=JSON.parse(d);}catch{d={};}}
  return Object.entries(d||{}).filter(([k])=>k!=="rate_pct")
    .map(([k,v])=>({k,label:MK_FLD[k]||k,from:v&&v.from,to:v&&v.to}));
};

// strip-plot распределения категории: полоса IQR, засечка медианы,
// точки — лучший оффер каждого банка, Сбер — крупная accent-точка
// подсказка точки: величина каждого поля подписана своей единицей — грейс в
// днях, ПСК и кешбэк в процентах, обслуживание в рублях (раньше всё гналось
// через pct() и грейс выглядел как «120%»)
function mkPointTitle(p,c){
  // подписи оставляем как есть: «ПСК» — аббревиатура, строчными читается плохо
  const bits=[`${c.metric_label||"Значение"} ${mkMetric(p.rate,c.metric)}`];
  if(c.metric!=="rate_pct"&&p.rate_pct!=null)
    bits.push(`${c.rate_label||"Ставка"} ${pct(p.rate_pct)}`);
  if(c.secondary==="cashback_pct"&&p.secondary!=null)
    bits.push(`кешбэк до ${pct(p.secondary,1)}`);
  return `${p.is_sber?"Сбербанк":p.name}${p.title?` · ${p.title}`:""}\n${bits.join(" · ")}`;
}

function MkStrip({c,big}){
  if(c.status!=="ok")return null;
  const rng=(c.max-c.min)||1;
  const X=v=>((v-c.min)/rng)*94+3;
  return <div className={"mk-strip"+(big?" mk-strip-big":"")}>
    <i className="mk-iqr" style={{left:X(c.p25)+"%",width:Math.max(X(c.p75)-X(c.p25),.6)+"%"}}
       title={`середина рынка: ${mkMetric(c.p25,c.metric)} – ${mkMetric(c.p75,c.metric)}`}/>
    <i className="mk-med" style={{left:X(c.median)+"%"}} title={`медиана ${mkMetric(c.median,c.metric)}`}/>
    {(c.points||[]).map((p,i)=>
      <i key={i} className={"mk-dot"+(p.is_sber?" sber":"")} style={{left:X(p.rate)+"%"}}
         title={mkPointTitle(p,c)}/>)}
  </div>;
}

// ступенчатая история ставки оффера (SCD2-версии условий)
function MkStep({series}){
  const pts=(series||[]).filter(p=>p.rate_pct!=null);
  if(pts.length<2)return null;
  const W=280,H=64;
  const vals=pts.map(p=>parseFloat(p.rate_pct));
  const t0=new Date(pts[0].valid_from).getTime(),t1=Date.now();
  const min=Math.min(...vals),max=Math.max(...vals),rng=(max-min)||1;
  const X=t=>4+((t-t0)/((t1-t0)||1))*(W-8);
  const Y=v=>H-10-((v-min)/rng)*(H-24);
  let d=`M${X(t0).toFixed(1)} ${Y(vals[0]).toFixed(1)}`;
  for(let i=1;i<pts.length;i++){
    const x=X(new Date(pts[i].valid_from).getTime());
    d+=` H${x.toFixed(1)} V${Y(vals[i]).toFixed(1)}`;
  }
  d+=` H${W-4}`;
  return <div className="mk-stepw">
    <svg viewBox={`0 0 ${W} ${H}`} style={{width:"100%",display:"block"}} aria-hidden>
      <path d={d} fill="none" stroke="var(--accent)" strokeWidth="1.6"/>
    </svg>
    <div className="mk-steplbl">
      <span>{fmtDate(pts[0].valid_from)}</span>
      <span className="tnum">{pct(min)} – {pct(max)}</span>
      <span>сейчас {pct(vals[vals.length-1])}</span>
    </div>
  </div>;
}

function MarketPage({params}){
  const P=params||{};
  // одноразовый легаси-пресет от bfGoDrill — только как фолбэк при пустом URL
  const legacy=useRef(null);
  if(legacy.current===null){try{
    const p=JSON.parse(sessionStorage.getItem("al-mk-preset")||"null");
    sessionStorage.removeItem("al-mk-preset");legacy.current=p||{};
  }catch{legacy.current={};}}
  const L=legacy.current;
  const[cat,setCat]=useState(P.cat||P.category||L.category||null);
  const[view,setView]=useState(P.view||L.view||"vitrina");
  const[term,setTerm]=useState(P.term||null);
  const[qLive,setQLive]=useState(P.q||L.q||"");
  const[q,setQ]=useState(P.q||L.q||"");
  const[bank,setBank]=useState(P.bank||L.bank||null);
  const hlChange=useRef(P.change?parseInt(P.change):null);
  const[meta,setMeta]=useState(null);
  const[atlas,setAtlas]=useState(null);
  const[sum,setSum]=useState(null);
  const[sch,setSch]=useState(null);
  const[offers,setOffers]=useState(null);
  const[moreBusy,setMoreBusy]=useState(false);
  const[changes,setChanges]=useState(null);
  const[noise,setNoise]=useState(false);
  const[drawer,setDrawer]=useState(P.offer?parseInt(P.offer):null);
  const[dossier,setDossier]=useState(null);
  const[err,setErr]=useState(null);

  // повторный диплинк, когда страница уже открыта: применяем новые URL-параметры
  const lastP=useRef(JSON.stringify(P));
  useEffect(()=>{const s=JSON.stringify(P);
    if(s!==lastP.current){lastP.current=s;
      setCat(P.cat||P.category||null);setView(P.view||"vitrina");
      setTerm(P.term||null);setQLive(P.q||"");setQ(P.q||"");setBank(P.bank||null);
      hlChange.current=P.change?parseInt(P.change):null;
      if(P.offer)setDrawer(parseInt(P.offer));}
  },[params]); // eslint-disable-line

  // debounce поиска (серверный q — не дёргаем API на каждую букву)
  useEffect(()=>{const t=setTimeout(()=>setQ(qLive),350);return()=>clearTimeout(t);},[qLive]);

  // состояние → hash: диплинк живёт в адресе, F5 ничего не теряет
  useEffect(()=>{
    const sp=new URLSearchParams();
    if(cat)sp.set("cat",cat);
    if(view!=="vitrina")sp.set("view",view);
    if(term)sp.set("term",term);
    if(q)sp.set("q",q);
    if(bank)sp.set("bank",bank);
    const s=sp.toString();
    history.replaceState(null,"","#market"+(s?"?"+s:""));
  },[cat,view,term,q,bank]);

  useEffect(()=>{
    Promise.all([apiFetch("/api/meta/categories"),apiFetch("/api/market/atlas"),
                 apiFetch("/api/summary").catch(()=>null),
                 apiFetch("/api/meta/schedule").catch(()=>null)])
      .then(([m,a,s,sc])=>{setMeta(m);setAtlas(a);setSum(s);setSch(sc);})
      .catch(e=>setErr(e.message));
  },[]);

  useEffect(()=>{ // витрина
    if(!cat||view!=="vitrina")return;
    setOffers(null);
    const sp=new URLSearchParams({category:cat,limit:"100"});
    if(term)sp.set("term",term);
    if(q)sp.set("q",q);
    apiFetch("/api/market?"+sp).then(setOffers).catch(e=>setErr(e.message));
  },[cat,term,q,view]);

  useEffect(()=>{ // журнал
    if(view!=="changes")return;
    setChanges(null);
    const sp=new URLSearchParams({days:"7",limit:"120"});
    if(cat)sp.set("category",cat);
    if(bank)sp.set("bank_slug",bank);
    if(noise)sp.set("significant","false");
    apiFetch("/api/recent-changes?"+sp).then(setChanges).catch(e=>setErr(e.message));
  },[cat,bank,noise,view]);

  useEffect(()=>{ // досье оффера
    if(!drawer){setDossier(null);return;}
    setDossier(null);
    apiFetch(`/api/market/offer/${drawer}/history`)
      .then(setDossier).catch(()=>setDossier({error:true}));
  },[drawer]);

  const hlRef=useRef(null);
  useEffect(()=>{if(changes&&hlRef.current)
    hlRef.current.scrollIntoView({block:"center",behavior:"smooth"});},[changes]);

  const A=atlas?Object.fromEntries((atlas.categories||[]).map(c=>[c.category,c])):{};
  const M=meta?Object.fromEntries(meta.map(m=>[m.id,m])):{};
  const ac=cat?A[cat]:null;
  const total=offers&&offers.length?offers[0].total:null;
  const loadMore=()=>{
    if(!offers||moreBusy)return;
    setMoreBusy(true);
    const sp=new URLSearchParams({category:cat,limit:"100",offset:String(offers.length)});
    if(term)sp.set("term",term);
    if(q)sp.set("q",q);
    apiFetch("/api/market?"+sp).then(d=>{setOffers([...offers,...(d||[])]);setMoreBusy(false);})
      .catch(()=>setMoreBusy(false));
  };
  const openAI=(o)=>bfGoAI(`Проанализируй позицию Сбера относительно оффера «${o.title}» банка ${o.bank_name} (${(CAT_LABELS[o.category]||o.category).toLowerCase()}, ставка ${o.rate_pct??"—"}%). Насколько условия Сбера конкурентны и стоит ли реагировать?`);

  const lower=ac&&ac.lower_is_better;
  const sberIn=offers&&offers.some(o=>o.is_sber);
  const bestRate=offers&&offers.length?parseFloat(offers[0].rate_pct):null;
  const mcat=cat?(M[cat]||{}):{};                 // семантика витрины категории
  const showRateCol=mcat.show_rate!==false;
  const showBarCol=mcat.show_bar!==false&&showRateCol;

  return <div className="fade-in">
    <header style={{marginBottom:20}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Рынок · позиция объекта аудита</div>
      <h1 className="t-h" style={{marginBottom:6}}>Позиция Сбера на рынке</h1>
      <p className="t-cap" style={{maxWidth:"72ch"}}>
        {sum?`${sum.offers} офферов · ${sum.banks} банков`:"…"}
        {sch&&sch.enabled?` · автосбор ежедневно ${String(sch.ingest_hour_msk).padStart(2,"0")}:00 МСК`:sch?" · автосбор выключен":""}
        {sum&&sum.last_run?` · срез ${fmtDateMsk(sum.last_run)}`:""}
        {sch&&sch.stale&&<span className="mk-stale" title={`последний успешный сбор ${sch.last_ok_age_h!=null?sch.last_ok_age_h+" ч назад":"не зафиксирован"}; сторож догонит автоматически`}> · ⚠ данные устарели</span>}
      </p>
      <p className="mk-disc">Сравнение внутри сопоставимой выборки: ₽, лучший оффер банка, без промо-строк рейтингов. Для кредитных продуктов ниже ставка = лучше позиция. Наведите на любую цифру — покажем, как она посчитана.</p>
    </header>

    <div className="filter-row" style={{marginBottom:18}}>
      <div className="tab-row">
        <button className={`tab ${!cat&&view!=="changes"?"active":""}`} onClick={()=>{setCat(null);setView("vitrina");setDrawer(null);}}>Атлас</button>
        {(meta||[]).filter(m=>m.n>0).map(m=>{
          const sb=A[m.id]&&A[m.id].sber;
          return <button key={m.id} className={`tab ${cat===m.id?"active":""}`} onClick={()=>{setCat(m.id);setBank(null);}}>
            {m.label}{sb&&<span className={"mk-rk"+(sb.beats_share<0.5?" bad":"")}
              title={`Сбер — #${sb.rank} из ${A[m.id].n_banks} банков по лучшему офферу`}>#{sb.rank}</span>}
          </button>;})}
      </div>
      <div className="search-wrap">
        <Ic.search/>
        <input className="input" placeholder="Банк или продукт…" value={qLive}
               onChange={e=>{setQLive(e.target.value);if(!cat&&e.target.value)setCat("deposit");}}/>
      </div>
    </div>

    {err&&<ErrState msg={err}/>}

    {/* ── СЛОЙ 1 · АТЛАС ─────────────────────────────────────────────── */}
    {!cat&&view!=="changes"&&!err&&<div className="surface" style={{overflow:"hidden"}}>
      <div style={{padding:"18px 24px",borderBottom:"1px solid var(--hair)"}}>
        <div className="eyebrow" style={{marginBottom:2}}>Атлас позиций · категория × распределение рынка</div>
        <div className="t-cap">Точка — лучший оффер банка. Красная — Сбер. Полоса — середина рынка (25–75 перцентиль), засечка — медиана. Клик — в категорию.</div>
      </div>
      {!atlas?<div style={{padding:28}}><Skel h={40}/><div style={{height:10}}/><Skel h={40}/><div style={{height:10}}/><Skel h={40}/></div>:
        (atlas.categories||[]).map(c=>{
          const m=M[c.category]||{};
          const sb=c.sber;
          return <button key={c.category} className="mk-arow" onClick={()=>setCat(c.category)}>
            <div className="mk-alabel">
              <div style={{fontWeight:500}}>{m.label||c.category}</div>
              <div className="mk-an">{c.status==="ok"?`${c.n_banks} банков · ${(c.metric_label||"").toLowerCase()}`:""}{c.lower_is_better&&c.status==="ok"?" · ниже = лучше":""}{c.subsidized_excluded>0?` · без ${c.subsidized_excluded} господдержки`:""}</div>
            </div>
            {c.status==="ok"?<MkStrip c={c}/>:
              <div className="mk-anote">{c.status==="no_metric"?((M[c.category]||{}).caveat||"сопоставимой метрики нет")+" — доступна витрина":"нет данных"}</div>}
            <div className="mk-apos">
              {sb?<>
                <b className={"serif"+(sb.beats_share<0.5?" bad":"")}
                   title={`лучший оффер Сбера против лучших офферов ${c.n_banks} банков${c.small_n?" · малая база!":""}`}>#{sb.rank}</b>
                <span className="mk-an" title={sb.title||""}>{mkMetric(sb.rate,c.metric)} · {mkGap(sb.gap_median,c.metric)} к медиане{c.small_n?" · малая база":""}</span>
              </>:c.status==="ok"?<span className="mk-an">Сбера нет в выборке</span>:null}
            </div>
            <div className="mk-ago" aria-hidden>→</div>
          </button>;})}
    </div>}

    {/* ── СЛОЙ 2 · КАТЕГОРИЯ (журнал доступен и без категории) ───────── */}
    {(cat||view==="changes")&&!err&&<>
      {ac&&ac.status==="ok"&&<div className="mk-kpis">
        {ac.sber&&<div className="surface mk-kpi bf-tip" data-tip={`ранг лучшего оффера Сбера среди лучших офферов ${ac.n_banks} банков${ac.sber.tied>1?`; ${ac.sber.tied} банков с тем же значением делят это место`:""}${ac.small_n?" · малая база!":""}`}>
          <b className={ac.sber.beats_share<0.5?"bad":""}>#{ac.sber.rank}<small> из {ac.n_banks}</small></b>
          <span>ранг Сбера{ac.sber.tied>1?` · ${ac.sber.tied} наравне`:""}{ac.small_n?" · малая база":""}</span></div>}
        {ac.sber&&<div className="surface mk-kpi bf-tip" data-tip={ac.sber.title||""}>
          <b>{mkMetric(ac.sber.rate,ac.metric)}</b><span>{ac.sber.title?String(ac.sber.title).slice(0,28):"лучшее у Сбера"}</span></div>}
        {ac.sber&&<div className="surface mk-kpi bf-tip" data-tip={`лидер: ${ac.leader.name} · ${mkMetric(ac.leader.rate,ac.metric)} · «${ac.leader.title}»`}>
          <b className={Math.abs(ac.sber.gap_leader)>=1?"bad":""}>{mkGap(ac.sber.gap_leader,ac.metric)}</b>
          <span>до лидера ({ac.leader.name})</span></div>}
        <div className="surface mk-kpi bf-tip" data-tip={`медиана лучших офферов ${ac.n_banks} банков · разброс ${mkMetric(ac.min,ac.metric)}–${mkMetric(ac.max,ac.metric)}`}>
          <b>{mkMetric(ac.median,ac.metric)}</b><span>медиана рынка</span></div>
      </div>}
      {ac&&ac.status==="ok"&&<div className="surface" style={{padding:"14px 20px",marginBottom:14}}>
        <MkStrip c={ac} big/>
      </div>}

      <div className="filter-row" style={{marginBottom:14}}>
        <div className="tab-row">
          {cat&&<button className={`tab ${view==="vitrina"?"active":""}`} onClick={()=>setView("vitrina")}>Витрина</button>}
          <button className={`tab ${view==="changes"?"active":""}`} onClick={()=>setView("changes")}>Журнал изменений{!cat?" · весь рынок":""}</button>
        </div>
        {view==="vitrina"&&mcat.show_terms&&<div className="tab-row">
          {MK_TERMS.map(([id,l])=><button key={id} className={`tab ${term===id?"active":""}`}
            onClick={()=>setTerm(term===id?null:id)}>{l}</button>)}
        </div>}
        {view==="changes"&&<label className="mk-noise">
          <input type="checkbox" checked={noise} onChange={e=>setNoise(e.target.checked)}/> показать микрошум
        </label>}
        {view==="changes"&&bank&&<button className="tab active" onClick={()=>setBank(null)}>банк: {bank} ✕</button>}
      </div>

      {/* ВИТРИНА */}
      {mcat.caveat&&<p className="mk-disc" style={{margin:"0 0 12px"}}>⚠ {mcat.caveat}.</p>}
      {ac&&ac.subsidized_excluded>0&&<p className="mk-disc" style={{margin:"0 0 12px"}}>
        Из рыночного сравнения исключено программ с господдержкой: {ac.subsidized_excluded} — их ставку задаёт государство, она одинакова у всех банков. В витрине ниже они присутствуют.</p>}
      {view==="vitrina"&&<div className="surface" style={{overflow:"hidden"}}>
        {ac&&ac.sber&&!sberIn&&offers&&<button className="mk-sber-pin" onClick={()=>setDrawer(ac.sber.offer_id)}>
          <BankAvatar slug="sberbank" name="Сбербанк" isSber={true}/>
          <div style={{textAlign:"left"}}>
            <div style={{fontWeight:500}}>Сбербанк · {ac.sber.title}</div>
            <div className="mk-an">лучший оффер Сбера · #{ac.sber.rank} из {ac.n_banks} банков{term?" · фильтр срока может его скрывать":""}</div>
          </div>
          <div className="serif" style={{fontSize:18,marginLeft:"auto"}}>{mkMetric(ac.sber.rate,ac.metric)}</div>
        </button>}
        {!offers?<div style={{padding:28}}><Skel h={40}/><div style={{height:10}}/><Skel h={40}/><div style={{height:10}}/><Skel h={40}/></div>:
         offers.length===0?<EmptyState text="Нет предложений под фильтры. Сбросьте срок или поиск."/>:
        <table className="m-cards">
          <thead><tr>
            <th className="right" style={{width:"5%"}}>№</th>
            <th>Банк</th><th>Продукт</th>
            <th className="right">{mcat.metric_label||"Ставка"}</th>
            {showRateCol&&mcat.metric!=="rate_pct"&&<th className="right">{mcat.rate_label}</th>}
            {mcat.secondary&&<th className="right">Кешбэк</th>}
            {showBarCol&&<th>К лидеру</th>}
            <th>Сумма</th><th>Срок</th>
          </tr></thead>
          <tbody>
            {offers.map((r,i)=>{
              const isSber=!!r.is_sber;
              const rate=parseFloat(r.rate_pct);
              const rel=bestRate&&rate?(lower?bestRate/rate:rate/bestRate):null;
              return <tr key={r.offer_id||i} className={(isSber?"is-sber ":"")+"mk-click"} onClick={()=>setDrawer(r.offer_id)}>
                <td className="right mono tnum" data-label="№" style={{color:"var(--ink-3)",fontSize:12}}>{String(i+1).padStart(2,"0")}</td>
                <td className="m-primary" data-label="Банк"><div style={{display:"flex",alignItems:"center",gap:10}}>
                  <BankAvatar slug={r.bank_slug} name={r.bank_name} isSber={isSber}/>
                  <div><div style={{fontWeight:500}}>{r.bank_name||r.bank_slug}</div>
                    {isSber&&<div className="t-cap" style={{fontSize:10.5,color:"var(--accent)",fontFamily:"'JetBrains Mono',monospace",letterSpacing:".06em"}}>СБЕР · ОБЪЕКТ АУДИТА</div>}
                  </div></div></td>
                <td data-label="Продукт">{r.title}</td>
                <td className="right mono tnum" data-label={mcat.metric_label||"Ставка"} style={{fontWeight:500,fontSize:14}}>{mkMetric(r[mcat.metric||"rate_pct"],mcat.metric)}</td>
                {showRateCol&&mcat.metric!=="rate_pct"&&<td className="right mono tnum" data-label={mcat.rate_label} style={{color:"var(--ink-2)",fontSize:12.5}}>{r.rate_pct!=null?pct(r.rate_pct):"—"}</td>}
                {mcat.secondary&&<td className="right mono tnum" data-label="Кешбэк" style={{color:"var(--ink-2)",fontSize:12.5}}>{r.cashback_pct!=null?pct(r.cashback_pct,1):"—"}</td>}
                {showBarCol&&<td data-label="К лидеру">
                  {rel!=null&&isFinite(rel)?<div style={{display:"flex",alignItems:"center",gap:8}}>
                    <div className="bar" style={{flex:1,maxWidth:70}}>
                      <i style={{width:`${Math.min(rel*100,100)}%`,background:isSber?"var(--accent)":"var(--ink-3)"}}/>
                    </div>
                    <span className="mono tnum" style={{fontSize:11,color:"var(--ink-3)"}}>
                      {i===0?"лидер":`${(lower?"+":"−")}${Math.abs(rate-bestRate).toFixed(2)} пп`}</span>
                  </div>:<span className="mono" style={{color:"var(--ink-4)"}}>—</span>}
                </td>}
                <td className="mono tnum" data-label="Сумма" style={{color:"var(--ink-2)",fontSize:12.5}}>{fmtAmount(r.amount_min,r.amount_max)}</td>
                <td className="mono tnum" data-label="Срок" style={{color:"var(--ink-2)",fontSize:12.5}}>{fmtTerm(r.term_months_min,r.term_months_max)}</td>
              </tr>;})}
          </tbody>
        </table>}
        {offers&&total>offers.length&&<button className="btn btn-ghost mk-more" onClick={loadMore} disabled={moreBusy}>
          {moreBusy?"Загружаю…":`Показать ещё (${offers.length} из ${total})`}</button>}
      </div>}

      {/* ЖУРНАЛ ИЗМЕНЕНИЙ */}
      {view==="changes"&&<div className="surface" style={{overflow:"hidden"}}>
        <div style={{padding:"14px 20px",borderBottom:"1px solid var(--hair)"}}>
          <div className="eyebrow" style={{marginBottom:2}}>Журнал изменений · 7 дней{noise?" · включая микрошум":" · только значимые"}</div>
          <div className="t-cap">Значимое = изменение нестаточного условия или сдвиг ставки от 0.01 пп. Клик по строке — досье оффера.</div>
        </div>
        {!changes?<div style={{padding:28}}><Skel h={30}/><div style={{height:8}}/><Skel h={30}/><div style={{height:8}}/><Skel h={30}/></div>:
         changes.length===0?<EmptyState text="За неделю изменений не зафиксировано."/>:
         changes.map(ch=>{
           const hl=hlChange.current&&ch.change_id===hlChange.current;
           const others=mkDiffOthers(ch.diff);
           const big=ch.rate_delta!=null&&Math.abs(ch.rate_delta)>=0.05;
           const showRateMove=ch.rate_from!=null&&ch.rate_to!=null&&(Math.abs(ch.rate_delta||0)>=0.01||!others.length);
           return <button key={ch.change_id} ref={hl?hlRef:null}
             className={"mk-chrow"+(hl?" hl":"")+(big?" big":"")} onClick={()=>setDrawer(ch.offer_id)}>
             <span className="mono mk-chdate">{fmtDateMsk(ch.changed_at)}</span>
             <span className="mk-chbank">
               <BankAvatar slug={ch.bank_slug} name={ch.bank_name} isSber={!!ch.is_sber}/>
               <span>{ch.bank_name}<i className="mk-an" style={{display:"block",fontStyle:"normal"}}>{ch.title}{!cat?` · ${(CAT_LABELS[ch.category]||ch.category).toLowerCase()}`:""}</i></span>
             </span>
             <span className="mk-chmove mono tnum">
               {showRateMove&&<>{pct(ch.rate_from)} → <b>{pct(ch.rate_to)}</b>
                  {Math.abs(ch.rate_delta||0)>=0.01&&<em className={ch.rate_delta>0?"up":"dn"}>{ch.rate_delta>0?"▲":"▼"} {Math.abs(ch.rate_delta).toFixed(2)}</em>}</>}
               {others.slice(0,3).map(o=><span key={o.k} className="mk-dv">
                 {o.label}: {mkFldVal(o.k,o.from)} → <b>{mkFldVal(o.k,o.to)}</b></span>)}
               {others.length>3&&<span className="mk-dv mk-an">ещё {others.length-3}</span>}
             </span>
           </button>;})}
      </div>}
    </>}

    {/* ── СЛОЙ 3 · ДОСЬЕ ОФФЕРА ──────────────────────────────────────── */}
    {drawer&&<RvModal side="right" onClose={()=>setDrawer(null)}
        title={dossier&&dossier.offer?`${dossier.offer.bank_name} · ${dossier.offer.title}`:"Досье оффера"}
        sub={dossier&&dossier.offer?(CAT_LABELS[dossier.offer.category]||dossier.offer.category):""}>
      {!dossier?<div style={{padding:8}}><Skel h={60}/><div style={{height:10}}/><Skel h={120}/></div>:
       dossier.error?<RvNote err={true}/>:<>
        <div className="mk-pass">
          {(M[dossier.offer.category]||{}).show_rate!==false&&
          <div><span>{(M[dossier.offer.category]||{}).rate_label||"Ставка"}</span><b className="tnum">{dossier.offer.rate_pct!=null?pct(dossier.offer.rate_pct):"—"}</b>
            {dossier.offer.rate_kind&&<i className="mk-an">{dossier.offer.rate_kind}</i>}</div>}
          <div><span>Сумма</span><b className="tnum">{fmtAmount(dossier.offer.amount_min,dossier.offer.amount_max)}</b></div>
          <div><span>Срок</span><b className="tnum">{fmtTerm(dossier.offer.term_months_min,dossier.offer.term_months_max)}</b></div>
          {dossier.offer.capitalization!=null&&<div><span>Капитализация</span><b>{dossier.offer.capitalization?"да":"нет"}</b></div>}
          {dossier.offer.replenishable!=null&&<div><span>Пополнение</span><b>{dossier.offer.replenishable?"да":"нет"}</b></div>}
          {dossier.offer.early_withdraw!=null&&<div><span>Досрочное</span><b>{dossier.offer.early_withdraw?"да":"нет"}</b></div>}
          {dossier.offer.grace_days!=null&&<div><span>Грейс-период</span><b className="tnum">{dossier.offer.grace_days} дн</b></div>}
          {dossier.offer.fee_service!=null&&<div><span>Обслуживание</span><b className="tnum">{mkMetric(dossier.offer.fee_service,"fee_service")}</b></div>}
          {dossier.offer.fee_open!=null&&parseFloat(dossier.offer.fee_open)>0&&<div><span>Выпуск (разово)</span><b className="tnum">{fmtNum(Math.round(dossier.offer.fee_open))} ₽</b></div>}
          {dossier.offer.cashback_pct!=null&&<div><span>Кешбэк до</span><b className="tnum">{pct(dossier.offer.cashback_pct,1)}</b></div>}
          <div><span>Версия условий с</span><b className="tnum">{fmtDate(dossier.offer.valid_from)}</b></div>
        </div>
        {(M[dossier.offer.category]||{}).show_rate!==false&&dossier.rate_series&&dossier.rate_series.length>1&&<>
          <div className="eyebrow" style={{margin:"16px 0 6px"}}>История ставки</div>
          <MkStep series={dossier.rate_series}/>
        </>}
        {dossier.changes&&dossier.changes.length>0&&<>
          <div className="eyebrow" style={{margin:"16px 0 6px"}}>Изменения условий</div>
          {dossier.changes.map(ch=>{
            const other=mkDiffOthers(ch.diff);
            return <div key={ch.change_id} className="mk-dhrow mono tnum">
              <span className="mk-an">{fmtDateMsk(ch.changed_at)}</span>
              <span style={{textAlign:"right",display:"flex",flexDirection:"column",gap:2}}>
                {ch.rate_from!=null&&ch.rate_to!=null&&Math.abs(ch.rate_delta||0)>=0.01
                  &&<span>{pct(ch.rate_from)} → <b>{pct(ch.rate_to)}</b></span>}
                {other.map(o=><span key={o.k} className="mk-an">
                  {o.label}: {mkFldVal(o.k,o.from)} → {mkFldVal(o.k,o.to)}</span>)}
              </span>
            </div>;})}
        </>}
        {dossier.offer.conditions&&<>
          <div className="eyebrow" style={{margin:"16px 0 6px"}}>Условия</div>
          <p className="t-cap" style={{whiteSpace:"pre-wrap"}}>{String(dossier.offer.conditions).slice(0,600)}</p>
        </>}
        <div className="mk-dbtns">
          {dossier.offer.url&&<a className="btn btn-ghost btn-sm" href={dossier.offer.url} target="_blank" rel="noopener noreferrer">↗ Первоисточник</a>}
          <button className="btn btn-accent btn-sm" onClick={()=>openAI(dossier.offer)}>✦ Спросить ИИ</button>
        </div>
      </>}
    </RvModal>}
  </div>;
}
const SberPage=MarketPage; // вкладки объединены («Позиция», 07.2026) — алиас для старых закладок

// ─── REVIEWS PAGE — риск-радар голоса клиента (корпус banki.ru ~390к) ─────────
const RV_BANKS=["Сбербанк","ВТБ","Т-Банк","Альфа-Банк","Газпромбанк","Совкомбанк",
  "Россельхозбанк","Почта Банк","Райффайзен Банк","Ак Барс Банк","Уралсиб","ОТП Банк",
  "МТС Банк","ПСБ","Ozon Банк","Яндекс Банк","Московский кредитный банк (МКБ)",
  "Росбанк","Банк «Открытие»","Хоум Банк"];
const RV_PERIODS=[[90,"3 мес"],[180,"6 мес"],[365,"12 мес"]];
const RV_RISK={compliance:"комплаенс",conduct:"практики",ops:"операции"};
const pct1=v=>v==null?"—":String(v).replace(".",",")+"%";
const rvDelta=(d)=> d==null ? <span className="rv-flat">→</span>
  : d>4 ? <span className="rv-up">↑ {d}%</span>
  : d<-4 ? <span className="rv-down">↓ {Math.abs(d)}%</span>
  : <span className="rv-flat">→ {d>=0?"+":""}{d}%</span>;
// Сбой загрузки панели ≠ «данных нет» — для аудитора это важное различие.
function RvNote({err}){return <div className="rv-note">{err?"⚠ Не удалось загрузить — обновите страницу":"Нет данных за выбранный период"}</div>;}

// Переиспользуемый оверлей: центральный модал (полный текст) или правый драуэр
// (drill-in по городу/месяцу). Закрытие по клику-вне, ✕ и Esc.
function RvModal({onClose,title,sub,side,children}){
  useEffect(()=>{
    const h=e=>{if(e.key==="Escape")onClose();};
    document.addEventListener("keydown",h);
    const prev=document.body.style.overflow;
    document.body.style.overflow="hidden";   // фон не скроллим, пока открыт оверлей
    return ()=>{document.removeEventListener("keydown",h);document.body.style.overflow=prev;};
  },[onClose]);
  // ПОРТАЛ в body: у предка .fade-in есть transform (animation fill-mode both),
  // который иначе становится containing-block для position:fixed и «роняет» модал вниз.
  return ReactDOM.createPortal(
    <div className={"rv-ovl"+(side==="right"?" rv-ovl-r":"")} onClick={onClose}>
      <div className={"rv-ovl-card"+(side==="right"?" rv-ovl-right":"")} onClick={e=>e.stopPropagation()}>
        <div className="rv-ovl-head">
          <div style={{minWidth:0}}>
            <div className="rv-ttl" style={{fontSize:15}}>{title}</div>
            {sub&&<div className="rv-cap" style={{margin:"2px 0 0"}}>{sub}</div>}
          </div>
          <button className="rv-ovl-x" onClick={onClose} aria-label="Закрыть">✕</button>
        </div>
        <div className="rv-ovl-body">{children}</div>
      </div>
    </div>, document.body);
}

// Чипы тем обращения (классификация): regex-baseline или LLM-уточнённые.
function RvThemes({list,src}){
  if(!list||!list.length) return <span className="rv-tag other">Прочее</span>;
  return <>{list.slice(0,3).map((t,j)=>(
    <span key={j} className={"rv-tag "+(t.risk||"other")} title={t.label}>{t.short||t.label}</span>
  ))}{src==="llm"&&<span className="rv-llm" title="темы уточнены ИИ">✦</span>}</>;
}

// Карточка отзыва (переиспользуется в ленте, в модале и в драуэре).
function RvReview({r,onOpen,full}){
  const txt=r.text||"";
  return <div className="rv-rev">
    <div className="rv-rh">
      <span>{r.date}</span>
      <RvThemes list={r.themes} src={r.theme_src}/>
      {r.product&&<span className="rv-pill rv-pill-dim" title="направление banki.ru">{r.product}</span>}
      {r.city&&<span className="rv-pill">{r.city}</span>}
      {r.similar>0&&<span className="rv-sim">+{r.similar} похожих</span>}
    </div>
    <div className={"rv-rq"+(onOpen?" rv-rq-click":"")} role={onOpen?"button":undefined}
         tabIndex={onOpen?0:undefined} onClick={onOpen||undefined}
         onKeyDown={onOpen?(e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();onOpen();}}):undefined}>
      {full?txt:(txt.slice(0,420)+(txt.length>420?"…":""))}
      {onOpen&&txt.length>420&&<span className="rv-more"> читать полностью →</span>}
    </div>
  </div>;
}

// SVG-иконки радара (без эмодзи, currentColor, feather-стиль)
const IcoRadar=()=> <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M2 13h4l2.5 6 4-14 2.5 9 1.5-4 1.5 3H22"/></svg>;
const IcoCheck=()=> <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M8.4 12.4l2.5 2.5 4.7-5.4"/></svg>;
const IcoTrendUp=()=> <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/></svg>;

function ReviewsPage(){
  // prefill из «Обзора» (Разобраться → по сигналу): банк/тема/период/город
  const preset=(()=>{try{
    const p=JSON.parse(sessionStorage.getItem("al-rv-prefilter")||"null");
    sessionStorage.removeItem("al-rv-prefilter");return p;
  }catch{return null;}})();
  const[bank,setBank]=useState((preset&&preset.bank)||"Сбербанк");
  const[bankList,setBankList]=useState(RV_BANKS);
  const[product,setProduct]=useState((preset&&preset.product)||"");
  const firstBankRun=useRef(true);   // не сбрасывать префилл продукта при монтировании
  const[days,setDays]=useState((preset&&preset.days)||90);
  const[theme,setTheme]=useState((preset&&preset.theme)||"");
  const[q,setQ]=useState("");
  const[qInput,setQInput]=useState("");
  const[ov,setOv]=useState(null),[tr,setTr]=useState(null),[th,setTh]=useState(null);
  const[vm,setVm]=useState(null),[ge,setGe]=useState(null),[prods,setProds]=useState([]);
  const[feed,setFeed]=useState(null);
  const[busy,setBusy]=useState(true),[feedBusy,setFeedBusy]=useState(false);
  const[caseN,setCaseN]=useState(()=>{try{return JSON.parse(localStorage.getItem("al-case")||"[]").length;}catch{return 0;}});
  const[modalRev,setModalRev]=useState(null);            // полный текст отзыва
  const[drill,setDrill]=useState(null);                  // {type:'city'|'month',value,label}
  const[drillItems,setDrillItems]=useState(null),[drillBusy,setDrillBusy]=useState(false);
  const[explain,setExplain]=useState(null),[explainBusy,setExplainBusy]=useState(false);
  const[clsBusy,setClsBusy]=useState(false),[clsOn,setClsOn]=useState(false);
  const[thAll,setThAll]=useState(false);   // показать все темы риск-карты vs топ-12
  const[anom,setAnom]=useState(null),[anomBusy,setAnomBusy]=useState(false);  // радар аномалий

  const enc=encodeURIComponent;
  const pq=()=>product?`&product=${enc(product)}`:"";

  // drill-in: открыть боковую панель по городу/месяцу, подгрузить жалобы среза
  const openDrill=(type,value,label)=>{
    setDrill({type,value,label});setExplain(null);setExplainBusy(false);
    setDrillItems(null);setDrillBusy(true);
    const f=type==="city"?`&city=${enc(value)}`:`&month=${enc(value)}`;
    apiFetch(`/api/reviews/feed?bank=${enc(bank)}${pq()}${f}&limit=40`)
      .then(d=>{setDrillItems(d.items||[]);setDrillBusy(false);}).catch(()=>{setDrillItems([]);setDrillBusy(false);});
  };
  const runExplain=()=>{
    if(!drill)return; setExplainBusy(true);
    const f=drill.type==="city"?`&city=${enc(drill.value)}`:`&month=${enc(drill.value)}`;
    apiFetch(`/api/reviews/explain?bank=${enc(bank)}${pq()}${f}`)
      .then(d=>{setExplain(d&&d.summary?d.summary:"__none__");setExplainBusy(false);})
      .catch(()=>{setExplain("__none__");setExplainBusy(false);});
  };

  // prefill-город из «Обзора» → сразу открываем drill-in драуэр
  useEffect(()=>{
    if(preset&&preset.city)openDrill("city",preset.city,`г. ${preset.city}`);
  },[]);

  useEffect(()=>{
    apiFetch("/api/reviews/banks").then(d=>{
      if(d&&d.items&&d.items.length)setBankList(d.items.map(x=>x.bank));
    }).catch(()=>{});
  },[]);

  useEffect(()=>{
    setBusy(true);
    // allSettled: падение одной панели не должно стирать остальные четыре.
    Promise.allSettled([
      apiFetch(`/api/reviews/overview?bank=${enc(bank)}${pq()}&days=${days}`),
      apiFetch(`/api/reviews/trend?bank=${enc(bank)}${pq()}`),
      apiFetch(`/api/reviews/themes?bank=${enc(bank)}${pq()}`),
      apiFetch(`/api/reviews/vs-market?bank=${enc(bank)}${pq()}&days=${days}`),
      apiFetch(`/api/reviews/geo?bank=${enc(bank)}${pq()}`),
    ]).then(([o,t,h,v,g])=>{
      const V=s=>s.status==="fulfilled"?s.value:{__err:true};
      setOv(V(o));setTr(V(t));setTh(V(h));setVm(V(v));setGe(V(g));setBusy(false);
    });
  },[bank,product,days]);

  useEffect(()=>{ if(firstBankRun.current){firstBankRun.current=false;}else{setProduct("");}
    apiFetch(`/api/reviews/products?bank=${enc(bank)}`).then(d=>setProds(d.items||[])).catch(()=>setProds([]));
  },[bank]);

  // радар срочных аномалий — грузится ОТДЕЛЬНО (LLM), не блокирует дашборд
  useEffect(()=>{ setAnomBusy(true);setAnom(null);
    apiFetch(`/api/reviews/anomalies?bank=${enc(bank)}${pq()}`)
      .then(d=>{setAnom(d||{calm:true});setAnomBusy(false);})
      .catch(()=>{setAnom({calm:true});setAnomBusy(false);});
  },[bank,product]);

  useEffect(()=>{ setFeedBusy(true);setClsOn(false);
    const tq=theme?`&theme=${theme}`:"", qq=q?`&q=${enc(q)}`:"";
    apiFetch(`/api/reviews/feed?bank=${enc(bank)}${pq()}${tq}${qq}&limit=20`)
      .then(d=>{setFeed(d.items||[]);setFeedBusy(false);}).catch(()=>{setFeed([]);setFeedBusy(false);});
  },[bank,product,theme,q]);

  // on-demand: уточнить темы показанных отзывов через LLM (по кнопке)
  const classifyFeed=()=>{
    setClsBusy(true);
    const tq=theme?`&theme=${theme}`:"", qq=q?`&q=${enc(q)}`:"";
    apiFetch(`/api/reviews/feed-classified?bank=${enc(bank)}${pq()}${tq}${qq}&limit=20`)
      .then(d=>{if(d&&d.items)setFeed(d.items);setClsOn(!!(d&&d.llm));setClsBusy(false);})
      .catch(()=>setClsBusy(false));
  };

  const addCase=(r)=>{try{const k="al-case";const cur=JSON.parse(localStorage.getItem(k)||"[]");
    if(!cur.find(x=>x.url===r.url)){cur.push({bank:r.bank,product:r.product,date:r.date,city:r.city,url:r.url,text:r.text});
      localStorage.setItem(k,JSON.stringify(cur));setCaseN(cur.length);}}catch{}};
  const exportCase=()=>{try{const cur=JSON.parse(localStorage.getItem("al-case")||"[]");
    if(!cur.length)return;
    const esc=v=>`"${String(v==null?"":v).replace(/"/g,'""')}"`;
    const rows=[["банк","продукт","дата","город","ссылка","текст"].map(esc).join(",")]
      .concat(cur.map(r=>[r.bank,r.product,r.date,r.city,r.url,r.text].map(esc).join(",")));
    const blob=new Blob(["﻿"+rows.join("\n")],{type:"text/csv;charset=utf-8"});
    const a=document.createElement("a");a.href=URL.createObjectURL(blob);a.download=`audit-case-${Date.now().toString(36)}.csv`;
    document.body.appendChild(a);a.click();a.remove();}catch{}};

  const onKey=fn=>e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();fn();}};
  const themeLabel = theme && th && th.themes ? (th.themes.find(x=>x.key===theme)||{}).label : "";
  const trendMax = tr&&tr.series&&tr.series.length ? Math.max(...tr.series.map(s=>s.n))||1 : 1;
  const thMax = th&&th.themes&&th.themes.length ? Math.max(...th.themes.map(t=>t.n))||1 : 1;
  const vmMax = vm&&vm.rows&&vm.rows.length ? Math.max(...vm.rows.map(r=>r.pct))||1 : 1;
  const geMax = ge&&ge.cities&&ge.cities.length ? Math.max(...ge.cities.map(c=>c.n))||1 : 1;

  return <div className="fade-in rv">
    <div className="eyebrow" style={{marginBottom:6}}>§ Отзывы · аудит-сигналы</div>
    <h1 className="t-h" style={{marginBottom:4,fontFamily:"'Source Serif 4',Georgia,serif",fontWeight:600,letterSpacing:"-.015em"}}>Голос клиента — риск-радар</h1>
    <div className="rv-src">banki.ru · ~390 тыс. жалоб · 217 банков{ov&&ov.as_of?<> · данные по {ov.as_of}</>:""}</div>
    <div className="rv-disclaimer">⚠ Корпус — <b>только негатив (1–2★)</b>. Все метрики — динамика и структура <b>внутри жалоб</b>, а не доля недовольных клиентов. «Доля рынка» и «место» отражают объём выгрузки banki.ru, <b>не нормированы на клиентскую базу</b> банка.</div>

    <div className="rv-filters">
      <label className="rv-fl">Банк
        <select value={bank} onChange={e=>setBank(e.target.value)}>
          {bankList.map(b=><option key={b} value={b}>{b}</option>)}
        </select>
      </label>
      <label className="rv-fl">Продукт
        <select value={product} onChange={e=>setProduct(e.target.value)}>
          <option value="">Все продукты</option>
          {prods.map(p=><option key={p.product} value={p.product}>{p.product} ({fmtNum(p.n)})</option>)}
        </select>
      </label>
      <div className="rv-chips">
        {RV_PERIODS.map(([d,l])=><button key={d} className={"rv-chip"+(days===d?" on":"")} onClick={()=>setDays(d)}>{l}</button>)}
      </div>
      <button className="rv-export" onClick={exportCase} disabled={!caseN}>↧ Аудит-дело{caseN?` · ${caseN}`:""}</button>
    </div>

    {/* KPI */}
    <div className="rv-kpis">
      <div className="rv-card rv-kpi">
        <div className="rv-kl">Жалоб за {days} дн</div>
        <div className="rv-kv">{busy?"…":(ov&&ov.total!=null?fmtNum(ov.total):"—")}</div>
        <div className="rv-ks">{ov&&ov.delta_pct!=null?<>{ov.delta_pct<0?<span className="rv-down">↓ {Math.abs(ov.delta_pct)}%</span>:<span className="rv-up">↑ {ov.delta_pct}%</span>} к пред. периоду{ov.delta_low_n?<span className="rv-lown"> · малая база</span>:""}</>:"—"}</div>
      </div>
      <div className="rv-card rv-kpi">
        <div className="rv-kl">Доля рынка жалоб</div>
        <div className="rv-kv">{busy?"…":pct1(ov&&ov.market_share_pct)}</div>
        <div className="rv-ks">{ov&&ov.market_rank?`${ov.market_rank}-е место · ⓘ без нормировки на базу`:"—"}</div>
      </div>
      <div className={"rv-card rv-kpi"+(ov&&ov.escalation_pct>=12?" rv-alert":"")}>
        <div className="rv-kl">Регуляторная эскалация {ov&&ov.escalation_pct>=12&&<span className="rv-tag compliance">риск</span>}</div>
        <div className="rv-kv rv-up">{busy?"…":pct1(ov&&ov.escalation_pct)}</div>
        <div className="rv-ks">упоминают ЦБ / суд / ФАС</div>
      </div>
      <div className="rv-card rv-kpi">
        <div className="rv-kl">Главная тема</div>
        <div className="rv-kv-sm">{busy?"…":(th&&th.themes&&th.themes.length?th.themes[0].label:"—")}</div>
        <div className="rv-ks">{th&&th.themes&&th.themes.length?`${pct1(th.themes[0].pct)} жалоб за 90 дн · ${RV_RISK[th.themes[0].risk]}`:""}</div>
      </div>
    </div>

    {/* АНАЛИТИКА — 2 колонки: слева динамика+темы, справа география+рынок */}
    <div className="rv-main2">
      <div className="rv-col">
        {/* TREND */}
        <div className="rv-card">
          <div className="rv-ct"><div><div className="rv-ttl">Динамика жалоб</div><div className="rv-cap">помесячно · клик по столбцу → жалобы месяца{tr&&tr.series&&tr.series.some(s=>s.partial)?" · последний месяц неполный (штриховка)":""}</div></div></div>
          {busy?<Skel h={150}/>:!tr||!tr.series||!tr.series.length?<RvNote err={tr&&tr.__err}/>:<>
            <div className="rv-bars">
              {tr.series.map((s,i)=><div key={i} className={"rv-bcol"+(s.partial?" partial":"")} title={`${s.ym}: ${fmtNum(s.n)}${s.partial?" (неполный месяц)":""}`}
                   role="button" tabIndex={0} onClick={()=>openDrill("month",s.ym,`Жалобы за ${s.ym}${s.partial?" (неполный месяц)":""}`)}
                   onKeyDown={onKey(()=>openDrill("month",s.ym,`Жалобы за ${s.ym}`))}>
                <div className={"rv-bar"+(s.spike?" hot":"")+(s.partial?" part":"")} style={{height:Math.max(4,Math.round(s.n/trendMax*100))+"%"}}/>
                <div className="rv-blab">{s.ym.slice(2).replace("-",".")}</div>
              </div>)}
            </div>
            {(()=>{const sp=tr.series.filter(s=>s.spike);return sp.length?<div className="rv-spike">⚠ пик {sp.map(s=>s.ym).join(", ")} — выше базовой линии (медиана+MAD по завершённым месяцам). Клик по столбцу — разобрать, что произошло.</div>:null;})()}
          </>}
        </div>
        {/* THEMES */}
        <div className="rv-card">
          <div className="rv-ttl">Темы жалоб — риск-карта</div>
          <div className="rv-cap">доля от жалоб за 90 дн · мультилейбл (сумма ≠ 100%) · клик → лента темы</div>
          {busy?<Skel h={220}/>:!th||!th.themes||!th.themes.length?<RvNote err={th&&th.__err}/>:(()=>{
            const real=th.themes.filter(t=>t.key!=="other"), other=th.themes.find(t=>t.key==="other");
            const shown=thAll?real:real.slice(0,12);
            const row=t=>{
              const clk=t.key!=="other", risky=t.risk==="compliance"||t.risk==="conduct";
              return <div key={t.key} className={"rv-trow"+(theme===t.key?" sel":"")+(clk?"":" rv-trow-static")}
                   role={clk?"button":undefined} tabIndex={clk?0:undefined} aria-pressed={clk?(theme===t.key):undefined}
                   onClick={clk?()=>setTheme(theme===t.key?"":t.key):undefined}
                   onKeyDown={clk?onKey(()=>setTheme(theme===t.key?"":t.key)):undefined}>
                <div className="rv-tname">{t.label}{RV_RISK[t.risk]&&<span className={"rv-tag "+t.risk}>{RV_RISK[t.risk]}</span>}</div>
                <div className="rv-tbarw"><div className={"rv-tbar"+(risky?"":" n")} style={{width:Math.round(t.n/thMax*100)+"%"}}/></div>
                <div className="rv-tn mono">{fmtNum(t.n)}</div>
                <div className="rv-ttr">{rvDelta(t.delta_pct)}</div>
              </div>;
            };
            return <>
              {shown.map(row)}
              {real.length>12&&<div className="rv-th-toggle" role="button" tabIndex={0} onClick={()=>setThAll(!thAll)} onKeyDown={onKey(()=>setThAll(!thAll))}>
                {thAll?"свернуть ▴":`ещё ${real.length-12} тем ▾`}</div>}
              {other&&row(other)}
            </>;
          })()}
        </div>
      </div>
      <div className="rv-col">
        {/* GEO */}
        <div className="rv-card">
          <div className="rv-ttl">География</div>
          <div className="rv-cap">города · per-capita аномалии (12 мес) · клик → жалобы города</div>
          {busy?<Skel h={220}/>:!ge||!ge.cities||!ge.cities.length?<RvNote err={ge&&ge.__err}/>:ge.cities.map((c,i)=>(
            <div key={i} className="rv-grow rv-grow-click" role="button" tabIndex={0}
                 onClick={()=>openDrill("city",c.city,`Жалобы · ${c.city}`)}
                 onKeyDown={onKey(()=>openDrill("city",c.city,`Жалобы · ${c.city}`))}>
              <div style={{minWidth:0}}>
                <div className="rv-gcity">{c.city}{c.anomaly&&<span className="rv-tag conduct">аномалия</span>}</div>
                <div className={"rv-gbar"+(c.anomaly?" anom":"")} style={{width:Math.round(c.n/geMax*100)+"%",background:c.anomaly?"var(--accent)":"var(--ink-4)"}}/>
              </div>
              <div className="mono rv-gn">{fmtNum(c.n)}{c.per_100k?<span className="rv-gp"> · {c.per_100k}/100k</span>:""}</div>
            </div>
          ))}
        </div>
        {/* VS MARKET */}
        <div className="rv-card">
          <div className="rv-ttl">{bank} против рынка</div>
          <div className="rv-cap">доля в общем потоке жалоб banki.ru · {days} дн{product?` · ${product}`:""}</div>
          {busy?<Skel h={120}/>:!vm||!vm.rows||!vm.rows.length?<RvNote err={vm&&vm.__err}/>:vm.rows.map((r,i)=>(
            <div key={i} className="rv-vrow">
              <div className={"rv-vname"+(r.is_target?" t":"")}>{r.bank}</div>
              <div className={"rv-vbar"+(r.is_target?" t":"")} style={{width:Math.round(r.pct/vmMax*100)+"%"}}/>
              <div className="rv-vp mono">{String(r.pct).replace(".",",")}%</div>
            </div>
          ))}
        </div>
        {/* RADAR: срочные аномалии за 7 дней (грузится отдельно, LLM-анализ) */}
        <div className="rv-card rv-radar">
          <div className="rv-radar-head">
            <span className="rv-radar-ico" aria-hidden="true"><IcoRadar/></span>
            <div style={{flex:1,minWidth:0}}>
              <div className="rv-ttl">Срочные аномалии</div>
              <div className="rv-cap">резкие изменения за 7 дней{ov&&ov.as_of?` · ${ov.as_of}`:""}</div>
            </div>
            <span className={"rv-radar-live"+(anomBusy?" scan":"")} title="радар активен"/>
          </div>
          {anomBusy?
            <div className="rv-radar-scan"><div className="rv-radar-beam"/><span>Анализирую сигналы недели…</span></div>
           :(!anom||anom.calm||!anom.signals||!anom.signals.length)?
            <div className="rv-radar-calm"><span className="rv-radar-check"><IcoCheck/></span> Резких аномалий за неделю не выявлено</div>
           :<>
              <div className="rv-radar-chips">
                {anom.signals.map((s,i)=>{
                  const tip=`${s.week} за 7 дн (обычно ~${s.baseline_week}/нед)`
                    +(s.bank_specific?" · всплеск только у банка":"")
                    +(s.accel?` · ускоряется (${s.prev_week}→${s.week})`:"")
                    +(s.geo?` · ${s.geo.share}% из ${s.geo.city}`:"");
                  return <span key={i} className={"rv-radar-chip lvl-"+(s.level||"medium")+(s.bank_specific?" only":"")}
                        role="button" tabIndex={0} title={tip}
                        onClick={()=>setTheme(s.key)} onKeyDown={onKey(()=>setTheme(s.key))}>
                    {s.short||s.label}<b>{s.new?"новое":"×"+s.ratio}</b>{s.accel&&<span className="rv-radar-acc"><IcoTrendUp/></span>}
                  </span>;
                })}
              </div>
              {anom.summary?<div className="rv-radar-brief">{renderMD(anom.summary)}</div>
                :<div className="rv-cap" style={{marginTop:6}}>LLM-разбор недоступен — см. всплески выше (числа за 7 дн точны).</div>}
            </>}
        </div>
      </div>
    </div>

    {/* FEED */}
    <div className="rv-card">
      <div className="rv-ct">
        <div><div className="rv-ttl">Лента — доказательная база</div>
          <div className="rv-cap">{theme?<>тема: <b>{themeLabel}</b> · <span className="rv-clear" role="button" tabIndex={0} onClick={()=>setTheme("")} onKeyDown={onKey(()=>setTheme(""))}>сбросить ✕</span></>:"темы обращений определены автоматически (regex) · ✦ уточнить ИИ для точности"}</div></div>
        <button className="rv-cls-btn" onClick={classifyFeed} disabled={clsBusy||feedBusy||!feed||!feed.length}
                title="Переклассифицировать показанные отзывы с учётом смысла и отрицаний">
          {clsBusy?"Уточняю…":clsOn?"✦ темы уточнены ИИ":"✦ Уточнить темы (ИИ)"}
        </button>
      </div>
      <div className="rv-search">
        <span>⌕</span>
        <input value={qInput} onChange={e=>setQInput(e.target.value)}
          onKeyDown={e=>{if(e.key==="Enter")setQ(qInput.trim());}}
          placeholder="Найти жалобы по смыслу: «не зачисляют выручку по эквайрингу», «навязали страховку»… (Enter)"/>
        {q&&<span className="rv-clear" role="button" tabIndex={0} aria-label="Сбросить поиск" onClick={()=>{setQ("");setQInput("");}} onKeyDown={onKey(()=>{setQ("");setQInput("");})}>✕</span>}
      </div>
      {feedBusy?<><Skel h={70}/><div style={{height:8}}/><Skel h={70}/></>:
       !feed||!feed.length?<EmptyState text="Нет жалоб по выбранным фильтрам — попробуйте другой банк/продукт/тему."/>:
       feed.map((r,i)=>(
        <div key={i} className="rv-rev">
          <div className="rv-rh">
            <span>{r.date}</span>
            <RvThemes list={r.themes} src={r.theme_src}/>
            {r.product&&<span className="rv-pill rv-pill-dim" title="направление banki.ru">{r.product}</span>}
            {r.city&&<span className="rv-pill">{r.city}</span>}
            {r.similar>0&&<span className="rv-sim">+{r.similar} похожих</span>}
          </div>
          <div className="rv-rq rv-rq-click" role="button" tabIndex={0} onClick={()=>setModalRev(r)} onKeyDown={onKey(()=>setModalRev(r))}>
            {(r.text||"").slice(0,420)}{(r.text||"").length>420?<>…<span className="rv-more"> читать полностью →</span></>:""}
          </div>
          <div className="rv-rf">
            {r.url&&<a href={r.url} target="_blank" rel="noopener noreferrer" className="rv-lnk">banki.ru ↗</a>}
            <span className="rv-lnk2" role="button" tabIndex={0} onClick={()=>addCase(r)} onKeyDown={onKey(()=>addCase(r))}>＋ в аудит-дело</span>
          </div>
        </div>
       ))}
    </div>

    {/* МОДАЛ: полный текст обращения */}
    {modalRev&&<RvModal onClose={()=>setModalRev(null)} title="Обращение клиента"
        sub={[modalRev.date,modalRev.product,modalRev.city].filter(Boolean).join(" · ")}>
      <div className="rv-rh" style={{marginBottom:10}}>
        <RvThemes list={modalRev.themes} src={modalRev.theme_src}/>
        {modalRev.similar>0&&<span className="rv-sim">+{modalRev.similar} похожих (массовая жалоба)</span>}
      </div>
      <div className="rv-modal-text">{modalRev.text}</div>
      <div className="rv-rf" style={{marginTop:16}}>
        {modalRev.url&&<a href={modalRev.url} target="_blank" rel="noopener noreferrer" className="rv-lnk">banki.ru ↗</a>}
        <span className="rv-lnk2" role="button" tabIndex={0} onClick={()=>addCase(modalRev)} onKeyDown={onKey(()=>addCase(modalRev))}>＋ в аудит-дело</span>
      </div>
    </RvModal>}

    {/* ДРАУЭР: drill-in по городу/месяцу + LLM-объяснение */}
    {drill&&<RvModal side="right" onClose={()=>setDrill(null)} title={drill.label}
        sub={`${bank}${product?` · ${product}`:""}${drillItems?` · показано ${drillItems.length}`:""}`}>
      <button className="rv-explain-btn" onClick={runExplain} disabled={explainBusy}>
        {explainBusy?"Анализирую жалобы…":"✦ Объяснить причину (LLM)"}
      </button>
      {explain&&explain!=="__none__"&&<div className="rv-explain">{renderMD(explain)}</div>}
      {explain==="__none__"&&<div className="rv-explain rv-explain-err">Не удалось получить объяснение (LLM недоступен). Жалобы ниже — для ручного разбора.</div>}
      <div style={{marginTop:6}}>
        {drillBusy?<><Skel h={70}/><div style={{height:8}}/><Skel h={70}/></>:
         !drillItems||!drillItems.length?<RvNote/>:
         drillItems.map((r,i)=><RvReview key={i} r={r} onOpen={()=>setModalRev(r)}/>)}
      </div>
    </RvModal>}
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
    left: (()=>{const vw=window.innerWidth;const w=Math.min(300,vw-24);return Math.max(12,Math.min(vw-w-12,r.left-180));})(),
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
          report_md: report,   // [[CHART:i]]-маркеры остаются: PDF ставит графики по местам
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

// ─── Премиальный индикатор ожидания: пульс + подпись стадии + бегущие точки.
//     Закрывает «тихие окна» (генерация вопросов, сборка запроса, старт
//     research) — пользователь всегда видит, что система жива. ──────────────
function PendingDots({label}){
  return <div className="pending-row">
    <span className="dr-stage-pulse"/>
    <span className="pending-label">{label||"Думаю"}</span>
    <span className="pending-dots"><i/><i/><i/></span>
  </div>;
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
      <span className="dr-stage-pulse" style={{background:"var(--accent)"}}/>
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
      <button className="clarify-go" onClick={submit}>Уточнить и запустить →</button>
    </div>
  </div>;
}

// ─── Deep Research console — единая timeline-консоль прогона (редизайн).
//     Шесть display-фаз поверх реальных phase-событий; внутри каждой —
//     живые агенты (research), «размышления модели» (reasoning по стадиям,
//     переиспользуется ThinkingPanel), план отчёта (outline) и доуточнение
//     пробелов (gap-loop). Всё на реальном SSE-стриме. ─────────────────────
const DEEP_FLOW = [
  {key:"parse",     label:"Разбор запроса",       phases:["planning"]},
  {key:"discovery", label:"Поиск источников",     phases:["discovery"]},
  {key:"collect",   label:"Сбор данных",          phases:["research"]},
  {key:"synth",     label:"Синтез отчёта",        phases:["synthesizing"]},
  {key:"gaps",      label:"Доуточнение пробелов",  phases:["agent_iter_1","agent_iter_2","second_pass"]},
  {key:"verify",    label:"Проверка фактов",      phases:["merging","post_processing","verifying","charting"]},
];
// Какие reasoning-стадии показывать под какой display-фазой. active-флаг по-
// прежнему вычисляется через STAGE_PHASE (стадия активна ТОЛЬКО на своей фазе).
const PHASE_REASON = {parse:["conductor"], synth:["analyst","repair"], verify:["critic"]};

function DeepConsole({m, loading, elapsed}){
  const phase=m.phase;
  const curIdx = phase==="done" ? DEEP_FLOW.length
    : Math.max(0, DEEP_FLOW.findIndex(d=>d.phases.includes(phase)));
  const srcN   = (m.sources||[]).length;
  const states = Object.values(m.stepStates||{});
  const doneA  = states.filter(s=>s?.status==="done").length;
  const totA   = (m.plan||[]).length;
  const verified=m.claimCheck?.verified||0, dropped=m.claimCheck?.dropped||0;
  const iters  = (m.agentIters||[]).length;
  const pct = Math.min(100, Math.round(((curIdx + (phase==="done"?0:0.5))/DEEP_FLOW.length)*100));
  const el = elapsed||0;
  const elapsedDisplay = `${String(Math.floor(el/60)).padStart(2,"0")}:${String(el%60).padStart(2,"0")}`;
  const runLabel = PHASE_LABELS[phase] || (phase ? phase : "запуск");
  const right=(key,status)=>{
    if(status==="pending") return "";
    const done=status==="done";
    switch(key){
      case "parse":     return done?"запрос разобран":"извлекаю сущности";
      case "discovery": return done?`${srcN} источников`:"сканирую веб";
      case "collect":   return done?`${doneA}/${totA||"·"} · ${srcN} источн.`:(totA?`${totA} агентов параллельно`:"запуск агентов");
      case "synth":     return done?"черновик готов":"пишу черновик";
      case "gaps":      return done?(iters?`${iters} итер.`:"без пробелов"):"ищу пробелы";
      case "verify":    return done?`${verified} подтв.${dropped?` · ${dropped} фильтр`:""}`:"сверяю числа";
      default:          return "";
    }
  };
  const dotStyle=(s)=> s==="done"
    ? {background:"var(--ink)",borderColor:"var(--ink)"}
    : s==="running"
      ? {background:"var(--accent)",borderColor:"var(--accent)",boxShadow:"0 0 0 4px var(--accent-soft)"}
      : {background:"transparent",borderColor:"var(--hair-2)"};
  const titleStyle=(s)=> s==="pending" ? {color:"var(--ink-4)",fontWeight:450}
                       : s==="running" ? {color:"var(--ink)",fontWeight:600}
                       : {color:"var(--ink)",fontWeight:500};
  return <div className="dr-con-wrap">
    <div className="dr-con">
      <div className="dr-con-head">
        <span className="dr-con-pulse"/>
        <span className="dr-con-title">Глубокое исследование</span>
        <span className="dr-con-sub">{runLabel}</span>
        <span className="dr-con-el mono">{elapsedDisplay}</span>
      </div>
      <div className="dr-con-bar"><div className="dr-con-bar-fill" style={{width:pct+"%"}}/></div>
      <div className="dr-con-spine">
        {DEEP_FLOW.map((d,idx)=>{
          const status = idx<curIdx?"done":(idx===curIdx?"running":"pending");
          const last = idx===DEEP_FLOW.length-1;
          return <div key={d.key} className="dr-con-row">
            <div className="dr-con-col">
              <span className="dr-con-dot" style={dotStyle(status)}>
                {status==="done" && <svg width="7" height="7" viewBox="0 0 24 24" fill="none"
                  stroke="var(--paper)" strokeWidth="3.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>}
              </span>
              {!last && <span className="dr-con-conn" style={{background:status==="done"?"var(--ink)":"var(--hair)"}}/>}
            </div>
            <div className="dr-con-body">
              <div className="dr-con-line">
                <span className="dr-con-label" style={titleStyle(status)}>{d.label}</span>
                <span className="dr-con-right mono" style={{color:status==="running"?"var(--accent)":"var(--ink-3)"}}>{right(d.key,status)}</span>
              </div>
              {/* агенты — фаза сбора */}
              {d.key==="collect" && status!=="pending" && totA>0 &&
                <div className="dr-con-agents">
                  {m.plan.map((step,si)=>{
                    const s=_agentStatus(m.stepStates?.[step.n]);
                    return <div key={si} className="dr-con-agent">
                      <span className="dr-con-agent-dot" style={{background:s.c,animation:s.run?"pulse 1.4s ease-in-out infinite":"none"}}/>
                      <span className="dr-con-agent-name">{step.title}</span>
                      <span className="dr-con-agent-st mono" style={{color:s.c}}>{s.t}</span>
                    </div>;
                  })}
                </div>}
              {/* размышления модели — reuse ThinkingPanel (таймер/скролл/стрим) */}
              {(PHASE_REASON[d.key]||[]).filter(s=>m.reasoningStages?.[s]).map(s=>
                <ThinkingPanel key={s} stage={s} text={m.reasoningStages[s]}
                  active={loading && m.phase===STAGE_PHASE[s]}/>)}
              {/* план отчёта (outline preview) */}
              {d.key==="synth" && status!=="pending" && (m.outline||[]).length>0 &&
                <div className="dr-con-outline">
                  <div className="dr-con-outline-h">План отчёта</div>
                  <div className="dr-con-outline-grid">
                    {m.outline.map((o,oi)=>(
                      <div key={oi} className="dr-con-outline-item">
                        <span className="dr-con-outline-n mono">{String(oi+1).padStart(2,"0")}</span>{o.title||o.kind}
                      </div>))}
                  </div>
                </div>}
              {/* доуточнение пробелов (gap loop) */}
              {d.key==="gaps" && status!=="pending" && (m.agentIters||[]).length>0 &&
                <div className="dr-con-gaps">
                  {m.agentIters.flatMap((it,ii)=>(it.gaps||[]).map((g,gi)=>(
                    <div key={`${ii}-${gi}`} className="dr-con-gap">
                      <span className="dr-con-gap-i mono">↻</span>
                      <span className="dr-con-gap-w">{g.what||g.query}</span>
                      <span className="dr-con-gap-q mono">{g.query}</span>
                    </div>)))}
                </div>}
            </div>
          </div>;
        })}
      </div>
    </div>
  </div>;
}

// ─── Сводка завершённого прогона (collapsed bar над отчётом, редизайн). ────
function ResearchSummary({m}){
  const states=Object.values(m.stepStates||{});
  const total=(m.plan||[]).length;
  const srcN=(m.sources||[]).length;
  const verified=m.claimCheck?.verified||0, dropped=m.claimCheck?.dropped||0;
  return <div className="dr-summary-bar">
    <span className="dr-summary-ok">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
      Исследование завершено
    </span>
    {total>0 && <><span>·</span><span>{total} этапов</span></>}
    <span>·</span><span><b>{srcN}</b> источников</span>
    {verified>0 && <><span>·</span><span><b>{verified}</b> фактов подтверждено</span></>}
    {dropped>0 && <><span>·</span><span><b>{dropped}</b> отфильтровано</span></>}
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

// ─── Статус агента (ждёт/ищет/читает/обдумывает/готов) по РЕАЛЬНЫМ событиям
//     agent_tool_call. Используется карточками агентов внутри DeepConsole. ─────
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
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{flex:"none"}}><path d="M20 6L9 17l-5-5"/></svg>
      Автопроверка достоверности пройдена — утверждений, требующих ручной сверки, не выявлено.
    </div>;
  }
  const word = u.length===1?"утверждение требует":(u.length<5?"утверждения требуют":"утверждений требуют");
  return <div className="dr-verify dr-verify-warn">
    <div className="dr-verify-head">{u.length} {word} ручной проверки</div>
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
      <span className="dr-ranking-title">Рейтинг</span>
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
      <span className="dr-insights-title">Ключевые инсайты</span>
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
    // Палитра из дизайн-токенов (живьём из CSS → авто тёмная тема).
    // Сбер/highlight — всегда фирменный accent; остальные — ink-градации:
    // иерархия сохраняется, но график в языке инструмента, а не ч/б-ксерокс.
    const css=(n,fb)=>{try{const v=getComputedStyle(document.documentElement).getPropertyValue(n).trim();return v||fb;}catch{return fb;}};
    const ACC=css("--accent","#c94f34"), INK=css("--ink","#16181d"),
          INK2=css("--ink-2","#44464d"), INK3=css("--ink-3","#707075"),
          INK4=css("--ink-4","#9c9ea3"), HAIR=css("--hair","#ebebed"),
          PAPER=css("--paper","#faf9f7");
    const palette=[INK,INK2,INK3,INK4,HAIR];
    const hl=spec.highlight||null;
    const isHl=(lb)=>hl&&String(lb||"").toLowerCase().includes(String(hl).toLowerCase().slice(0,5));
    // цвет позиции: подсвеченная метка (Сбер) = accent, прочие — sequential ink
    const posColor=(lb,i)=>isHl(lb)?ACC:palette[i%palette.length];
    const horizontal = spec.chartType==="horizontalBar";
    const isDoughnut = spec.chartType==="doughnut";
    const isLine     = spec.chartType==="line";
    const single=(spec.datasets||[]).length===1;
    const datasets = (spec.datasets||[]).map((d,i)=>{
      const seriesHl=isHl(d.label);
      const base=seriesHl?ACC:palette[i%palette.length];
      return {
        ...d,
        backgroundColor: isDoughnut ? (spec.labels||[]).map((lb,li)=>posColor(lb,li))
                         : isLine ? "transparent"
                         : single ? (spec.labels||[]).map((lb,li)=>posColor(lb,li))
                         : base,
        borderColor:     isDoughnut ? PAPER : base,
        borderWidth:     isLine ? 2 : (isDoughnut ? 2 : 0),
        borderRadius:    (!isLine&&!isDoughnut) ? 3 : 0,
        pointRadius:     isLine ? 3 : 0,
        pointBackgroundColor: base,
        tension:         isLine ? 0.25 : 0,
      };
    });
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
            ctx.fillStyle = INK;
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
    // Пунктирная референс-линия (медиана/ключевая ставка) с подписью.
    const refPlugin = {
      id:"refLine",
      afterDatasetsDraw(chart){
        const rl=spec.referenceLine;
        if(!rl||isDoughnut||rl.value==null) return;
        const area=chart.chartArea;
        const sc=horizontal?chart.scales.x:chart.scales.y;
        if(!sc)return;
        const px=sc.getPixelForValue(+rl.value);
        const c2=chart.ctx; c2.save();
        c2.strokeStyle=INK3; c2.setLineDash([4,4]); c2.lineWidth=1;
        c2.beginPath();
        if(horizontal){c2.moveTo(px,area.top);c2.lineTo(px,area.bottom);}
        else{c2.moveTo(area.left,px);c2.lineTo(area.right,px);}
        c2.stroke();
        c2.setLineDash([]);
        c2.font="500 9.5px 'JetBrains Mono', monospace"; c2.fillStyle=INK3;
        const t=((rl.label||"")+" "+fmtVal(+rl.value)).trim();
        if(horizontal) c2.fillText(t, Math.min(px+5,area.right-60), area.top+10);
        else c2.fillText(t, area.left+5, Math.max(px-5,area.top+10));
        c2.restore();
      },
    };
    // Монограммы банков на категорийной оси (horizontalBar): бейдж-кружок цвета
    // бара с инициалами + имя (Сбер — акцентом). Родные тики оси скрываются.
    const monoPlugin = {
      id:"monoAxis",
      afterDraw(chart){
        if(!horizontal||isDoughnut) return;
        const s=chart.scales.y; if(!s) return;
        const c2=chart.ctx;
        const inits=(lb)=>{const p=String(lb||"").split(/[\s\-]+/).filter(Boolean);
          return ((p.length>1?p[0][0]+p[1][0]:String(lb||"").slice(0,2))||"·").toUpperCase();};
        (spec.labels||[]).forEach((lb,i)=>{
          const y=s.getPixelForTick(i);
          const cx=s.left+12;
          c2.save();
          c2.beginPath(); c2.arc(cx,y,9,0,Math.PI*2);
          c2.fillStyle=posColor(lb,i); c2.fill();
          c2.font="600 8px 'JetBrains Mono', monospace"; c2.fillStyle=PAPER;
          c2.textAlign="center"; c2.textBaseline="middle";
          c2.fillText(inits(lb),cx,y+0.5);
          c2.font="500 10.5px Geist, sans-serif";
          c2.fillStyle=isHl(lb)?ACC:INK3;
          c2.textAlign="left";
          let nm=String(lb||""); if(nm.length>13)nm=nm.slice(0,12)+"…";
          c2.fillText(nm,cx+14,y+0.5);
          c2.restore();
        });
      },
    };
    const inst = new window.Chart(ctx, {
      type: horizontal ? "bar" : (isDoughnut ? "doughnut" : isLine ? "line" : "bar"),
      data: {labels: spec.labels||[], datasets},
      plugins: [dataLabelsPlugin, refPlugin, monoPlugin],
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
              color:INK2, boxWidth:10, boxHeight:10, padding:14,
              usePointStyle: true, pointStyle: "rect",
            },
          },
          title: {
            display: !!spec.title,
            text: spec.title + (spec.unit ? " · " + spec.unit : ""),
            font: {size:13.5, weight:"600", family:"'Source Serif 4', Georgia, serif"},
            color: INK, padding: {bottom: 14},
            align: "start",
          },
          tooltip: {
            intersect: false, backgroundColor: INK,
            titleColor: PAPER, bodyColor: PAPER,
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
            ticks: {font:{size:10.5, family:"Geist, sans-serif"},
                    // категорийная ось снизу (vertical bar): Сбер — акцентом
                    color: horizontal ? INK3
                      : (c)=>isHl((spec.labels||[])[c.index]) ? ACC : INK3},
            grid: {display: !horizontal, color:HAIR, lineWidth: 1, drawTicks: false},
            border: {display: false},
          },
          y: {
            beginAtZero: true,
            // horizontalBar: родные тики скрыты — ось рисует monoPlugin
            // (бейджи-монограммы банков + имена, Сбер акцентом)
            afterFit: horizontal ? (sc)=>{sc.width=Math.max(sc.width,118);} : undefined,
            ticks: horizontal ? {display:false}
              : {font:{size:10.5, family:"Geist, sans-serif"}, color:INK3},
            grid: {display: horizontal, color:HAIR, lineWidth: 1, drawTicks: false},
            border: {display: false},
          },
        },
      },
    });
    return ()=>inst.destroy();
  },[spec]);
  return <div className="dr-chart">
    <canvas ref={ref} id={idRef.current}/>
    {spec.insight&&<div className="dr-chart-insight">{spec.insight}</div>}
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
  news_pool:            "Новостной пул дня",
  execute_code:         "Код и SQL",
  skill_view:           "Навык агента",
  terminal:             "Терминал",
  web_search:           "Веб-поиск",
  search_complaints:    "Поиск жалоб",
  get_bank_features:    "Условия банка",
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
function SourcesRail({sources, activeN, onHover, onClick, failed}){
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
    {failed>0&&<div className="dr-rail-failed">⚠ {failed} источник(ов) недоступны — исключены из списка</div>}
  </aside>;
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

// ─── История чатов/отчётов: off-canvas drawer (премиальный, лёгкий) ───────────
function fmtHistTime(s){
  try{
    const d=new Date(s), now=new Date();
    if(d.toDateString()===now.toDateString())
      return d.toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"});
    return d.toLocaleDateString("ru",{day:"2-digit",month:"2-digit"});
  }catch{return "";}
}
function histGroup(s){
  try{
    const d=new Date(s), now=new Date(), day=86400000;
    const startToday=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime();
    const t=d.getTime();
    if(t>=startToday) return "Сегодня";
    if(t>=startToday-day) return "Вчера";
    if(t>=startToday-6*day) return "На этой неделе";
    return "Раньше";
  }catch{return "Раньше";}
}
const CP_CSS=`
.cp-ov{position:fixed;inset:0;z-index:200;display:grid;place-items:start center;padding:12vh 20px 20px;
  background:oklch(20% 0.02 260 / .34);backdrop-filter:blur(4px) saturate(1.05);
  opacity:0;transition:opacity .17s ease;}
.cp-ov.in{opacity:1;}
.cp{width:600px;max-width:100%;max-height:74vh;display:flex;flex-direction:column;
  background:var(--surface);border:1px solid var(--hair);border-radius:16px;overflow:hidden;
  box-shadow:0 24px 70px oklch(0% 0 0 / .22), 0 3px 10px oklch(0% 0 0 / .08);
  transform:translateY(10px) scale(.986);opacity:0;
  transition:transform .22s cubic-bezier(.2,0,0,1),opacity .2s ease;}
.cp-ov.in .cp{transform:none;opacity:1;}
.cp-search{display:flex;align-items:center;gap:11px;padding:16px 18px;border-bottom:1px solid var(--hair);}
.cp-search>svg{color:var(--ink-4);flex:none;}
.cp-search input{flex:1;border:0;background:none;font-size:16px;line-height:1.3;color:var(--ink);
  font-family:'Geist','Inter',sans-serif;letter-spacing:-.01em;}
.cp-search input::placeholder{color:var(--ink-4);}
.cp-search input:focus{outline:none;}
.cp-esc{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--ink-4);
  border:1px solid var(--hair);border-radius:5px;padding:3px 7px;flex:none;}
.cp-seg{display:flex;gap:3px;padding:9px 14px 5px;}
.cp-seg button{font-size:12px;color:var(--ink-3);padding:5px 11px;border-radius:7px;display:flex;
  align-items:center;gap:7px;transition:background .14s,color .14s;}
.cp-seg button:hover{color:var(--ink-2);}
.cp-seg button.on{background:var(--accent-soft);color:var(--accent);font-weight:500;}
.cp-seg .n{font-family:'JetBrains Mono',monospace;font-size:10px;font-variant-numeric:tabular-nums;opacity:.75;}
.cp-list{flex:1;overflow-y:auto;overscroll-behavior:contain;padding:3px 8px 10px;}
.cp-group{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;
  color:var(--ink-4);padding:13px 10px 5px;}
.cp-row{display:flex;align-items:center;gap:12px;padding:8px 10px;border-radius:9px;cursor:pointer;
  scroll-margin:10px;transition:background .12s;}
.cp-row:active{transform:scale(.97);}
.cp-row.sel{background:var(--accent-soft);}
.cp-ic{width:30px;height:30px;flex:none;border-radius:8px;display:grid;place-items:center;
  background:var(--paper-2);color:var(--ink-3);border:1px solid var(--hair);transition:color .12s,border-color .12s,background .12s;}
.cp-row.sel .cp-ic{color:var(--accent);border-color:color-mix(in oklab,var(--accent),transparent 78%);background:var(--surface);}
.cp-main{flex:1;min-width:0;}
.cp-t{font-size:13.5px;line-height:1.35;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.cp-p{font-size:12px;line-height:1.35;color:var(--ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px;}
.cp-meta{display:flex;align-items:center;gap:9px;flex:none;}
.cp-time{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-4);font-variant-numeric:tabular-nums;white-space:nowrap;}
.cp-acts{display:none;gap:2px;}
.cp-row:hover .cp-acts,.cp-row.sel .cp-acts{display:flex;}
.cp-acts button{width:28px;height:28px;border-radius:7px;color:var(--ink-4);display:grid;place-items:center;
  transition:background .12s,color .12s;}
.cp-acts button:hover{background:var(--paper-2);color:var(--ink);}
.cp-acts button.on{color:var(--accent);}
.cp-banks{display:flex;gap:4px;}
.cp-bank{font-family:'JetBrains Mono',monospace;font-size:9.5px;text-transform:uppercase;letter-spacing:.03em;
  color:var(--ink-3);background:var(--paper-2);border:1px solid var(--hair);border-radius:5px;padding:1px 6px;}
.cp-owner{font-size:11px;color:var(--accent);white-space:nowrap;}
.cp-empty{display:flex;flex-direction:column;align-items:center;gap:12px;padding:52px 24px;color:var(--ink-4);text-align:center;}
.cp-empty>svg{opacity:.45;}
.cp-empty .t{font-size:14px;color:var(--ink-3);text-wrap:balance;max-width:320px;line-height:1.5;}
.cp-empty .h{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--ink-4);}
.cp-foot{display:flex;align-items:center;gap:18px;padding:10px 16px;border-top:1px solid var(--hair);
  font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-4);}
.cp-foot span{display:inline-flex;align-items:center;gap:5px;}
.cp-foot kbd{border:1px solid var(--hair);border-radius:4px;padding:1px 5px;color:var(--ink-3);
  min-width:16px;text-align:center;line-height:1.5;}
/* вход в историю: пилюля рядом с «Новый запрос» и на welcome */
.hist-btn{display:inline-flex;align-items:center;gap:7px;padding:6px 12px;border-radius:8px;
  border:1px solid var(--hair);background:var(--surface);color:var(--ink-2);font-size:12.5px;
  box-shadow:var(--shadow-1);transition:border-color .14s,color .14s,transform .1s;}
.hist-btn:hover{border-color:var(--ink-4);color:var(--ink);}
.hist-btn:active{transform:scale(.97);}
.hist-btn kbd{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--ink-4);
  border:1px solid var(--hair);border-radius:4px;padding:0 4px;}
/* welcome: «Продолжить» — недавние диалоги */
.aw-recent{margin-top:30px;width:100%;max-width:640px;}
.aw-recent-h{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;}
.aw-recent-h .l{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-4);}
.aw-recent-h button{font-size:11.5px;color:var(--ink-3);display:inline-flex;align-items:center;gap:5px;transition:color .12s;}
.aw-recent-h button:hover{color:var(--accent);}
.aw-recent-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;}
.aw-rec{text-align:left;padding:11px 13px;border:1px solid var(--hair);background:var(--surface);border-radius:10px;
  display:flex;flex-direction:column;gap:3px;transition:border-color .14s,transform .12s,box-shadow .14s;min-width:0;}
.aw-rec:hover{border-color:var(--ink-4);transform:translateY(-2px);box-shadow:var(--shadow-1);}
.aw-rec .t{font-size:12.5px;color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.aw-rec .m{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--ink-4);font-variant-numeric:tabular-nums;}
`;

// SVG-иконки (единый штрих, оптически выверенные)
const IcSearch = () => <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>;
const IcChat = () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 15a2 2 0 0 1-2 2H8l-4 4V5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2z"/></svg>;
const IcDoc = () => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M14 3v5h5"/><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M8 13h8M8 17h6"/></svg>;
const IcPin = ({on}) => <svg width="14" height="14" viewBox="0 0 24 24" fill={on?"currentColor":"none"} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M12 17v5"/><path d="M9 10.8V4h6v6.8l2 3.2H7z"/></svg>;
const IcTrash = () => <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/></svg>;

// Command palette истории (⌘K): диалоги + отчёты, клавиатурная навигация.
function CommandPalette({open,onClose,onLoadSession,onLoadReport,refreshTick}){
  const[vis,setVis]=useState(false);
  const[tab,setTab]=useState("chats");
  const[query,setQuery]=useState("");
  const[sessions,setSessions]=useState([]);
  const[reports,setReports]=useState([]);
  const[shared,setShared]=useState([]);
  const[sel,setSel]=useState(0);
  const[loading,setLoading]=useState(false);
  const inputRef=useRef();
  const listRef=useRef();

  const reload=useCallback(()=>{
    setLoading(true);
    Promise.all([
      apiFetch("/api/chat/sessions").then(d=>setSessions(d.sessions||[])).catch(()=>{}),
      apiFetch("/api/reports").then(d=>{setReports(d.reports||[]);setShared(d.shared||[]);}).catch(()=>{}),
    ]).finally(()=>setLoading(false));
  },[]);
  useEffect(()=>{
    if(open){ setVis(true); setQuery(""); setSel(0); reload();
      setTimeout(()=>inputRef.current&&inputRef.current.focus(),70); }
  },[open,refreshTick,reload]);
  const close=useCallback(()=>{ setVis(false); setTimeout(onClose,170); },[onClose]);

  const match=(t)=>!query||(t||"").toLowerCase().includes(query.toLowerCase());
  const fSessions=sessions.filter(s=>match(s.title)||match(s.last_preview));
  const fReports=reports.filter(r=>match(r.title)||match(r.question));
  const fShared=shared.filter(r=>match(r.title)||match(r.question));
  // Плоский nav-список (в порядке отображения) для клавиатуры.
  const nav = tab==="chats"
    ? fSessions.map(s=>({kind:"chat",id:s.session_id}))
    : [...fReports.map(r=>({kind:"report",id:r.report_id})), ...fShared.map(r=>({kind:"report",id:r.report_id}))];
  useEffect(()=>{ setSel(s=>Math.max(0,Math.min(s,nav.length-1))); },[tab,query,sessions,reports,shared]); // eslint-disable-line

  const activate=(it)=>{ if(!it)return; close();
    setTimeout(()=>{ it.kind==="chat"?onLoadSession(it.id):onLoadReport(it.id); },60); };
  useEffect(()=>{
    if(!open)return;
    const onKey=(e)=>{
      if(e.key==="Escape"){e.preventDefault();close();}
      else if(e.key==="ArrowDown"){e.preventDefault();setSel(s=>Math.min(nav.length-1,s+1));}
      else if(e.key==="ArrowUp"){e.preventDefault();setSel(s=>Math.max(0,s-1));}
      else if(e.key==="Enter"){e.preventDefault();activate(nav[sel]);}
    };
    window.addEventListener("keydown",onKey);
    return ()=>window.removeEventListener("keydown",onKey);
  },[open,nav,sel]); // eslint-disable-line
  useEffect(()=>{ // автоскролл выделенной строки
    const el=listRef.current&&listRef.current.querySelector(".cp-row.sel");
    if(el)el.scrollIntoView({block:"nearest"});
  },[sel,tab]);

  const delSession=async(e,sid)=>{ e.stopPropagation();
    await apiDel(`/api/chat/sessions/${sid}`); setSessions(s=>s.filter(x=>x.session_id!==sid)); };
  const pinSession=async(e,s)=>{ e.stopPropagation();
    await apiPost(`/api/chat/sessions/${s.session_id}/pin`,{pinned:!s.pinned}).catch(()=>{}); reload(); };

  if(!open&&!vis)return null;
  const nChats=sessions.length, nReports=reports.length+shared.length;

  // Рендер списка чатов с группами по времени (порядок = nav-порядок).
  let navIdx=-1, lastG=null;
  const chatRows=[];
  fSessions.forEach((s)=>{
    navIdx++; const i=navIdx;
    const g=s.pinned?"Закреплённые":histGroup(s.updated_at);
    if(g!==lastG){ chatRows.push(<div className="cp-group" key={"g"+i}>{g}</div>); lastG=g; }
    chatRows.push(
      <div key={s.session_id} className={"cp-row"+(i===sel?" sel":"")}
           onMouseEnter={()=>setSel(i)} onClick={()=>activate({kind:"chat",id:s.session_id})}>
        <div className="cp-ic"><IcChat/></div>
        <div className="cp-main">
          <div className="cp-t">{s.title||"Без названия"}</div>
          <div className="cp-p">{(s.last_preview||"").replace(/[#*|>\n]/g," ").replace(/\s+/g," ").trim().slice(0,70)||"—"}</div>
        </div>
        <div className="cp-meta">
          <span className="cp-time">{fmtHistTime(s.updated_at)}</span>
          <div className="cp-acts">
            <button className={s.pinned?"on":""} onClick={(e)=>pinSession(e,s)} title={s.pinned?"Открепить":"Закрепить"}><IcPin on={s.pinned}/></button>
            <button onClick={(e)=>delSession(e,s.session_id)} title="Удалить"><IcTrash/></button>
          </div>
        </div>
      </div>);
  });

  const reportRow=(r,i,ownerName)=>(
    <div key={(ownerName?"s":"r")+r.report_id} className={"cp-row"+(i===sel?" sel":"")}
         onMouseEnter={()=>setSel(i)} onClick={()=>activate({kind:"report",id:r.report_id})}>
      <div className="cp-ic"><IcDoc/></div>
      <div className="cp-main">
        <div className="cp-t">{r.title||r.question}</div>
        <div className="cp-p">{ownerName?<span className="cp-owner">от {ownerName}</span>:(r.question||"")}</div>
      </div>
      <div className="cp-meta">
        {(r.banks||[]).slice(0,2).length>0 && <div className="cp-banks">{(r.banks||[]).slice(0,2).map(b=><span key={b} className="cp-bank">{b}</span>)}</div>}
        <span className="cp-time">{fmtHistTime(r.created_at)}</span>
      </div>
    </div>);
  let ri=-1;
  const reportRows=[];
  if(fReports.length){ fReports.forEach(r=>{ ri++; reportRows.push(reportRow(r,ri,null)); }); }
  if(fShared.length){ reportRows.push(<div className="cp-group" key="shg">Поделились со мной</div>);
    fShared.forEach(r=>{ ri++; reportRows.push(reportRow(r,ri,r.owner_name||r.owner)); }); }

  const empty=(tab==="chats"?!fSessions.length:!reportRows.length);

  return <div className={"cp-ov"+(vis?" in":"")} onClick={close}>
    <div className="cp" onClick={e=>e.stopPropagation()}>
      <div className="cp-search">
        <IcSearch/>
        <input ref={inputRef} value={query} onChange={e=>{setQuery(e.target.value);setSel(0);}}
               placeholder={tab==="chats"?"Поиск по диалогам…":"Поиск по отчётам…"}/>
        <span className="cp-esc">ESC</span>
      </div>
      <div className="cp-seg">
        <button className={tab==="chats"?"on":""} onClick={()=>{setTab("chats");setSel(0);}}><IcChat/>Диалоги <span className="n">{nChats}</span></button>
        <button className={tab==="reports"?"on":""} onClick={()=>{setTab("reports");setSel(0);}}><IcDoc/>Отчёты <span className="n">{nReports}</span></button>
      </div>
      <div className="cp-list" ref={listRef}>
        {empty
          ? <div className="cp-empty">
              {tab==="chats"?<IcChat/>:<IcDoc/>}
              <div className="t">{loading?"Загрузка…":(query?"Ничего не найдено":(tab==="chats"?"Здесь появятся ваши диалоги с ИИ-аналитиком":"Здесь появятся ваши аудит-отчёты"))}</div>
              {!query&&!loading&&<div className="h">задайте вопрос, чтобы начать</div>}
            </div>
          : (tab==="chats"?chatRows:reportRows)}
      </div>
      <div className="cp-foot">
        <span><kbd>↑</kbd><kbd>↓</kbd> навигация</span>
        <span><kbd>↵</kbd> открыть</span>
        <span><kbd>esc</kbd> закрыть</span>
      </div>
    </div>
  </div>;
}

function AIPage(){
  // Пустая лента → показывается welcome-экран (он и есть приветствие). Отдельным
  // ai-сообщением «Здравствуйте…» не засоряем диалог после первой отправки.
  const me=useMe();
  const[msgs,setMsgs]=useState([]);
  const[q,setQ]=useState("");
  const[loading,setLoading]=useState(false);
  const[deepMode,setDeepMode]=useState(false);
  const[showKbd,setShowKbd]=useState(false);
  const[hoverCite,setHoverCite]=useState(null);          // {n, anchor} для tooltip
  const[activeCite,setActiveCite]=useState(null);        // подсветка bidirectional
  const[hideRail,setHideRail]=useState(false);
  const[hideToc,setHideToc]=useState(false);
  const[sessionId,setSessionId]=useState(null);          // текущая сессия истории
  const[aiFb,setAiFb]=useState(null);                    // мои оценки ответов (ai_answer)
  useEffect(()=>{apiFetch("/api/feedback?kind=ai_answer").then(d=>setAiFb(d.items||{})).catch(()=>setAiFb({}));},[]);
  // страница живёт в фоне (Shell держит смонтированной) — сообщаем Shell о ходе
  // прогона: точка в rail + тост «Отчёт готов», когда пользователь на другой вкладке
  useEffect(()=>{ try{window.dispatchEvent(new CustomEvent("al-ai-state",
    {detail:{running:loading}}));}catch{} },[loading]);
  const[histOpen,setHistOpen]=useState(false);           // command palette истории
  const[recent,setRecent]=useState([]);                  // недавние диалоги для welcome
  const[elapsed,setElapsed]=useState(0);                 // таймер прогона deep
  const runStartRef=useRef(0);
  const feedRef=useRef();
  const inputRef=useRef();
  const msgsRef=useRef(msgs);
  useEffect(()=>{msgsRef.current=msgs;},[msgs]);
  // Недавние диалоги для welcome-экрана (обновляются при возврате к пустой ленте).
  useEffect(()=>{
    if(!msgs.some(m=>m.role==="user"))
      apiFetch("/api/chat/sessions").then(d=>setRecent((d.sessions||[]).slice(0,4))).catch(()=>{});
  },[msgs]);
  // prefill из «Обзора» (✦ Спросить ИИ): композер заполняется, но НЕ отправляется —
  // пользователь видит и правит промпт (контроль + экономия токенов)
  useEffect(()=>{
    try{
      const p=sessionStorage.getItem("al-ai-prefill");
      if(p){sessionStorage.removeItem("al-ai-prefill");setQ(p);
        setTimeout(()=>{inputRef.current&&inputRef.current.focus();},50);}
    }catch{}
  },[]);
  // авто-рост textarea как в современных мессенджерах: высота по контенту до max
  useEffect(()=>{const el=inputRef.current;if(el){el.style.height="auto";el.style.height=Math.min(el.scrollHeight,160)+"px";}},[q]);
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
  // Единый таймер прогона: считаем от runStartRef. Интервал создаётся один раз
  // (не зависит от msgs), поэтому частые SSE-апдейты его не сбрасывают.
  useEffect(()=>{
    const id=setInterval(()=>{ if(runStartRef.current) setElapsed(Math.floor((Date.now()-runStartRef.current)/1000)); },500);
    return ()=>clearInterval(id);
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
        body:JSON.stringify({question,history,force_deep:forceDeep,session_id:sessionId}),
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
              if(data.type==="session"){
                if(data.session_id) setSessionId(data.session_id);
              }else if(data.type==="text"&&data.chunk){
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
              }else if(data.type==="engine"){
                updateLast(()=>({engine:data.value}));
              }else if(data.type==="tool_call"){
                updateLast(last=>({tools:[...(last.tools||[]),data.name]}));
              }else if(data.type==="sources"&&Array.isArray(data.sources)){
                updateLast(()=>({sources:data.sources,sourcesFailed:data.failed||0}));
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
              }else if(data.type==="report_saved"){
                updateLast(()=>({report_id:data.report_id}));   // «Поделиться» сразу после прогона
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
    runStartRef.current=Date.now(); setElapsed(0);     // старт таймера прогона
    setMsgs(m=>[...m.filter(x=>x.role!=="pending"),{role:"ai",text:"",tools:[]}]);
    streamChat(t,history,forceDeep);
  };
  // Точка входа: модуль «asking» — сначала clarify-воронка (если запрос неполный),
  // потом research. Fail-open: ошибка/полный запрос → сразу research.
  const send=async(txt)=>{
    const t=(txt||q).trim();
    if(!t||loading)return;
    setQ("");
    const forceDeep = deepMode ? true : null;     // null = auto-detect на бэке
    // Снимаем незакрытую clarify-карточку; сразу показываем индикатор «анализирую»
    // (генерация вопросов идёт ~5с — без него экран пустой = «тишина»).
    setMsgs(m=>[...m.filter(x=>x.role!=="clarify"),{role:"user",text:t},
                {role:"pending",label:"Анализирую запрос…"}]);
    setLoading(true);
    // Уточняющая воронка — только для Deep Research: быстрый режим отвечает сразу,
    // агент сам делает разумные допущения (фидбек владельца 22.07).
    if(!deepMode && !forceDeep){ runSend(t,forceDeep); return; }
    let data=null;
    try{ data=await apiPost("/api/ai/clarify",{question:t,deep:!!deepMode}); }catch(e){ data=null; }
    if(!data || data.complete!==false || !(Array.isArray(data.questions)&&data.questions.length)){
      runSend(t,forceDeep);                       // воронка не нужна / ошибка → research
      return;
    }
    setLoading(false);                            // интерактивная карточка вопросов
    setMsgs(m=>[...m.filter(x=>x.role!=="pending"),
                {role:"clarify",question:t,forceDeep,questions:data.questions}]);
  };
  // Submit воронки: собрать обогащённый промпт (сервер) → пометить запрос → research.
  const clarifySubmit=async(srcQuestion,forceDeep,answers)=>{
    if(loading)return;
    setLoading(true);
    // Индикатор на время сборки обогащённого запроса (~5с rewrite) — без него
    // после ответа на воронку экран молчит ~15с до старта research.
    setMsgs(m=>[...m.filter(x=>x.role!=="clarify"),{role:"pending",label:"Собираю уточнённый запрос…"}]);
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
  // Апселл из быстрого ответа: запускаем тот же запрос как Deep Research.
  const runDeepFromQuick=(srcQ)=>{
    if(loading||!srcQ)return;
    setDeepMode(true);
    setMsgs(m=>[...m,{role:"user",text:srcQ}]);
    runSend(srcQ,true);
  };
  // «Новый запрос» — сброс ленты к приветствию (welcome). Заблокировано во
  // время прогона, чтобы не оборвать активный stream-reader.
  const newQuery=()=>{
    if(loading)return;
    setMsgs([]);                                  // → welcome
    setSessionId(null);                           // новая сессия истории
    setQ(""); setActiveCite(null); setHoverCite(null);
    setTimeout(()=>inputRef.current?.focus(),0);
  };

  // Загрузка сессии из истории → в ленту (продолжение возможно, sessionId сохранён).
  const openSession=async(sid)=>{
    if(loading)return;
    setHistOpen(false);
    try{
      const d=await apiFetch(`/api/chat/sessions/${sid}`);
      const mapped=(d.messages||[]).map(m=>{
        if(m.role==="user") return {role:"user",text:m.content};
        const meta=m.meta||{};
        return {role:"ai",text:m.content,sources:meta.sources||[],
                mode:meta.mode||undefined,phase:meta.mode==="deep"?"done":undefined};
      });
      setMsgs(mapped); setSessionId(sid); setActiveCite(null); setHoverCite(null);
      setTimeout(()=>{const el=feedRef.current;if(el)el.scrollTop=el.scrollHeight;},60);
    }catch{}
  };
  // Открыть сохранённый отчёт (свой или расшаренный) в ленте.
  const openReport=async(rid)=>{
    if(loading)return;
    setHistOpen(false);
    try{
      const r=await apiFetch(`/api/reports/${rid}`);
      const p=r.payload||{};
      setMsgs([{role:"user",text:r.question},
               {role:"ai",text:r.body,sources:p.sources||[],charts:p.charts||[],
                mode:p.mode||"deep",phase:"done",
                report_id:r.report_id,report_owner:r.owner,owner_name:r.owner_name}]);
      setSessionId(r.session_id||null); setActiveCite(null); setHoverCite(null);
      setTimeout(()=>{const el=feedRef.current;if(el)el.scrollTop=el.scrollHeight;},60);
    }catch{}
  };
  // ⌘K / Ctrl+K — открыть/закрыть историю.
  useEffect(()=>{
    const onKey=(e)=>{ if((e.metaKey||e.ctrlKey)&&(e.key==="k"||e.key==="K")){e.preventDefault();setHistOpen(o=>!o);} };
    window.addEventListener("keydown",onKey);
    return ()=>window.removeEventListener("keydown",onKey);
  },[]);

  const isEmpty = !msgs.some(m=>m.role==="user");
  const lastMsg = msgs[msgs.length-1];
  const isClarify = lastMsg?.role==="clarify";
  const lastDeep = [...msgs].reverse().find(m=>m.mode==="deep");
  // Идёт активный deep-прогон (консоль + нижний бар, композер скрыт).
  const isRunning = loading && !!lastDeep && lastDeep.phase!=="done"
    && lastMsg?.role!=="clarify" && lastMsg?.role!=="pending";
  const showThreadHead = !isEmpty && !isRunning && !isClarify;
  const showComposer   = !isRunning && !isClarify;
  const fmtEl = (s)=>`${String(Math.floor(s/60)).padStart(2,"0")}:${String(s%60).padStart(2,"0")}`;
  return <div className={"fade-in chat-shell"+(isEmpty?" is-welcome":"")}>
    <style>{CP_CSS}</style>
    {showKbd && <KbdHelp onClose={()=>setShowKbd(false)}/>}
    {hoverCite && hoverCite.source && <CitationTooltip source={hoverCite.source} anchor={hoverCite.anchor}/>}
    <CommandPalette open={histOpen} onClose={()=>setHistOpen(false)}
                    onLoadSession={openSession} onLoadReport={openReport}/>
    <div className="chat-stream">
      <div className="chat-feed" ref={feedRef}>
        {showThreadHead &&
          <div className="al-thread-head" style={{display:"flex",gap:8,alignItems:"center"}}>
            <button className="al-newq" onClick={newQuery}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5M11 18l-6-6 6-6"/></svg>
              Новый запрос
            </button>
            <button className="hist-btn" onClick={()=>setHistOpen(true)} title="История (⌘K)">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7.5V12l3 1.8"/></svg>
              История <kbd>⌘K</kbd>
            </button>
          </div>}
        {isEmpty && <AiWelcome onPick={send} recent={recent} onOpenHistory={()=>setHistOpen(true)} onLoadSession={openSession}/>}
        {!isEmpty && msgs.map((m,i)=>{
          if(m.role==="clarify"){
            return <div key={i} className="chat-msg ai">
              <ClarifyCard msg={m} onSubmit={clarifySubmit} onSkip={clarifySkip}/>
            </div>;
          }
          if(m.role==="pending"){
            return <div key={i} className="chat-msg ai">
              <PendingDots label={m.label}/>
            </div>;
          }
          if(m.mode==="deep"){
            // Editorial document layout
            const userQ = (i>0 && msgs[i-1]?.role==="user") ? msgs[i-1].text : "Аудит-отчёт";
            const showPdfBtn = m.role==="ai" && m.text && m.text.length>200;
            const streaming  = m.role==="ai" && loading && i===msgs.length-1;
            // Режим живой консоли прогона: показываем DeepConsole. Отчёт (toolbar/
            // toc/rail/article) появляется когда phase==="done" — как в дизайне.
            const consoleMode = m.role==="ai" && loading && i===msgs.length-1 && m.phase!=="done";
            return <div key={i} className={`chat-msg ${m.role}`}>
              {consoleMode ? (
                <div className="chat-bubble chat-bubble-deep">
                  <DeepConsole m={m} loading={loading} elapsed={elapsed}/>
                </div>
              ) : (<>
                <div className="dr-doc-toolbar">
                  <span className="who">AuditLens · аналитический отчёт</span>
                  {m.report_owner&&me&&m.report_owner!==me.username&&
                    <span className="shr-owner">поделился: {m.owner_name||m.report_owner}</span>}
                  {m.report_id&&(!m.report_owner||(me&&m.report_owner===me.username))&&!streaming&&
                    <ShareButton reportId={m.report_id}/>}
                  {showPdfBtn &&
                    <PdfExportButton question={userQ} report={m.text}
                                     sources={m.sources||[]} verification={m.verification}
                                     claimCheck={m.claimCheck} streaming={streaming}
                                     charts={m.charts||[]} ranking={m.ranking}
                                     insights={m.insights} gaps={m.gaps}/>}
                  {m.matrix && <MatrixExportButton matrix={m.matrix} question={userQ} streaming={streaming}/>}
                </div>
                <div className="chat-bubble chat-bubble-deep">
                  {/* Сводка завершённого прогона (collapsed bar над отчётом). */}
                  {m.phase==="done" && m.plan && m.plan.length>0 && <ResearchSummary m={m}/>}
                  {/* Coverage — только как предупреждение о слабом покрытии. */}
                  {m.coverage?.warning && <CoverageBanner coverage={m.coverage}/>}
                  <div className="dr-doc">
                    {!hideToc && <DocTocSlot/>}
                    <article className="dr-doc-main" ref={(el)=>{ m._mainEl=el; }}>
                      {(m.claimCheck || m.verification) &&
                        <ClaimCheckRow claimCheck={m.claimCheck}
                                        verification={m.verification}
                                        sourcesCount={(m.sources||[]).length}/>}
                      {renderMD(m.text, m.sources, m.charts)}
                      {streaming && m.text && <span className="dr-type-caret"/>}
                      {/* Charts-wrap внизу: только графики БЕЗ [[CHART:N]] маркера. */}
                      {(()=>{
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
                      {showPdfBtn && !streaming &&
                        <div className="dr-doc-footer">
                          <PdfExportButton question={userQ} report={m.text}
                                           sources={m.sources||[]} verification={m.verification}
                                           claimCheck={m.claimCheck} streaming={false}
                                           charts={m.charts||[]} ranking={m.ranking}
                                           insights={m.insights} gaps={m.gaps}/>
                          {m.matrix && <MatrixExportButton matrix={m.matrix} question={userQ} streaming={false}/>}
                          <span className="dr-doc-footer-hint">
                            Готовый отчёт для аудита · нумерация страниц, источники, A4
                          </span>
                        </div>}
                      {!streaming&&m.text&&
                        <AiFbBar q={userQ} text={m.text} sessionId={sessionId} mode="deep" fbMap={aiFb}/>}
                    </article>
                    {!hideRail && <DocRailSlot>
                      <SourcesRail sources={m.sources||[]} failed={m.sourcesFailed||0} activeN={activeCite}
                                    onHover={setActiveCite}/>
                    </DocRailSlot>}
                  </div>
                </div>
              </>)}
            </div>;
          }
          // Quick mode — пользовательский пузырь
          if(m.role==="user"){
            return <div key={i} className="chat-msg user">
              <div className="who">Вы{me&&firstName(me.name)?" · "+firstName(me.name):""}</div>
              <div className="chat-bubble">{renderMD(m.text)}</div>
            </div>;
          }
          // Quick mode — ответ ИИ (редизайн: голый текст + tool-бокс + источники + апселл)
          const prevQ = (i>0 && msgs[i-1]?.role==="user") ? msgs[i-1].text : "";
          const thinking = !m.text && loading && i===msgs.length-1;
          return <div key={i} className="chat-msg ai quick-msg">
            <div className="who">AuditLens AI{m.engine==="hermes"?" · Hermes ✦":""}</div>
            {m.tools&&m.tools.length>0 &&
              <div className="quick-tools">
                {m.tools.map((t,ti)=>(
                  <span key={ti} className="quick-tool">
                    <span className="quick-tool-dot" style={ti===m.tools.length-1&&thinking?{background:"var(--accent)",animation:"pulse 1.4s ease-in-out infinite"}:null}/>
                    {TOOL_LABELS[t]||t}
                  </span>))}
              </div>}
            {thinking
              ? <PendingDots label="Думаю над ответом…"/>
              : <div className="quick-answer chat-bubble">{renderMD(m.text, m.sources)}</div>}
            {m.sources&&m.sources.length>0 &&
              <div className="quick-sources">
                {m.sources.map((s,si)=>(
                  <a key={si} href={s.url||"#"} target="_blank" rel="noopener noreferrer" className="quick-src">
                    <span className="quick-src-n">{s.n}</span>
                    <span className="quick-src-bank">{s.bank_name||domainOf(s.url)||"источник"}</span>
                    <span className="quick-src-dom">{domainOf(s.url)||"—"}</span>
                  </a>))}
              </div>}
            {m.text && !loading && prevQ &&
              <div className="quick-upsell">
                <span className="quick-upsell-t">Нужен документ для аудит-дела — с таблицей, рисками и проверкой чисел?</span>
                <button className="quick-upsell-btn" onClick={()=>runDeepFromQuick(prevQ)}>
                  Запустить Deep Research
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
                </button>
              </div>}
            {m.report_owner&&me&&m.report_owner!==me.username&&
              <div style={{marginTop:10}}><span className="shr-owner">поделился: {m.owner_name||m.report_owner}</span></div>}
            {m.report_id&&(!m.report_owner||(me&&m.report_owner===me.username))&&!(loading&&i===msgs.length-1)&&
              <div style={{marginTop:10}}><ShareButton reportId={m.report_id}/></div>}
            {m.text&&!(loading&&i===msgs.length-1)&&
              <AiFbBar q={prevQ} text={m.text} sessionId={sessionId} mode="quick" fbMap={aiFb}/>}
          </div>;
        })}
      </div>
      {isRunning &&
        <div className="al-runbar">
          <span className="al-runbar-dot"/>
          <span className="al-runbar-text">Идёт исследование — обычно 60–120с на реальных данных</span>
          <span className="al-runbar-el mono">{fmtEl(elapsed)}</span>
          <button className="al-runbar-btn" onClick={()=>{const el=feedRef.current;if(el){stickRef.current=true;el.scrollTo({top:el.scrollHeight,behavior:"smooth"});}}}>Показать отчёт →</button>
        </div>}
      {showComposer &&
      <div className="composer-dock">
        <div className="composer-inner">
          <div className="chat-input-wrap">
            {deepMode && <div className="composer-accent"/>}
            <textarea ref={inputRef} className="chat-textarea" rows={1}
              placeholder={deepMode?"Опишите задачу для глубокого исследования…":"Спросите об условиях, ставках, рисках или позиции Сбера…"}
              value={q} onChange={e=>setQ(e.target.value)}
              onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}}}/>
            <div className="composer-bar">
              <div className="seg">
                <button className={"seg-btn"+(!deepMode?" on":"")} onClick={()=>setDeepMode(false)} disabled={loading}>Быстрый</button>
                <button className={"seg-btn"+(deepMode?" on":"")} onClick={()=>setDeepMode(true)} disabled={loading} title="Deep Research: планировщик → мульти-агент → проверка фактов"><span className="seg-dot"/>Deep Research</button>
              </div>
              <span className="composer-hint">{deepMode?"планировщик · мульти-агент · проверка фактов":"агент Hermes · БД, новости, веб"}</span>
              <span className="composer-kbd">Enter ↵</span>
              <button className={"composer-send"+(deepMode?" deep":"")} disabled={!q.trim()||loading} onClick={()=>send()} aria-label="Отправить">
                {deepMode?"Запустить research":"Спросить"}
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
              </button>
            </div>
          </div>
          <div className="composer-note">Внутренний контур · данные не покидают периметр · Llama 3.3 70B</div>
        </div>
      </div>}
    </div>
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
      <table className="m-cards">
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
              <td data-label="" className="right mono tnum" style={{color:"var(--ink-3)",fontSize:12}}>{String(idx+1).padStart(2,"0")}</td>
              <td className="m-primary">
                <div style={{display:"flex",alignItems:"center",gap:12}}>
                  <BankAvatar slug={b.slug} name={b.name} isSber={b.is_sber}/>
                  <div>
                    <div style={{fontWeight:500}}>{b.name||b.slug}</div>
                    <div className="mono" style={{fontSize:11,color:"var(--ink-3)"}}>{b.slug}</div>
                  </div>
                </div>
              </td>
              <td data-label="СР. ОЦЕНКА" className="right">
                <span className="serif" style={{fontSize:22,fontWeight:400,color:grade>=4?"var(--pos)":grade>=3.5?"var(--warn)":"var(--neg)"}}>
                  {grade>0?grade.toFixed(2):"—"}
                </span>
              </td>
              <td data-label="РАСПРЕДЕЛЕНИЕ">
                {grade>0?<div style={{display:"flex",gap:2,height:6,maxWidth:160}}>
                  <div style={{flex:Math.round(grade*18),background:"var(--pos)",borderRadius:2}}/>
                  <div style={{flex:Math.round((5-grade)*15),background:"var(--accent)",borderRadius:2}}/>
                </div>:<span style={{color:"var(--ink-4)",fontSize:12}}>нет данных</span>}
              </td>
              <td data-label="ОТЗЫВОВ" className="right mono tnum">{fmtNum(b.total_reviews)}</td>
              <td data-label="РЕШЕНО" className="right mono tnum" style={{color:"var(--ink-2)"}}>{solved>0?`${solved}%`:"—"}</td>
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

function SourcesTech({data:extData}){
  const[data,setData]=useState(extData||{runs:[],captcha_pending:[],configured:[]});
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

  return <div>

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

// ─── ИСТОЧНИКИ — карта доверия для аудитора ──────────────────────────────────
// Была техническая консоль (прогоны сборщиков, капчи). Аудитору нужно понимать,
// откуда взялась каждая цифра и насколько источнику доверяет инструмент, и уметь
// предложить свой. Инженерная часть переехала под кат «Техническое состояние».

const SRC_STATUS_RU={pending:"на рассмотрении",approved:"одобрен",rejected:"отклонён"};

function SrcProposeForm({purpose,onDone}){
  const[url,setUrl]=useState("");
  const[title,setTitle]=useState("");
  const[reason,setReason]=useState("");
  const[check,setCheck]=useState(null);
  const[busy,setBusy]=useState(false);
  const[done,setDone]=useState(null);
  const[err,setErr]=useState(null);

  // проверка ДО отправки: занят ли домен, не предлагали ли раньше
  useEffect(()=>{
    if(!url.trim()){setCheck(null);return;}
    const t=setTimeout(()=>{
      apiFetch(`/api/sources/check?purpose=${purpose.id}&url=${encodeURIComponent(url.trim())}`)
        .then(setCheck).catch(()=>setCheck(null));
    },450);
    return()=>clearTimeout(t);
  },[url,purpose.id]);

  const submit=async()=>{
    setBusy(true);setErr(null);
    try{
      const r=await apiPost("/api/sources/propose",
        {purpose:purpose.id,url:url.trim(),title:title.trim(),reason:reason.trim()});
      setDone(r);setUrl("");setTitle("");setReason("");setCheck(null);
      if(onDone)onDone();
    }catch(e){ setErr(e.message||"не удалось отправить"); }
    setBusy(false);
  };

  if(done)return <div className="src-done">
    <div className="src-done-t">Заявка принята — {done.domain}</div>
    <p>Команда рассмотрит источник. При одобрении вы увидите его
      {purpose.id==="ai"?" в новых отчётах ИИ-аналитика"
        :purpose.id==="digest"?" в новых утренних выпусках"
        :purpose.id==="reviews"?" в анализе отзывов после следующего сбора"
        :" в витрине тарифов после следующего сбора"}.
      Статус заявки виден ниже на этой странице.</p>
    <button className="btn btn-ghost btn-sm" onClick={()=>setDone(null)}>Предложить ещё один</button>
  </div>;

  const bad=check&&!check.ok;
  return <div className="src-form">
    <div className="src-form-row">
      <label>
        <span>Адрес источника</span>
        <input className="input" value={url} placeholder="cbr.ru или t.me/канал"
               onChange={e=>setUrl(e.target.value)}/>
      </label>
      <label>
        <span>Название <i>необязательно</i></span>
        <input className="input" value={title} placeholder="Как его называть"
               onChange={e=>setTitle(e.target.value)}/>
      </label>
    </div>
    {check&&<div className={"src-check "+(bad?"bad":"ok")}>
      {bad?"⚠ ":"✓ "}{check.message}</div>}
    <label className="src-form-full">
      <span>Чем полезен аудиту</span>
      <textarea className="input" rows={3} value={reason}
        placeholder="Например: публикует предписания ЦБ раньше агрегаторов; нужен для проверки сроков реагирования"
        onChange={e=>setReason(e.target.value)}/>
    </label>
    {err&&<div className="src-check bad">⚠ {err}</div>}
    <div className="src-form-foot">
      <button className="btn btn-primary btn-sm" disabled={busy||!url.trim()||bad}
              onClick={submit}>{busy?"Отправляю…":"Отправить на рассмотрение"}</button>
      <span className="t-cap">Перед отправкой сверьтесь с требованиями выше</span>
    </div>
  </div>;
}

function SrcPurpose({p,openForm,setOpenForm,onProposed}){
  const[showAll,setShowAll]=useState(false);
  const list=showAll?p.sources:(p.sources||[]).slice(0,8);
  const open=openForm===p.id;
  return <section className="surface src-card">
    <div className="src-head">
      <div>
        <div className="eyebrow" style={{marginBottom:4}}>{p.title}</div>
        <p className="src-lead">{p.lead}</p>
      </div>
      <span className="src-count mono">{p.n} источн.</span>
    </div>

    <div className="src-what"><b>Где используется.</b> {p.what_for}</div>
    <div className="src-what"><b>Как учитывается доверие.</b> {p.trust_note}</div>

    {(p.sources||[]).length>0&&<div className="src-list">
      {list.map((s,i)=><a key={i} className="src-item" href={s.url}
                          target="_blank" rel="noopener noreferrer">
        <span className="src-dom">{s.domain}
          {s.weight!=null&&<i className={"src-w "+(s.weight>=0.9?"hi":s.weight>=0.7?"mid":"lo")}>
            {s.band} · {s.weight}</i>}</span>
        <span className="src-meta">{s.role}{s.kind?` · ${s.kind}`:""}
          {s.coverage?` · ${s.coverage}`:""}</span>
        <span className="src-ttl">{s.title!==s.domain?s.title:""}</span>
      </a>)}
      {(p.sources||[]).length>8&&<button className="btn btn-ghost btn-sm src-more"
        onClick={()=>setShowAll(v=>!v)}>
        {showAll?"Свернуть":`Показать все ${p.n}`}</button>}
    </div>}

    <details className="src-req" open={open}>
      <summary onClick={e=>{e.preventDefault();setOpenForm(open?null:p.id);}}>
        Требования к источнику для этого раздела
      </summary>
      <ul className="src-req-list">
        {(p.requirements||[]).map((r,i)=><li key={i}>{r}</li>)}
      </ul>
      {p.examples&&<div className="t-cap" style={{marginTop:8}}>Примеры подходящих: {p.examples}</div>}
      <SrcProposeForm purpose={p} onDone={onProposed}/>
    </details>
  </section>;
}

function SourcesPage(){
  const[cat,setCat]=useState(null);
  const[props_,setProps]=useState(null);
  const[openForm,setOpenForm]=useState(null);
  const[tech,setTech]=useState(null);
  const[techOpen,setTechOpen]=useState(false);
  const[err,setErr]=useState(null);
  const me=useContext(MeCtx);

  const loadProps=()=>apiFetch("/api/sources/proposals").then(setProps).catch(()=>{});
  useEffect(()=>{
    apiFetch("/api/sources/catalog").then(setCat).catch(e=>setErr(e.message));
    loadProps();
  },[]);
  useEffect(()=>{ if(techOpen&&!tech)apiFetch("/api/sources").then(setTech).catch(()=>{}); },[techOpen,tech]);

  const review=async(id,status)=>{
    const note=status==="rejected"?prompt("Причина отклонения (увидит автор):")||"":"";
    await apiPost(`/api/sources/proposals/${id}/review`,{status,note});
    loadProps();
  };

  if(err)return <ErrState msg={err}/>;
  if(!cat)return <LoadingPage/>;

  const mine=(props_&&props_.proposals)||[];
  const isAdmin=!!(props_&&props_.is_admin);

  return <div className="fade-in">
    <header style={{marginBottom:22}}>
      <div className="eyebrow" style={{marginBottom:6}}>§ Источники · доверие и покрытие</div>
      <h1 className="t-h" style={{marginBottom:6}}>Откуда инструмент берёт данные</h1>
      <p className="t-cap" style={{maxWidth:"74ch"}}>
        Для каждого раздела — свой набор источников и своя планка доверия. Здесь видно,
        кто участвует в выводах, и можно предложить источник, которого не хватает:
        требования к нему у каждого раздела отдельные.
      </p>
    </header>

    <div className="src-grid">
      {(cat.purposes||[]).map(p=>
        <SrcPurpose key={p.id} p={p} openForm={openForm} setOpenForm={setOpenForm}
                    onProposed={loadProps}/>)}
    </div>

    {mine.length>0&&<section className="surface src-card" style={{marginTop:18}}>
      <div className="eyebrow" style={{marginBottom:10}}>
        {isAdmin?"Заявки на источники — все":"Мои заявки"}</div>
      <table className="m-cards">
        <thead><tr><th>Источник</th><th>Раздел</th><th>Статус</th>
          {isAdmin&&<th>Автор</th>}<th></th></tr></thead>
        <tbody>{mine.map(p=>{
          const pur=(cat.purposes||[]).find(x=>x.id===p.purpose);
          return <tr key={p.proposal_id}>
            <td className="m-primary" data-label="Источник">
              <div style={{fontWeight:500}}>{p.domain}</div>
              {p.title&&<div className="t-cap" style={{fontSize:11}}>{p.title}</div>}
              {p.review_note&&<div className="t-cap" style={{fontSize:11,color:"var(--accent)"}}>
                {p.review_note}</div>}
            </td>
            <td data-label="Раздел">{pur?pur.title:p.purpose}</td>
            <td data-label="Статус">
              <span className={"badge "+(p.status==="approved"?"pos":p.status==="rejected"?"neg":"warn")}>
                {SRC_STATUS_RU[p.status]||p.status}</span>
              <div className="t-cap" style={{fontSize:10.5}}>{fmtDateMsk(p.created_at)}</div>
            </td>
            {isAdmin&&<td data-label="Автор" className="t-cap">{p.proposer_name||p.proposed_by}</td>}
            <td className="right">
              {isAdmin&&p.status==="pending"&&<div style={{display:"flex",gap:6,justifyContent:"flex-end"}}>
                <button className="btn btn-ghost btn-sm" onClick={()=>review(p.proposal_id,"approved")}>Одобрить</button>
                <button className="btn btn-ghost btn-sm" onClick={()=>review(p.proposal_id,"rejected")}>Отклонить</button>
              </div>}
            </td>
          </tr>;})}
        </tbody>
      </table>
    </section>}

    <details className="surface src-card src-tech" open={techOpen}
             onToggle={e=>setTechOpen(e.target.open)} style={{marginTop:18}}>
      <summary>Техническое состояние сборщиков</summary>
      <p className="t-cap" style={{margin:"6px 0 12px"}}>
        Для инженерной проверки: расписание, последние прогоны, ручной запуск.
        Данные обновляются автоматически — вмешательство обычно не требуется.</p>
      {!tech?<Skel h={80}/>:<SourcesTech data={tech}/>}
    </details>
  </div>;
}

function QualityPage(){
  const[data,setData]=useState(null);
  const[loading,setLoading]=useState(true);
  const[err,setErr]=useState(null);
  const[aifb,setAifb]=useState(null);          // оценки ответов ИИ (контур владельца)

  useEffect(()=>{
    apiFetch("/api/quality").then(d=>{setData(d);setLoading(false);}).catch(e=>{setErr(e.message);setLoading(false);});
    apiFetch("/api/quality/ai-feedback").then(setAifb).catch(()=>{});
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

    {/* Оценки ответов ИИ-аналитика: пульс 👍/👎 + последние дизлайки на разбор */}
    {aifb&&<div className="surface" style={{padding:"16px 20px",marginBottom:18}}>
      <div className="eyebrow-row" style={{marginBottom:(aifb.recent_dislikes||[]).length?10:0}}>
        <div className="eyebrow">Оценки ответов ИИ · 7 дней</div>
        <span className="mono tnum" style={{fontSize:12}}>
          <span style={{color:"var(--pos)"}}>▲ {aifb.likes_7d||0}</span>
          <span style={{color:"var(--neg)",marginLeft:12}}>▼ {aifb.dislikes_7d||0}</span>
        </span>
      </div>
      {(aifb.recent_dislikes||[]).length>0
        ? (aifb.recent_dislikes||[]).slice(0,8).map((d,i)=>(
            <div key={i} style={{display:"flex",gap:10,alignItems:"baseline",padding:"7px 2px",
                                 borderTop:"1px solid var(--hair)",fontSize:12.5}}>
              <span className="mono" style={{fontSize:10,color:"var(--neg)",flex:"none"}}>▼</span>
              <span style={{flex:1,minWidth:0,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}
                    title={d.question||""}>{d.question||"—"}</span>
              {(d.reasons||[]).length>0&&<span className="mono" style={{fontSize:9.5,color:"var(--ink-3)",flex:"none"}}>
                {(d.reasons||[]).join(" · ")}</span>}
              <span className="mono" style={{fontSize:9.5,color:"var(--ink-4)",flex:"none"}}>
                {(d.created_at||"").slice(5,16).replace("T"," ")}</span>
            </div>))
        : <div className="t-cap" style={{color:"var(--ink-4)"}}>Дизлайков нет — либо всё хорошо, либо оценок пока мало.</div>}
      {(aifb.recent_dislikes||[]).some(d=>d.comment)&&
        <div className="t-cap" style={{marginTop:8,color:"var(--ink-3)"}}>
          Последний комментарий: «{(aifb.recent_dislikes.find(d=>d.comment)||{}).comment}»</div>}
    </div>}


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


// ─── Loophole page (встраивает frontend модуля loophole) ─────────────────────
function LoopholePage(){
  return <section className="surface" style={{padding:0,overflow:"hidden"}}>
    <iframe src="/static/loophole/loophole.html"
            title="Лазейки и уязвимости"
            style={{width:"100%",height:"calc(100vh - 120px)",border:"none",display:"block"}}/>
  </section>;
}

// ─── SHELL ────────────────────────────────────────────────────────────────────
// ─── «Пульс» — дашборд владельца: аудитория + продукт + техника в одном ───────
const AD_CSS=`
.pu-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0 22px;}
.pu-tile{background:var(--surface);border:1px solid var(--hair);border-radius:var(--r-lg);padding:14px 16px 12px;}
.pu-tile .l{font-family:'JetBrains Mono',monospace;font-size:9.5px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--ink-4);margin-bottom:7px;display:flex;align-items:center;gap:6px;}
.pu-tile .v{font-family:'Source Serif 4',Georgia,serif;font-size:27px;line-height:1;}
.pu-tile .s{font-size:10.5px;color:var(--ink-4);margin-top:5px;font-family:'JetBrains Mono',monospace;}
.pu-tile.neg .v{color:var(--neg);}
.pu-live{width:6px;height:6px;border-radius:50%;background:var(--pos);animation:pulse 1.8s ease infinite;}
.pu-sec{margin-top:24px;}
.pu-grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;}
@media(max-width:1000px){.pu-grid2{grid-template-columns:1fr;}}
.pu-card{background:var(--surface);border:1px solid var(--hair);border-radius:var(--r-lg);padding:16px 18px;}
.pu-card .h{font-family:'JetBrains Mono',monospace;font-size:10px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--ink-3);margin-bottom:12px;display:flex;justify-content:space-between;gap:8px;}
.pu-bar-row{display:flex;align-items:center;gap:10px;padding:4px 0;font-size:12.5px;}
.pu-bar-row .lb{width:110px;flex:none;color:var(--ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.pu-bar-row .tr{flex:1;height:16px;background:var(--paper-2);border-radius:4px;overflow:hidden;}
.pu-bar-row .fl{height:100%;background:color-mix(in oklab,var(--accent),transparent 35%);border-radius:4px;
  transition:width .5s ease;}
.pu-bar-row .vv{width:100px;flex:none;text-align:right;font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-3);}
.pu-kv{display:flex;justify-content:space-between;align-items:baseline;padding:7px 2px;border-top:1px solid var(--hair);font-size:12.5px;}
.pu-kv:first-of-type{border-top:0;}
.pu-kv b{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;}
.pu-heat{display:grid;grid-template-columns:34px repeat(24,1fr);gap:2px;margin-top:12px;}
.pu-heat .hl{font-family:'JetBrains Mono',monospace;font-size:8.5px;color:var(--ink-4);align-self:center;}
.pu-heat .c{aspect-ratio:1;border-radius:2.5px;background:var(--paper-2);min-width:0;}
.pu-tbl{width:100%;font-size:11.5px;border-collapse:collapse;}
.pu-tbl th{font-family:'JetBrains Mono',monospace;font-size:9px;letter-spacing:.05em;text-transform:uppercase;
  color:var(--ink-4);text-align:right;padding:4px 6px;border-bottom:1px solid var(--hair);font-weight:500;}
.pu-tbl th:first-child{text-align:left;}
.pu-tbl td{padding:5px 6px;border-bottom:1px solid var(--hair);font-family:'JetBrains Mono',monospace;
  font-size:10.5px;text-align:right;color:var(--ink-2);}
.pu-tbl td:first-child{text-align:left;color:var(--ink);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.pu-tbl tr:last-child td{border-bottom:0;}
.pu-err{display:flex;gap:9px;align-items:baseline;padding:6px 2px;border-top:1px solid var(--hair);font-size:11.5px;}
.pu-err:first-of-type{border-top:0;}
.pu-err .t{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--ink-4);flex:none;}
.pu-err .k{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--neg);flex:none;text-transform:uppercase;}
.pu-err .m{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2);}
.pu-feed-row{display:flex;gap:9px;align-items:center;padding:5px 2px;border-top:1px solid var(--hair);font-size:11.5px;}
.pu-feed-row:first-of-type{border-top:0;}
.pu-feed-row .t{font-family:'JetBrains Mono',monospace;font-size:9.5px;color:var(--ink-4);flex:none;width:34px;}
.pu-feed-row .a{width:20px;height:20px;border-radius:50%;background:var(--accent-soft);color:var(--accent);
  display:grid;place-items:center;font-size:8.5px;font-weight:600;flex:none;}
.pu-feed-row .w{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--ink-2);}
.pu-chip{font-family:'JetBrains Mono',monospace;font-size:9.5px;padding:3px 9px;border-radius:999px;border:1px solid var(--hair);color:var(--ink-3);}
.pu-chip.ok{color:var(--pos);border-color:color-mix(in oklab,var(--pos),transparent 70%);}
.pu-chip.bad{color:var(--neg);border-color:color-mix(in oklab,var(--neg),transparent 70%);}
.pu-note{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--ink-4);margin-top:10px;line-height:1.6;}
.pu-note .acc{color:var(--accent);}
.pu-u{display:inline-flex;align-items:center;gap:8px;font-family:'Geist','Inter',sans-serif;font-size:12.5px;color:var(--ink);}
.pu-u .a{width:22px;height:22px;border-radius:50%;background:var(--accent-soft);color:var(--accent);
  display:grid;place-items:center;font-size:8.5px;font-weight:600;flex:none;}
.pu-crown{font-family:'JetBrains Mono',monospace;font-size:8.5px;color:var(--accent);white-space:nowrap;
  border:1px solid color-mix(in oklab,var(--accent),transparent 70%);background:var(--accent-soft);
  border-radius:999px;padding:2px 7px;}
.pu-tm{display:inline-flex;align-items:center;gap:7px;justify-content:flex-end;}
.pu-tm .bar{height:5px;border-radius:3px;background:color-mix(in oklab,var(--accent),transparent 40%);display:inline-block;}
.pu-team td:first-child{max-width:260px;}
`;
const AD_PAGE_RU={overview:"Обзор",foryou:"Для вас",market:"Рынок",sber:"Сбер/Рынок",reviews:"Отзывы",
  ai:"ИИ-аналитик",knowledge:"База знаний",loophole:"Лазейки",banks:"Банки",sources:"Источники",
  quality:"Качество",profile:"Профиль",pulse:"Пульс"};
const adFmtS=(s)=>{ s=Math.round(s||0); if(s<60)return s+"с";
  if(s<3600)return Math.round(s/60)+"м"; return (s/3600).toFixed(1)+"ч"; };

// area-график: users (заливка) + views (тонкая линия), даты по оси
function AdArea({data,h=130}){
  const w=640, vals=(data||[]);
  if(vals.length<2) return <div style={{color:"var(--ink-4)",fontSize:12,padding:"20px 0"}}>Данные накапливаются — график появится после пары дней жизни телеметрии.</div>;
  const maxU=Math.max(...vals.map(v=>v.users||0),1);
  const maxV=Math.max(...vals.map(v=>v.views||0),1);
  const px=(i)=>i/(vals.length-1)*(w-8)+4;
  const pyU=(v)=>h-16-((v||0)/maxU)*(h-34);
  const pyV=(v)=>h-16-((v||0)/maxV)*(h-34);
  const dU=vals.map((v,i)=>(i?"L":"M")+px(i).toFixed(1)+","+pyU(v.users).toFixed(1)).join("");
  const dV=vals.map((v,i)=>(i?"L":"M")+px(i).toFixed(1)+","+pyV(v.views).toFixed(1)).join("");
  const last=vals[vals.length-1];
  const dd=(s)=>(s||"").slice(8,10)+"."+(s||"").slice(5,7);
  return <svg width="100%" viewBox={"0 0 "+w+" "+h} style={{display:"block"}}>
    <path d={dU+"L"+(w-4)+","+(h-14)+"L4,"+(h-14)+"Z"} fill="var(--accent-soft)" opacity=".6"/>
    <path d={dV} fill="none" stroke="var(--ink-4)" strokeWidth="1" opacity=".55" strokeDasharray="3 3"/>
    <path d={dU} fill="none" stroke="var(--accent)" strokeWidth="1.6" strokeLinejoin="round"/>
    <circle cx={px(vals.length-1)} cy={pyU(last.users)} r="2.6" fill="var(--accent)"/>
    <text x="4" y={h-3} fontSize="8.5" fill="var(--ink-4)" fontFamily="JetBrains Mono">{dd(vals[0].d)}</text>
    <text x={w-4} y={h-3} fontSize="8.5" fill="var(--ink-4)" fontFamily="JetBrains Mono" textAnchor="end">{dd(last.d)}</text>
    <text x={w-4} y="10" fontSize="8.5" fill="var(--ink-4)" fontFamily="JetBrains Mono" textAnchor="end">макс {maxU} польз. · {maxV} просм.</text>
  </svg>;
}

function AdHeat({cells}){
  const map={}; let max=1;
  (cells||[]).forEach(c=>{ map[c.dow+"-"+c.hour]=c.n; if(c.n>max)max=c.n; });
  const days=["Пн","Вт","Ср","Чт","Пт","Сб","Вс"];
  const out=[];
  days.forEach((dl,di)=>{
    out.push(<span key={"l"+di} className="hl">{dl}</span>);
    for(let hh=0;hh<24;hh++){
      const n=map[(di+1)+"-"+hh]||0;
      out.push(<span key={di+"-"+hh} className="c" title={dl+" "+hh+":00 · "+n+" событий"}
        style={n?{background:"color-mix(in oklab,var(--accent),var(--paper-2) "+Math.round(88-(n/max)*78)+"%)"}:null}/>);
    }
  });
  return <div className="pu-heat">{out}</div>;
}

// донат-сегментация аудитории (SVG, без библиотек)
function AdDonut({parts,center,sub}){
  const total=(parts||[]).reduce((s,p)=>s+(p.value||0),0)||1;
  const R=40,C=2*Math.PI*R; let cum=0;
  return <div style={{display:"flex",gap:22,alignItems:"center",flexWrap:"wrap"}}>
    <svg width="116" height="116" viewBox="0 0 116 116" style={{flex:"none"}}>
      <circle cx="58" cy="58" r={R} fill="none" stroke="var(--paper-2)" strokeWidth="15"/>
      {(parts||[]).filter(p=>p.value>0).map((p,i)=>{
        const frac=p.value/total;
        const el=<circle key={i} cx="58" cy="58" r={R} fill="none" stroke={p.color} strokeWidth="15"
          strokeDasharray={Math.max(frac*C-1.6,.6)+" "+(C-Math.max(frac*C-1.6,.6))}
          transform={"rotate("+(cum*360-90)+" 58 58)"}/>;
        cum+=frac; return el;})}
      <text x="58" y="57" textAnchor="middle" fontSize="21" fontWeight="600" fill="var(--ink)"
        fontFamily="'Source Serif 4',Georgia,serif">{center}</text>
      <text x="58" y="73" textAnchor="middle" fontSize="7.5" fill="var(--ink-4)"
        fontFamily="'JetBrains Mono',monospace">{sub}</text>
    </svg>
    <div style={{display:"flex",flexDirection:"column",gap:7}}>
      {(parts||[]).map((p,i)=><div key={i} style={{display:"flex",alignItems:"center",gap:9,fontSize:12.5}}>
        <span style={{width:9,height:9,borderRadius:3,background:p.color,flex:"none"}}/>
        <span style={{color:"var(--ink-2)"}}>{p.label}</span>
        <b className="tnum" style={{fontFamily:"'JetBrains Mono',monospace",fontSize:11.5}}>{p.value||0}</b>
      </div>)}
    </div>
  </div>;
}

// парные колонки по дням: ИИ-запросы (accent) + отчёты (ink); ось — дни из dau
function AdCols({axis,a,b,h=118}){
  const w=620;
  const am={};(a||[]).forEach(x=>am[x.d]=+x.n||0);
  const bm={};(b||[]).forEach(x=>bm[x.d]=+x.n||0);
  const days=(axis||[]).map(x=>x.d);
  if(!days.length) return <div style={{color:"var(--ink-4)",fontSize:12}}>Накапливается.</div>;
  const max=Math.max(...days.map(d=>Math.max(am[d]||0,bm[d]||0)),1);
  const slot=(w-32)/days.length, bw=Math.max(3,Math.min(13,slot/2-2));
  const dd=(s)=>(s||"").slice(8,10)+"."+(s||"").slice(5,7);
  return <svg width="100%" viewBox={"0 0 "+w+" "+h} style={{display:"block"}}>
    {days.map((d,i)=>{
      const x=16+i*slot+(slot-bw*2-2)/2;
      const ha=(am[d]||0)/max*(h-32), hb=(bm[d]||0)/max*(h-32);
      return <g key={d}>
        {am[d]>0&&<rect x={x} y={h-16-Math.max(ha,2)} width={bw} height={Math.max(ha,2)} rx="2" fill="var(--accent)" opacity=".88"/>}
        {bm[d]>0&&<rect x={x+bw+2} y={h-16-Math.max(hb,2)} width={bw} height={Math.max(hb,2)} rx="2" fill="var(--ink-3)" opacity=".65"/>}
      </g>;})}
    <text x="16" y={h-3} fontSize="8.5" fill="var(--ink-4)" fontFamily="JetBrains Mono">{dd(days[0])}</text>
    <text x={w-16} y={h-3} fontSize="8.5" fill="var(--ink-4)" fontFamily="JetBrains Mono" textAnchor="end">{dd(days[days.length-1])}</text>
    <text x={w-16} y="10" fontSize="8.5" fill="var(--ink-4)" fontFamily="JetBrains Mono" textAnchor="end">макс {max}/день</text>
  </svg>;
}

function PulsePage(){
  const me=useMe();
  const[days,setDays]=useState(14);
  const[m,setM]=useState(null);
  const[err,setErr]=useState(false);
  const[ts,setTs]=useState(null);
  const load=useCallback(()=>{
    apiFetch("/api/admin/pulse?days="+days)
      .then(d=>{setM(d);setErr(false);setTs(new Date());})
      .catch(()=>setErr(true));
  },[days]);
  useEffect(()=>{ load(); const t=setInterval(load,60000); return ()=>clearInterval(t); },[load]);

  if(me&&!me.is_admin) return <div className="fade-in"><ErrState msg="Раздел доступен только владельцу инструмента."/></div>;
  if(err) return <div className="fade-in"><ErrState msg="Не удалось загрузить метрики."/></div>;
  if(!m) return <LoadingPage/>;

  const t=m.today||{}, f=m.features||{}, sg=m.segments||{};
  const maxPage=Math.max(...(m.pages||[]).map(x=>x.views||0),1);
  const nErr=(m.errors_recent||[]).length;
  const tokSum=(m.tokens||[]).reduce((a,x)=>a+(+x.tin||0)+(+x.tout||0),0);
  const team=m.users_table||[];
  const maxT=Math.max(...team.map(x=>+x.time_s||0),1);
  return <div className="fade-in">
    <style>{AD_CSS}</style>
    <header style={{marginBottom:4}}>
      <div className="eyebrow-row">
        <div className="eyebrow">Пульс инструмента · доступ: владелец · <span style={{color:"var(--accent)"}}>автообновление 60с</span></div>
        <div style={{display:"flex",alignItems:"center",gap:10}}>
          {ts&&<span className="bf-stamp">{ts.toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit",second:"2-digit"})}</span>}
          <div className="seg">{[7,14,30].map(d=><button key={d} className={"seg-btn"+(days===d?" on":"")}
            onClick={()=>setDays(d)}>{d} дн</button>)}</div>
        </div>
      </div>
      <h1 className="t-display" style={{maxWidth:"26ch",marginBottom:6}}>Как <em style={{fontStyle:"italic",color:"var(--accent)"}}>живёт</em> AuditLens</h1>
      <p className="lede">Маркетинг и техника в одном экране: аудитория, вовлечённость, фичи, ошибки, латентность.</p>
    </header>

    {/* ① сегодня */}
    <div className="pu-tiles">
      <div className="pu-tile"><div className="l"><span className="pu-live"/>Онлайн сейчас</div>
        <div className="v tnum">{t.online||0}</div><div className="s">за 15 минут</div></div>
      <div className="pu-tile"><div className="l">Активных сегодня</div>
        <div className="v tnum">{t.active||0}</div><div className="s">из {t.users_total||0} всего</div></div>
      <div className="pu-tile"><div className="l">Просмотров сегодня</div>
        <div className="v tnum">{t.views||0}</div><div className="s">страниц</div></div>
      <div className="pu-tile"><div className="l">ИИ-запросов сегодня</div>
        <div className="v tnum">{t.ai||0}</div><div className="s">quick + deep</div></div>
      <div className={"pu-tile"+(t.errors>0?" neg":"")}><div className="l">Ошибок сегодня</div>
        <div className="v tnum">{t.errors||0}</div><div className="s">{t.errors>0?"см. раздел техники ↓":"чисто ✓"}</div></div>
    </div>

    {/* ② аудитория */}
    <div className="pu-card">
      <div className="h"><span>Аудитория · уникальные в день</span>
        <span>— пользователи · ‥ просмотры · новых за период: {(m.new_users||[]).reduce((a,x)=>a+(+x.n||0),0)}</span></div>
      <AdArea data={m.dau}/>
    </div>

    {/* ②b сегменты аудитории + генерация по дням */}
    <div className="pu-grid2 pu-sec">
      <div className="pu-card">
        <div className="h"><span>Кто наша аудитория · {m.days} дн</span></div>
        <AdDonut center={String(sg.active||0)} sub="активных"
          parts={[
            {label:"исследователи · ИИ и отчёты",value:sg.researchers||0,color:"var(--accent)"},
            {label:"читатели новостей",value:sg.readers||0,color:"var(--warn)"},
            {label:"разовые визиты",value:sg.casual||0,color:"var(--ink-4)"},
            {label:"спящие за период",value:sg.sleepers||0,color:"var(--hair-2)"},
          ]}/>
        <div className="pu-note">
          {sg.readers>0
            ? <><span className="acc">✦</span> {sg.readers} заход{sg.readers===1?"ит":"ят"} только почитать новости («Обзор»/«Для вас») — точка роста для ИИ-аналитика</>
            : "читатели ≥60% просмотров в «Обзоре»/«Для вас» без единого ИИ-запроса"}
        </div>
      </div>
      <div className="pu-card">
        <div className="h"><span>Генерация · по дням</span>
          <span><span style={{color:"var(--accent)"}}>■</span> ИИ-запросы · <span style={{color:"var(--ink-3)"}}>■</span> отчёты</span></div>
        <AdCols axis={m.dau} a={m.ai_per_day} b={m.reports_per_day}/>
        <div className="pu-note">за период: {f.ai_total||0} запросов · {f.reports||0} отчётов создано · {f.report_opens||0} открытий сохранённых · {f.shares||0} шерингов</div>
      </div>
    </div>

    {/* ②c команда пофамильно */}
    {team.length>0&&<div className="pu-card pu-sec">
      <div className="h"><span>Команда · пофамильно · {m.days} дн</span>
        <span>скор: время + просмотры + ИИ×15 + отчёты×30 + оценки×5</span></div>
      <table className="pu-tbl pu-team">
        <thead><tr><th>пользователь</th><th>время</th><th>дней</th><th>просм.</th><th>ИИ</th><th>отчёты</th><th>оценки</th><th>был(а)</th></tr></thead>
        <tbody>
          {team.map((u,i)=><tr key={u.username}>
            <td><span className="pu-u"><span className="a">{initials(u.name)}</span>{u.name}
              {i===0&&(+u.views>0)&&<span className="pu-crown">✦ самый активный</span>}</span></td>
            <td><span className="pu-tm"><span className="bar" style={{width:Math.max(4,(+u.time_s||0)/maxT*54)+"px"}}/>{adFmtS(u.time_s)}</span></td>
            <td>{u.days_active}</td><td>{u.views}</td><td>{u.ai}</td><td>{u.reports}</td><td>{u.ratings}</td>
            <td>{u.last_seen||"—"}</td>
          </tr>)}
        </tbody>
      </table>
    </div>}

    {/* ③ вовлечённость + фичи */}
    <div className="pu-grid2">
      <div className="pu-card">
        <div className="h"><span>Страницы · {m.days} дн</span><span>просмотры · время</span></div>
        {(m.pages||[]).length===0&&<div style={{color:"var(--ink-4)",fontSize:12}}>Пока пусто.</div>}
        {(m.pages||[]).map(pg=><div key={pg.page} className="pu-bar-row">
          <span className="lb">{AD_PAGE_RU[pg.page]||pg.page}</span>
          <span className="tr"><span className="fl" style={{width:Math.max(3,(pg.views/maxPage)*100)+"%"}}/></span>
          <span className="vv tnum">{pg.views} · {adFmtS(pg.total_s)}</span>
        </div>)}
      </div>
      <div className="pu-card">
        <div className="h"><span>Функции · {m.days} дн</span></div>
        <div className="pu-kv"><span>ИИ-запросы</span><b className="tnum">{f.ai_total||0}</b></div>
        <div className="pu-kv"><span>Аудит-отчёты создано</span><b className="tnum">{f.reports||0}</b></div>
        <div className="pu-kv"><span>Шеринги отчётов</span><b className="tnum">{f.shares||0}</b></div>
        <div className="pu-kv"><span>Оценки контента 👍/👎</span>
          <b className="tnum"><span style={{color:"var(--pos)"}}>{f.fb_likes||0}</span> / <span style={{color:"var(--neg)"}}>{f.fb_dislikes||0}</span></b></div>
        <div className="pu-kv"><span>Оценки ответов ИИ 👍/👎</span>
          <b className="tnum"><span style={{color:"var(--pos)"}}>{f.ai_likes||0}</span> / <span style={{color:"var(--neg)"}}>{f.ai_dislikes||0}</span></b></div>
        <div className="pu-kv"><span>Профилей заполнено</span><b className="tnum">{f.profiles||0} из {t.users_total||0}</b></div>
      </div>
    </div>

    {/* ④ тепловая карта */}
    <div className="pu-card pu-sec">
      <div className="h"><span>Когда пользуются · час × день недели (МСК)</span><span>{m.days} дн</span></div>
      <AdHeat cells={m.heatmap}/>
    </div>

    {/* ⑤ техника */}
    <div className="pu-grid2 pu-sec">
      <div className="pu-card">
        <div className="h"><span>Латентность API · 7 дн</span><span>мс</span></div>
        {(m.latency||[]).length===0?<div style={{color:"var(--ink-4)",fontSize:12}}>Накапливается.</div>
          :<table className="pu-tbl"><thead><tr><th>endpoint</th><th>n</th><th>p50</th><th>p95</th><th>5xx</th></tr></thead>
            <tbody>{(m.latency||[]).map((r,i)=><tr key={i}>
              <td title={r.path}>{(r.path||"").replace("/api/","")}</td>
              <td>{r.n}</td><td>{r.p50}</td>
              <td style={r.p95>3000?{color:"var(--warn)"}:null}>{r.p95}</td>
              <td style={r.errs>0?{color:"var(--neg)"}:null}>{r.errs||0}</td>
            </tr>)}</tbody></table>}
      </div>
      <div className="pu-card">
        <div className="h"><span>Ошибки · последние</span>
          <span className={"pu-chip "+(nErr?"bad":"ok")}>{nErr?nErr+" в журнале":"чисто ✓"}</span></div>
        {nErr===0?<div style={{color:"var(--ink-4)",fontSize:12}}>Ни одной ошибки в журнале — так держать.</div>
          :(m.errors_recent||[]).slice(0,10).map((e,i)=><div key={i} className="pu-err">
            <span className="t">{e.ts}</span><span className="k">{e.kind==="client_error"?"js":"api"}</span>
            <span className="m" title={e.msg||""}>{e.page||"—"}{e.status?" · "+e.status:""}{e.msg?" · "+e.msg:""}</span>
          </div>)}
      </div>
    </div>

    <div className="pu-grid2 pu-sec">
      <div className="pu-card">
        <div className="h"><span>Дайджест · последний выпуск</span>
          <span>LLM-токены за период: {tokSum.toLocaleString("ru")}</span></div>
        <div style={{display:"flex",gap:7,flexWrap:"wrap"}}>
          {(m.digest||[]).map(s=><span key={s.section}
            className={"pu-chip "+(s.status==="ok"?"ok":s.status==="failed"?"bad":"")}
            title={(s.error||"")+(s.gen_ms?" · "+s.gen_ms+"мс":"")}>
            {s.section} · {s.status}{s.at?" · "+s.at:""}</span>)}
        </div>
      </div>
      <div className="pu-card">
        <div className="h"><span>Живая лента</span><span>последние события</span></div>
        {(m.feed||[]).map((e,i)=><div key={i} className="pu-feed-row">
          <span className="t">{e.ts}</span>
          <span className="a">{initials(e.username||"?")}</span>
          <span className="w">{e.kind==="page_view"?"открыл "+(AD_PAGE_RU[e.page]||e.page)
            :e.kind==="page_leave"?((AD_PAGE_RU[e.page]||e.page)+" · "+adFmtS((e.dur_ms||0)/1000))
            :e.kind==="client_error"?"⚠ JS-ошибка на "+(AD_PAGE_RU[e.page]||e.page)
            :"⚠ API "+(e.page||"")+(e.status?" · "+e.status:"")}</span>
        </div>)}
      </div>
    </div>

    <div style={{marginTop:26,paddingTop:12,borderTop:"1px solid var(--hair)",
                 fontFamily:"'JetBrains Mono',monospace",fontSize:10.5,color:"var(--ink-4)"}}>
      телеметрия: page_view/page_leave с фронта · api_request/api_error из middleware · доступ по env ADMIN_USERS
    </div>
  </div>;
}

const NAV=[
  {id:"overview",label:"Обзор",       icon:Ic.grid,   group:"Анализ"},
  {id:"market",  label:"Рынок · позиция",icon:Ic.market, group:"Анализ"},
  {id:"reviews", label:"Отзывы",      icon:Ic.msg,    group:"Анализ"},
  {id:"ai",      label:"ИИ-аналитик", icon:Ic.spark,  group:"Анализ"},
  {id:"knowledge",label:"База знаний",icon:Ic.src,    group:"Анализ"},
  {id:"loophole",label:"Лазейки",     icon:Ic.shield, group:"Анализ"},
  {id:"banks",   label:"Банки",       icon:Ic.bank,   group:"Данные"},
  {id:"sources", label:"Источники",   icon:Ic.src,    group:"Данные"},
  {id:"quality", label:"Качество",    icon:Ic.shield, group:"Данные"},
];
const PAGES_FN={overview:OverviewPage,foryou:ForYouPage,market:MarketPage,sber:SberPage,reviews:ReviewsPage,ai:AIPage,knowledge:KnowledgePage,loophole:LoopholePage,banks:BanksPage,sources:SourcesPage,quality:QualityPage,profile:ProfilePage,pulse:PulsePage};
const PAGE_LABELS={overview:["01","Обзор"],foryou:["01","Для вас"],market:["02","Рынок · позиция"],sber:["02","Рынок · позиция"],reviews:["04","Отзывы"],ai:["05","ИИ-аналитик"],knowledge:["06","База знаний"],loophole:["07","Лазейки"],banks:["08","Банки"],sources:["09","Источники"],quality:["10","Качество"],profile:["·","Профиль"],pulse:["·","Пульс"]};

// ─── Профиль и персонализация (Фазы 2+4, AI-forward редизайн) ─────────────────
const PROFILE_CSS=`
.pf-wrap{max-width:720px;}
.pf-hero{display:flex;align-items:center;gap:18px;margin-bottom:24px;}
.pf-avatar{width:60px;height:60px;flex:none;border-radius:16px;display:grid;place-items:center;
  font-size:22px;font-weight:600;color:var(--accent);background:var(--accent-soft);
  border:1px solid color-mix(in oklab,var(--accent),transparent 80%);letter-spacing:-.01em;}
.pf-hero h1{font-family:'Instrument Serif',Georgia,serif;font-weight:400;font-size:32px;line-height:1.05;letter-spacing:-.01em;color:var(--ink);margin:3px 0 4px;}
.pf-sub{font-size:12px;color:var(--ink-3);}
.pf-card{padding:22px 24px;margin-bottom:16px;position:relative;}
.pf-card-h{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;}
.pf-ai-badge{display:inline-flex;align-items:center;gap:6px;font-family:'JetBrains Mono',monospace;font-size:10px;
  letter-spacing:.05em;text-transform:uppercase;color:var(--accent);}
.pf-ai-badge .sp{animation:pf-sparkle 3s ease-in-out infinite;}
@keyframes pf-sparkle{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.55;transform:scale(.86)}}
.pf-ai{border:1px solid color-mix(in oklab,var(--accent),transparent 84%);
  background:linear-gradient(180deg,color-mix(in oklab,var(--accent-soft),transparent 62%),transparent 60%);}
.pf-mini{font-size:11.5px;color:var(--ink-3);border:1px solid var(--hair);border-radius:7px;padding:5px 11px;
  transition:border-color .14s,color .14s,transform .1s;white-space:nowrap;}
.pf-mini:hover:not(:disabled){border-color:var(--accent);color:var(--accent);}
.pf-mini:active:not(:disabled){transform:scale(.96);}
.pf-mini:disabled{opacity:.55;cursor:default;}
.pf-hint{font-size:12.5px;line-height:1.5;color:var(--ink-3);margin-bottom:12px;max-width:64ch;text-wrap:pretty;}
.pf-ta{width:100%;min-height:80px;resize:vertical;border:1px solid var(--hair);border-radius:10px;background:var(--surface);
  color:var(--ink);font-size:13.5px;line-height:1.55;padding:12px 14px;font-family:'Geist','Inter',sans-serif;transition:border-color .14s;}
.pf-ta:focus{outline:none;border-color:var(--accent);}
.pf-ta::placeholder{color:var(--ink-4);}
.pf-note{font-family:'Source Serif 4',serif;font-size:16.5px;line-height:1.56;color:var(--ink);text-wrap:pretty;}
.pf-note-empty{font-size:13.5px;line-height:1.55;color:var(--ink-3);text-wrap:pretty;max-width:62ch;}
.pf-note-gen{display:flex;align-items:center;gap:10px;color:var(--ink-3);font-size:13.5px;}
.pf-note-gen .dots{display:inline-flex;gap:3px;}
.pf-note-gen .dots i{width:5px;height:5px;border-radius:50%;background:var(--accent);animation:pf-bounce 1.1s infinite;}
.pf-note-gen .dots i:nth-child(2){animation-delay:.15s;} .pf-note-gen .dots i:nth-child(3){animation-delay:.3s;}
@keyframes pf-bounce{0%,100%{opacity:.3;transform:translateY(0)}50%{opacity:1;transform:translateY(-3px)}}
.pf-src{font-size:10.5px;color:var(--ink-4);margin-top:12px;font-family:'JetBrains Mono',monospace;display:flex;align-items:center;gap:6px;}
.pf-src .live{width:5px;height:5px;border-radius:50%;background:var(--pos);}
.pf-sub-h{font-size:11px;font-family:'JetBrains Mono',monospace;letter-spacing:.05em;text-transform:uppercase;color:var(--ink-4);margin:16px 0 9px;}
.pf-sub-h:first-child{margin-top:2px;}
.pf-topics{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
.pf-topic{display:inline-flex;align-items:center;gap:5px;font-size:12.5px;padding:5px 7px 5px 12px;border-radius:9px;
  background:var(--paper-2);border:1px solid var(--hair);color:var(--ink-2);transition:border-color .14s,background .14s,color .14s;}
.pf-topic.anchor{background:var(--accent-soft);border-color:color-mix(in oklab,var(--accent),transparent 80%);color:var(--accent);font-weight:500;}
.pf-topic .lock{font-size:9px;opacity:.7;}
.pf-tacts{display:inline-flex;gap:0;max-width:0;overflow:hidden;transition:max-width .18s ease;}
.pf-topic:hover .pf-tacts{max-width:28px;}
.pf-tacts button{width:20px;height:20px;border-radius:5px;display:grid;place-items:center;color:currentColor;opacity:.6;transition:opacity .12s,background .12s;}
.pf-tacts button:hover{opacity:1;background:color-mix(in oklab,currentColor,transparent 88%);}
.pf-rec{display:flex;flex-wrap:wrap;gap:8px;align-items:center;}
.pf-rec-chip{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;padding:5px 12px;border-radius:9px;
  border:1px dashed color-mix(in oklab,var(--accent),transparent 62%);background:none;color:var(--accent);
  transition:background .14s,border-style .14s,transform .1s;animation:pf-pop .3s ease-out;}
.pf-rec-chip:hover{background:var(--accent-soft);border-style:solid;}
.pf-rec-chip:active{transform:scale(.96);}
@keyframes pf-pop{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:scale(1)}}
.pf-add input{border:1px dashed var(--hair-2);border-radius:9px;background:none;color:var(--ink);font-size:12.5px;
  padding:5px 12px;width:130px;transition:border-color .16s,border-style .16s,width .2s;font-family:inherit;}
.pf-add input:focus{outline:none;border-color:var(--accent);border-style:solid;width:210px;}
.pf-add input::placeholder{color:var(--ink-4);}
.pf-muted-h{font-size:11px;color:var(--ink-4);margin-top:16px;cursor:pointer;display:inline-flex;align-items:center;gap:6px;
  font-family:'JetBrains Mono',monospace;transition:color .12s;}
.pf-muted-h:hover{color:var(--ink-3);}
.pf-muted-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px;}
.pf-muted-chip{font-size:11.5px;padding:4px 10px;border-radius:8px;border:1px solid var(--hair);color:var(--ink-4);
  text-decoration:line-through;cursor:pointer;transition:color .12s,border-color .12s,text-decoration .12s;}
.pf-muted-chip:hover{color:var(--ink-2);border-color:var(--ink-4);text-decoration:none;}
.pf-row{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:14px 0;border-bottom:1px solid var(--hair);}
.pf-row:last-of-type{border-bottom:0;}
.pf-row-t{font-size:13.5px;color:var(--ink);}
.pf-row-d{font-size:12px;color:var(--ink-3);margin-top:2px;max-width:44ch;}
.pf-row-r{display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex:none;}
.pf-detected{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--ink-4);display:inline-flex;align-items:center;gap:5px;}
.pf-detected .d{width:5px;height:5px;border-radius:50%;background:var(--pos);}
.pf-select{border:1px solid var(--hair);border-radius:8px;background:var(--surface);color:var(--ink);
  font-size:13px;padding:8px 30px 8px 11px;min-width:210px;cursor:pointer;transition:border-color .14s;
  appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23999' stroke-width='2'%3E%3Cpath d='M6 9l6 6 6-6'/%3E%3C/svg%3E");
  background-repeat:no-repeat;background-position:right 10px center;}
.pf-select:focus{outline:none;border-color:var(--accent);}
.pf-input-sm{border:1px solid var(--hair);border-radius:8px;background:var(--surface);color:var(--ink);
  font-size:13px;padding:8px;font-family:'JetBrains Mono',monospace;min-width:60px;width:60px;text-align:center;transition:border-color .14s;}
.pf-input-sm:focus{outline:none;border-color:var(--accent);}
.pf-toggle{width:42px;height:24px;border-radius:999px;background:var(--hair-2);position:relative;flex:none;transition:background .18s;}
.pf-toggle.on{background:var(--accent);}
.pf-toggle span{position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:var(--surface);
  box-shadow:var(--shadow-1);transition:transform .18s cubic-bezier(.2,0,0,1);}
.pf-toggle.on span{transform:translateX(18px);}
.pf-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;margin-top:16px;}
.pf-saved{font-size:12px;color:var(--pos);font-family:'JetBrains Mono',monospace;}
.pf-save{font-size:13px;color:#fff;background:var(--accent);border-radius:9px;padding:9px 20px;font-weight:500;
  transition:transform .1s,filter .14s;}
.pf-save:hover{filter:brightness(1.05);}
.pf-save:active{transform:scale(.97);}
`;
const BANK_RU={sberbank:"Сбербанк",vtb:"ВТБ",alfabank:"Альфа-Банк",tinkoff:"Т-Банк",gazprombank:"Газпромбанк",rshb:"Россельхозбанк",domrf:"Банк ДОМ.РФ",psb:"ПСБ",sovcombank:"Совкомбанк",mtsbank:"МТС-Банк",raiffeisen:"Райффайзен",otkritie:"Открытие"};
const PROD_RU={ipoteka:"Ипотека",deposit:"Вклады",credit_card:"Кредитные карты",debit_card:"Дебетовые карты",consumer_loan:"Потребкредиты",auto:"Автокредиты",rko:"РКО",savings:"Накопит. счета",acquiring:"Эквайринг",premium:"Премиальные пакеты",transfers:"Переводы и комиссии"};
const topicLabel=(t)=>BANK_RU[t]||PROD_RU[t]||t;
const TZ_ZONES=[
  ["Europe/Kaliningrad","Калининград · МСК−1"],["Europe/Moscow","Москва · МСК"],
  ["Europe/Samara","Самара · МСК+1"],["Asia/Yekaterinburg","Екатеринбург · МСК+2"],
  ["Asia/Omsk","Омск · МСК+3"],["Asia/Krasnoyarsk","Красноярск · МСК+4"],
  ["Asia/Irkutsk","Иркутск · МСК+5"],["Asia/Yakutsk","Якутск · МСК+6"],
  ["Asia/Vladivostok","Владивосток · МСК+7"],["Asia/Magadan","Магадан · МСК+8"],
  ["Asia/Kamchatka","Камчатка · МСК+9"],
];
const IcMuteSm=()=><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 6L6 18M6 6l12 12"/></svg>;
const IcSpark=()=><svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l1.6 6.4L20 10l-6.4 1.6L12 18l-1.6-6.4L4 10l6.4-1.6z"/></svg>;

function ProfilePage(){
  const me=useMe();
  const[data,setData]=useState(null);
  const[selfDesc,setSelfDesc]=useState("");
  const[interests,setInterests]=useState({banks:[],products:[],pinned:[],muted:[],custom:[]});
  const[recs,setRecs]=useState([]);
  const[tz,setTz]=useState("");
  const[detectedTz,setDetectedTz]=useState("");
  const[personalDigest,setPersonalDigest]=useState(true);
  const[bandHome,setBandHome]=useState(false);
  const[morningHour,setMorningHour]=useState(7);
  const[newTopic,setNewTopic]=useState("");
  const[showMuted,setShowMuted]=useState(false);
  const[busy,setBusy]=useState(false);
  const[savedDesc,setSavedDesc]=useState(false);
  const[savedSet,setSavedSet]=useState(false);
  const[ps,setPs]=useState(null);              // «сила персонализации» из /api/me

  const applyMe=(d)=>{ setData(d);
    const p=d.prefs||{}; setSelfDesc(p.self_description||"");
    setPersonalDigest(p.personal_digest!==false); setBandHome(p.personal_band_home===true);
    setMorningHour(p.morning_hour||7);
    setInterests(d.interests||{banks:[],products:[],pinned:[],muted:[],custom:[]});
    setRecs(d.recommendations||[]); setPs(d.personalization||null); };
  useEffect(()=>{
    let dtz=""; try{dtz=Intl.DateTimeFormat().resolvedOptions().timeZone||"";}catch{}
    setDetectedTz(dtz);
    apiFetch("/api/me").then(d=>{applyMe(d); setTz(d.timezone||dtz||"Europe/Moscow");}).catch(()=>{});
    apiPut("/api/me",{prefs:{onboarded:true}}).catch(()=>{});
  },[]);

  const saveInterests=async(patch)=>{
    const next={...interests,...patch}; setInterests(next);
    try{ const r=await apiPut("/api/me/interests",{pinned:next.pinned,muted:next.muted,custom:next.custom});
      if(r&&r.interests) setInterests(r.interests); }catch{}
  };
  const mute=(t)=>saveInterests({muted:[...new Set([...(interests.muted||[]),t])],
                                 pinned:(interests.pinned||[]).filter(x=>x!==t),
                                 custom:(interests.custom||[]).filter(x=>x!==t)});
  const unmute=(t)=>saveInterests({muted:(interests.muted||[]).filter(x=>x!==t)});
  const addCustom=()=>{ const t=newTopic.trim(); if(!t)return; setNewTopic("");
    saveInterests({custom:[...new Set([...(interests.custom||[]),t])]}); };
  const acceptRec=(slug)=>{ setRecs(r=>r.filter(x=>x!==slug));
    saveInterests({pinned:[...new Set([...(interests.pinned||[]),slug])]}); };

  const refreshNote=async()=>{ setBusy(true);
    try{ const r=await apiPost("/api/me/profile/refresh",{}); if(r&&r.note) setData(d=>({...d,profile_note:r.note})); }catch{}
    setBusy(false); };
  const saveDesc=async()=>{
    try{ await apiPut("/api/me",{prefs:{self_description:selfDesc.trim()}}); }catch{}
    setSavedDesc(true); setTimeout(()=>setSavedDesc(false),1800);
    // подсказать пересбор нарратива в фоне
    apiPost("/api/me/profile/refresh",{}).then(r=>{ if(r&&r.note) setData(d=>({...d,profile_note:r.note})); }).catch(()=>{});
  };
  const saveSettings=async()=>{
    try{ await apiPut("/api/me",{timezone:tz||"Europe/Moscow",
      prefs:{personal_digest:personalDigest,personal_band_home:bandHome,morning_hour:Number(morningHour)||7}}); }catch{}
    setSavedSet(true); setTimeout(()=>setSavedSet(false),1800);
  };

  if(!data) return <LoadingPage/>;
  const products=(interests.products||[]);
  const custom=(interests.custom||[]);
  const muted=(interests.muted||[]);
  const hasTopics=products.length||custom.length;
  const tzOptions=TZ_ZONES.some(z=>z[0]===tz)?TZ_ZONES:[[tz,tz],...TZ_ZONES];
  const detectedMatch=detectedTz&&detectedTz===tz;

  return <div className="fade-in pf-wrap">
    <style>{PROFILE_CSS}</style>
    <div className="pf-hero">
      <div className="pf-avatar">{initials(me&&me.name||data.name)}</div>
      <div>
        <div className="eyebrow">§ Профиль · персонализация</div>
        <h1>{data.name||(me&&me.name)||"Аудитор"}</h1>
        <div className="pf-sub mono">{data.username} · внутренний аудит Сбербанка</div>
      </div>
    </div>

    {/* Единственный ручной ввод — зона ответственности */}
    <div className="surface pf-card">
      <div className="eyebrow" style={{marginBottom:8}}>Чем вы занимаетесь в Сбере</div>
      <p className="pf-hint">Опишите своими словами, какие продукты, процессы и риски Сбера вы проверяете. Это единственное, что нужно ввести — остальное система соберёт и настроит сама.</p>
      <textarea id="pf-desc" className="pf-ta" value={selfDesc} onChange={e=>setSelfDesc(e.target.value)}
        placeholder="Например: проверяю корректность начисления процентов по вкладам Сбера и комиссии по эквайрингу для ИП; слежу за ипотечными программами и жалобами по кредитным картам."/>
      <div className="pf-actions">
        {savedDesc&&<span className="pf-saved">Сохранено · профиль пересобирается ✦</span>}
        <button className="pf-save" onClick={saveDesc}>Сохранить</button>
      </div>
    </div>

    {/* AI-нарратив — центральная «умная» карточка */}
    <div className="surface pf-card pf-ai">
      <div className="pf-card-h">
        <div className="pf-ai-badge"><span className="sp"><IcSpark/></span>Ваш профиль · собран ИИ</div>
        <button className="pf-mini" onClick={refreshNote} disabled={busy}>{busy?"Собираю…":"Пересобрать"}</button>
      </div>
      {busy
        ? <div className="pf-note-gen"><span className="dots"><i/><i/><i/></span>ИИ анализирует ваши запросы и описание…</div>
        : data.profile_note
          ? <><p className="pf-note">{data.profile_note}</p>
              <div className="pf-src"><span className="live"/>обновляется автоматически по вашим запросам и описанию</div></>
          : <p className="pf-note-empty">Здесь ИИ соберёт краткий портрет ваших интересов — автоматически, по мере ваших запросов и из описания выше. Задайте пару вопросов ИИ-аналитику или нажмите «Пересобрать».</p>}
    </div>

    {/* Сила персонализации: сколько система уже знает + что даст больше всего */}
    {ps&&<div className="surface pf-card">
      <style>{`
        .pf-power{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap;}
        .pf-power-list{flex:1;min-width:260px;display:flex;flex-direction:column;}
        .pf-power-row{display:flex;align-items:baseline;gap:10px;padding:7px 4px;border-top:1px solid var(--hair);
          font-size:13px;color:var(--ink-2);}
        .pf-power-row:first-child{border-top:0;}
        .pf-power-row .tick{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--ink-4);flex:none;width:14px;}
        .pf-power-row.done .tick{color:var(--pos);}
        .pf-power-row.done{color:var(--ink-3);}
        .pf-power-row:not(.done){cursor:pointer;transition:color .12s;}
        .pf-power-row:not(.done):hover{color:var(--accent);}
        .pf-power-row .lbl{flex:1;min-width:0;}
        .pf-power-row .pts{font-family:'JetBrains Mono',monospace;font-size:10.5px;color:var(--ink-4);flex:none;}
        .pf-power-row:not(.done) .pts{color:var(--accent);font-weight:600;}
        .pf-power-cap{font-size:11.5px;color:var(--ink-4);margin-top:10px;line-height:1.5;}
      `}</style>
      <div className="eyebrow" style={{marginBottom:12}}>Сила персонализации · <span style={{color:"var(--accent)"}}>✦ растёт от ваших действий</span></div>
      <div className="pf-power">
        <PfRing score={ps.score}/>
        <div className="pf-power-list">
          {(ps.parts||[]).filter(x=>x.max>0&&x.key!=="regular")
            .sort((a,b)=>(a.done?1:0)-(b.done?1:0)||((b.max-b.earned)-(a.max-a.earned)))
            .map(x=><div key={x.key} className={"pf-power-row"+(x.done?" done":"")}
              onClick={()=>{ if(x.done)return;
                if(x.target==="profile"){const el=document.getElementById("pf-desc");
                  if(el){el.focus();el.scrollIntoView({behavior:"smooth",block:"center"});}}
                else if(x.target) location.hash=x.target; }}>
              <span className="tick">{x.done?"✓":"○"}</span>
              <span className="lbl">{x.done?x.label:x.cta||x.label}</span>
              <span className="pts">{x.done?"+"+x.max+"%":"+"+Math.max(x.max-x.earned,0)+"%"}</span>
            </div>)}
        </div>
      </div>
      <div className="pf-power-cap">Оценки 👍/👎 на «Для вас» и полосе учат ВАШИ рекомендации; оценки ответов
        ИИ-аналитика уходят команде — по ним мы чиним инструмент. Каждый пункт выше показывает свой вклад.</div>
    </div>}

    {/* Темы в фокусе — определяет система, ручное вторично */}
    <div className="surface pf-card">
      <div className="eyebrow" style={{marginBottom:8}}>Темы в фокусе</div>
      <div className="pf-sub-h">Система определила по вашим запросам</div>
      {hasTopics
        ? <div className="pf-topics">
            <span className="pf-topic anchor">Сбербанк <span className="lock">якорь</span></span>
            {products.map(t=>(
              <span key={t} className="pf-topic">{topicLabel(t)}
                <span className="pf-tacts"><button onClick={()=>mute(t)} title="Заглушить"><IcMuteSm/></button></span>
              </span>))}
            {custom.map(t=>(
              <span key={"c"+t} className="pf-topic">{t}
                <span className="pf-tacts"><button onClick={()=>mute(t)} title="Убрать"><IcMuteSm/></button></span>
              </span>))}
          </div>
        : <div className="pf-topics"><span className="pf-topic anchor">Сбербанк <span className="lock">якорь</span></span>
            <span className="t-cap" style={{color:"var(--ink-4)"}}>ваши продукты появятся после нескольких запросов</span></div>}

      {recs.length>0 && <>
        <div className="pf-sub-h">Рекомендуем добавить</div>
        <div className="pf-rec">
          {recs.map(s=><button key={s} className="pf-rec-chip" onClick={()=>acceptRec(s)}>+ {topicLabel(s)}</button>)}
        </div>
      </>}

      <div className="pf-sub-h">Добавить своё</div>
      <div className="pf-add"><input value={newTopic} onChange={e=>setNewTopic(e.target.value)}
        onKeyDown={e=>{if(e.key==="Enter")addCustom();}} placeholder="+ своя тема"/></div>

      {muted.length>0 && <>
        <div className="pf-muted-h" onClick={()=>setShowMuted(v=>!v)}>{showMuted?"▾":"▸"} Заглушённые · {muted.length}</div>
        {showMuted && <div className="pf-muted-list">
          {muted.map(t=><span key={t} className="pf-muted-chip" onClick={()=>unmute(t)} title="Вернуть">{topicLabel(t)}</span>)}
        </div>}
      </>}
    </div>

    {/* Настройки */}
    <div className="surface pf-card">
      <div className="eyebrow" style={{marginBottom:6}}>Настройки</div>
      <div className="pf-row">
        <div><div className="pf-row-t">Часовой пояс</div><div className="pf-row-d">Приветствие и «утро» вашей главной подстраиваются под него</div></div>
        <div className="pf-row-r">
          <select className="pf-select" value={tz} onChange={e=>setTz(e.target.value)}>
            {tzOptions.map(z=><option key={z[0]} value={z[0]}>{z[1]}</option>)}
          </select>
          {detectedMatch&&<span className="pf-detected"><span className="d"/>определён автоматически</span>}
          {detectedTz&&!detectedMatch&&<span className="pf-detected" style={{cursor:"pointer"}} onClick={()=>setTz(detectedTz)}>ваш пояс: {detectedTz} — применить</span>}
        </div>
      </div>
      <div className="pf-row">
        <div><div className="pf-row-t">Страница «Для вас»</div><div className="pf-row-d">Личный разворот в «Обзоре»: направления, новости и зацепки под ваш профиль, каждое утро</div></div>
        <button className={"pf-toggle"+(personalDigest?" on":"")} onClick={()=>setPersonalDigest(v=>!v)} aria-label="переключить"><span/></button>
      </div>
      <div className="pf-row">
        <div><div className="pf-row-t">Личная полоса в «Общем»</div><div className="pf-row-d">Краткая выжимка из «Для вас» над общим брифингом</div></div>
        <button className={"pf-toggle"+(bandHome?" on":"")} onClick={()=>setBandHome(v=>!v)} aria-label="переключить"><span/></button>
      </div>
      <div className="pf-row">
        <div><div className="pf-row-t">Начало «утра»</div><div className="pf-row-d">С какого часа показывать утренний выпуск (0–12)</div></div>
        <input className="pf-input-sm" type="number" min="0" max="12" value={morningHour} onChange={e=>setMorningHour(e.target.value)}/>
      </div>
      <div className="pf-actions">
        {savedSet&&<span className="pf-saved">Сохранено ✓</span>}
        <button className="pf-save" onClick={saveSettings}>Сохранить</button>
      </div>
    </div>
  </div>;
}

// Любая ошибка рендера страницы → заглушка с кнопкой вместо белого экрана,
// ошибка уходит в журнал «Пульса» (kind=client_error) даже если трекер страницы мёртв.
class PageBoundary extends React.Component{
  constructor(p){super(p);this.state={err:null};}
  static getDerivedStateFromError(e){return{err:e};}
  componentDidCatch(e,info){
    try{
      fetch("/api/journal",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({events:[{kind:"client_error",page:(location.hash||"#").slice(1),
          payload:{msg:String((e&&e.message)||e).slice(0,300),
                   stack:String((info&&info.componentStack)||"").slice(0,400)}}]})}).catch(()=>{});
    }catch{}
  }
  componentDidUpdate(prev){ if(prev.pageKey!==this.props.pageKey&&this.state.err)this.setState({err:null}); }
  render(){
    if(this.state.err) return <div style={{padding:"64px 24px",textAlign:"center"}}>
      <div style={{fontSize:26,marginBottom:10,color:"var(--warn)"}}>⚠</div>
      <div style={{fontWeight:500,marginBottom:6}}>Страница не смогла отрисоваться</div>
      <div className="t-cap" style={{maxWidth:"46ch",margin:"0 auto 16px"}}>
        Ошибка записана в журнал «Пульса». Чаще всего в вкладке осталась старая версия
        приложения — обновление решает.</div>
      <button className="btn btn-accent" onClick={()=>location.reload()}>Обновить приложение</button>
    </div>;
    return this.props.children;
  }
}

// hash → {p: id страницы, prm: параметры}. Диплинки несут срез в адресе:
// #market?cat=deposit&view=changes&change=123. #sber — алиас (вкладки
// объединены в «Позицию», 07.2026).
function parseHash(){
  const h=(location.hash||"").slice(1);
  const qi=h.indexOf("?");
  let p=qi>=0?h.slice(0,qi):h, prm={};
  if(qi>=0){try{prm=Object.fromEntries(new URLSearchParams(h.slice(qi+1)).entries());}catch{}}
  if(p==="sber")p="market";
  return{p,prm};
}

function Shell(){
  const[page,setPage]=useState(()=>{ const h=parseHash().p;
    if(h) return h;
    try{ if(localStorage.getItem("al-ov-mode")==="foryou") return "foryou"; }catch{}
    return "overview"; });
  const[pageParams,setPageParams]=useState(()=>parseHash().prm);
  const[loopholeMounted,setLoopholeMounted]=useState(()=>(parseHash().p||"overview")==="loophole");
  // ИИ-аналитик живёт в фоне: страница не размонтируется при уходе на другие
  // вкладки — прогон продолжается, по завершении сигналим точкой в rail и тостом.
  const[aiMounted,setAiMounted]=useState(()=>(parseHash().p||"overview")==="ai");
  const[aiBusy,setAiBusy]=useState(false);
  const[aiReady,setAiReady]=useState(false);
  const aiPrevRun=useRef(false);
  const pageCurRef=useRef(null);
  const{theme,setTheme}=useTheme();
  const[banks,setBanks]=useState([]);
  const[qualityCount,setQualityCount]=useState(0);
  const[hasCaptcha,setHasCaptcha]=useState(false);
  const[navOpen,setNavOpen]=useState(false);
  const[me,setMe]=useState(null);
  const[onbSeen,setOnbSeen]=useState(false);
  useEffect(()=>{document.documentElement.classList.toggle("nav-lock",navOpen);return()=>document.documentElement.classList.remove("nav-lock");},[navOpen]);

  // Load banks for context + sidebar badges
  useEffect(()=>{
    apiFetch("/api/banks").then(d=>{setBanks(d||[]);}).catch(()=>{});
    apiFetch("/api/quality").then(d=>{setQualityCount((d?.flags||[]).length);}).catch(()=>{});
    apiFetch("/api/sources").then(d=>{setHasCaptcha((d?.captcha_pending||[]).length>0);}).catch(()=>{});
  },[]);

  // Профиль пользователя (+ отдаём серверу свой часовой пояс из браузера).
  // Ретрай: без me не появляются админ-вкладка и персональные тумблеры.
  const loadMe=(attempt)=>{
    let tz=""; try{tz=Intl.DateTimeFormat().resolvedOptions().timeZone||"";}catch{}
    apiFetch("/api/me"+(tz?"?tz="+encodeURIComponent(tz):"")).then(setMe)
      .catch(()=>{ const a=typeof attempt==="number"?attempt:0;
        if(a<2)setTimeout(()=>loadMe(a+1),3000); });
  };
  useEffect(()=>{loadMe(0);},[]); // eslint-disable-line
  // после выхода из «Профиля» перечитываем me: тумблеры (полоса на главной и т.п.)
  // должны действовать сразу, без F5
  useEffect(()=>{ if(page!=="profile") return; return loadMe; },[page]);

  // ── телеметрия: page_view / page_leave(время) / клиентские ошибки ──────────
  const trkQ=useRef([]); const trkPage=useRef({page:null,t:Date.now()});
  const trkFlush=(beacon)=>{ const evs=trkQ.current.splice(0);
    if(!evs.length)return;
    const body=JSON.stringify({events:evs});
    if(beacon&&navigator.sendBeacon){
      try{navigator.sendBeacon("/api/journal",new Blob([body],{type:"application/json"}));return;}catch{}
    }
    fetch("/api/journal",{method:"POST",headers:{"Content-Type":"application/json"},body}).catch(()=>{});
  };
  const trk=(ev)=>{ trkQ.current.push(ev); if(trkQ.current.length>=8)trkFlush(); };
  useEffect(()=>{
    const prev=trkPage.current;
    if(prev.page&&prev.page!==page)
      trk({kind:"page_leave",page:prev.page,dur_ms:Math.min(Date.now()-prev.t,1800000)});
    trkPage.current={page,t:Date.now()};
    trk({kind:"page_view",page});
    const t=setTimeout(trkFlush,1500);
    return ()=>clearTimeout(t);
  },[page]); // eslint-disable-line
  useEffect(()=>{
    const onVis=()=>{ if(document.visibilityState==="hidden"){
        const p=trkPage.current;
        if(p.page) trkQ.current.push({kind:"page_leave",page:p.page,dur_ms:Math.min(Date.now()-p.t,1800000)});
        trkPage.current={...p,t:Date.now()};
        trkFlush(true);
      } else { trkPage.current={...trkPage.current,t:Date.now()}; } };
    const onErr=(e)=>trk({kind:"client_error",page:(location.hash||"#").slice(1),
      payload:{msg:String((e&&(e.message||e.reason))||"").slice(0,300)}});
    document.addEventListener("visibilitychange",onVis);
    window.addEventListener("error",onErr);
    window.addEventListener("unhandledrejection",onErr);
    return ()=>{document.removeEventListener("visibilitychange",onVis);
      window.removeEventListener("error",onErr);
      window.removeEventListener("unhandledrejection",onErr);};
  },[]); // eslint-disable-line

  useEffect(()=>{
    const onHash=()=>{const{p,prm}=parseHash();setPage(p||"overview");setPageParams(prm);};
    window.addEventListener("hashchange",onHash);
    return ()=>window.removeEventListener("hashchange",onHash);
  },[]);
  // пишем hash только если сменилась СТРАНИЦА — параметры (#market?cat=…)
  // зеркалит сама страница, затирать их нельзя
  useEffect(()=>{
    const cur=(location.hash||"").slice(1).split("?")[0];
    if((cur==="sber"?"market":cur)!==page)history.replaceState(null,"","#"+page);
  },[page]);
  useEffect(()=>{if(page==="loophole")setLoopholeMounted(true);},[page]);
  useEffect(()=>{if(page==="ai"){setAiMounted(true);setAiReady(false);}
    pageCurRef.current=page;},[page]);
  // сигналы от AIPage о ходе прогона (running true/false)
  useEffect(()=>{
    const h=(e)=>{ const r=!!(e.detail&&e.detail.running);
      setAiBusy(r);
      if(aiPrevRun.current&&!r&&pageCurRef.current!=="ai") setAiReady(true);
      aiPrevRun.current=r; };
    window.addEventListener("al-ai-state",h);
    return ()=>window.removeEventListener("al-ai-state",h);
  },[]);
  // запоминаем последний режим «Обзора» (Общий/Для вас) — возвращаем туда же
  useEffect(()=>{ if(page==="overview"||page==="foryou"){try{localStorage.setItem("al-ov-mode",page);}catch{}} },[page]);

  const groups=useMemo(()=>{
    const items=(me&&me.is_admin)?[...NAV,{id:"pulse",label:"Пульс",icon:Ic.spark,group:"Данные"}]:NAV;
    const g={};items.forEach(n=>{(g[n.group]=g[n.group]||[]).push(n);});return g;},[me]);
  // Страница есть на сервере, но неизвестна ЭТОМУ бандлу (вкладка держит старую
  // версию SPA — hash-переход её не перезагружает) → одно само-обновление.
  useEffect(()=>{
    if(page&&!PAGES_FN[page]){
      try{
        if(sessionStorage.getItem("al-reload-for")!==page){
          sessionStorage.setItem("al-reload-for",page);
          location.reload();
        }
      }catch{}
    }
  },[page]);
  const Page=PAGES_FN[page]||OverviewPage;
  const[idx,label]=PAGE_LABELS[page]||["01","Обзор"];

  return <MeCtx.Provider value={me}><BanksCtx.Provider value={banks}>
    <div id="app">
      <TipLayer/>
      <style>{FB_CSS}</style>
      <style>{`.user-chip:hover{background:var(--surface);} .user-chip.active{background:var(--accent-soft);} .user-chip.active .nm{color:var(--accent);}
        .nav-dot.ai-run{background:var(--accent);animation:pulse 1.5s ease infinite;}
        .nav-dot.ai-done{background:var(--pos);}
        .ai-ready{position:fixed;right:22px;bottom:22px;z-index:300;display:flex;align-items:center;gap:9px;cursor:pointer;
          background:var(--surface);border:1px solid color-mix(in oklab,var(--accent),transparent 70%);border-radius:12px;
          padding:12px 16px;font-size:13px;font-weight:500;color:var(--ink);box-shadow:var(--shadow-2);
          animation:fade-in .3s ease-out;transition:transform .15s,border-color .15s;}
        .ai-ready:hover{transform:translateY(-2px);border-color:var(--accent);}
        .ai-ready .sp{color:var(--accent);}
        .ai-ready .x{color:var(--ink-4);font-size:12px;padding:2px 4px;border-radius:5px;}
        .ai-ready .x:hover{color:var(--ink);background:var(--paper-2);}
        @keyframes onb-pulse{0%,100%{box-shadow:0 0 0 0 var(--accent-soft)}50%{box-shadow:0 0 0 5px var(--accent-soft)}}
        .user-chip.onb{animation:onb-pulse 2.2s ease-in-out infinite;background:var(--accent-soft);}
        .rail-foot{position:relative;}
        .onb-callout{position:absolute;left:6px;right:6px;bottom:64px;z-index:60;background:var(--surface);
          border:1px solid var(--hair);border-radius:12px;box-shadow:var(--shadow-2);padding:13px 15px;animation:fade-in .3s ease-out;}
        .onb-callout .t{font-size:12.5px;color:var(--ink);line-height:1.5;margin-bottom:11px;text-wrap:pretty;}
        .onb-callout .t b{color:var(--accent);font-weight:600;}
        .onb-callout .b{display:flex;gap:8px;}
        .onb-callout button{font-size:11.5px;padding:6px 12px;border-radius:8px;transition:transform .1s,filter .12s;}
        .onb-callout button:active{transform:scale(.96);}
        .onb-callout .go{background:var(--accent);color:#fff;font-weight:500;}
        .onb-callout .skip{color:var(--ink-3);border:1px solid var(--hair);}
        .onb-callout::after{content:"";position:absolute;left:26px;bottom:-6px;width:11px;height:11px;background:var(--surface);
          border-right:1px solid var(--hair);border-bottom:1px solid var(--hair);transform:rotate(45deg);}`}</style>
      <aside className={"rail"+(navOpen?" open":"")}>
        <div className="rail-brand">
          <svg className="rail-mark" viewBox="0 0 100 100" role="img" aria-label="AuditLens">
            <path fill="#1F4DFF" d="M47.5 13 L59.5 13 L89.5 89 L75.5 89 Z"/>
            <path fill="currentColor" fillRule="evenodd" d="M47.5 13 L57.5 13 L83.5 89 L66.5 89 L58.5 67 L36.5 67 L27.5 89 L10.5 89 Z M47.5 36 L56.5 58 L38.5 58 Z"/>
          </svg>
          <div>
            <h1>AuditLens</h1>
            <small>v1.0 · Internal</small>
          </div>
        </div>
        {Object.entries(groups).map(([gr,items])=>(
          <div key={gr}>
            <div className="rail-section">{gr}</div>
            {items.map(n=>{
              const active=page===n.id||(n.id==="overview"&&page==="foryou");
              const allItems=items;   // items группы (включая условный «Пульс»), не базовый NAV
              const num=allItems.findIndex(x=>x.id===n.id)+1+(gr==="Анализ"?0:5);
              const dot=n.id==="sources"&&hasCaptcha;
              const count=n.id==="quality"&&qualityCount>0?qualityCount:null;
              // ИИ-аналитик: пульсирующая точка = прогон идёт; зелёная = отчёт готов
              const aiDot=n.id==="ai"&&(aiBusy||aiReady);
              return <button key={n.id} className={`nav-item ${active?"active":""}`} onClick={()=>{setPage(n.id);setNavOpen(false);}}>
                <span className="rail-num">{String(num).padStart(2,"0")}</span>
                <span style={{display:"inline-flex",marginRight:10,color:"var(--ink-3)"}}><n.icon/></span>
                {n.label}
                {dot&&<span className="nav-dot"/>}
                {aiDot&&<span className={"nav-dot"+(aiBusy?" ai-run":" ai-done")}/>}
                {count&&<span className="nav-count">{count}</span>}
              </button>;
            })}
          </div>
        ))}
        <div className="rail-foot">
          {(()=>{ const showOnb = me && !(me.prefs&&me.prefs.onboarded) && !onbSeen && page!=="profile";
            return showOnb ? <div className="onb-callout">
              <div className="t">✦ <b>Новое:</b> настройте инструмент под себя — опишите, что проверяете, и получайте персональную подачу и сводки.</div>
              <div className="b">
                <button className="go" onClick={()=>{setOnbSeen(true);setPage("profile");setNavOpen(false);}}>Настроить</button>
                <button className="skip" onClick={()=>{setOnbSeen(true);apiPut("/api/me",{prefs:{onboarded:true}}).catch(()=>{});}}>Позже</button>
              </div>
            </div> : null; })()}
          <button className={"user-chip"+(page==="profile"?" active":"")+(me&&!(me.prefs&&me.prefs.onboarded)&&!onbSeen&&page!=="profile"?" onb":"")} title="Профиль и персонализация"
                  onClick={()=>{setOnbSeen(true);setPage("profile");setNavOpen(false);}}
                  style={{width:"100%",textAlign:"left",transition:"background .14s"}}>
            <div className="avatar">{me?initials(me.name):"А"}</div>
            <div>
              <div className="nm">{me?.name||"Аудитор"}</div>
              <div className="role">Внутренний аудит</div>
            </div>
          </button>
        </div>
      </aside>
      {navOpen&&<div className="rail-backdrop" onClick={()=>setNavOpen(false)}/>}

      <div className="main">
        <div className="topbar">
          <div className="mobile-nav">
            <button className="icon-btn" aria-label="меню" onClick={()=>setNavOpen(true)}><Ic.menu/></button>
          </div>
          <div className="crumb">
            {page!=="profile" && <><span className="crumb-idx">{idx} / 10</span>
            <span style={{color:"var(--hair-2)"}}>—</span></>}
            <b>{label}</b>
          </div>
          {(page==="overview"||page==="foryou")&&
            <div className="ovseg-wrap desk-only"><OvSeg page={page}/></div>}
          <div className="tb-spacer"/>
          {/* на overview/foryou центр занят сегмент-пилюлей — мета убрана, чтобы не перекрывались на ~1024px */}
          {page!=="overview"&&page!=="foryou"&&<div className="tb-meta desk-only">
            <span className="live">данные актуальны</span>
            <span>{new Date().toLocaleTimeString("ru",{hour:"2-digit",minute:"2-digit"})} МСК</span>
            <span className="kbd">API</span>
          </div>}
          <button className="icon-btn" aria-label="обновить" title="Обновить страницу" onClick={()=>setPage(p=>p)}>
            <Ic.refresh/>
          </button>
          <button className="icon-btn" aria-label="тема" onClick={()=>setTheme(theme==="dark"?"light":"dark")} title="Сменить тему">
            {theme==="dark"?<Ic.sun/>:<Ic.moon/>}
          </button>
        </div>
        <div className="content">
          {loopholeMounted&&<div style={{display:page==="loophole"?"block":"none",height:"100%"}}>
            <LoopholePage/>
          </div>}
          {aiMounted&&<div style={{display:page==="ai"?"block":"none",height:"100%"}}>
            <PageBoundary pageKey="ai"><AIPage/></PageBoundary>
          </div>}
          {page!=="loophole"&&page!=="ai"&&<PageBoundary pageKey={page}><Page key={page} params={pageParams}/></PageBoundary>}
          {aiReady&&page!=="ai"&&
            <div className="ai-ready" onClick={()=>{setAiReady(false);setPage("ai");}}>
              <span className="sp">✦</span> Отчёт готов — открыть
              <button className="x" onClick={(e)=>{e.stopPropagation();setAiReady(false);}}>✕</button>
            </div>}
        </div>
      </div>
    </div>
  </BanksCtx.Provider></MeCtx.Provider>;
}

function App(){
  return <ThemeProvider><Shell/></ThemeProvider>;
}

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
