// orchX dashboard — minimal vanilla JS (no React, no Vue, no build step).

async function loadRuns() {
  const resp = await fetch("/api/runs");
  const runs = await resp.json();
  const tbody = document.querySelector("#runs tbody");
  tbody.innerHTML = "";
  for (const r of runs) {
    const tr = document.createElement("tr");
    const counts = r.counts || {};
    const ts = new Date((r.mtime || 0) * 1000).toLocaleString();
    tr.innerHTML = `
      <td><a href="#" data-task="${escapeAttr(r.task_id)}">${escapeHtml(r.task_id)}</a></td>
      <td class="success">${counts.success ?? "-"}</td>
      <td class="error">${counts.failed ?? "-"}</td>
      <td class="muted">${counts.skipped ?? "-"}</td>
      <td>${r.wall_seconds ?? "-"}</td>
      <td>${r.cost != null ? "$" + Number(r.cost).toFixed(4) : "-"}</td>
      <td class="muted">${ts}</td>
    `;
    tbody.appendChild(tr);
  }
  document.querySelector("#last-update").textContent = new Date().toLocaleTimeString();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

function connectSSE() {
  const status = document.querySelector("#sse-status");
  const list = document.querySelector("#events");
  const es = new EventSource("/api/events");
  es.onopen = () => { status.textContent = "live"; status.classList.remove("muted"); status.classList.add("success"); };
  es.onerror = () => { status.textContent = "disconnected"; status.classList.remove("success"); status.classList.add("error"); };
  // Универсальный обработчик: добавляем listener'ы на известные события.
  const known = [
    "run_started", "phase_completed", "phase_failed", "replan_triggered",
    "pr_opened", "cost_alert", "budget_exceeded", "wall_budget_exceeded",
    "ci_failed", "changes_requested", "approved_and_green", "run_finished",
  ];
  for (const evName of known) {
    es.addEventListener(evName, e => addEvent(evName, e.data));
  }
  es.onmessage = e => addEvent("message", e.data);
}

function addEvent(name, data) {
  const list = document.querySelector("#events");
  const li = document.createElement("li");
  const ts = new Date().toLocaleTimeString();
  let payload = data;
  try { payload = JSON.stringify(JSON.parse(data)); } catch (_) {}
  li.innerHTML = `<span class="ts">${ts}</span><span class="event">[${escapeHtml(name)}]</span> ${escapeHtml(payload)}`;
  list.insertBefore(li, list.firstChild);
  // Trim to last 200 events.
  while (list.children.length > 200) list.removeChild(list.lastChild);
}

document.querySelector("#refresh").addEventListener("click", loadRuns);
loadRuns();
setInterval(loadRuns, 10_000);
connectSSE();
