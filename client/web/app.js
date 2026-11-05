// Funktionaler Browser-Client (noch ohne Gestaltung).
//
// Nur Anzeige und Klicks → Aktionen; keine Spielregeln. Ob eine Aktion
// gültig ist, entscheidet der Server, Fehler erscheinen als Hinweis.
// Die Texte entsprechen client/render.py.

import { QueensClient, session } from "./protocol.js";

const REVEAL_MS = 3000; // angesehene Karten nur kurz aufdecken („wie am echten Tisch“)
const LOG_LINES = 40;

const ui = {
  lobby: null,      // letzte Lobby-Antwort, in der ich Mitglied bin
  view: null,       // eigene Spielersicht
  names: {},        // player_id → Name
  deadline: null,   // Zeitpunkt (ms) für die Restzeit
  selection: [],    // ausgewählte Slots für Ansehen bzw. Bube
  throwMode: false, // nächster Klick auf eigenen Slot wirft zwischen
  revealed: new Map(), // "player:slot" → {card, until}
  log: [],
  message: "",
};

const $ = (id) => document.getElementById(id);

const client = new QueensClient({ onMessage, onStatus });

// --- Texte ------------------------------------------------------------------------
function name(playerId) {
  const n = ui.names[playerId] ?? playerId ?? "?";
  return playerId === client.playerId ? `${n} (du)` : n;
}
const ref = (r) => `${name(r.player_id)} Slot ${r.slot}`;

const LOBBY_STATUS = {
  waiting: "wartet auf Spieler", starting: "Partie startet …", started: "Partie läuft",
  finished: "Partie beendet", deleted: "aufgelöst",
};
const PHASES = { initial_peek: "Startkarten ansehen", finished: "Partie beendet" };

const TEXTS = {
  GameStarted: (d) => `Karten sind ausgeteilt, Ablage: ${d.discard_top}`,
  CardsPeeked: (d) => "deine Karten: " + Object.entries(d.cards)
    .sort((a, b) => a[0] - b[0]).map(([s, c]) => `Slot ${s} = ${c}`).join(", "),
  PlayerPeeked: (d) => `${name(d.player_id)} hat sich die Startkarten angesehen`,
  PeekTimedOut: (d) => "Zeit zum Ansehen abgelaufen, ohne Ansehen weiter: "
    + d.player_ids.map(name).join(", "),
  TurnStarted: (d) => `Zug ${d.turn_number}: ${name(d.player_id)} ist dran`,
  CardDrawn: (d) => `du hast gezogen: ${d.card}`,
  PlayerDrew: (d) => `${name(d.player_id)} zieht vom `
    + (d.source === "deck" ? "Ziehstapel" : `Ablagestapel (${d.card})`),
  CardSwapped: (d) => `${name(d.player_id)} tauscht Slot ${d.slot} und legt ${d.discarded} ab`,
  CardDiscarded: (d) => `${name(d.player_id)} legt ${d.card} ab`,
  SpecialAvailable: (d) => `${name(d.player_id)} darf `
    + (d.special === "jack" ? "zwei Karten tauschen (Bube)" : "eine Karte ansehen (König)"),
  CardsSwappedByJack: (d) => `Bube: ${ref(d.first)} ↔ ${ref(d.second)}`,
  CardRevealedByKing: (d) => `König: ${ref(d.target)} ist ${d.card}`,
  PlayerPeekedByKing: (d) => `${name(d.player_id)} sieht sich ${ref(d.target)} an (König)`,
  SpecialSkipped: (d) => `${name(d.player_id)} verzichtet auf die Sonderaktion`,
  PlayerKnocked: (d) => `${name(d.player_id)} klopft! Alle anderen noch einmal.`,
  ThrowInSucceeded: (d) => `${name(d.player_id)} wirft ${d.card} (Slot ${d.slot}) zwischen ✓`,
  ThrowInFailed: (d) => `${name(d.player_id)} wirft falsch: ${d.card} (Slot ${d.slot}) bleibt liegen`,
  PenaltyCard: (d) => `${name(d.player_id)} bekommt eine Strafkarte in Slot ${d.slot}`,
  DeckReshuffled: (d) => `Ablagestapel neu gemischt (${d.deck_size} Karten)`,
  TurnTimedOut: (d) => `Zeit abgelaufen für ${name(d.player_id)}`,
  GameFinished: () => "Partie beendet, Karten werden aufgedeckt",
};

