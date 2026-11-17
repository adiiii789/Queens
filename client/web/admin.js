// Tab „Server“: Leitstand für das lokale Backend (control.py, gestartet von run_local.py).
//
// Zeigt Raft-Knoten, Lobby-Instanzen, Gateway und Datenbank und kann Dienste
// starten, hart beenden (Absturz), isolieren und wieder verbinden. Der
// Leitstand ist nur auf dem Host-Rechner erreichbar (127.0.0.1).

const POLL_MS = 1500;
const LOG_EVERY = 2; // Log bei jeder zweiten Abfrage neu laden

const $ = (id) => document.getElementById(id);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return node;
}

const KIND_LABEL = { raft: "Knoten", lobby: "Lobby", gateway: "Gateway" };
const label = (s) => s.kind === "gateway" ? "Gateway" : `${KIND_LABEL[s.kind]} ${s.name}`;

export function createAdmin({ base, onStatus = () => {}, toast = () => {} }) {
  let visible = false;
  let timer = null;
  let tick = 0;
  let busy = false;
  let lastSignature = "";
  let lastStatus = null;
  let logName = "gateway";

  async function call(path, options = {}) {
    const response = await fetch(base + path, {
      cache: "no-store",
      signal: AbortSignal.timeout(options.method === "POST" ? 15000 : 5000),
      ...options,
      headers: options.method === "POST" ? { "Content-Type": "application/json" } : undefined,
    });
    let data = {};
    try { data = await response.json(); } catch { /* leer */ }
    if (!response.ok) throw new Error(data.detail ?? `HTTP ${response.status}`);
    return data;
  }

  async function poll() {
    try {
      const status = await call("/api/status");
      lastStatus = status;
      $("admin-offline").hidden = true;
      $("admin").hidden = false;
      render(status);
      onStatus(status);
      if (tick++ % LOG_EVERY === 0) loadLog();
    } catch (error) {
      lastStatus = null;
      lastSignature = "";
      $("admin").hidden = true;
      $("admin-offline").hidden = false;
      $("admin-link").href = base + "/";
      $("admin-link").textContent = base + "/";
      $("admin-offline-detail").textContent = `Versucht: ${base}/api/status (${error.message})`;
    }
  }

  function schedule() {
    clearTimeout(timer);
    if (visible) timer = setTimeout(async () => { await poll(); schedule(); }, POLL_MS);
  }

  async function act(path, label) {
    if (busy) return;
    busy = true;
    document.querySelectorAll("#view-server button[data-op]").forEach((b) => { b.disabled = true; });
    try {
      const result = await call(path, { method: "POST", body: "{}" });
      toast(result.message ?? `${label}: ok`, "good");
    } catch (error) {
      toast(`${label}: ${error.message}`, "error");
    } finally {
      busy = false;
      lastSignature = "";
      await poll();
    }
  }

  // --- Anzeige ---
  function stat(label, value, cls = "") {
    return el("div", { className: "stat" },
      el("div", { className: "label", textContent: label }),
      el("div", { className: "value " + cls, textContent: value }));
  }

  function button(text, op, path, cls = "btn small") {
    const b = el("button", { className: cls, type: "button", textContent: text, disabled: busy });
    b.dataset.op = op;
    b.addEventListener("click", () => act(path, text));
    return b;
  }

  function serviceCard(s, status) {
    const d = s.detail ?? {};
    const isLeader = s.kind === "raft" && status.leader?.node_id === s.name;
    const stateText = !s.running ? "aus" : s.responding ? "läuft" : "startet …";
    const dotClass = !s.running ? "" : s.responding ? "up" : "wait";
    const rows = [];
    let role = null;
    if (s.kind === "raft" && s.responding) {
      role = el("span", {
        className: `role ${d.isolated ? "isolated" : d.role}`,
        textContent: d.isolated ? `isoliert · ${d.role}` : d.role,
      });
      rows.push(["Term", d.term], ["Leader", d.leader_id ?? "–"], ["Log / Commit", `${d.log_length} / ${d.commit_index}`],
        ["Partien", d.games]);
    } else if (s.kind === "lobby" && s.responding) {
      rows.push(["Datenbank", d.db ? "erreichbar" : "NICHT erreichbar"]);
    } else if (s.kind === "gateway" && s.responding) {
      rows.push(["Verbunden", d.connected], ["Laufende Partien", d.games]);
    }
    const name = encodeURIComponent(s.name);
    const actions = [
      s.running ? button("■ Stoppen", "kill", `/api/services/${name}/kill`, "btn small danger")
        : button("▶ Starten", "start", `/api/services/${name}/start`, "btn small primary"),
    ];
    if (s.kind === "raft" && s.responding) {
      actions.push(d.isolated ? button("Verbinden", "heal", `/api/services/${name}/heal`)
        : button("Isolieren", "isolate", `/api/services/${name}/isolate`));
    }
    const logBtn = el("button", { className: "btn ghost small", type: "button", textContent: "Log" });
    logBtn.addEventListener("click", () => { logName = s.name; $("log-select").value = s.name; loadLog(); });
    actions.push(logBtn);

    return el("div", { className: "svc" + (isLeader ? " leader" : "") + (s.running ? "" : " down") },
      el("div", { className: "svc-head" },
        el("span", { className: `state-dot ${dotClass}` }),
        el("span", { className: "name", textContent: label(s) + (isLeader ? " ♛" : "") }),
        role,
        el("span", { className: "state muted small", textContent: stateText })),
      el("div", { className: "url", textContent: s.url }),
      rows.length ? el("dl", {}, rows.map(([k, v]) => [el("dt", { textContent: k }), el("dd", { textContent: String(v) })])) : null,
      el("div", { className: "row" }, actions));
  }

  function render(status) {
    const signature = JSON.stringify(status.services.map(({ uptime, ...rest }) => rest))
      + JSON.stringify(status.leader) + JSON.stringify(status.database.ok);
    if (signature === lastSignature) return;
    lastSignature = signature;

    const raft = status.services.filter((s) => s.kind === "raft");
    const up = raft.filter((s) => s.responding && !s.detail?.isolated).length;
    const gateway = status.services.find((s) => s.kind === "gateway");
    $("admin-summary").replaceChildren(
      stat("Leader", status.leader ? `Knoten ${status.leader.node_id} · Term ${status.leader.term}` : "keiner",
        status.leader ? "" : "muted"),
      stat("Mehrheit", `${up} von ${status.nodes} erreichbar (nötig ${status.majority})`),
      stat("Datenbank", status.database.ok ? "erreichbar" : "nicht erreichbar"),
      stat("Gateway", gateway?.responding ? `${gateway.detail.connected} Spieler · ${gateway.detail.games} Partien` : "aus"),
    );
    if (!status.database.ok) $("admin-summary").lastChild.previousSibling.title = status.database.error ?? "";

    for (const kind of ["raft", "lobby", "gateway"]) {
      $(`admin-${kind}`).replaceChildren(...status.services.filter((s) => s.kind === kind)
        .map((s) => serviceCard(s, status)));
    }
    const allUp = status.services.every((s) => s.running);
    const allDown = status.services.every((s) => !s.running);
    $("start-all").disabled = busy || allUp;
    $("stop-all").disabled = busy || allDown;

    const select = $("log-select");
    if (select.options.length !== status.services.length) {
      select.replaceChildren(...status.services.map((s) => el("option", {
        value: s.name, textContent: label(s),
      })));
      select.value = logName;
    }
  }

  async function loadLog() {
    const box = $("service-log");
    try {
      const data = await call(`/api/logs/${encodeURIComponent(logName)}?lines=120`);
      const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
      box.textContent = data.lines.length ? data.lines.join("\n") : "(noch kein Log)";
      $("log-name").textContent = "";
      if (atBottom) box.scrollTop = box.scrollHeight;
    } catch (error) {
      box.textContent = `Log nicht abrufbar: ${error.message}`;
    }
  }

  async function loadResults() {
    const box = $("results");
    try {
      const data = await call("/api/results?limit=8");
      if (!data.ok) { box.replaceChildren(el("p", { className: "muted", textContent: `Datenbank: ${data.error}` })); return; }
      if (!data.games.length) { box.replaceChildren(el("p", { className: "muted", textContent: "Noch keine gespeicherten Partien." })); return; }
      box.replaceChildren(...data.games.map((g) => el("div", { className: "results-game" },
        el("div", {}, el("b", { textContent: g.game_id }),
          el("span", { className: "meta", textContent: `  ·  Lobby ${g.lobby_id ?? "–"} · ${String(g.finished_at).slice(0, 19).replace("T", " ")}` })),
        el("ol", {}, g.results.map((r) => el("li", { className: r.winner ? "w" : "", textContent: `${r.name}: ${r.score} Punkte${r.winner ? " ★" : ""}` }))))));
    } catch (error) {
      box.replaceChildren(el("p", { className: "muted", textContent: `Nicht abrufbar: ${error.message}` }));
    }
  }

  $("start-all").dataset.op = "start-all";
  $("stop-all").dataset.op = "stop-all";
  $("start-all").addEventListener("click", () => act("/api/start-all", "Alle starten"));
  $("stop-all").addEventListener("click", () => {
    if (window.confirm("Alle Dienste beenden? Laufende Partien werden unterbrochen.")) act("/api/stop-all", "Alle stoppen");
  });
  $("log-select").addEventListener("change", (e) => { logName = e.target.value; loadLog(); });
  $("results-refresh").addEventListener("click", loadResults);

  return {
    get status() { return lastStatus; },
    async show() {
      if (visible) return;
      visible = true;
      lastSignature = "";
      await poll();
      loadResults();
      schedule();
    },
    hide() {
      visible = false;
      clearTimeout(timer);
    },
  };
}
