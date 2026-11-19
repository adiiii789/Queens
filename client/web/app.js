// Browser-Client für Queens: Spieltisch, Lobby und (im Tab „Server“) der Leitstand.
//
// Nur Anzeige und Klicks → Aktionen; Spielregeln prüft der Server. Die
// Oberfläche hebt lediglich hervor, was gerade sinnvoll anklickbar ist.
// Fehler des Servers erscheinen als Hinweis. Die Texte entsprechen client/render.py.

import { QueensClient, fetchConfig, inviteTargets, resolveEndpoints, session } from "./protocol.js";
import { createAdmin } from "./admin.js";

const REVEAL_MS = 3000; // angesehene Karten nur kurz aufdecken („wie am echten Tisch“)
const FLASH_MS = 1400;
const LOG_LINES = 60;
const TOAST_MS = 3500;

const ui = {
  lobby: null,        // letzte Lobby-Antwort, in der ich Mitglied bin
  view: null,         // eigene Spielersicht
  names: {},          // player_id → Name
  deadline: null,     // Zeitpunkt (ms) für die Restzeit
  timerKey: null,     // "peek-0" | "turn-<n>": Gesamtzeit nur bei neuem Zug merken
  timerTotal: null,
  selection: [],      // ausgewählte Karten für Ansehen bzw. Bube
  throwMode: false,   // nächster Klick auf eigene Karte wirft zwischen
  revealed: new Map(), // "player:slot" → {card, until, shown}
  flash: new Map(),    // "player:slot" → {kind, until}
  resultShown: null,  // game_id, dessen Auflösung schon angezeigt wurde
  log: [],
  lastAction: "",     // letzte öffentliche Aktion für die Tischmitte
  lastActionAt: 0,
  status: "getrennt",
  pendingJoin: null,  // Lobby-Code aus dem Einladungslink
};

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(window.location.search);
ui.pendingJoin = params.get("lobby")?.trim().toUpperCase() || null;

const endpoints = await resolveEndpoints();
const client = new QueensClient({ url: endpoints.ws, onMessage, onStatus });
const admin = createAdmin({
  base: endpoints.control,
  toast,
  onStatus(status) {
    // Gateway läuft (wieder): nicht auf den Backoff des Clients warten.
    const gateway = status?.services?.find((s) => s.kind === "gateway");
    if (gateway?.responding && !client.connected) client.reconnectNow();
  },
});

// --- Texte ------------------------------------------------------------------------
function name(playerId) {
  const n = ui.names[playerId] ?? ui.view?.players?.find((p) => p.player_id === playerId)?.name ?? playerId ?? "?";
  return playerId === client.playerId ? `${n} (du)` : n;
}
const ref = (r) => `${name(r.player_id)} Slot ${r.slot}`;

const LOBBY_STATUS = {
  waiting: "wartet auf Spieler", starting: "Partie startet …", started: "Partie läuft",
  finished: "Partie beendet", deleted: "aufgelöst",
};

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
  return text ?? `${event.type} ${JSON.stringify(event.data)}`;
}

// --- Karten ------------------------------------------------------------------------
const RANK_LABEL = { J: "B", Q: "D", K: "K", A: "A" };
const RANK_NAME = { J: "Bube", Q: "Dame", K: "König", A: "Ass" };
const SUIT_NAME = { "♠": "Pik", "♥": "Herz", "♦": "Karo", "♣": "Kreuz" };

function parseCard(text) {
  const suit = text.slice(-1);
  const rank = text.slice(0, -1);
  const value = rank === "Q" ? -1 : rank === "A" ? 1 : rank === "J" || rank === "K" ? 10 : Number(rank);
  return { rank, suit, value, red: suit === "♥" || suit === "♦" };
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "dataset") Object.assign(node.dataset, value);
    else if (key in node) node[key] = value;
    else node.setAttribute(key, value);
  }
  node.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return node;
}

