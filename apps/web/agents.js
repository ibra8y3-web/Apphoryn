(() => {
  let adminToken = "";
  let refreshHandle = null;
  let lastDashboard = null;
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const labels = {available:"متاح",paused:"متوقف بإعداد أمان",not_configured:"غير مربوط",unavailable:"غير متاح",queued:"في الانتظار",running:"يعمل",completed:"اكتمل",failed:"فشل",retry:"إعادة محاولة",idle:"خامل",error:"خطأ",interrupted:"توقف بسبب إعادة تشغيل العامل"};
  async function api(path, method="GET", body=null) {
    const response = await fetch("/api/v1/admin/agents"+path, {
      method, headers: {"X-Apphoryn-Admin":adminToken, ...(body?{"Content-Type":"application/json"}:{})},
      body: body ? JSON.stringify(body) : undefined, cache:"no-store"
    });
    const payload = await response.json().catch(()=>({}));
    if (!response.ok) throw new Error(payload.detail || payload.message || ("HTTP "+response.status));
    return payload;
  }
  function stateTag(value) {
    const status = String(value || "unknown");
    return '<span class="state '+esc(status)+'">'+esc(labels[status] || status)+'</span>';
  }
  function renderAgents(items) {
    $("agentsList").innerHTML = (items||[]).map(agent => '<article class="agent-card"><strong>'+esc(agent.name)+' <span class="tiny">· '+esc(agent.title)+'</span></strong><p>'+esc(agent.description)+'</p><div class="tag-list">'+(agent.tools||[]).map(tool=>'<span class="tag">'+esc(tool)+'</span>').join("")+'</div></article>').join("") || '<p class="muted">لا يوجد موظفون مسجلون.</p>';
  }
  function renderWorkers(items) {
    $("workersList").innerHTML = (items||[]).map(worker => '<article class="worker-row"><div class="worker-head"><b>'+esc(worker.worker_id)+'</b>'+stateTag(worker.status)+'</div><p class="tiny">آخر نبضة: '+esc(new Date(worker.last_heartbeat_at).toLocaleString())+'</p><p class="tiny">المهمة الحالية: '+esc(worker.current_run_id || "—")+'</p></article>').join("") || '<p class="muted">لم يرسل العامل أول نبضة بعد.</p>';
  }
  function renderProviders(payload) {
    const providers = payload?.providers || [];
    $("spendingBanner").textContent = "حماية الإنفاق: "+(payload.spending_protection_enabled ? "مفعّلة — الطلبات المدفوعة ممنوعة" : "معطّلة — يجب مراجعة إعدادات الفوترة");
    $("providersList").innerHTML = providers.map(item => {
      const status = item.enabled && item.models_discovered ? "available" : (!item.configured ? "not_configured" : item.enabled ? "unavailable" : "paused");
      const names = (payload.models||[]).filter(m=>m.provider===item.provider).slice(0,12).map(m=>'<span class="model-chip">'+esc(m.model)+'</span>').join("");
      return '<article class="provider-row"><div class="provider-head"><strong>'+esc(item.provider)+'</strong>'+stateTag(status)+'</div><p>'+esc(item.note||"")+'</p><div class="provider-models">'+names+'</div><div class="form-row"><span class="tiny">نماذج مكتشفة: '+Number(item.models_discovered||0)+'</span><button class="secondary provider-test" data-provider="'+esc(item.provider)+'">اختبار فعلي</button></div></article>';
    }).join("") || '<p class="muted">لا توجد مزودات في التقرير.</p>';
    $("providersList").querySelectorAll(".provider-test").forEach(button => button.addEventListener("click", async () => {
      button.disabled=true; button.textContent="جارٍ الاختبار...";
      try {
        const result=await api("/providers/"+encodeURIComponent(button.dataset.provider)+"/test","POST");
        button.textContent=result.success ? "نجح · "+result.latency_ms+"ms" : "لم ينجح";
        button.title=result.success ? result.model : result.reason;
      } catch(error) { button.textContent="خطأ"; button.title=error.message; }
      finally { button.disabled=false; }
    }));
  }
  function renderRuns(items) {
    $("runsList").innerHTML = (items||[]).map(run => '<article class="run-row"><div class="run-head"><b>'+esc(run.goal.slice(0,100))+'</b>'+stateTag(run.status)+'</div><p>'+esc(run.id)+' · '+esc(new Date(run.created_at).toLocaleString())+'</p><div class="run-head"><span class="tiny">الأولوية '+Number(run.priority)+'</span><button class="secondary open-run" data-id="'+esc(run.id)+'">تفاصيل</button></div>'+(run.last_error?'<p class="tiny">'+esc(run.last_error)+'</p>':'')+'</article>').join("") || '<p class="muted">لا توجد مهام مسجلة.</p>';
    $("runsList").querySelectorAll(".open-run").forEach(button => button.addEventListener("click",()=>loadRun(button.dataset.id)));
  }
  function renderSchedules(items) {
    $("schedulesList").innerHTML = (items||[]).map(schedule=>'<article class="schedule-row"><div class="schedule-head"><b>كل '+Math.round(schedule.interval_seconds/3600*10)/10+' ساعة</b>'+stateTag(schedule.enabled?"available":"paused")+'</div><p class="tiny">'+esc(schedule.goal.slice(0,160))+'</p><p class="tiny">التشغيل التالي: '+esc(new Date(schedule.next_run_at).toLocaleString())+'</p><button class="secondary toggle-schedule" data-id="'+esc(schedule.id)+'" data-enabled="'+String(!schedule.enabled)+'">'+(schedule.enabled?"إيقاف الجدولة":"تفعيل الجدولة")+'</button></article>').join("");
    $("schedulesList").querySelectorAll(".toggle-schedule").forEach(button=>button.addEventListener("click",async()=>{
      try{await api("/schedules/"+button.dataset.id+"/enabled?enabled="+button.dataset.enabled,"POST");await refresh();}
      catch(error){alert(error.message);}
    }));
  }
  function renderTasks(items) {
    $("taskDetails").innerHTML = (items||[]).map(task=>{
      const result=task.result ? JSON.stringify(task.result,null,2) : "";
      const tool=task.tool_result ? JSON.stringify(task.tool_result,null,2) : "";
      return '<article class="task-row"><div class="run-head"><b>'+esc(task.agent_id)+' · '+esc(task.title)+'</b>'+stateTag(task.status)+'</div><p>'+esc(task.provider||"—")+' / '+esc(task.model||"—")+'</p>'+(task.error?'<p>'+esc(task.error)+'</p>':'')+(result?'<details><summary>نتيجة الموظف</summary><pre>'+esc(result)+'</pre></details>':'')+(tool?'<details><summary>نتيجة الأداة الفعلية</summary><pre>'+esc(tool)+'</pre></details>':'')+'</article>';
    }).join("") || '<p class="muted">لا توجد تسليمات بعد.</p>';
  }
  async function loadRun(id) {
    $("taskDetails").innerHTML='<p class="muted">جارٍ تحميل تفاصيل المهمة...</p>';
    try { const result=await api("/tasks/"+encodeURIComponent(id)); renderTasks(result.tasks); }
    catch(error) { $("taskDetails").textContent=error.message; }
  }
  async function refresh() {
    const data=await api("/dashboard");
    lastDashboard=data;
    const counts=data.counts||{};
    $("runsTotal").textContent=counts.runs_total??0;
    $("runsActive").textContent=Number(counts.runs_queued||0)+Number(counts.runs_running||0)+Number(counts.runs_retry||0);
    $("runsCompleted").textContent=counts.runs_completed??0;
    $("memoryCount").textContent=counts.memory_records??0;
    renderAgents(data.agents); renderWorkers(data.workers); renderProviders(data.providers); renderRuns(data.runs); renderSchedules(data.schedules);
    $("connectionStatus").textContent="متصل بالـBackend. تم تحديث البيانات: "+new Date().toLocaleTimeString();
    $("refreshProviders").disabled=false; $("refreshAll").disabled=false;
  }
  async function connect() {
    adminToken=$("adminToken").value.trim();
    if(!adminToken){$("connectionStatus").textContent="أدخل رمز الإدارة أولًا.";return;}
    try{
      await refresh();
      if(refreshHandle)clearInterval(refreshHandle);
      refreshHandle=setInterval(()=>refresh().catch(error=>$("connectionStatus").textContent=error.message),6000);
    }catch(error){adminToken="";$("connectionStatus").textContent=error.message;}
  }
  $("connectButton").addEventListener("click",connect);
  $("refreshAll").addEventListener("click",()=>refresh().catch(error=>alert(error.message)));
  $("refreshProviders").addEventListener("click",async()=>{
    try{const result=await api("/providers/refresh","POST");renderProviders(result);}
    catch(error){alert(error.message);}
  });
  $("taskForm").addEventListener("submit",async event=>{
    event.preventDefault();
    const button=$("submitTask");button.disabled=true;
    try{
      const result=await api("/tasks","POST",{goal:$("taskGoal").value,priority:Number($("priority").value),
        allow_external_actions:$("externalApproval").checked,max_attempts:2});
      $("taskMessage").textContent="تم تسجيل المهمة في الطابور: "+result.id+" · الحالة: "+result.status;
      $("taskForm").reset();await refresh();
    }catch(error){$("taskMessage").textContent=error.message;}
    finally{button.disabled=false;}
  });
  $("scheduleForm").addEventListener("submit",async event=>{
    event.preventDefault();
    try{
      const hours=Math.max(1,Math.min(720,Number($("scheduleHours").value)||24));
      await api("/schedules","POST",{goal:$("scheduleGoal").value,interval_seconds:hours*3600,priority:5});
      $("scheduleForm").reset();$("scheduleHours").value="24";await refresh();
    }catch(error){alert(error.message);}
  });
})();