function describe(event) {
  let text;
  try { text = TEXTS[event.type]?.(event.data ?? {}); } catch { text = undefined; }
  text ??= `${event.type} ${JSON.stringify(event.data)}`;
  return (event.audience ? "🔒 " : "• ") + text;
}

// --- Nachrichten vom Gateway ---------------------------------------------------------
function onStatus(status, detail) {
  const texts = {
    verbinde: "verbinde …",
    verbunden: detail ? "wieder verbunden" : "verbunden",
    getrennt: `Verbindung weg, neuer Versuch in ${Math.round(detail / 1000)} s`,
    ersetzt: "In einem anderen Tab mit derselben ID angemeldet – hier beendet.",
    abgelehnt: "Anmeldung abgelehnt.",
  };
  $("status").textContent = texts[status] ?? status;
  if (status === "verbunden") ui.message = "";
  render();
}

function onMessage(m) {
  switch (m.op) {
    case "welcome":
      $("me").textContent = `${m.name} · ID ${m.player_id}`;
      break;
    case "lobby": {
      const mine = m.lobby.players?.some((p) => p.player_id === client.playerId);
      ui.lobby = mine && m.lobby.status !== "deleted" ? m.lobby : null;
      break;
    }
    case "game_started":
      ui.names = m.names ?? {};
      ui.view = null;
      ui.selection = [];
      ui.log = [];
      addLog(`Partie ${m.game_id} beginnt`);
      break;
    case "events":
      m.events.forEach(onEvent);
      break;
    case "state":
      ui.view = m.view;
      ui.deadline = m.timeout == null ? null : Date.now() + m.timeout * 1000;
      ui.selection = [];
      if (m.view.phase === "finished") ui.throwMode = false;
      break;
    case "info":
      addLog(`ℹ ${m.message}`);
      break;
    case "error":
      ui.message = `✗ ${m.message}`;
      break;
    default:
      addLog(`? ${JSON.stringify(m)}`);
  }
  render();
}

function onEvent(event) {
  addLog(describe(event));
  const d = event.data ?? {};
  if (event.type === "CardsPeeked") {
    for (const [slot, card] of Object.entries(d.cards)) reveal(client.playerId, slot, card);
  } else if (event.type === "CardRevealedByKing") {
    reveal(d.target.player_id, d.target.slot, d.card);
  } else if (event.type === "ThrowInFailed") {
    reveal(d.player_id, d.slot, d.card); // falsch geworfen: alle sehen die Karte
  } else if (event.type === "CardsSwappedByJack") {
    ui.revealed.delete(`${d.first.player_id}:${d.first.slot}`);
    ui.revealed.delete(`${d.second.player_id}:${d.second.slot}`);
  }
}

function reveal(playerId, slot, card) {
  const key = `${playerId}:${slot}`;
  ui.revealed.set(key, { card, until: Date.now() + REVEAL_MS });
  setTimeout(render, REVEAL_MS + 50);
}

function addLog(line) {
  ui.log.push(line);
  if (ui.log.length > LOG_LINES) ui.log.shift();
}

// --- Klicks → Aktionen ---------------------------------------------------------------
function isActive() {
  return ui.view?.active_player === client.playerId;
}