function cardFace(text, tag = "div") {
  const c = parseCard(text);
  const label = RANK_LABEL[c.rank] ?? c.rank;
  const court = ["J", "Q", "K"].includes(c.rank);
  const points = c.value === 1 || c.value === -1 ? `${c.value === -1 ? "−1" : "1"} Punkt` : `${c.value} Punkte`;
  return el(tag, {
    className: "card face" + (c.red ? " red" : ""),
    title: `${RANK_NAME[c.rank] ?? c.rank} ${SUIT_NAME[c.suit] ?? c.suit} (${points})`,
  },
  el("span", { className: "corner" }, label, el("br"), c.suit),
  el("span", { className: "pip" + (court ? " court" : ""), textContent: court ? label : c.suit }),
  el("span", { className: "corner br" }, label, el("br"), c.suit));
}

function cardBack(tag = "div") {
  return el(tag, { className: "card back" });
}

// --- Nachrichten vom Gateway ---------------------------------------------------------
function onStatus(status, detail) {
  ui.status = status;
  const texts = {
    verbinde: "verbinde …",
    verbunden: "verbunden",
    getrennt: "getrennt",
    ersetzt: "in anderem Tab",
    abgelehnt: "abgelehnt",
  };
  const pill = $("status");
  pill.textContent = texts[status] ?? status;
  pill.className = "pill" + (status === "verbunden" ? " ok" : status === "verbinde" ? "" : " bad");
  if (status === "verbunden" && detail) toast("Wieder verbunden.", "good");
  ui.retryIn = status === "getrennt" ? detail : null;
  render();
}

function onMessage(m) {
  switch (m.op) {
    case "welcome":
      $("me").textContent = m.name;
      if (!m.reconnected && ui.pendingJoin) {
        client.joinLobby(ui.pendingJoin);
        clearInviteParam();
      }
      break;
    case "lobby": {
      const mine = m.lobby.players?.some((p) => p.player_id === client.playerId);
      ui.lobby = mine && m.lobby.status !== "deleted" ? m.lobby : null;
      break;
    }
    case "game_started":
      ui.names = m.names ?? {};
      ui.lastAction = "";
      ui.view = null;
      ui.selection = [];
      ui.throwMode = false;
      ui.revealed.clear();
      ui.flash.clear();
      ui.log = [];
      ui.pendingJoin = null;
      addLog(`Partie ${m.game_id} beginnt`);
      closeResult();
      break;
    case "events":
      m.events.forEach(onEvent);
      break;
    case "state":
      onState(m);
      break;
    case "info":
      addLog(`ℹ ${m.message}`);
      toast(m.message);
      break;
    case "error":
      toast(m.message, "error");
      $("message").textContent = m.message;
      ui.selection = [];
      break;
    default:
      addLog(`? ${JSON.stringify(m)}`);
  }
  render();
}

function onState(m) {
  const v = m.view;
  ui.view = v;
  const key = v.phase === "initial_peek" ? "peek-0" : `turn-${v.turn_number}`;
  if (m.timeout == null || v.phase === "finished") {
    ui.deadline = null;
  } else {
    ui.deadline = Date.now() + m.timeout * 1000;
    if (key !== ui.timerKey || ui.timerTotal == null || m.timeout > ui.timerTotal) ui.timerTotal = m.timeout;
  }
  ui.timerKey = key;
  ui.selection = [];
  if (v.phase === "finished") {
    ui.throwMode = false;
    if (ui.resultShown !== v.game_id) {
      ui.resultShown = v.game_id;
      setTimeout(openResult, 600);
    }
  }
  if (ui.throwMode && !canThrow(v)) ui.throwMode = false;
}

// Was in der Tischmitte als „letzte Aktion“ stehen soll (Rest nur im Verlauf)
const TABLE_EVENTS = new Set(["PlayerDrew", "CardSwapped", "CardDiscarded", "CardsSwappedByJack",
  "PlayerPeekedByKing", "SpecialSkipped", "PlayerKnocked", "ThrowInSucceeded", "ThrowInFailed",
  "PenaltyCard", "TurnTimedOut", "DeckReshuffled", "PeekTimedOut"]);

