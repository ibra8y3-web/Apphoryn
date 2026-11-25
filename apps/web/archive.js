(()=>{
  const $=id=>document.getElementById(id);
  const translations={
    en:{back:"Storefront",eyebrow:"CENTRAL REPOSITORY",headline:"A living archive of real apps.",subhead:"Discover applications already built in public repositories, keep their source provenance, and browse their complete published release and tag history.",note:"This archive indexes source and version metadata. Installable files remain subject to Apphoryn's license and package verification pipeline.",repositories:"Repositories scanned",applications:"Verified app projects",versions:"Version records",pending:"In the archive queue",searchTitle:"Search the application archive",searchDescription:"Search app names, repository owners, descriptions, or primary languages.",placeholder:"Search archived apps…",searchButton:"Search archive",results:"Archived applications",loading:"Loading the archive from Apphoryn…",more:"Load more",versionsButton:"Version history",source:"GitHub source",stars:"stars",updated:"Updated",language:"Language",license:"License",releases:"releases",tags:"tags",openRelease:"Open upstream release",assets:"assets",noVersions:"No releases or tags have been recorded yet.",noApps:"No verified app projects have been archived yet. The background worker will add projects as it classifies them.",error:"Could not load the archive. Check the Apphoryn API connection and try again.",noResults:"No archived applications match this search.",foot:"Source links and upstream metadata are preserved.",count:"applications"},
    ar:{back:"المتجر",eyebrow:"المستودع المركزي",headline:"أرشيف حي للتطبيقات الحقيقية.",subhead:"اكتشف التطبيقات المبنية بالفعل داخل المستودعات العامة، واحتفظ بمصدر كل مشروع، واستعرض سجل إصداراته المنشورة ووسومه بالكامل.",note:"يفهرس الأرشيف بيانات المصدر والإصدارات. أما ملفات التثبيت فتظل خاضعة لفحص الترخيص والحزم في Apphoryn.",repositories:"مستودعات تم رصدها",applications:"مشروعات تطبيقات تم التحقق منها",versions:"سجلات الإصدارات",pending:"في قائمة الأرشفة",searchTitle:"ابحث في أرشيف التطبيقات",searchDescription:"ابحث باسم التطبيق أو صاحب المستودع أو الوصف أو لغة البرمجة الأساسية.",placeholder:"ابحث في التطبيقات المؤرشفة…",searchButton:"بحث في الأرشيف",results:"التطبيقات المؤرشفة",loading:"جارٍ تحميل الأرشيف من Apphoryn…",more:"تحميل المزيد",versionsButton:"سجل الإصدارات",source:"المصدر على GitHub",stars:"نجمة",updated:"آخر تحديث",language:"اللغة",license:"الترخيص",releases:"إصدارات منشورة",tags:"وسوم",openRelease:"فتح الإصدار الأصلي",assets:"ملفات",noVersions:"لم يتم تسجيل إصدارات أو وسوم لهذا المشروع بعد.",noApps:"لم تتم أرشفة مشروعات تطبيقات مؤكدة بعد. سيضيفها العامل الخلفي بعد فحصها وتصنيفها.",error:"تعذر تحميل الأرشيف. تحقق من اتصال Apphoryn ثم حاول مجددًا.",noResults:"لا توجد تطبيقات مؤرشفة تطابق هذا البحث.",foot:"يتم الاحتفاظ بروابط المصدر وبيانات الإصدارات الأصلية.",count:"تطبيقات"}
  };
  let lang=localStorage.getItem("apphoryn-language")==="ar"?"ar":"en";
  let offset=0, query="", loading=false, more=true;
  const t=k=>translations[lang][k]||k;
  const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const fmt=v=>Number(v||0).toLocaleString(lang==="ar"?"ar-EG":"en-US");
  const date=v=>{if(!v)return "—";const d=new Date(v);return Number.isNaN(d.getTime())?esc(v):d.toLocaleDateString(lang==="ar"?"ar-EG":"en-US",{year:"numeric",month:"short",day:"numeric"})};
  function applyLanguage(){
    document.documentElement.lang=lang;document.documentElement.dir=lang==="ar"?"rtl":"ltr";
    $("backText").textContent=t("back");$("eyebrow").textContent=t("eyebrow");$("headline").textContent=t("headline");$("subhead").textContent=t("subhead");$("heroNote").textContent=t("note");
    $("repositoriesLabel").textContent=t("repositories");$("applicationsLabel").textContent=t("applications");$("versionsLabel").textContent=t("versions");$("pendingLabel").textContent=t("pending");
    $("searchTitle").textContent=t("searchTitle");$("searchDescription").textContent=t("searchDescription");$("query").placeholder=t("placeholder");$("searchButton").textContent=t("searchButton");$("resultsTitle").textContent=t("results");$("loadMore").textContent=t("more");$("footNote").textContent=t("foot");$("langToggle").textContent=lang==="ar"?"English":"العربية";
    if(lang==="ar"){$(".hero-note").style.borderLeft="0";$(".hero-note").style.borderRight="2px solid var(--accent)";$(".hero-note").style.paddingLeft="0";$(".hero-note").style.paddingRight="14px"}else{$(".hero-note").style.borderRight="0";$(".hero-note").style.borderLeft="2px solid var(--accent)";$(".hero-note").style.paddingRight="0";$(".hero-note").style.paddingLeft="14px"}
  }
  function renderCard(app){
    const evidence=(app.classification_evidence||[]).filter(x=>String(x).startsWith("manifest:")).map(x=>x.split(":")[1].replaceAll("_"," "));
    return '<article class="repo" data-id="'+esc(app.id)+'"><div class="repo-top"><div><h3><a href="'+esc(app.repository_url)+'" target="_blank" rel="noopener noreferrer">'+esc(app.name)+'</a></h3><div class="slug">'+esc(app.full_name)+'</div></div><div class="repo-actions"><button type="button" data-versions="'+esc(app.id)+'">'+t("versionsButton")+' · '+fmt(app.version_count)+'</button><a class="btn" style="padding:7px 10px;font-size:11px" target="_blank" rel="noopener noreferrer" href="'+esc(app.repository_url)+'">'+t("source")+' ↗</a></div></div><p>'+esc(app.description||"")+'</p><div class="meta">'+(app.primary_language?'<span>'+t("language")+': '+esc(app.primary_language)+'</span>':"")+(app.license_spdx?'<span>'+t("license")+': '+esc(app.license_spdx)+'</span>':"")+'<span>★ '+fmt(app.stars)+' '+t("stars")+'</span><span>'+t("updated")+': '+date(app.upstream_updated_at)+'</span>'+(evidence.length?'<span class="chip">'+esc(evidence.join(", "))+'</span>':"")+'</div><div class="versions" id="versions-'+esc(app.id)+'" hidden></div></article>';
  }
  async function refreshStats(){
    const r=await fetch("/api/v1/archive/stats",{cache:"no-store"});if(!r.ok)throw new Error("stats");
    const s=await r.json();$("repositories").textContent=fmt(s.repositories_seen);$("applications").textContent=fmt(s.applications_archived);$("versions").textContent=fmt(s.versions_total);$("pending").textContent=fmt(s.queue_pending);
  }
  async function loadList(reset=false){
    if(loading)return;loading=true;
    if(reset){offset=0;more=true;$("repos").innerHTML='<div class="empty">'+t("loading")+'</div>'}
    try{
      const params=new URLSearchParams({limit:"50",offset:String(offset)});if(query)params.set("q",query);
      const r=await fetch("/api/v1/archive/repositories?"+params.toString(),{cache:"no-store"});if(!r.ok)throw new Error("list");
      const payload=await r.json(),items=payload.items||[];
      if(reset)$("repos").innerHTML="";
      if(!items.length&&offset===0){$("repos").innerHTML='<div class="empty">'+t(query?"noResults":"noApps")+'</div>'}
      else $("repos").insertAdjacentHTML("beforeend",items.map(renderCard).join(""));
      offset+=items.length;more=items.length===50;$("loadMore").hidden=!more;
      $("resultCount").textContent=fmt(offset)+(lang==="ar"?" تطبيق":" "+t("count"));
      await refreshStats();
    }catch(e){$("repos").innerHTML='<div class="empty">'+t("error")+'</div>';$("loadMore").hidden=true}
    finally{loading=false}
  }
  async function loadVersions(id,button){
    const host=$("versions-"+id);if(!host)return;
    if(!host.hidden){host.hidden=true;button.textContent=t("versionsButton")+" · "+button.textContent.split("·").pop().trim();return}
    host.hidden=false;host.innerHTML='<div class="muted">'+t("loading")+'</div>';button.disabled=true;
    try{
      const r=await fetch("/api/v1/archive/repositories/"+encodeURIComponent(id)+"/versions?limit=100&offset=0",{cache:"no-store"});if(!r.ok)throw new Error("versions");
      const payload=await r.json(),items=payload.items||[];
      if(!items.length){host.innerHTML='<div class="muted">'+t("noVersions")+'</div>';return}
      host.innerHTML=items.map(v=>{
        const assets=(v.assets||[]).length;
        const href=v.html_url?'<a href="'+esc(v.html_url)+'" target="_blank" rel="noopener noreferrer">'+t("openRelease")+' ↗</a>':"";
        const assetText=assets?'<span class="muted">'+fmt(assets)+" "+t("assets")+'</span>':"";
        return '<div class="version"><div class="version-head"><div><b>'+esc(v.name||v.version_key)+'</b><small>'+esc(v.version_kind==="release"?t("releases"):t("tags"))+' · '+date(v.published_at||v.created_at_upstream||v.recorded_at)+(v.prerelease?' · prerelease':"")+'</small></div><span class="chip">'+esc(v.version_key)+'</span></div><div class="version-links">'+href+assetText+(v.commit_sha?'<span class="muted">commit '+esc(v.commit_sha.slice(0,10))+'</span>':"")+'</div></div>';
      }).join("");
    }catch(e){host.innerHTML='<div class="muted">'+t("error")+'</div>'}
    finally{button.disabled=false}
  }
  $("repos").addEventListener("click",e=>{const b=e.target.closest("[data-versions]");if(b)loadVersions(b.dataset.versions,b)});
  $("searchForm").addEventListener("submit",e=>{e.preventDefault();query=$("query").value.trim();loadList(true)});
  $("loadMore").addEventListener("click",()=>loadList(false));
  $("langToggle").addEventListener("click",()=>{lang=lang==="ar"?"en":"ar";localStorage.setItem("apphoryn-language",lang);applyLanguage();loadList(true)});
  applyLanguage();loadList(true);
  fetch("/api/v1/archive/stats",{cache:"no-store"}).then(r=>r.ok?r.json():null).then(s=>{if(s){$("repositories").textContent=fmt(s.repositories_seen);$("applications").textContent=fmt(s.applications_archived);$("versions").textContent=fmt(s.versions_total);$("pending").textContent=fmt(s.queue_pending)}}).catch(()=>{});
})();