function onSlot(playerId, slot) {
  const v = ui.view;
  if (!v) return;
  const own = playerId === client.playerId;
  ui.message = "";
  if (ui.throwMode) {
    if (own) { client.throwIn(slot); ui.throwMode = false; } else ui.message = "Zwischenwerfen geht nur mit eigenen Karten.";
  } else if (v.phase === "initial_peek" && v.needs_initial_peek) {
    if (!own) { ui.message = "Zu Beginn nur eigene Karten ansehen."; } else {
      toggle(playerId, slot);
      if (ui.selection.length === 2) client.peekInitial(ui.selection.map((s) => s.slot));
    }
  } else if (v.phase === "await_decision" && isActive()) {
    if (own) client.swapWithHand(slot); else ui.message = "Tauschen geht nur mit einem eigenen Slot.";
  } else if (v.phase === "await_special" && isActive() && v.pending_special === "jack") {
    toggle(playerId, slot);
    if (ui.selection.length === 2) client.useJack(...ui.selection);
  } else if (v.phase === "await_special" && isActive() && v.pending_special === "king") {
    client.useKing({ player_id: playerId, slot });
  } else {
    ui.message = "Gerade ist hier nichts zu tun. Zwischenwerfen: erst den Knopf „Zwischenwerfen“.";
  }
  render();
}

function toggle(playerId, slot) {
  const i = ui.selection.findIndex((s) => s.player_id === playerId && s.slot === slot);
  if (i >= 0) ui.selection.splice(i, 1); else ui.selection.push({ player_id: playerId, slot });
}

function hint() {
  const v = ui.view;
  if (!v) return "";
  if (v.phase === "initial_peek") {
    return v.needs_initial_peek ? "Zwei eigene Karten anklicken, um sie anzusehen."
      : "Warte, bis alle ihre Startkarten angesehen haben.";
  }
  if (v.phase === "finished") return "Partie beendet. Neue Lobby erstellen oder beitreten.";
  if (ui.throwMode) return "Eigene Karte anklicken, die du zwischenwerfen willst.";
  if (!isActive()) return "Warte auf den Zug. Zwischenwerfen ist jederzeit möglich.";
  if (v.phase === "await_draw") return "Ziehstapel oder Ablage anklicken – oder klopfen.";
  if (v.phase === "await_decision") return "Eigenen Slot zum Tauschen anklicken – oder „Ablegen“.";
  if (v.pending_special === "jack") return "Bube: zwei beliebige Karten anklicken – oder „Überspringen“.";
  return "König: eine beliebige Karte zum Ansehen anklicken – oder „Überspringen“.";
}

// --- Anzeige ------------------------------------------------------------------------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children.filter((c) => c != null));
  return node;
}

function render() {
  const connected = client.ws?.readyState === WebSocket.OPEN;
  const v = ui.view;
  const inGame = v && v.phase !== "finished";
  $("connect").hidden = connected || client.hasSession;
  $("lobby").hidden = !connected || inGame;
  $("game").hidden = !v;
  $("message").textContent = ui.message;
  renderLobby();
  if (v) renderGame(v);
  $("log").replaceChildren(...ui.log.slice().reverse().map((line) => el("li", { textContent: line })));
}

function renderLobby() {
  const l = ui.lobby;
  const waiting = l?.status === "waiting";
  $("lobby-start").hidden = waiting;
  $("lobby-ready").hidden = !waiting;
  if (!l) { $("lobby-info").replaceChildren(); return; }
  $("lobby-info").replaceChildren(
    el("p", { textContent: `Lobby ${l.lobby_id} – ${LOBBY_STATUS[l.status] ?? l.status}`
      + ` (Instanz ${l.served_by ?? "?"})` }),
    el("ul", {}, ...l.players.map((p) => el("li", {
      textContent: `${p.name}${p.player_id === l.host_id ? " (Host)" : ""}`
        + `${p.player_id === client.playerId ? " (du)" : ""} – ${p.ready ? "✓ bereit" : "✗ nicht bereit"}`,
    }))),
  );
}