function onEvent(event) {
  const d = event.data ?? {};
  addLog(describe(event), Boolean(event.audience));
  if (!event.audience && TABLE_EVENTS.has(event.type)) {
    ui.lastAction = describe(event);
    ui.lastActionAt = Date.now();
  }
  switch (event.type) {
    case "CardsPeeked":
      for (const [slot, card] of Object.entries(d.cards)) reveal(client.playerId, Number(slot), card);
      break;
    case "CardRevealedByKing":
      reveal(d.target.player_id, d.target.slot, d.card);
      flash(d.target.player_id, d.target.slot);
      break;
    case "PlayerPeekedByKing":
      flash(d.target.player_id, d.target.slot);
      break;
    case "ThrowInFailed":
      reveal(d.player_id, d.slot, d.card); // falsch geworfen: alle sehen die Karte
      flash(d.player_id, d.slot, "bad");
      toast(`${name(d.player_id)} wirft falsch (${d.card}) – Strafkarte!`, "warn");
      break;
    case "ThrowInSucceeded":
      forget(d.player_id, d.slot);
      toast(`${name(d.player_id)} wirft ${d.card} zwischen!`, "good");
      break;
    case "PenaltyCard":
      flash(d.player_id, d.slot, "bad");
      break;
    case "CardSwapped":
      forget(d.player_id, d.slot);
      flash(d.player_id, d.slot);
      break;
    case "CardsSwappedByJack":
      forget(d.first.player_id, d.first.slot);
      forget(d.second.player_id, d.second.slot);
      flash(d.first.player_id, d.first.slot);
      flash(d.second.player_id, d.second.slot);
      break;
    case "PlayerKnocked":
      toast(`${name(d.player_id)} klopft! Letzte Runde.`, "warn");
      break;
    case "TurnStarted":
      if (d.player_id === client.playerId) toast("Du bist am Zug.", "good");
      break;
    case "DeckReshuffled":
      toast("Ablagestapel wurde neu gemischt.");
      break;
    case "TurnTimedOut":
      if (d.player_id === client.playerId) toast("Deine Zeit ist abgelaufen.", "warn");
      break;
    default:
  }
}

const key = (playerId, slot) => `${playerId}:${slot}`;

function reveal(playerId, slot, card) {
  ui.revealed.set(key(playerId, slot), { card, until: Date.now() + REVEAL_MS, shown: false });
  setTimeout(render, REVEAL_MS + 50);
}

function forget(playerId, slot) {
  ui.revealed.delete(key(playerId, slot));
}

function flash(playerId, slot, kind = "") {
  ui.flash.set(key(playerId, slot), { kind, until: Date.now() + FLASH_MS });
  setTimeout(render, FLASH_MS + 50);
}

function addLog(line, private_ = false) {
  ui.log.push({ line, private_ });
  if (ui.log.length > LOG_LINES) ui.log.shift();
}

function toast(text, kind = "") {
  const node = el("div", { className: `toast ${kind}`, textContent: text });
  $("toasts").append(node);
  while ($("toasts").children.length > 4) $("toasts").firstChild.remove();
  setTimeout(() => node.remove(), TOAST_MS);
}

// --- Was ist gerade anklickbar? ------------------------------------------------------
const me = () => client.playerId;
const isActive = (v = ui.view) => Boolean(v && v.active_player === me());
const TURN_PHASES = ["await_draw", "await_decision", "await_special"];

function canThrow(v) {
  return Boolean(v && TURN_PHASES.includes(v.phase) && v.discard_top && v.knocker !== me());
}

function slotMode(v) {
  if (!v || v.phase === "finished") return null;
  if (ui.throwMode) return "throw";
  if (v.phase === "initial_peek") return v.needs_initial_peek ? "peek" : null;
  if (!isActive(v)) return null;
  if (v.phase === "await_decision") return "swap";
  if (v.phase === "await_special") return v.pending_special; // "jack" | "king"
  return null;
}

function canClickSlot(v, playerId) {
  const mode = slotMode(v);
  if (!mode) return false;
  const own = playerId === me();
  if (mode === "jack") return playerId !== v.knocker; // Karten des Klopfenden gesperrt
  if (mode === "king") return true;
  return own;
}

// --- Klicks → Aktionen ---------------------------------------------------------------
function onSlot(playerId, slot) {
  const v = ui.view;
  const mode = slotMode(v);
  if (!mode || !canClickSlot(v, playerId)) return;
  const entry = { player_id: playerId, slot };
  if (mode === "throw") {
    client.throwIn(slot);
    ui.throwMode = false;
  } else if (mode === "peek") {
    toggle(entry);
    if (ui.selection.length === 2) client.peekInitial(ui.selection.map((s) => s.slot));
  } else if (mode === "swap") {
    client.swapWithHand(slot);
  } else if (mode === "jack") {
    toggle(entry);
    if (ui.selection.length === 2) client.useJack(...ui.selection);
  } else if (mode === "king") {
    client.useKing(entry);
  }
  render();
}

function toggle(entry) {
  const i = ui.selection.findIndex((s) => s.player_id === entry.player_id && s.slot === entry.slot);
  if (i >= 0) ui.selection.splice(i, 1); else ui.selection.push(entry);
}

function onDiscardPile() {
  const v = ui.view;
  if (!isActive(v)) return;
  if (v.phase === "await_draw") client.takeFromDiscard();
  else if (v.phase === "await_decision") client.discardDrawn();
}

function hint(v) {
  if (!v) return "";
  if (v.phase === "finished") return "Partie beendet – alle Karten liegen offen.";
  if (v.phase === "initial_peek") {
    if (!v.needs_initial_peek) return "Warte, bis alle ihre Startkarten angesehen haben …";
    return ui.selection.length ? "Noch eine Karte wählen." : "Wähle zwei deiner Karten, um sie dir einmal anzusehen.";
  }
  if (ui.throwMode) return "Zwischenwerfen: Klicke eine eigene Karte mit demselben Rang wie die Ablage.";
  if (!isActive(v)) return `${name(v.active_player)} ist am Zug. Zwischenwerfen geht jederzeit.`;
  if (v.phase === "await_draw") {
    return v.knocker ? "Zieh vom Stapel oder nimm die Ablage – letzte Runde!"
      : "Zieh vom Stapel, nimm die Ablage – oder klopfe.";
  }
  if (v.phase === "await_decision") return "Klicke eine eigene Karte zum Tauschen – oder lege die gezogene Karte ab.";
  if (v.pending_special === "jack") {
    return ui.selection.length ? "Bube: noch eine zweite Karte wählen." : "Bube: Wähle zwei beliebige Karten zum Tauschen – oder überspringe.";
  }
  return "König: Wähle eine beliebige Karte zum Ansehen – oder überspringe.";
}

// --- Anzeige ------------------------------------------------------------------------
function render() {
  const connected = client.connected;
  const v = ui.view;
  const playing = v && v.phase !== "finished";
  $("connect").hidden = connected || client.hasSession;
  $("lobby").hidden = !connected || Boolean(v);
  $("game").hidden = !v;
  $("logout").hidden = !client.hasSession;
  renderBanner();
  renderLobby();
  if (v) renderGame(v);
  $("log").replaceChildren(...ui.log.slice().reverse().map((entry) =>
    el("li", { className: entry.private_ ? "private" : "", textContent: (entry.private_ ? "🔒 " : "") + entry.line })));
  document.title = playing && isActive(v) ? "● Du bist dran – Queens" : "Queens";
}

function renderBanner() {
  const banner = $("banner");
  const show = client.hasSession && !client.connected && ["getrennt", "verbinde", "ersetzt", "abgelehnt"].includes(ui.status);
  banner.hidden = !show;
  if (!show) return;
  const texts = {
    getrennt: "Keine Verbindung zum Gateway. Neuer Versuch läuft …",
    verbinde: "Verbinde mit dem Gateway …",
    ersetzt: "Du bist in einem anderen Tab mit diesem Spieler angemeldet.",
    abgelehnt: "Anmeldung abgelehnt.",
  };
  const retry = el("button", { className: "btn small", type: "button", textContent: "Jetzt verbinden" });
  retry.addEventListener("click", () => client.connect());
  const server = el("button", { className: "btn ghost small", type: "button", textContent: "Server-Steuerung" });
  server.addEventListener("click", () => showTab("server"));
  banner.replaceChildren(texts[ui.status], ui.status !== "verbinde" ? retry : null,
    ui.status === "getrennt" ? server : null);
}