function renderTurn(v) {
  const remaining = ui.deadline == null ? null : Math.max(0, Math.round((ui.deadline - Date.now()) / 1000));
  $("turn").textContent = [
    `Partie ${v.game_id}`,
    v.turn_number ? `Zug ${v.turn_number}` : null,
    v.active_player ? `${name(v.active_player)} ist am Zug` : PHASES[v.phase] ?? v.phase,
    remaining != null && v.phase !== "finished" ? `noch ${remaining} s` : null,
  ].filter(Boolean).join(" · ");
}

function renderGame(v) {
  renderTurn(v);
  $("deck").textContent = `Ziehstapel (${v.deck_size})`;
  $("discard").textContent = `Ablage: ${v.discard_top ?? "–"} (${v.discard_size})`;
  $("drawn").textContent = v.drawn_card ? `Gezogen: ${v.drawn_card}`
    : v.someone_holds_card && v.active_player ? `${name(v.active_player)} hält eine Karte` : "";
  $("throw-mode").setAttribute("aria-pressed", String(ui.throwMode));
  $("hint").textContent = hint();

  const now = Date.now();
  $("players").replaceChildren(...v.players.map((p) => {
    const marks = [p.player_id === v.active_player ? "◀ am Zug" : null,
      p.player_id === v.knocker ? "(hat geklopft)" : null].filter(Boolean).join(" ");
    const slots = p.slots.map((filled, slot) => {
      const shown = ui.revealed.get(`${p.player_id}:${slot}`);
      const card = shown && shown.until > now ? shown.card : null;
      const selected = ui.selection.some((s) => s.player_id === p.player_id && s.slot === slot);
      const button = el("button", {
        className: "slot" + (selected ? " selected" : "") + (card ? " revealed" : ""),
        textContent: !filled ? "–" : card ?? `[${slot}]`,
        disabled: !filled,
        title: `${name(p.player_id)}, Slot ${slot}`,
      });
      button.addEventListener("click", () => onSlot(p.player_id, slot));
      return button;
    });
    return el("div", { className: "player" + (p.player_id === v.active_player ? " active" : "") },
      el("span", { className: "name", textContent: name(p.player_id) }), ...slots,
      el("span", { className: "marks", textContent: marks }));
  }));

  const r = v.result;
  $("result").replaceChildren(...(r ? [
    el("h2", { textContent: "Auflösung" }),
    el("table", {}, ...v.players.map((p) => el("tr", {},
      el("td", { textContent: name(p.player_id) }),
      el("td", { textContent: (r.hands[p.player_id] ?? []).filter(Boolean).join(" ") }),
      el("td", { textContent: `${r.scores[p.player_id]} Punkte` }),
      el("td", { textContent: r.winners.includes(p.player_id) ? "★ Gewinner" : "" }),
    ))),
  ] : []));
}

// --- Bedienelemente -------------------------------------------------------------------
$("connect-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const n = $("name").value.trim();
  if (n) client.connect(n);
});
$("join-form").addEventListener("submit", (e) => {
  e.preventDefault();
  if ($("code").value.trim()) client.joinLobby($("code").value);
});
$("create").addEventListener("click", () => client.createLobby());
$("ready").addEventListener("click", () => client.ready(true));
$("unready").addEventListener("click", () => client.ready(false));
$("leave").addEventListener("click", () => client.leaveLobby());
$("deck").addEventListener("click", () => client.drawFromDeck());
$("discard").addEventListener("click", () => client.takeFromDiscard());
$("discard-drawn").addEventListener("click", () => client.discardDrawn());
$("skip").addEventListener("click", () => client.skipSpecial());
$("knock").addEventListener("click", () => client.knock());
$("throw-mode").addEventListener("click", () => { ui.throwMode = !ui.throwMode; render(); });
$("logout").addEventListener("click", () => {
  client.disconnect();
  session.clear();
  window.location.reload();
});
// Nur die Restzeit sekündlich auffrischen; die Slots neu zu bauen würde Klicks verschlucken.
setInterval(() => { if (ui.view && ui.deadline != null) renderTurn(ui.view); }, 1000);

// Neuladen: mit gespeicherter ID automatisch zurück ins Spiel
if (client.hasSession) client.connect();
render();