function renderLobby() {
  const l = ui.lobby;
  $("lobby-start").hidden = Boolean(l);
  $("lobby-room").hidden = !l;
  if (!l) return;
  if ($("lobby-code").textContent !== l.lobby_id) {
    $("lobby-code").textContent = l.lobby_id;
    renderInviteList();
  }
  $("lobby-status").textContent = `${LOBBY_STATUS[l.status] ?? l.status} · Instanz ${l.served_by ?? "?"}`;
  $("lobby-players").replaceChildren(...l.players.map((p) => el("li", { className: p.ready ? "ready" : "" },
    el("span", { className: "dot" }),
    el("span", { textContent: p.name + (p.player_id === client.playerId ? " (du)" : "") }),
    p.player_id === l.host_id ? el("span", { className: "tag-s", textContent: "Host" }) : null,
    el("span", { className: "tag", textContent: p.ready ? "bereit" : "wartet" }))));
  const mine = l.players.find((p) => p.player_id === client.playerId);
  const ready = $("ready");
  ready.textContent = mine?.ready ? "Doch nicht bereit" : "Bereit";
  ready.className = mine?.ready ? "btn" : "btn primary";
  ready.disabled = l.status !== "waiting";
  $("leave").disabled = l.status !== "waiting";
}

function renderSlot(v, p, slot, filled, big) {
  if (!filled) return el("div", { className: "card empty", title: "leer" });
  const k = key(p.player_id, slot);
  const now = Date.now();
  const result = v.result?.hands?.[p.player_id]?.[slot];
  const shown = ui.revealed.get(k);
  const revealedCard = shown && shown.until > now ? shown.card : null;
  const card = result ?? revealedCard;
  const clickable = canClickSlot(v, p.player_id);
  const node = card ? cardFace(card, "button") : cardBack("button");
  node.type = "button";
  node.disabled = !clickable;
  node.setAttribute("aria-label", `${name(p.player_id)}, Slot ${slot}${card ? `: ${card}` : ""}`);
  if (clickable) node.classList.add("clickable");
  const selectedAt = ui.selection.findIndex((s) => s.player_id === p.player_id && s.slot === slot);
  if (selectedAt >= 0) {
    node.classList.add("selected");
    node.append(el("span", { className: "badge", textContent: String(selectedAt + 1) }));
  }
  if (revealedCard && !result && !shown.shown) { node.classList.add("revealed"); shown.shown = true; }
  const fx = ui.flash.get(k);
  if (fx && fx.until > now) node.classList.add(fx.kind === "bad" ? "flash-bad" : "flash");
  if (big) node.append(el("span", { className: "slotno", textContent: String(slot) }));
  node.addEventListener("click", () => onSlot(p.player_id, slot));
  return node;
}

function renderSeat(v, p, big) {
  const r = v.result;
  const tags = [
    p.player_id === v.active_player ? el("span", { className: "tag-s turn", textContent: "am Zug" }) : null,
    p.player_id === v.knocker ? el("span", { className: "tag-s knock", textContent: "geklopft" }) : null,
    r?.winners?.includes(p.player_id) ? el("span", { className: "tag-s win", textContent: "★ Sieger" }) : null,
    p.player_id === v.active_player && v.someone_holds_card && p.player_id !== me()
      ? el("span", { className: "tag-s", textContent: "hält Karte" }) : null,
  ];
  return el("div", { className: "seat" + (p.player_id === v.active_player ? " active" : "") },
    el("div", { className: "seat-head" },
      el("span", { textContent: name(p.player_id) }),
      el("span", { className: "tags" }, tags)),
    el("div", { className: "seat-cards" + (ui.throwMode && p.player_id === me() ? " throwing" : "") },
      p.slots.map((filled, slot) => renderSlot(v, p, slot, filled, big))),
    r ? el("div", { className: "score", textContent: `${r.scores[p.player_id]} Punkte` }) : null);
}

function renderTimer() {
  const v = ui.view;
  const timer = $("timer");
  if (!v || ui.deadline == null || v.phase === "finished") { timer.hidden = true; return; }
  const remaining = Math.max(0, (ui.deadline - Date.now()) / 1000);
  const total = ui.timerTotal || remaining || 1;
  timer.hidden = false;
  timer.classList.toggle("low", remaining <= 10);
  $("timer-bar").style.width = `${Math.min(100, (remaining / total) * 100)}%`;
  $("timer-text").textContent = `${Math.ceil(remaining)} s`;
}

function renderGame(v) {
  // Sitzordnung: eigene Karten unten, die anderen ab dem nächsten Spieler oben
  const players = v.players;
  const mine = players.findIndex((p) => p.player_id === me());
  const others = mine < 0 ? players : [...players.slice(mine + 1), ...players.slice(0, mine)];
  $("opponents").replaceChildren(...others.map((p) => renderSeat(v, p, false)));
  $("my-seat").replaceChildren(mine >= 0 ? renderSeat(v, players[mine], true) : "");

  // Zuginfo
  const phaseText = v.phase === "initial_peek" ? "Startphase: Karten ansehen"
    : v.phase === "finished" ? "Partie beendet"
      : `Zug ${v.turn_number} · ${isActive(v) ? "Du bist am Zug" : `${name(v.active_player)} ist am Zug`}`;
  $("turn").textContent = phaseText + (v.knocker && v.phase !== "finished" ? " · letzte Runde" : "");
  renderTimer();

  // Stapel
  const active = isActive(v);
  const deck = $("deck");
  const canDraw = active && v.phase === "await_draw" && (v.deck_size > 0 || v.discard_size > 1);
  deck.disabled = !canDraw;
  deck.classList.toggle("clickable", canDraw);
  deck.classList.toggle("empty", v.deck_size === 0);
  deck.title = canDraw ? "Karte vom Ziehstapel ziehen" : "Ziehstapel";
  $("deck-label").textContent = `Ziehstapel · ${v.deck_size}`;

  const discard = $("discard");
  const canTake = active && v.phase === "await_draw" && Boolean(v.discard_top);
  const canDrop = active && v.phase === "await_decision";
  discard.replaceChildren(v.discard_top ? cardFace(v.discard_top) : el("div", { className: "card empty" }));
  discard.disabled = !(canTake || canDrop);
  discard.classList.toggle("clickable", canTake || canDrop);
  discard.title = canTake ? "Oberste Ablagekarte nehmen" : canDrop ? "Gezogene Karte ablegen" : "Ablage";
  $("discard-label").textContent = canDrop ? "hier ablegen" : `Ablage · ${v.discard_size}`;

  const drawnPile = $("drawn-pile");
  if (v.drawn_card) {
    drawnPile.hidden = false;
    $("drawn").replaceChildren(cardFace(v.drawn_card));
    $("drawn-label").textContent = "Deine gezogene Karte";
  } else if (v.someone_holds_card && v.active_player && v.phase !== "finished") {
    drawnPile.hidden = false;
    $("drawn").replaceChildren(cardBack());
    $("drawn-label").textContent = `${name(v.active_player)} hält eine Karte`;
  } else {
    drawnPile.hidden = true;
  }

  const special = $("special");
  special.hidden = v.phase !== "await_special";
  if (!special.hidden) {
    special.textContent = v.pending_special === "jack"
      ? `🗡 Bube: ${name(v.active_player)} darf zwei Karten tauschen`
      : `👑 König: ${name(v.active_player)} darf eine Karte ansehen`;
  }

  const last = $("last-action");
  if (last.textContent !== ui.lastAction) {
    last.textContent = ui.lastAction;
    last.classList.remove("fresh");
    void last.offsetWidth; // Animation neu starten
    last.classList.add("fresh");
  }

  // Knöpfe
  $("discard-drawn").hidden = !canDrop;
  $("skip").hidden = !(active && v.phase === "await_special");
  $("knock").hidden = !(active && v.phase === "await_draw" && !v.knocker);
  const throwBtn = $("throw-mode");
  throwBtn.hidden = v.phase === "finished";
  throwBtn.disabled = !canThrow(v);
  throwBtn.setAttribute("aria-pressed", String(ui.throwMode));
  throwBtn.title = v.knocker === me() ? "Nach dem Klopfen sind deine Karten gesperrt." : "Eigene Karte auf gleichrangige Ablagekarte werfen";
  $("show-result").hidden = v.phase !== "finished";
  $("new-round").hidden = v.phase !== "finished";
  $("hint").textContent = hint(v);
}

// --- Auflösung ----------------------------------------------------------------------
function openResult() {
  const v = ui.view;
  if (!v?.result) return;
  const r = v.result;
  const ranked = v.players.slice().sort((a, b) => r.scores[a.player_id] - r.scores[b.player_id]);
  const iWon = r.winners.includes(me());
  $("result-title").textContent = iWon ? "🏆 Du gewinnst!"
    : `${r.winners.map((id) => ui.names[id] ?? id).join(" & ")} gewinnt`;
  $("result").replaceChildren(...ranked.map((p, i) => el("div", {
    className: "result-row" + (r.winners.includes(p.player_id) ? " winner" : ""),
  },
  el("span", { textContent: `${i + 1}.` }),
  el("span", { textContent: name(p.player_id) + (p.player_id === v.knocker ? " ✊" : "") }),
  el("div", { className: "cards" }, (r.hands[p.player_id] ?? []).filter(Boolean).map((c) => cardFace(c))),
  el("span", { className: "pts", textContent: String(r.scores[p.player_id]) }))));
  const dialog = $("result-dialog");
  if (!dialog.open) dialog.showModal();
}

function closeResult() {
  const dialog = $("result-dialog");
  if (dialog.open) dialog.close();
}

function newRound() {
  closeResult();
  ui.view = null;
  ui.lobby = null;
  ui.log = [];
  render();
}

// --- Einladung ----------------------------------------------------------------------
// Zeigt aufs Gateway unter einer Netzwerkadresse des Hosts, nicht auf 127.0.0.1.
function inviteLink(code, base = endpoints.invite.links[0].base) {
  const url = new URL(base + "/");
  url.searchParams.set("lobby", code);
  return url.toString();
}

// Adressen frisch holen: Der Host kann inzwischen das Netz gewechselt haben.
async function refreshInvite() {
  const config = await fetchConfig();
  if (config) endpoints.invite = inviteTargets(window.location, endpoints.gateway, config);
  renderInviteList();
}

function renderInviteList() {
  const list = $("invite-list");
  if (!ui.lobby || !list) return;
  list.replaceChildren(...endpoints.invite.links.map((target) => {
    const link = inviteLink(ui.lobby.lobby_id, target.base);
    const copy = el("button", { className: "btn ghost small", type: "button", textContent: "Kopieren" });
    copy.addEventListener("click", () => copyText(link));
    return el("li", {},
      el("div", {},
        el("code", { textContent: link }),
        el("div", { className: "muted small", textContent: target.label })),
      copy);
  }));
}

async function copyText(link) {
  try {
    await navigator.clipboard.writeText(link);
    toast(`Einladungslink kopiert: ${link}`, "good");
  } catch {
    // Zwischenablage gibt es nur über https/localhost, z. B. nicht über http://<LAN-IP>
    window.prompt("Einladungslink kopieren:", link);
  }
}

const INVITE_WARNING = {
  local: "Achtung: Das Gateway ist nur auf diesem Rechner erreichbar. "
    + "Für Mitspieler das Backend mit „python run_local.py --public“ starten.",
  "no-ip": "Keine Netzwerkadresse gefunden – der Link funktioniert nur auf diesem Rechner.",
};

function clearInviteParam() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("lobby")) return;
  url.searchParams.delete("lobby");
  window.history.replaceState(null, "", url);
  ui.pendingJoin = null;
}

async function copyInvite() {
  if (!ui.lobby) return;
  await refreshInvite();
  await copyText(inviteLink(ui.lobby.lobby_id));
  if (!endpoints.invite.reachable) toast(INVITE_WARNING[endpoints.invite.reason], "warn");
  else if (endpoints.invite.links.length > 1) {
    toast("Klappt der Link nicht? Unter „Andere Adressen“ stehen alle Adressen dieses Rechners.");
  }
}

// --- Tabs ---------------------------------------------------------------------------
function showTab(tab) {
  const server = tab === "server";
  $("view-play").hidden = server;
  $("view-server").hidden = !server;
  $("tab-play").classList.toggle("active", !server);
  $("tab-server").classList.toggle("active", server);
  $("tab-play").setAttribute("aria-selected", String(!server));
  $("tab-server").setAttribute("aria-selected", String(server));
  const hash = server ? "#server" : "";
  if (window.location.hash !== hash) window.history.replaceState(null, "", hash || window.location.pathname + window.location.search);
  if (server) admin.show(); else admin.hide();
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
  $("code").value = "";
});
$("create").addEventListener("click", () => client.createLobby());
$("ready").addEventListener("click", () => {
  const mine = ui.lobby?.players.find((p) => p.player_id === client.playerId);
  client.ready(!mine?.ready);
});
$("leave").addEventListener("click", () => client.leaveLobby());
$("copy-link").addEventListener("click", copyInvite);
$("invite-more").addEventListener("toggle", () => { if ($("invite-more").open) refreshInvite(); });
$("deck").addEventListener("click", () => client.drawFromDeck());
$("discard").addEventListener("click", onDiscardPile);
$("discard-drawn").addEventListener("click", () => client.discardDrawn());
$("skip").addEventListener("click", () => client.skipSpecial());
$("knock").addEventListener("click", () => {
  if (window.confirm("Klopfen? Alle anderen spielen noch einmal, danach wird aufgedeckt.")) client.knock();
});
$("throw-mode").addEventListener("click", () => {
  ui.throwMode = !ui.throwMode;
  ui.selection = [];
  render();
});
$("show-result").addEventListener("click", openResult);
$("new-round").addEventListener("click", newRound);
$("result-close").addEventListener("click", closeResult);
$("result-new").addEventListener("click", newRound);
$("tab-play").addEventListener("click", () => showTab("play"));
$("tab-server").addEventListener("click", () => showTab("server"));
$("logout").addEventListener("click", () => {
  client.disconnect();
  session.clear();
  window.location.reload();
});
// Tastaturkürzel: dieselben Wege wie die Knöpfe, nur wenn der Knopf gerade aktiv ist.
function press(id) {
  const button = $(id);
  if (button && !button.hidden && !button.disabled) button.click();
}

document.addEventListener("keydown", (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey || e.target.closest("input, textarea, select")) return;
  if ($("game").hidden || !ui.view || $("view-play").hidden || $("result-dialog").open) return;
  const k = e.key.toLowerCase();
  if (k === "escape") {
    if (!ui.throwMode && !ui.selection.length) return;
    ui.throwMode = false;
    ui.selection = [];
    render();
  } else if (k === "z") press("deck");
  else if (k === "a") { if (ui.view.phase === "await_draw") press("discard"); }
  else if (k === "w") press("discard-drawn");
  else if (k === "s") press("skip");
  else if (k === "k") press("knock");
  else if (k === "x") press("throw-mode");
  else if (/^[0-9]$/.test(k)) {
    const mine = ui.view.players.find((p) => p.player_id === me());
    if (mine?.slots[Number(k)] && canClickSlot(ui.view, me())) onSlot(me(), Number(k));
  } else return;
  e.preventDefault();
});
// Nur die Restzeit auffrischen; die Karten neu zu bauen würde Klicks verschlucken.
setInterval(renderTimer, 250);

if (ui.pendingJoin) {
  $("connect").querySelector("p").textContent = `Du wurdest in die Lobby ${ui.pendingJoin} eingeladen. Gib deinen Namen ein.`;
}
$("name").value = client.name ?? "";
showTab(window.location.hash === "#server" ? "server" : "play");
// Neuladen: mit gespeicherter ID automatisch zurück ins Spiel
if (client.hasSession) client.connect();
render();
