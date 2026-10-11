/* Hermes Pixel Office + Operator Agent Office: dashboard tab.
 * Host shell around the Pixel Agents office (iframe: dist/app/index.html).
 * Zero-build IIFE: uses the dashboard's React via window.__HERMES_PLUGIN_SDK__.
 */
(function () {
  "use strict";
  var SDK = window.__HERMES_PLUGIN_SDK__;
  var REG = window.__HERMES_PLUGINS__;
  if (!SDK || !REG) return;

  var React = SDK.React;
  var h = React.createElement;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;
  var useMemo = SDK.hooks.useMemo;
  var useRef = SDK.hooks.useRef;

  var NAME = "pixel-office";
  var VERSION = "1.4.1";
  var API = "/api/plugins/" + NAME;
  var APP_URL = "/dashboard-plugins/" + NAME + "/dist/app/index.html?v=" + VERSION;
  var NS = "pixel-office";
  var TEAM_COLORS = ["#4f8cff", "#f5a524", "#17c964", "#f31260", "#9353d3", "#06b6d4", "#ff7a45", "#a3e635"];

  function call(path, method, body) {
    var init = { method: method || "GET", headers: { "Content-Type": "application/json" } };
    if (body !== undefined) init.body = JSON.stringify(body);
    return SDK.fetchJSON(API + path, init);
  }

  function errText(e) {
    if (!e) return "error";
    var m = e.message || String(e);
    try {
      var j = JSON.parse(m.slice(m.indexOf("{")));
      if (j && j.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_) {}
    return m;
  }

  function ago(ts) {
    if (!ts) return "";
    var s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.round(s) + "s";
    if (s < 3600) return Math.round(s / 60) + "m";
    if (s < 86400) return Math.round(s / 3600) + "h";
    return Math.round(s / 86400) + "d";
  }

  var STATUS_ORDER = { active: 0, waiting: 1, idle: 2 };
  function statusLabel(a) {
    if (a.permission) return "needs you";
    if (a.status === "active") return "working";
    if (a.status === "waiting") return a.awaiting_input ? "waiting for you" : "done";
    if (a.kanban && a.kanban.running && a.kanban.running.length) return "kanban";
    return "idle";
  }
  function statusClass(a) {
    if (a.permission) return "po-dot po-dot-perm";
    if (a.status === "active") return "po-dot po-dot-active";
    if (a.status === "waiting") return "po-dot po-dot-wait";
    return "po-dot";
  }

  // ── small UI atoms ─────────────────────────────────────────────────────────
  function Btn(props) {
    var cls = "po-btn" + (props.kind ? " po-btn-" + props.kind : "") + (props.small ? " po-btn-sm" : "");
    return h("button", { className: cls, onClick: props.onClick, disabled: props.disabled, title: props.title, type: props.type || "button" }, props.children);
  }
  function Field(props) {
    return h("label", { className: "po-field" }, h("span", { className: "po-field-label" }, props.label), props.children,
      props.hint ? h("span", { className: "po-field-hint" }, props.hint) : null);
  }
  function Modal(props) {
    useEffect(function () {
      function onKey(e) { if (e.key === "Escape") props.onClose(); }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [props.onClose]);
    return h("div", { className: "po-modal-backdrop", onMouseDown: function (e) { if (e.target === e.currentTarget) props.onClose(); } },
      h("div", { className: "po-modal", role: "dialog" },
        h("div", { className: "po-modal-head" }, h("span", null, props.title), h("button", { className: "po-x", onClick: props.onClose }, "×")),
        h("div", { className: "po-modal-body" }, props.children)));
  }
  function TeamChip(props) {
    if (!props.name) return null;
    return h("span", { className: "po-chip", style: { borderColor: props.color || "var(--color-border)", color: props.color || undefined } }, props.name);
  }

  // ── dialogs ────────────────────────────────────────────────────────────────
  // NewAgentDialog removed in 1.3.0: profile creation from the office is disabled server-side (POST /agents -> 403).

  function TeamDialog(props) {
    var t = props.team || {};
    var s = useState({
      name: t.name || "", color: t.color || TEAM_COLORS[props.teams.length % TEAM_COLORS.length],
      lead: t.lead || "", members: (t.members || []).slice(), description: t.description || "",
    });
    var f = s[0], setF = s[1];
    var b = useState(false), busy = b[0], setBusy = b[1];
    var e = useState(""), err = e[0], setErr = e[1];
    var owner = {};
    props.teams.forEach(function (x) { if (x.id !== t.id) (x.members || []).forEach(function (m) { owner[m] = x.name; }); });
    function toggle(key) {
      setF(function (o) {
        var m = o.members.indexOf(key) >= 0 ? o.members.filter(function (x) { return x !== key; }) : o.members.concat([key]);
        return Object.assign({}, o, { members: m, lead: m.indexOf(o.lead) >= 0 ? o.lead : (m[0] || "") });
      });
    }
    function submit(ev) {
      ev.preventDefault();
      setBusy(true); setErr("");
      var body = { name: f.name, color: f.color, lead: f.lead || null, members: f.members, description: f.description };
      (t.id ? call("/teams/" + t.id, "PATCH", body) : call("/teams", "POST", body))
        .then(function (r) { setBusy(false); props.onDone(r); })
        .catch(function (x) { setBusy(false); setErr(errText(x)); });
    }
    return h(Modal, { title: t.id ? "Edit team" : "New team", onClose: props.onClose },
      h("form", { onSubmit: submit, className: "po-form" },
        h(Field, { label: "Team name" }, h("input", { className: "po-input", value: f.name, autoFocus: true, required: true, maxLength: 40,
          onChange: function (ev) { var v = ev.target.value; setF(function (o) { return Object.assign({}, o, { name: v }); }); } })),
        h(Field, { label: "Color" }, h("div", { className: "po-colors" }, TEAM_COLORS.map(function (c) {
          return h("button", { key: c, type: "button", className: "po-swatch" + (f.color === c ? " po-swatch-on" : ""), style: { background: c },
            onClick: function () { setF(function (o) { return Object.assign({}, o, { color: c }); }); } });
        }))),
        h(Field, { label: "Members", hint: "An agent belongs to one team. Moving it here removes it from its old team." },
          h("div", { className: "po-member-grid" }, props.agents.map(function (a) {
            var on = f.members.indexOf(a.key) >= 0;
            return h("label", { key: a.key, className: "po-member" + (on ? " po-member-on" : "") },
              h("input", { type: "checkbox", checked: on, onChange: function () { toggle(a.key); } }),
              h("span", null, a.name), owner[a.key] && !on ? h("span", { className: "po-muted po-small" }, " · " + owner[a.key]) : null);
          }))),
        h(Field, { label: "Lead", hint: "Team tasks go to the lead. The lead fans out to members via Kanban." },
          h("select", { className: "po-input", value: f.lead, onChange: function (ev) { var v = ev.target.value; setF(function (o) { return Object.assign({}, o, { lead: v }); }); } },
            f.members.length ? null : h("option", { value: "" }, "pick members first"),
            f.members.map(function (m) { var a = props.agents.find(function (x) { return x.key === m; }); return h("option", { key: m, value: m }, a ? a.name : m); }))),
        h(Field, { label: "Purpose" }, h("input", { className: "po-input", value: f.description,
          onChange: function (ev) { var v = ev.target.value; setF(function (o) { return Object.assign({}, o, { description: v }); }); } })),
        err ? h("div", { className: "po-error" }, err) : null,
        h("div", { className: "po-actions" },
          h(Btn, { onClick: props.onClose }, "Cancel"),
          h(Btn, { kind: "primary", type: "submit", disabled: busy || !f.name.trim() }, busy ? "Saving…" : (t.id ? "Save team" : "Create team")))));
  }

  function TaskDialog(props) {
    var keyRef = useRef("k" + Date.now().toString(36) + Math.random().toString(36).slice(2));
    var s = useState({ title: "", body: "", priority: 0 });
    var f = s[0], setF = s[1];
    var b = useState(false), busy = b[0], setBusy = b[1];
    var e = useState(""), err = e[0], setErr = e[1];
    function submit(ev) {
      ev.preventDefault();
      setBusy(true); setErr("");
      call(props.path, "POST", { title: f.title, body: f.body, priority: Number(f.priority) || 0, board: props.board || null, idempotency_key: keyRef.current })
        .then(function (r) { setBusy(false); props.onDone(r); })
        .catch(function (x) { setBusy(false); setErr(errText(x)); });
    }
    return h(Modal, { title: props.title, onClose: props.onClose },
      h("form", { onSubmit: submit, className: "po-form" },
        h("p", { className: "po-muted" }, props.subtitle),
        h(Field, { label: "Title" }, h("input", { className: "po-input", value: f.title, required: true, autoFocus: true,
          onChange: function (ev) { var v = ev.target.value; setF(function (o) { return Object.assign({}, o, { title: v }); }); } })),
        h(Field, { label: "Details" }, h("textarea", { className: "po-input po-textarea", rows: 7, value: f.body,
          onChange: function (ev) { var v = ev.target.value; setF(function (o) { return Object.assign({}, o, { body: v }); }); } })),
        h(Field, { label: "Priority" }, h("select", { className: "po-input", value: f.priority,
          onChange: function (ev) { var v = ev.target.value; setF(function (o) { return Object.assign({}, o, { priority: v }); }); } },
          h("option", { value: 0 }, "normal"), h("option", { value: 1 }, "high"), h("option", { value: 2 }, "urgent"))),
        err ? h("div", { className: "po-error" }, err) : null,
        h("div", { className: "po-actions" },
          h(Btn, { onClick: props.onClose }, "Cancel"),
          h(Btn, { kind: "primary", type: "submit", disabled: busy || !f.title.trim() }, busy ? "Sending…" : "Create Kanban task"))));
  }

  // ── side panels ────────────────────────────────────────────────────────────
  function AgentRow(props) {
    var a = props.agent;
    return h("button", { className: "po-row" + (props.selected ? " po-row-on" : ""), onClick: function () { props.onSelect(a); } },
      h("span", { className: statusClass(a) }),
      h("span", { className: "po-row-main" },
        h("span", { className: "po-row-title" }, a.is_lead ? "★ " : "", a.name, h(TeamChip, { name: a.team, color: a.team_color })),
        h("span", { className: "po-row-sub" }, a.activity || statusLabel(a))),
      h("span", { className: "po-row-meta" }, statusLabel(a), h("br"), ago(a.last_at)));
  }

  function Inspector(props) {
    var a = props.agent;
    var d = useState(null), detail = d[0], setDetail = d[1];
    useEffect(function () {
      var alive = true;
      function load() { call("/agents/" + encodeURIComponent(a.key)).then(function (r) { if (alive) setDetail(r); }).catch(function () {}); }
      load();
      var t = setInterval(load, 6000);
      return function () { alive = false; clearInterval(t); };
    }, [a.key]);
    var tasks = (detail && detail.tasks) || {};
    var cols = ["running", "ready", "todo", "blocked", "review", "triage", "scheduled"];
    return h("div", { className: "po-inspector" },
      h("div", { className: "po-insp-head" },
        h("span", { className: statusClass(a) }),
        h("div", null,
          h("div", { className: "po-insp-name" }, a.is_lead ? "★ " : "", a.name),
          h("div", { className: "po-muted po-small" }, a.key, " · ", a.provider || "?", " / ", a.model || "no model")),
        h("button", { className: "po-x", onClick: props.onClose, title: "Close" }, "×")),
      a.description ? h("p", { className: "po-desc" }, a.description) : null,
      h("div", { className: "po-insp-status" },
        h("b", null, statusLabel(a)), a.activity ? " · " + a.activity : "", a.last_at ? h("span", { className: "po-muted" }, " · " + ago(a.last_at) + " ago") : null),
      h("div", { className: "po-insp-actions" },
        h(Btn, { kind: "primary", small: true, onClick: function () { props.onTask(a); } }, "Give task"),
        h("select", { className: "po-input po-input-sm", value: a.team_id || "", title: "Team",
          onChange: function (ev) { props.onSetTeam(a, ev.target.value); } },
          h("option", { value: "" }, "no team"),
          props.teams.map(function (t) { return h("option", { key: t.id, value: t.id }, "team: " + t.name); })),
        h(Btn, { small: true, onClick: function () { props.onHide(a, !a.hidden); } }, a.hidden ? "Show in office" : "Hide")),
      a.tools && a.tools.length ? h("div", { className: "po-section" }, h("div", { className: "po-section-title" }, "Now"),
        a.tools.map(function (t) { return h("div", { key: t.tool_id, className: "po-line" }, "▸ ", t.status); }),
        (a.subagents || []).map(function (s) {
          return h("div", { key: s.parent_tool_id, className: "po-line po-sub" }, "↳ ", s.label,
            s.tools.length ? h("span", { className: "po-muted" }, " — " + s.tools[s.tools.length - 1].status) : null);
        })) : null,
      h("div", { className: "po-section" }, h("div", { className: "po-section-title" }, "Kanban"),
        cols.every(function (c) { return !(tasks[c] || []).length; }) ? h("div", { className: "po-muted po-small" }, "No open tasks.") : null,
        cols.map(function (c) {
          var list = tasks[c] || [];
          if (!list.length) return null;
          return h("div", { key: c, className: "po-task-group" },
            h("div", { className: "po-task-col" }, c, " · ", list.length),
            list.slice(0, 6).map(function (t) {
              return h("a", { key: t.id, className: "po-task", href: "/kanban?task=" + encodeURIComponent(t.id) }, t.title);
            }),
            list.length > 6 ? h("div", { className: "po-muted po-small" }, "+" + (list.length - 6) + " more") : null);
        })),
      h("div", { className: "po-section" }, h("div", { className: "po-section-title" }, "Sessions"),
        ((detail && detail.activity && detail.activity.recent_sessions) || a.recent_sessions || []).slice(0, 6).map(function (s) {
          return h("a", { key: s.id, className: "po-task", href: "/chat?resume=" + encodeURIComponent(s.id), title: s.id },
            h("span", { className: "po-chip po-chip-plain" }, s.source), " ", s.title || s.id, h("span", { className: "po-muted" }, " · " + ago(s.at)));
        })),
      detail && detail.soul_excerpt ? h("details", { className: "po-section" }, h("summary", { className: "po-section-title" }, "SOUL"),
        h("pre", { className: "po-pre" }, detail.soul_excerpt)) : null);
  }

  function TeamsPanel(props) {
    var byKey = {};
    props.agents.forEach(function (a) { byKey[a.key] = a; });
    if (!props.teams.length) {
      return h("div", { className: "po-empty" },
        h("p", null, "No teams yet."),
        h("p", { className: "po-muted po-small" }, "A team groups agents in the office (paint an Area in Layout and map it to the team), shows a lead, and lets you hand one task to the whole team."),
        h(Btn, { kind: "primary", onClick: function () { props.onEdit(null); } }, "+ Team"));
    }
    return h("div", null, props.teams.map(function (t) {
      var members = (t.members || []).map(function (m) { return byKey[m]; }).filter(Boolean);
      var active = members.filter(function (m) { return m.status === "active"; }).length;
      return h("div", { key: t.id, className: "po-team", style: { borderLeftColor: t.color || "var(--color-border)" } },
        h("div", { className: "po-team-head" },
          h("span", { className: "po-team-name" }, t.name),
          h("span", { className: "po-muted po-small" }, members.length + " agents · " + active + " working")),
        t.description ? h("div", { className: "po-muted po-small" }, t.description) : null,
        h("div", { className: "po-team-members" }, members.map(function (m) {
          return h("button", { key: m.key, className: "po-member-pill", onClick: function () { props.onSelect(m); } },
            h("span", { className: statusClass(m) }), m.key === t.lead ? "★ " : "", m.name);
        })),
        h("div", { className: "po-insp-actions" },
          h(Btn, { kind: "primary", small: true, onClick: function () { props.onTask(t); }, disabled: !members.length }, "Team task"),
          h(Btn, { small: true, onClick: function () { props.onEdit(t); } }, "Edit"),
          h(Btn, { small: true, kind: "danger", onClick: function () { props.onDelete(t); } }, "Delete")));
    }));
  }


  // ══ Operator Agent Office (office.v1) ═══════════════════════════════════
  // Rooms, scoped boards, task inspector, approvals, incidents, live events.
  // Authority: Hermes Kanban + profiles. This layer renders + forwards only.

  var STATE_LABEL = {
    working: "working", blocked: "blocked", awaiting_review: "awaiting review", completed: "completed",
    idle: "idle", offline: "offline", unknown: "unknown", queued: "queued",
  };
  var TASK_GROUPS = ["working", "awaiting_review", "blocked", "queued", "completed"];
  var CHAR_URL = "/dashboard-plugins/" + NAME + "/dist/app/assets/characters/char_";

  function enc(s) { return encodeURIComponent(s); }
  function uid() {
    try { return crypto.randomUUID(); } catch (_) { return "k" + Date.now().toString(36) + Math.random().toString(36).slice(2); }
  }
  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    return d.toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  function StateTag(props) {
    var s = props.state || "unknown";
    return h("span", { className: "oa-tag oa-st-" + s, title: props.title || s }, h("i", { className: "oa-led" }), STATE_LABEL[s] || s);
  }

  // Normalize one Kanban WS event to the OfficeEvent contract (mirror of office_control.office_event).
  var SEVERITY = { crashed: "error", failed: "error", timed_out: "error", gave_up: "error", blocked: "warn", review_requested: "info", completed: "ok" };
  function normEvent(e, board) {
    var p = {};
    var raw = e.payload && typeof e.payload === "object" ? e.payload : {};
    Object.keys(raw).forEach(function (k) {
      if (k === "tenant" || k === "body" || k === "result" || k === "summary") return;
      p[k] = raw[k];
    });
    return { kind: "OfficeEvent", id: "kanban:" + board + ":" + e.id, seq: e.id, source: "hermes.kanban", board: board,
      type: e.kind || "unknown", severity: SEVERITY[e.kind] || "info", ts: e.created_at,
      correlation: { task_id: e.task_id, run_id: e.run_id }, payload: p };
  }

  // Snapshot + live stream with replay. Snapshot is authoritative; WS only
  // tells us *when* to reconcile, and feeds the event log.
  function useOffice(board) {
    var st = useState(null), state = st[0], setState = st[1];
    var er = useState(""), err = er[0], setErr = er[1];
    var cn = useState("idle"), conn = cn[0], setConn = cn[1];
    var fd = useState([]), feed = fd[0], setFeed = fd[1];
    var lu = useState(0), lastOk = lu[0], setLastOk = lu[1];
    var cursorRef = useRef(null);
    var listeners = useRef([]);
    var refreshT = useRef(null);

    // Overlay the approval-store queue onto the live os.approvals list.
    // We do this in-place by mutating each pending approval so ApprovalBox sees
    // notify_state / decided_via without an extra render pass. The matched
    // store row key is (board, task_id, kind).
    var queueDecidedRef = useRef([]);
    function _merge_approvals(q) {
      var list = (state && state.approvals) || [];
      var pend = q.pending || [];
      var byKey = {};
      for (var i = 0; i < pend.length; i++) {
        var p = pend[i];
        byKey[p.board + "\x1f" + p.task_id + "\x1f" + p.kind] = p;
      }
      for (var j = 0; j < list.length; j++) {
        var a = list[j];
        var tgt = a.target || {};
        var key = (a.source || "").replace(/^hermes\./, "") + "\x1f" + (tgt.board || "") + "\x1f" + (tgt.task_id || "");
        var hit = byKey[key] || byKey[tgt.board + "\x1f" + tgt.task_id + "\x1f" + (a.source || "").replace(/^hermes\./, "")];
        if (hit) {
          a.notify_state = hit.notify_state;
          a.decided_via = hit.decided_via;
          a.via_badge = (hit.decided_via || "office").split(":", 1)[0];
          a.decided_by = hit.decided_by;
          a.decided_by_label = hit.decided_by_label || hit.decided_by || "";
          a.store_status = hit.status;
          a.approval_id = hit.id;
        }
      }
      queueDecidedRef.current = q.decided || [];
    }

    var refresh = useCallback(function () {
      if (!board) return Promise.resolve(null);
      var stateP = call("/office/state?board=" + enc(board)).then(function (s) {
        setState(s); setErr(""); setLastOk(Date.now()); return s;
      }).catch(function (e) { setErr(errText(e)); throw e; });
      // Overlay the approval store (Telegram sync / decided rows) onto the live
      // Kanban approvals. Tolerates an unreachable store so the dashboard still
      // works when the hermes home has not been initialised.
      var storeP = call("/office/approvals/queue?board=" + enc(board) + "&limit=50")
        .then(function (q) { if (q && Array.isArray(q.pending)) _merge_approvals(q); return q; })
        .catch(function () { return null; });
      return Promise.all([stateP, storeP]).then(function (r) { return r[0]; });
    }, [board]);

    var soon = useCallback(function () {
      if (refreshT.current) return;
      refreshT.current = setTimeout(function () { refreshT.current = null; refresh().catch(function () {}); }, 350);
    }, [refresh]);

    useEffect(function () {
      if (!board) return undefined;
      var dead = false, ws = null, attempt = 0, retryT = null;
      cursorRef.current = null;
      setState(null); setFeed([]);
      function schedule() {
        attempt += 1;
        setConn(attempt > 3 ? "offline" : "reconnecting");
        retryT = setTimeout(connect, Math.min(15000, 500 * Math.pow(2, attempt)));
      }
      function connect() {
        if (dead) return;
        setConn(attempt ? "reconnecting" : "connecting");
        refresh().catch(function () { return null; }).then(function (s) {
          if (dead) return null;
          // Replay from the last processed cursor so the feed has no gap; first connect starts at the snapshot cursor.
          if (cursorRef.current == null && s && typeof s.cursor === "number") cursorRef.current = s.cursor;
          var params = { board: board };
          if (cursorRef.current != null) params.since = String(cursorRef.current);
          return SDK.buildWsUrl("/api/plugins/kanban/events", params);
        }).then(function (url) {
          if (dead || !url) { if (!dead) schedule(); return; }
          ws = new WebSocket(url);
          ws.onopen = function () { attempt = 0; setConn("live"); };
          ws.onmessage = function (m) {
            var d;
            try { d = JSON.parse(m.data); } catch (_) { return; }
            if (typeof d.cursor === "number") cursorRef.current = d.cursor;
            var evs = (d.events || []).map(function (e) { return normEvent(e, board); });
            if (!evs.length) return;
            setFeed(function (f) {
              var seen = {};
              return evs.slice().reverse().concat(f).filter(function (x) { if (seen[x.id]) return false; seen[x.id] = 1; return true; }).slice(0, 400);
            });
            listeners.current.forEach(function (fn) { try { fn(evs); } catch (_) {} });
            soon();
          };
          ws.onerror = function () { try { ws.close(); } catch (_) {} };
          ws.onclose = function () { if (!dead) schedule(); };
        }).catch(function () { if (!dead) schedule(); });
      }
      connect();
      var poll = setInterval(function () { refresh().catch(function () { setConn(function (c) { return c === "live" ? "degraded" : "offline"; }); }); }, 10000);
      return function () {
        dead = true; clearTimeout(retryT); clearInterval(poll);
        if (ws) { ws.onclose = null; try { ws.close(); } catch (_) {} }
      };
    }, [board, refresh, soon]);

    function subscribe(fn) {
      listeners.current.push(fn);
      return function () { listeners.current = listeners.current.filter(function (x) { return x !== fn; }); };
    }
    return { state: state, err: err, conn: conn, feed: feed, cursor: cursorRef.current, lastOk: lastOk, refresh: refresh, subscribe: subscribe, queueDecided: queueDecidedRef.current || [] };
  }

  function Sprite(props) {
    var pal = props.palette != null ? props.palette : ((props.id || 0) % 6);
    return h("span", { className: "oa-sprite oa-sprite-" + (props.state || "idle"), "aria-hidden": "true",
      style: { backgroundImage: "url(" + CHAR_URL + pal + ".png)" } });
  }

  function ConnPill(props) {
    var c = props.conn;
    var txt = { live: "LIVE", connecting: "CONNECTING", reconnecting: "RECONNECTING", degraded: "DEGRADED", offline: "OFFLINE", idle: "NO BOARD" }[c] || c;
    return h("span", { className: "oa-conn oa-conn-" + c, role: "status", "aria-live": "polite",
      title: "Kanban event stream" + (props.cursor != null ? " · cursor " + props.cursor : "") + (props.lastOk ? " · snapshot " + ago(props.lastOk / 1000) + " ago" : "") },
      h("i", { className: "oa-led" }), txt, props.cursor != null ? h("span", { className: "oa-dim" }, " #" + props.cursor) : null);
  }

  // ── floor plan ────────────────────────────────────────────────────────────
  function FloorPlan(props) {
    var all = props.rooms || [];
    var rooms = all.filter(function (r) { return !r.locked; });
    var restricted = all.filter(function (r) { return r.locked; });
    var refs = useRef([]);
    function onKey(e, i) {
      var col = i % 3, row = Math.floor(i / 3), n = null;
      if (e.key === "ArrowRight") n = row * 3 + Math.min(2, col + 1);
      else if (e.key === "ArrowLeft") n = row * 3 + Math.max(0, col - 1);
      else if (e.key === "ArrowDown") n = Math.min(rooms.length - 1, i + 3);
      else if (e.key === "ArrowUp") n = Math.max(0, i - 3);
      if (n != null) { e.preventDefault(); var el = refs.current[n]; if (el) el.focus(); }
    }
    return h("nav", { className: "oa-floor", "aria-label": "Office floor plan" },
      h("div", { className: "oa-floor-title" }, "FLOOR 1 · ", h("span", { className: "oa-dim" }, props.board || "no board"),
        h("span", { className: "oa-legend", title: "Presence truthfulness" }, h("b", { className: "oa-exec oa-exec-kanban_worker" }, "◉"), " Kanban worker · ",
          h("b", { className: "oa-exec oa-exec-session_activity" }, "○"), " session (inferred)")),
      h("div", { className: "oa-floor-grid" }, rooms.map(function (r, i) {
        var residents = (props.agents || []).filter(function (a) { return a.room === r.id; });
        var c = r.counts || {};
        var hits = props.matchCount ? props.matchCount[r.id] || 0 : null;
        return h("button", {
          key: r.id, ref: function (el) { refs.current[i] = el; },
          className: "oa-room" + (props.selected === r.id ? " oa-room-on" : "") + (r.locked ? " oa-room-locked" : "") + (hits === 0 ? " oa-room-dim" : ""),
          style: { "--room": r.color },
          onClick: function () { props.onSelect(r.id); },
          onKeyDown: function (e) { onKey(e, i); },
          "aria-pressed": props.selected === r.id,
          "aria-label": r.name + (r.locked ? ", locked" : "") + ", " + (c.working || 0) + " working, " + (c.awaiting_review || 0) + " in review, " + (c.blocked || 0) + " blocked",
          title: r.purpose,
        },
          h("div", { className: "oa-room-head" },
            h("span", { className: "oa-room-glyph" }, r.glyph),
            h("span", { className: "oa-room-name" }, r.name),
            h("span", { className: "oa-room-key" }, String(i + 1))),
          r.locked
            ? h("div", { className: "oa-room-lock" }, "LOCKED", r.withheld ? h("span", { className: "oa-dim" }, " · " + r.withheld + " withheld") : null)
            : h("div", { className: "oa-room-floor" },
                residents.length ? residents.map(function (a) {
                  var ex = a.presence.execution;
                  return h("span", { key: a.profile, className: "oa-res", title: a.name + " · " + (STATE_LABEL[a.presence.state] || a.presence.state) + " · " + execLabel(ex) },
                    h(Sprite, { id: a.character_id, palette: props.palettes[a.character_id], state: a.presence.state }),
                    h("i", { className: "oa-led oa-st-" + a.presence.state }),
                    ex ? h("b", { className: "oa-exec oa-exec-" + ex }, ex === "kanban_worker" ? "◉" : "○") : null);
                }) : h("span", { className: "oa-dim oa-small" }, "no resident profile")),
          r.locked ? null : h("div", { className: "oa-room-counts" },
            c.working ? h("span", { className: "oa-cnt oa-st-working" }, "▶" + c.working) : null,
            c.awaiting_review ? h("span", { className: "oa-cnt oa-st-awaiting_review" }, "◎" + c.awaiting_review) : null,
            c.blocked ? h("span", { className: "oa-cnt oa-st-blocked" }, "■" + c.blocked) : null,
            c.queued ? h("span", { className: "oa-cnt oa-st-queued" }, "·" + c.queued) : null,
            c.completed ? h("span", { className: "oa-cnt oa-st-completed" }, "✓" + c.completed) : null,
            r.incidents ? h("span", { className: "oa-badge oa-badge-inc" }, "!" + r.incidents) : null,
            r.approvals ? h("span", { className: "oa-badge oa-badge-appr" }, "?" + r.approvals) : null,
            hits ? h("span", { className: "oa-badge" }, "⌕" + hits) : null));
      })),
      restricted.length ? h("div", { className: "oa-restricted" }, restricted.map(function (r) {
        return h("button", { key: r.id, className: "oa-room-locked oa-restricted-item" + (props.selected === r.id ? " oa-room-on" : ""),
          onClick: function () { props.onSelect(r.id); }, title: r.purpose, "aria-label": r.name + ", locked, restricted area" },
          h("span", { className: "oa-room-glyph" }, r.glyph), " ", r.name, h("b", null, " · LOCKED"),
          r.withheld ? h("span", { className: "oa-dim" }, " · " + r.withheld + " withheld") : null);
      })) : null);
  }
  function execLabel(ex) {
    return ex === "kanban_worker" ? "confirmed: live Kanban worker" : ex === "session_activity" ? "inferred: session activity (not a Kanban run)" : "no live execution signal";
  }

  function TaskCard(props) {
    var t = props.task;
    return h("button", { className: "oa-card" + (props.selected ? " oa-card-on" : ""), onClick: function () { props.onOpen(t); },
      title: t.id + " · " + t.status + " · route: " + t.route_reason },
      h("div", { className: "oa-card-top" }, h(StateTag, { state: t.state, title: "raw status: " + t.status }),
        h("span", { className: "oa-dim oa-mono" }, t.id), t.priority ? h("span", { className: "oa-prio" }, "P" + t.priority) : null),
      h("div", { className: "oa-card-title" }, t.title),
      h("div", { className: "oa-card-meta" }, t.assignee ? "@" + t.assignee : "unassigned",
        t.tenant ? h("span", { className: "oa-dim" }, " · " + t.tenant) : null,
        h("span", { className: "oa-dim" }, " · " + ago(t.completed_at || t.started_at || t.created_at))));
  }

  function RoomPanel(props) {
    var r = props.room;
    if (!r) return h("div", { className: "oa-empty" }, "Pick a room on the floor plan (keys 1-9).");
    var tasks = props.tasks.filter(function (t) { return t.room === r.id; });
    var residents = props.agents.filter(function (a) { return a.room === r.id; });
    if (r.locked) {
      return h("div", { className: "oa-pane" },
        h("div", { className: "oa-pane-head", style: { "--room": r.color } }, h("span", { className: "oa-room-glyph" }, r.glyph), r.name),
        h("p", { className: "oa-locked-note" }, r.purpose),
        h("ul", { className: "oa-list" },
          h("li", null, r.withheld + " task(s) matched client markers and are withheld from this personal office (projection filter, not isolation)."),
          h("li", null, "Unlock path: dedicated board + dedicated profile + separate HERMES_HOME + separate credentials + server-side authorization. Operator decision, see mission blockers."),
          h("li", null, "Direct API access to a withheld task, artifact, attachment or mission returns 403.")));
    }
    return h("div", { className: "oa-pane" },
      h("div", { className: "oa-pane-head", style: { "--room": r.color } }, h("span", { className: "oa-room-glyph" }, r.glyph), r.name,
        h("span", { className: "oa-dim oa-small" }, " · " + tasks.length + " tasks shown")),
      h("p", { className: "oa-muted oa-small" }, r.purpose),
      h("div", { className: "oa-residents" },
        residents.length ? residents.map(function (a) {
          return h("button", { key: a.profile, className: "oa-res-pill", onClick: function () { props.onAgent(a); } },
            h(Sprite, { id: a.character_id, palette: props.palettes[a.character_id], state: a.presence.state }),
            h("span", null, a.name), h(StateTag, { state: a.presence.state, title: execLabel(a.presence.execution) + " · " + ((a.presence.evidence || []).join(" · ") || "no live signal") }),
            a.presence.execution ? h("span", { className: "oa-dim oa-small" }, a.presence.execution === "kanban_worker" ? "◉ Kanban" : "○ session") : null);
        }) : h("span", { className: "oa-dim oa-small" }, "No resident profile. Tasks are routed here by tenant/keyword."),
        r.missing_residents && r.missing_residents.length ? h("span", { className: "oa-dim oa-small" }, " missing: " + r.missing_residents.join(", ")) : null),
      residents.length ? h("div", { className: "oa-actions" },
        h(Btn, { kind: "primary", small: true, onClick: function () { props.onGive(r, residents); } }, "Give task in " + r.name)) : null,
      TASK_GROUPS.map(function (g) {
        var list = tasks.filter(function (t) { return t.state === g || (g === "queued" && t.state === "unknown"); });
        if (!list.length) return null;
        return h("section", { key: g, className: "oa-group" },
          h("div", { className: "oa-group-title" }, h(StateTag, { state: g }), h("span", { className: "oa-dim" }, list.length)),
          list.slice(0, g === "completed" ? 8 : 40).map(function (t) {
            return h(TaskCard, { key: t.id, task: t, selected: props.selectedTask === t.id, onOpen: props.onOpenTask });
          }));
      }),
      !tasks.length ? h("div", { className: "oa-empty" }, props.query ? "No tasks match the filter." : "No open or recent tasks in this room.") : null);
  }

  var DECISION_LABEL = { approve: "Approve", reject: "Reject", request_changes: "Request changes" };
  function ApprovalBox(props) {
    var a = props.approval;
    var nt = useState(""), note = nt[0], setNote = nt[1];
    var cf = useState(null), confirm = cf[0], setConfirm = cf[1];
    var bz = useState(false), busy = bz[0], setBusy = bz[1];
    var er = useState(""), err = er[0], setErr = er[1];
    var keyRef = useRef(uid());
    function submit() {
      setBusy(true); setErr("");
      call("/office/approvals/decision", "POST", {
        board: a.target.board, task_id: a.target.task_id, request_id: a.id, decision: confirm,
        token: a.tokens[confirm], fingerprint: a.params.fingerprint, expires_at: a.expires_at,
        idempotency_key: keyRef.current, note: note, confirm: true,
      }).then(function (r) { setBusy(false); setConfirm(null); props.onDone(r); })
        .catch(function (e) { setBusy(false); setErr(errText(e)); });
    }
    var expired = Date.now() / 1000 > a.expires_at;
    var ns = a.notify_state || (a.store_status === "pending" ? "pending" : null);
    var dv = a.decided_via || a.via_badge || null;
    var db = a.decided_by_label || a.decided_by || null;
    return h("div", { className: "oa-approval" },
      h("div", { className: "oa-approval-head" }, h(StateTag, { state: a.params.expected_status === "blocked" ? "blocked" : "awaiting_review", title: a.source }),
        h("button", { className: "oa-linkish", onClick: function () { props.onOpenTask(a.target.task_id); } }, a.title),
        ns ? h("span", { className: "oa-badge oa-badge-tg oa-badge-tg-" + (ns === "sent" ? "ok" : ns === "failed" ? "err" : "pending"), title: "Telegram notification state: " + ns }, ns) : null,
        dv ? h("span", { className: "oa-badge oa-badge-via oa-badge-via-" + dv, title: "decided via " + dv + (db ? " by " + db : "") }, dv + (db ? " · " + db : "")) : null),
      a.summary ? h("div", { className: "oa-quote" }, a.summary) : null,
      h("div", { className: "oa-dim oa-small oa-mono" }, a.target.task_id, " · @", a.target.assignee || "?", " · expires ", fmtTime(a.expires_at)),
      h("div", { className: "oa-dim oa-small" }, a.kind === "ApprovalRequest" ? a.source : "", " · state ", h("b", { className: "oa-mono" }, a.params.expected_status)),
      h("textarea", { className: "po-input po-textarea", rows: 2, placeholder: "Note to the worker (required to reject / request changes)", value: note,
        onChange: function (e) { setNote(e.target.value); }, "aria-label": "Decision note" }),
      h("div", { className: "oa-actions" },
        a.allowed_decisions.map(function (dcs) {
          var needsNote = dcs !== "approve";
          return h(Btn, { key: dcs, kind: dcs === "approve" ? "primary" : (dcs === "reject" ? "danger" : undefined), small: true,
            disabled: busy || expired || (needsNote && !note.trim()), title: (a.effects || {})[dcs],
            onClick: function () { setConfirm(dcs); } }, DECISION_LABEL[dcs] || dcs);
        }),
        expired ? h("span", { className: "oa-dim oa-small" }, "expired: refresh") : null),
      err ? h("div", { className: "po-error" }, err) : null,
      confirm ? h(Modal, { title: "Confirm operator decision", onClose: function () { setConfirm(null); } },
        h("div", { className: "po-form" },
          h("p", { className: "po-muted" }, "This decision is bound to the exact action, target, state and expiry below. If the task changed since you opened it, Hermes refuses it."),
          h("table", { className: "oa-kv" }, h("tbody", null,
            h("tr", null, h("th", null, "action"), h("td", null, a.action + " → " + confirm)),
            h("tr", null, h("th", null, "target"), h("td", { className: "oa-mono" }, a.target.board + " / " + a.target.task_id)),
            h("tr", null, h("th", null, "expected"), h("td", { className: "oa-mono" }, "status=" + a.params.expected_status + " · fp=" + a.params.fingerprint)),
            h("tr", null, h("th", null, "effect"), h("td", null, (a.effects || {})[confirm] || confirm)),
            h("tr", null, h("th", null, "expires"), h("td", null, fmtTime(a.expires_at))),
            note.trim() ? h("tr", null, h("th", null, "note"), h("td", null, note)) : null)),
          err ? h("div", { className: "po-error" }, err) : null,
          h("div", { className: "po-actions" },
            h(Btn, { onClick: function () { setConfirm(null); } }, "Cancel"),
            h(Btn, { kind: confirm === "approve" ? "primary" : "danger", disabled: busy, onClick: submit }, busy ? "Applying…" : "Confirm " + confirm.replace("_", " "))))) : null);
  }

  function TaskPanel(props) {
    var id = props.taskId, board = props.board;
    var d = useState(null), detail = d[0], setDetail = d[1];
    var e = useState(""), err = e[0], setErr = e[1];
    var sv = useState("timeline"), view = sv[0], setView = sv[1];
    var av = useState(null), art = av[0], setArt = av[1];
    var load = useCallback(function () {
      if (!id) return;
      call("/office/tasks/" + enc(id) + "?board=" + enc(board)).then(function (r) { setDetail(r); setErr(""); })
        .catch(function (x) { setErr(errText(x)); });
    }, [id, board]);
    useEffect(function () { setDetail(null); setArt(null); load(); }, [load]);
    useEffect(function () {
      return props.subscribe(function (evs) { if (evs.some(function (x) { return x.correlation.task_id === id; })) load(); });
    }, [id, load]);
    useEffect(function () {
      var running = detail && detail.task.status === "running";
      if (!running) return undefined;
      var t = setInterval(load, 6000);
      return function () { clearInterval(t); };
    }, [detail && detail.task.status, load]);

    if (!id) return h("div", { className: "oa-empty" }, "Open a task from a room board, approval or event.");
    if (err) return h("div", { className: "oa-pane" }, h("div", { className: "po-error" }, err), h(Btn, { small: true, onClick: load }, "Retry"));
    if (!detail) return h("div", { className: "oa-empty" }, "Loading ", id, "…");
    var t = detail.task;
    function openAttachment(a) {
      setArt({ name: a.name, loading: true });
      call("/office/tasks/" + enc(id) + "/attachments/" + a.id + "?board=" + enc(board))
        .then(function (r) { setArt(r); }).catch(function (x) { setArt({ name: a.name, error: errText(x) }); });
    }
    function openArtifact(a) {
      setArt({ name: a.name, loading: true });
      call("/office/tasks/" + enc(id) + "/artifact?board=" + enc(board) + "&path=" + enc(a.name))
        .then(function (r) { setArt(r); }).catch(function (x) { setArt({ name: a.name, error: errText(x) }); });
    }
    var views = [["timeline", "Timeline · " + detail.events.length], ["artifacts", "Artifacts · " + (detail.workspace_artifacts.length + detail.attachments.length)],
      ["log", "Log"], ["runs", "Runs · " + detail.runs.length], ["comments", "Notes · " + detail.comments.length]];
    var body;
    if (view === "timeline") {
      body = h("ol", { className: "oa-timeline" }, detail.events.slice().reverse().map(function (ev) {
        return h("li", { key: ev.id, className: "oa-ev oa-sev-" + ev.severity },
          h("span", { className: "oa-ev-type" }, ev.type), h("span", { className: "oa-dim oa-small" }, fmtTime(ev.ts)),
          ev.correlation.run_id ? h("span", { className: "oa-dim oa-small" }, " · run " + ev.correlation.run_id) : null,
          Object.keys(ev.payload || {}).length ? h("div", { className: "oa-ev-payload oa-mono" }, JSON.stringify(ev.payload)) : null);
      }));
    } else if (view === "artifacts") {
      body = h("div", null,
        detail.missions.length ? h("div", { className: "oa-sub" }, h("div", { className: "oa-sub-title" }, "Linked missions"),
          detail.missions.map(function (m) { return h("button", { key: m, className: "oa-linkish oa-mono", onClick: function () { props.onMission(m); } }, m); })) : null,
        h("div", { className: "oa-sub" }, h("div", { className: "oa-sub-title" }, "Workspace files", t.workspace_path ? h("span", { className: "oa-dim oa-small oa-mono" }, " " + t.workspace_path) : null),
          detail.workspace_artifacts.length ? detail.workspace_artifacts.map(function (a) {
            return h("button", { key: a.name, className: "oa-file", disabled: !a.readable, onClick: function () { openArtifact(a); } },
              h("span", { className: "oa-mono" }, a.name), h("span", { className: "oa-dim" }, (a.size_bytes / 1024).toFixed(1) + " KB"));
          }) : h("div", { className: "oa-dim oa-small" }, t.workspace_kind === "scratch" || !t.workspace_path ? "No files in the task workspace." : "Workspace outside the Kanban root: not served.")),
        detail.attachments.length ? h("div", { className: "oa-sub" }, h("div", { className: "oa-sub-title" }, "Preserved artifacts (Kanban attachments)"),
          detail.attachments.map(function (a) {
            return h("button", { key: a.id, className: "oa-file", disabled: !a.readable, title: a.readable ? "Preview" : "Binary: open in Kanban",
              onClick: function () { openAttachment(a); } }, h("span", { className: "oa-mono" }, a.name), h("span", { className: "oa-dim" }, ((a.size_bytes || 0) / 1024).toFixed(1) + " KB"));
          })) : null,
        detail.declared_artifacts && detail.declared_artifacts.length ? h("div", { className: "oa-sub" }, h("div", { className: "oa-sub-title" }, "Declared by worker"),
          detail.declared_artifacts.map(function (a) { return h("div", { key: a.path, className: "oa-mono oa-small" }, (a.preserved ? "✓ " : "· ") + a.path); })) : null,
        t.result ? h("div", { className: "oa-sub" }, h("div", { className: "oa-sub-title" }, "Result"), h("pre", { className: "oa-pre" }, t.result)) : null,
        art ? h("div", { className: "oa-sub" }, h("div", { className: "oa-sub-title" }, "Preview · ", h("span", { className: "oa-mono" }, art.name),
            art.truncated ? h("span", { className: "oa-dim" }, " (truncated)") : null),
          art.loading ? h("div", { className: "oa-dim" }, "Loading…") : art.error ? h("div", { className: "po-error" }, art.error) : h("pre", { className: "oa-pre" }, art.content)) : null);
    } else if (view === "log") {
      body = detail.log.exists ? h("div", null, h("div", { className: "oa-dim oa-small" }, (detail.log.size_bytes / 1024).toFixed(1) + " KB · tail · secrets redacted"),
        h("pre", { className: "oa-pre oa-log" }, detail.log.content)) : h("div", { className: "oa-dim" }, "No worker log (task never spawned a worker).");
    } else if (view === "runs") {
      body = detail.runs.length ? detail.runs.slice().reverse().map(function (r) {
        return h("div", { key: r.id, className: "oa-run" },
          h("div", null, h("b", null, "run " + r.id), " · @" + (r.profile || "?") + " · " + (r.outcome || r.status),
            h("span", { className: "oa-dim oa-small" }, " · " + fmtTime(r.started_at) + (r.ended_at ? " → " + fmtTime(r.ended_at) : " → running"))),
          r.summary ? h("div", { className: "oa-quote" }, r.summary) : null,
          r.error ? h("div", { className: "po-error" }, r.error) : null);
      }) : h("div", { className: "oa-dim" }, "No runs yet.");
    } else {
      body = detail.comments.length ? detail.comments.map(function (c) {
        return h("div", { key: c.id, className: "oa-run" }, h("div", { className: "oa-dim oa-small" }, (c.author || "?") + " · " + fmtTime(c.created_at)), h("pre", { className: "oa-pre" }, c.body));
      }) : h("div", { className: "oa-dim" }, "No comments.");
    }
    return h("div", { className: "oa-pane" },
      h("div", { className: "oa-pane-head" }, h(StateTag, { state: t.state, title: "raw status: " + t.status }), h("span", { className: "oa-mono oa-dim" }, t.id),
        h("button", { className: "po-x", onClick: props.onClose, title: "Close (Esc)", "aria-label": "Close task" }, "×")),
      h("div", { className: "oa-task-title" }, t.title),
      h("div", { className: "oa-kvline" },
        h("span", null, "raw ", h("b", { className: "oa-mono" }, t.status)), h("span", null, "@", t.assignee || "unassigned"),
        h("span", null, "room ", h("b", null, t.room)), h("span", { className: "oa-dim" }, "route: " + t.route_reason),
        t.tenant ? h("span", { className: "oa-dim" }, "tenant " + t.tenant) : null),
      t.latest_summary ? h("div", { className: "oa-quote" }, t.latest_summary) : null,
      t.last_failure_error ? h("div", { className: "po-error" }, t.last_failure_error) : null,
      detail.approval ? h(ApprovalBox, { approval: detail.approval, onOpenTask: function () {}, onDone: function (r) { props.onDecided(r); load(); } }) : null,
      h("div", { className: "oa-subtabs", role: "tablist" }, views.map(function (v) {
        return h("button", { key: v[0], role: "tab", "aria-selected": view === v[0], className: "oa-subtab" + (view === v[0] ? " oa-subtab-on" : ""), onClick: function () { setView(v[0]); } }, v[1]);
      })),
      body,
      t.body ? h("details", { className: "oa-sub" }, h("summary", { className: "oa-sub-title" }, "Task body (text only, never executed)"), h("pre", { className: "oa-pre" }, t.body)) : null,
      h("div", { className: "oa-actions" }, h("a", { className: "po-btn po-btn-sm", href: "/kanban?task=" + enc(t.id) }, "Open in Kanban")));
  }

  function MissionsPanel(props) {
    var m = useState(null), list = m[0], setList = m[1];
    var a = useState(null), art = a[0], setArt = a[1];
    var e = useState(""), err = e[0], setErr = e[1];
    useEffect(function () { call("/office/missions").then(function (r) { setList(r.missions); }).catch(function (x) { setErr(errText(x)); }); }, []);
    function open(slug, name) {
      setArt({ mission: slug, name: name, loading: true });
      call("/office/missions/" + enc(slug) + "/" + enc(name)).then(setArt).catch(function (x) { setArt({ mission: slug, name: name, error: errText(x) }); });
    }
    useEffect(function () { if (props.focus) open(props.focus, "final-report.md"); }, [props.focus]);
    if (err) return h("div", { className: "po-error" }, err);
    if (!list) return h("div", { className: "oa-empty" }, "Loading missions…");
    var q = (props.query || "").toLowerCase();
    var shown = list.filter(function (x) { return !q || x.slug.indexOf(q) >= 0; });
    return h("div", { className: "oa-pane" },
      h("p", { className: "oa-muted oa-small" }, "Workspace OS missions (.project-state). Read-only. Workspace OS stays the authority."),
      art ? h("div", { className: "oa-sub" },
        h("div", { className: "oa-sub-title" }, h("span", { className: "oa-mono" }, art.mission), h("button", { className: "po-x", onClick: function () { setArt(null); }, "aria-label": "Close artifact" }, "×")),
        h("div", { className: "oa-subtabs" }, ["source-task.md", "progress.md", "decisions.md", "blockers.md", "artifacts.md", "environment.md", "execution-log.md", "final-report.md"].map(function (n) {
          return h("button", { key: n, className: "oa-subtab" + (art.name === n ? " oa-subtab-on" : ""), onClick: function () { open(art.mission, n); } }, n.replace(".md", ""));
        })),
        art.loading ? h("div", { className: "oa-dim" }, "Loading…") : art.error ? h("div", { className: "po-error" }, art.error) : h("pre", { className: "oa-pre" }, art.content)) : null,
      shown.map(function (x) {
        return h("button", { key: x.slug, className: "oa-mission", onClick: function () { open(x.slug, "progress.md"); } },
          h("span", { className: "oa-mono" }, x.slug),
          h("span", { className: "oa-dim oa-small" }, x.status + " · " + x.artifacts.length + "/8 · " + ago(x.updated_at)));
      }));
  }

  function EventsPanel(props) {
    var known = {};
    (props.tasks || []).forEach(function (t) { known[t.id] = t; });
    var shown = props.feed.filter(function (e) { return known[e.correlation.task_id]; });
    var hidden = props.feed.length - shown.length;
    return h("div", { className: "oa-pane" },
      h("p", { className: "oa-muted oa-small" }, "Live Kanban events on board ", h("b", null, props.board), ". Replayed from the last cursor after reconnect."),
      shown.length ? h("ol", { className: "oa-timeline" }, shown.slice(0, 150).map(function (ev) {
        var t = known[ev.correlation.task_id];
        return h("li", { key: ev.id, className: "oa-ev oa-sev-" + ev.severity },
          h("span", { className: "oa-ev-type" }, ev.type), h("span", { className: "oa-dim oa-small" }, fmtTime(ev.ts) + " · #" + ev.seq),
          h("button", { className: "oa-linkish", onClick: function () { props.onOpenTask(t.id); } }, t.title));
      })) : h("div", { className: "oa-empty" }, "No events since this page opened. Waiting for real Kanban activity."),
      hidden ? h("div", { className: "oa-dim oa-small" }, hidden + " event(s) for tasks outside this view (withheld or not yet loaded).") : null);
  }

  function IncidentsPanel(props) {
    if (!props.incidents.length) return h("div", { className: "oa-empty" }, "No incidents on this board.");
    return h("div", { className: "oa-pane" }, props.incidents.map(function (i) {
      return h("div", { key: i.id, className: "oa-incident oa-sev-" + i.severity },
        h("div", null, h("b", null, i.severity.toUpperCase()), " · ", i.title),
        i.task_id ? h("button", { className: "oa-linkish", onClick: function () { props.onOpenTask(i.task_id); } }, i.task_title || i.task_id) : null,
        i.detail ? h("div", { className: "oa-dim oa-small" }, i.detail) : null,
        h("div", { className: "oa-dim oa-small" }, "source: " + i.source));
    }));
  }

  function GiveTaskDialog(props) {
    var keyRef = useRef(uid());
    var s = useState({ assignee: (props.residents[0] || {}).profile || "", title: "", body: "", priority: 0 });
    var f = s[0], setF = s[1];
    var b = useState(false), busy = b[0], setBusy = b[1];
    var e = useState(""), err = e[0], setErr = e[1];
    function set(k) { return function (ev) { var v = ev.target.value; setF(function (o) { var n = Object.assign({}, o); n[k] = v; return n; }); }; }
    function submit(ev) {
      ev.preventDefault(); setBusy(true); setErr("");
      call("/agents/" + enc(f.assignee) + "/task", "POST", { title: f.title, body: f.body, priority: Number(f.priority) || 0, board: props.board, idempotency_key: keyRef.current })
        .then(function (r) { setBusy(false); props.onDone(r); }).catch(function (x) { setBusy(false); setErr(errText(x)); });
    }
    return h(Modal, { title: "Task · " + props.room.name, onClose: props.onClose },
      h("form", { onSubmit: submit, className: "po-form" },
        h("p", { className: "po-muted" }, "Creates a Kanban task on board ", h("b", null, props.board), ". The existing dispatcher runs it. Idempotent: a double submit creates one task."),
        h(Field, { label: "Assignee" }, h("select", { className: "po-input", value: f.assignee, onChange: set("assignee") },
          props.residents.map(function (a) { return h("option", { key: a.profile, value: a.profile }, a.name + " (" + a.profile + ")"); }))),
        h(Field, { label: "Title" }, h("input", { className: "po-input", value: f.title, onChange: set("title"), required: true, autoFocus: true })),
        h(Field, { label: "Details" }, h("textarea", { className: "po-input po-textarea", rows: 7, value: f.body, onChange: set("body") })),
        h(Field, { label: "Priority" }, h("select", { className: "po-input", value: f.priority, onChange: set("priority") },
          h("option", { value: 0 }, "normal"), h("option", { value: 1 }, "high"), h("option", { value: 2 }, "urgent"))),
        err ? h("div", { className: "po-error" }, err) : null,
        h("div", { className: "po-actions" }, h(Btn, { onClick: props.onClose }, "Cancel"),
          h(Btn, { kind: "primary", type: "submit", disabled: busy || !f.title.trim() || !f.assignee }, busy ? "Creating…" : "Create Kanban task"))));
  }

  // ── overview (Mission Control) ───────────────────────────────────────────
  function OverviewPanel(props) {
    var os = props.os;
    if (!os) return h("div", { className: "oa-empty" }, "Loading office state…");
    var workers = (os.agents || []).filter(function (a) { return a.presence.execution === "kanban_worker"; });
    var sessions = (os.agents || []).filter(function (a) { return a.presence.execution === "session_activity"; });
    var lanes = os.lanes || [];
    var src = os.sources || {};
    var crit = (os.incidents || []).filter(function (i) { return i.severity === "critical"; });
    return h("div", { className: "oa-pane" },
      h("div", { className: "oa-pane-head" }, "◆ Operations overview · ", h("span", { className: "oa-dim" }, os.board),
        os.client_mode ? h("span", { className: "oa-confidential-badge", title: "Client Work board: content is shown only to authenticated operator requests" }, " ▣ CONFIDENTIAL") : null),
      h("div", { className: "oa-ov-grid" },
        h("button", { className: "oa-ov-card", onClick: function () { props.go("approvals"); } }, h("b", null, (os.approvals || []).length), h("span", null, "awaiting your decision")),
        h("button", { className: "oa-ov-card" + (crit.length ? " oa-ov-bad" : ""), onClick: function () { props.go("incidents"); } }, h("b", null, (os.incidents || []).length), h("span", null, "incidents" + (crit.length ? " · " + crit.length + " critical" : ""))),
        h("div", { className: "oa-ov-card" }, h("b", null, workers.length), h("span", null, "Kanban workers (confirmed)")),
        h("div", { className: "oa-ov-card" }, h("b", null, sessions.length), h("span", null, "session activity (inferred)")),
        h("button", { className: "oa-ov-card", onClick: function () { props.go("lanes"); } }, h("b", null, lanes.length), h("span", null, "lane runs")),
        h("div", { className: "oa-ov-card" }, h("b", null, (os.totals || {}).open || 0), h("span", null, "open tasks · " + ((os.totals || {}).shown || 0) + " shown")),
        h("div", { className: "oa-ov-card" }, h("b", null, (os.operator_queue || []).length), h("span", null, "operator queue (human lanes)")),
        h("div", { className: "oa-ov-card" }, h("b", { className: "oa-dim" }, "?"), h("span", null, "cost: " + ((os.cost || {}).status || "unknown")))),
      h("table", { className: "oa-kv oa-ov-rooms" }, h("thead", null, h("tr", null, h("th", null, "room"), h("th", null, "▶"), h("th", null, "◎"), h("th", null, "■"), h("th", null, "·"), h("th", null, "✓"), h("th", null, "?"), h("th", null, "!"))),
        h("tbody", null, (os.rooms || []).map(function (r, i) {
          var c = r.counts || {};
          return h("tr", { key: r.id, className: "oa-ov-row", onClick: function () { props.openRoom(r.id); } },
            h("td", null, h("span", { style: { color: r.color } }, r.glyph), " ", r.locked ? r.name + " (locked)" : (i + 1) + " · " + r.name),
            ["working", "awaiting_review", "blocked", "queued", "completed"].map(function (k) { return h("td", { key: k }, r.locked ? "—" : (c[k] || "")); }),
            h("td", null, r.approvals || ""), h("td", null, r.incidents || ""));
        }))),
      h("div", { className: "oa-dim oa-small" }, "Sources: ", Object.keys(src).map(function (k) { return k + "=" + (src[k] ? "ok" : "DOWN"); }).join(" · "),
        " · Policy: profile management ", (os.policy || {}).profile_management, ", external adapters ", (os.policy || {}).external_adapters,
        ", lanes on ", ((os.policy || {}).lane_boards || []).join(", ")),
      h("div", { className: "oa-dim oa-small" }, (os.policy || {}).presence));
  }

  // ── governed lanes ───────────────────────────────────────────────────────
  var LANE_STATUS = { running: "working", awaiting_operator: "awaiting_review", approved: "completed", rejected: "blocked",
    blocked: "blocked", gate_bypassed: "blocked", incomplete_chain: "unknown" };
  function LanesPanel(props) {
    var tp = useState(null), tpl = tp[0], setTpl = tp[1];
    var er = useState(""), err = er[0], setErr = er[1];
    useEffect(function () {
      call("/office/lanes/templates").then(function (r) { setTpl(r); }).catch(function (e) { setErr(errText(e)); });
    }, []);
    var runs = props.runs || [];
    var allowed = tpl && tpl.lane_boards.indexOf(props.board) >= 0;
    return h("div", { className: "oa-pane" },
      h("p", { className: "oa-muted oa-small" }, "Governed lanes expand into ordinary Kanban task chains run by the existing dispatcher: work → independent review (different profile) → your gate → execution adapter. ",
        h("b", null, "All execution adapters are disabled"), ": workers share your Unix user and a terminal, so no credential can be handed to an automated send/publish/submit/pay step yet."),
      err ? h("div", { className: "po-error" }, err) : null,
      h("div", { className: "oa-sub-title" }, "Runs on " + props.board + " · " + runs.length),
      runs.length ? runs.map(function (r) {
        return h("div", { key: r.run, className: "oa-lane-run" },
          h("div", { className: "oa-card-top" }, h(StateTag, { state: LANE_STATUS[r.status] || "unknown", title: r.status }), h("b", null, r.name), h("span", { className: "oa-dim oa-mono" }, " " + r.run),
            h("span", { className: "oa-dim oa-small" }, " · " + r.status.replace("_", " "))),
          h("div", { className: "oa-lane-steps" }, r.steps.map(function (st) {
            return h("button", { key: st.key, className: "oa-lane-step oa-st-" + ((st.status === "done") ? "completed" : st.status === "running" ? "working" : st.status === "blocked" ? "blocked" : "queued"),
              disabled: !st.task_id, onClick: function () { if (st.task_id) props.onOpenTask(st.task_id); }, title: (st.task_id || "missing") + " · " + (st.status || "missing") },
              h("span", null, st.key), h("span", { className: "oa-dim" }, " @" + st.assignee), h("span", { className: "oa-dim oa-small" }, " " + (st.status || "—")));
          })),
          r.status === "gate_bypassed" ? h("div", { className: "po-error" }, "Gate task completed outside the Operator Office. No adapter ran. Investigate.") : null,
          h("div", { className: "oa-dim oa-small" }, "adapter: " + r.adapter.key + (r.adapter.enabled ? " (enabled)" : " · DISABLED: " + r.adapter.reason)));
      }) : h("div", { className: "oa-empty" }, "No lane runs on this board."),
      h("div", { className: "oa-sub-title" }, "Templates"),
      !allowed && tpl ? h("div", { className: "oa-banner" }, "Lanes can start only on: " + tpl.lane_boards.join(", ") + ". Production boards need an operator policy decision (QA reviewer routing).") : null,
      tpl ? tpl.templates.map(function (t) {
        return h("div", { key: t.id, className: "oa-lane-tpl" },
          h("div", { className: "oa-card-top" }, h("b", null, t.name), h("span", { className: "oa-dim oa-small" }, " · room " + t.room)),
          h("div", { className: "oa-small" }, t.description),
          h("div", { className: "oa-lane-steps" }, t.steps.map(function (st) {
            return h("span", { key: st.key, className: "oa-lane-step oa-kind-" + st.kind }, st.key, h("span", { className: "oa-dim" }, " @" + st.assignee));
          }), h("span", { className: "oa-lane-step oa-kind-adapter", title: t.adapter.reason }, t.adapter.label + (t.adapter.enabled ? "" : " (disabled)"))),
          t.problems.length ? h("div", { className: "po-error" }, t.problems.join("; ")) : null,
          h("div", { className: "oa-actions" }, h(Btn, { small: true, kind: "primary", disabled: !allowed || !t.runnable,
            title: !allowed ? "board not enabled for lanes" : (!t.runnable ? t.problems.join("; ") : "Creates " + t.steps.length + " Kanban tasks on " + props.board),
            onClick: function () { props.onStart(t); } }, "Start lane")));
      }) : h("div", { className: "oa-empty" }, "Loading templates…"));
  }

  function LaneDialog(props) {
    var t = props.template;
    var keyRef = useRef(uid());
    var init = {};
    t.inputs.forEach(function (f) { init[f.key] = ""; });
    var s = useState(init), f = s[0], setF = s[1];
    var b = useState(false), busy = b[0], setBusy = b[1];
    var e = useState(""), err = e[0], setErr = e[1];
    var cf = useState(false), confirming = cf[0], setConfirming = cf[1];
    function set(k) { return function (ev) { var v = ev.target.value; setF(function (o) { var n = Object.assign({}, o); n[k] = v; return n; }); }; }
    var missing = t.inputs.filter(function (x) { return x.required && !String(f[x.key] || "").trim(); });
    function submit() {
      setBusy(true); setErr("");
      call("/office/lanes/runs", "POST", { template: t.id, board: props.board, inputs: f, idempotency_key: keyRef.current, confirm: true })
        .then(function (r) { setBusy(false); props.onDone(r); }).catch(function (x) { setBusy(false); setErr(errText(x)); });
    }
    return h(Modal, { title: "Lane · " + t.name, onClose: props.onClose },
      h("div", { className: "po-form" },
        h("p", { className: "po-muted" }, "Board ", h("b", null, props.board), ". Creates ", t.steps.length, " Kanban tasks linked as dependencies. Retrying this dialog never duplicates tasks (idempotency key)."),
        t.inputs.map(function (x) {
          return h(Field, { key: x.key, label: x.label + (x.required ? " *" : "") },
            x.multiline ? h("textarea", { className: "po-input po-textarea", rows: 4, maxLength: x.max_len, value: f[x.key], onChange: set(x.key) })
              : h("input", { className: "po-input", maxLength: x.max_len, value: f[x.key], onChange: set(x.key) }));
        }),
        confirming ? h("table", { className: "oa-kv" }, h("tbody", null, t.steps.map(function (st) {
          return h("tr", { key: st.key }, h("th", null, st.key), h("td", null, st.kind + " → " + st.assignee + (st.after.length ? " (after " + st.after.join(", ") + ")" : "")));
        }), h("tr", null, h("th", null, "adapter"), h("td", null, t.adapter.label + (t.adapter.enabled ? "" : ": DISABLED, nothing external will run"))))) : null,
        err ? h("div", { className: "po-error" }, err) : null,
        h("div", { className: "po-actions" }, h(Btn, { onClick: props.onClose }, "Cancel"),
          confirming ? h(Btn, { kind: "primary", disabled: busy, onClick: submit }, busy ? "Creating…" : "Confirm: create " + t.steps.length + " tasks")
            : h(Btn, { kind: "primary", disabled: missing.length > 0, title: missing.length ? "required: " + missing.map(function (x) { return x.key; }).join(", ") : "", onClick: function () { setConfirming(true); } }, "Review chain"))));
  }

  // ── room definitions editor (rooms.json) ─────────────────────────────────
  function RoomsEditor(props) {
    var d = useState(null), data = d[0], setData = d[1];
    var ed = useState({}), edits = ed[0], setEdits = ed[1];
    var er = useState(""), err = er[0], setErr = er[1];
    var bz = useState(false), busy = bz[0], setBusy = bz[1];
    function load() { call("/office/rooms").then(function (r) { setData(r); setEdits({}); }).catch(function (e) { setErr(errText(e)); }); }
    useEffect(load, []);
    if (!data) return h("div", { className: "oa-empty" }, err || "Loading rooms…");
    function val(r, k) { return edits[r.id] && edits[r.id][k] !== undefined ? edits[r.id][k] : r[k]; }
    function setv(r, k, v) { setEdits(function (o) { var n = Object.assign({}, o); n[r.id] = Object.assign({}, n[r.id] || {}); n[r.id][k] = v; return n; }); }
    function save() {
      var rooms = Object.keys(edits).map(function (id) { return Object.assign({ id: id }, edits[id]); });
      setBusy(true); setErr("");
      call("/office/rooms", "PUT", { rooms: rooms, confirm: true }).then(function () { setBusy(false); props.onSaved(); load(); })
        .catch(function (e) { setBusy(false); setErr(errText(e)); });
    }
    return h("div", { className: "oa-pane" },
      h("p", { className: "oa-muted oa-small" }, "Room definitions: names, residents (installed profiles only, one room per profile), routing prefixes and keywords. Client Work stays locked and cannot get residents. Saved to rooms.json with a backup; validated server-side."),
      data.unplaced_profiles.length ? h("div", { className: "oa-banner" }, "Profiles without a room: " + data.unplaced_profiles.join(", ")) : null,
      data.rooms.map(function (r) {
        var locked = r.sensitivity === "client";
        return h("details", { key: r.id, className: "oa-sub" }, h("summary", { className: "oa-sub-title" }, h("span", { style: { color: r.color } }, r.glyph), " ", val(r, "name"), locked ? " · locked" : " · " + (val(r, "residents") || []).join(", ")),
          h(Field, { label: "Name" }, h("input", { className: "po-input", value: val(r, "name"), onChange: function (e) { setv(r, "name", e.target.value); } })),
          locked ? h("div", { className: "oa-dim oa-small" }, "Residents, routing and visibility are fixed for the locked room.") :
            h("div", null,
              h(Field, { label: "Residents" }, h("div", { className: "oa-checks" }, data.profiles.map(function (p) {
                var on = (val(r, "residents") || []).indexOf(p) >= 0;
                return h("label", { key: p, className: "oa-check" }, h("input", { type: "checkbox", checked: on, onChange: function () {
                  var cur = (val(r, "residents") || []).slice();
                  setv(r, "residents", on ? cur.filter(function (x) { return x !== p; }) : cur.concat([p]));
                } }), p);
              }))),
              h(Field, { label: "Tenant prefixes (comma separated)" }, h("input", { className: "po-input", value: (val(r, "tenant_prefixes") || []).join(", "),
                onChange: function (e) { setv(r, "tenant_prefixes", e.target.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean)); } })),
              h(Field, { label: "Title keywords (comma separated)" }, h("input", { className: "po-input", value: (val(r, "keywords") || []).join(", "),
                onChange: function (e) { setv(r, "keywords", e.target.value.split(",").map(function (x) { return x.trim(); }).filter(Boolean)); } }))));
      }),
      err ? h("div", { className: "po-error" }, err) : null,
      h("div", { className: "oa-actions" }, h(Btn, { kind: "primary", small: true, disabled: busy || !Object.keys(edits).length, onClick: save }, busy ? "Saving…" : "Save room definitions"),
        h(Btn, { small: true, disabled: !Object.keys(edits).length, onClick: function () { setEdits({}); } }, "Discard")));
  }

  // ── root ───────────────────────────────────────────────────────────────────
  function PixelOffice() {
    var sn = useState(null), snap = sn[0], setSnap = sn[1];
    var er = useState(""), loadErr = er[0], setLoadErr = er[1];
    var tb = useState("overview"), tab = tb[0], setTab = tb[1];
    var se = useState(null), selected = se[0], setSelected = se[1];
    var dl = useState(null), dialog = dl[0], setDialog = dl[1];
    var sp = useState(true), sideOpen = sp[0], setSideOpen = sp[1];
    var so = useState(true), stageOpen = so[0], setStageOpen = so[1];
    var nt = useState(""), notice = nt[0], setNotice = nt[1];
    var sh = useState(false), showHidden = sh[0], setShowHidden = sh[1];
    var bd = useState(null), board = bd[0], setBoard = bd[1];
    var bl = useState([]), boards = bl[0], setBoards = bl[1];
    var cb = useState([]), clientBoards = cb[0], setClientBoards = cb[1];
    var rm = useState("command"), roomId = rm[0], setRoomId = rm[1];
    var tk = useState(null), taskId = tk[0], setTaskId = tk[1];
    var qq = useState(""), query = qq[0], setQuery = qq[1];
    var mf = useState(null), missionFocus = mf[0], setMissionFocus = mf[1];
    var pl = useState({}), palettes = pl[0], setPalettes = pl[1];
    var frameRef = useRef(null);
    var snapRef = useRef(null);
    var rootRef = useRef(null);
    var searchRef = useRef(null);
    var fk = useState(0), frameKey = fk[0], setFrameKey = fk[1];
    var hh = useState(0), height = hh[0], setHeight = hh[1];
    var office = useOffice(board);
    var os = office.state;

    useEffect(function () {
      function fit() {
        var el = rootRef.current;
        if (!el) return;
        var top = el.getBoundingClientRect().top + window.scrollY;
        setHeight(Math.max(520, window.innerHeight - top - 12));
      }
      fit();
      var t = setTimeout(fit, 300);
      window.addEventListener("resize", fit);
      return function () { clearTimeout(t); window.removeEventListener("resize", fit); };
    }, []);

    useEffect(function () {
      call("/office/boards").then(function (r) { setBoards(r.boards || []); setClientBoards(r.client_boards || []); if (r.selected) setBoard(r.selected); }).catch(function (e) { setLoadErr(errText(e)); });
      call("/bootstrap").then(function (b) {
        var p = {};
        Object.keys(b.seats || {}).forEach(function (k) { if (b.seats[k] && b.seats[k].palette != null) p[k] = b.seats[k].palette; });
        setPalettes(p);
      }).catch(function () {});
    }, []);

    function chooseBoard(b) {
      setBoard(b); setTaskId(null);
      call("/office/board", "PUT", { board: b }).catch(function () {});
    }

    var refresh = useCallback(function () {
      return call("/snapshot").then(function (s) { snapRef.current = s; setSnap(s); setLoadErr(""); })
        .catch(function (e) { setLoadErr(errText(e)); });
    }, []);
    function poke() {
      var w = frameRef.current && frameRef.current.contentWindow;
      if (w) w.postMessage({ ns: NS, type: "refresh" }, window.location.origin);
      office.refresh().catch(function () {});
      return refresh();
    }
    function flash(msg) { setNotice(msg); setTimeout(function () { setNotice(""); }, 5000); }

    useEffect(function () {
      refresh();
      var t = setInterval(refresh, 4000);
      return function () { clearInterval(t); };
    }, [refresh]);

    useEffect(function () {
      function onMsg(e) {
        if (e.origin !== window.location.origin) return;
        var d = e.data;
        if (!d || d.ns !== NS) return;
        var agents = (snapRef.current && snapRef.current.agents) || [];
        if (d.type === "focus" && d.profile) { setSelected(d.profile); setTab("agents"); setSideOpen(true); }
        else if (d.type === "launch") flash("Creating Hermes profiles from the office is disabled (no authorized profile-management policy). Use `hermes profile create` in a terminal.");
        else if (d.type === "close" && d.profile) {
          var a = agents.find(function (x) { return x.key === d.profile; });
          if (window.confirm("Hide " + ((a && a.name) || d.profile) + " from the office? (Profile stays. Restore from the side panel.)")) {
            call("/agents/" + encodeURIComponent(d.profile) + "/hide", "POST", { hidden: true }).then(poke);
          }
        }
      }
      window.addEventListener("message", onMsg);
      return function () { window.removeEventListener("message", onMsg); };
    }, []);

    // keyboard: "/" search, Esc close, 1-9 rooms, g a/i/e/m panels
    useEffect(function () {
      function onKey(e) {
        var tag = (e.target && e.target.tagName) || "";
        var typing = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
        if (e.key === "/" && !typing) { e.preventDefault(); if (searchRef.current) searchRef.current.focus(); return; }
        if (e.key === "Escape" && !typing) { if (taskId) { setTaskId(null); setTab("room"); } else setSelected(null); return; }
        if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
        var rooms = ((os && os.rooms) || []).filter(function (r) { return !r.locked; });
        var n = parseInt(e.key, 10);
        if (n >= 1 && n <= rooms.length) { setRoomId(rooms[n - 1].id); setTab("room"); setSideOpen(true); }
      }
      window.addEventListener("keydown", onKey);
      return function () { window.removeEventListener("keydown", onKey); };
    }, [os, taskId]);

    var agents = (snap && snap.agents) || [];
    var teams = (snap && snap.teams) || [];
    var visibleAgents = agents.filter(function (a) { return !a.hidden; });
    var hiddenAgents = agents.filter(function (a) { return a.hidden; });
    var presence = {};
    ((os && os.agents) || []).forEach(function (a) { presence[a.profile] = a.presence; });
    var sorted = useMemo(function () {
      return visibleAgents.slice().sort(function (x, y) {
        return (STATUS_ORDER[x.status] - STATUS_ORDER[y.status]) || ((y.last_at || 0) - (x.last_at || 0));
      });
    }, [snap]);
    var sel = agents.find(function (a) { return a.key === selected; }) || null;

    var q = query.trim().toLowerCase();
    var allTasks = (os && os.tasks) || [];
    var tasks = q ? allTasks.filter(function (t) {
      return (t.title || "").toLowerCase().indexOf(q) >= 0 || (t.id || "").toLowerCase().indexOf(q) >= 0 || (t.assignee || "").toLowerCase().indexOf(q) >= 0;
    }) : allTasks;
    var matchCount = null;
    if (q) { matchCount = {}; tasks.forEach(function (t) { matchCount[t.room] = (matchCount[t.room] || 0) + 1; }); }
    var stateCounts = {};
    ((os && os.agents) || []).forEach(function (a) { stateCounts[a.presence.state] = (stateCounts[a.presence.state] || 0) + 1; });
    var approvals = (os && os.approvals) || [];
    var incidents = (os && os.incidents) || [];
    var room = ((os && os.rooms) || []).find(function (r) { return r.id === roomId; });

    function select(a) {
      setSelected(a.key); setTab("agents"); setSideOpen(true);
      var w = frameRef.current && frameRef.current.contentWindow;
      if (w && !a.hidden) w.postMessage({ ns: NS, type: "select", id: a.id }, window.location.origin);
    }
    function selectOfficeAgent(oa) {
      var a = agents.find(function (x) { return x.key === oa.profile; });
      if (a) select(a);
    }
    function openTask(id) { setTaskId(id); setTab("task"); setSideOpen(true); }
    function setTeam(a, teamId) {
      var target = teams.find(function (t) { return t.id === teamId; });
      var current = teams.find(function (t) { return t.id === a.team_id; });
      var p;
      if (target) {
        var members = (target.members || []).concat([a.key]).filter(function (v, i, arr) { return arr.indexOf(v) === i; });
        p = call("/teams/" + target.id, "PATCH", { name: target.name, color: target.color, description: target.description, lead: target.lead || a.key, members: members });
      } else if (current) {
        var rest = (current.members || []).filter(function (m) { return m !== a.key; });
        p = call("/teams/" + current.id, "PATCH", { name: current.name, color: current.color, description: current.description,
          lead: current.lead === a.key ? (rest[0] || null) : current.lead, members: rest });
      } else return;
      p.then(function () { flash("Team updated."); poke(); }).catch(function (e) { flash(errText(e)); });
    }
    function applyPreset(name, label) {
      if (!window.confirm("Replace the office with the " + label + " layout? Current layout and seats are backed up.")) return;
      call("/layout/preset/" + name, "POST").then(function (r) {
        flash(label + " applied: " + r.cols + "x" + r.rows + ", " + r.furniture + " items.");
        setFrameKey(function (k) { return k + 1; });
        refresh();
      }).catch(function (e) { flash(errText(e)); });
    }
    function hide(a, hidden) { call("/agents/" + encodeURIComponent(a.key) + "/hide", "POST", { hidden: hidden }).then(poke); }
    function decided(r) {
      flash((r.idempotent_replay ? "Already applied: " : "Applied: ") + r.decision + " on " + r.task_id + " → " + r.status);
      poke();
    }

    var lanesRuns = (os && os.lanes) || [];
    var TABS = [
      ["overview", "Overview"],
      ["room", room ? room.name : "Room"],
      ["task", taskId ? "Task" : "Task"],
      ["approvals", "Approvals" + (approvals.length ? " · " + approvals.length : "")],
      ["lanes", "Lanes" + (lanesRuns.length ? " · " + lanesRuns.length : "")],
      ["incidents", "Incidents" + (incidents.length ? " · " + incidents.length : "")],
      ["events", "Events"],
      ["missions", "Missions"],
      ["agents", "Agents · " + visibleAgents.length],
      ["teams", "Teams · " + teams.length],
      ["rooms", "Rooms setup"],
    ];

    var content;
    if (!board) {
      content = h("div", { className: "oa-pane" }, h("p", null, "Select the Kanban board this office reads. Nothing is shown until a board is chosen explicitly."),
        boards.map(function (b) { return h(Btn, { key: b.slug, small: true,
          className: b.is_client ? "oa-btn-confidential" : null,
          onClick: function () { chooseBoard(b.slug); } },
          (b.is_client ? "▣ " : "") + b.name + " (" + b.slug + ")" + (b.is_client ? " · CONFIDENTIAL" : "")); }));
    } else if (tab === "overview") {
      content = h(OverviewPanel, { os: os, go: setTab, openRoom: function (id) { setRoomId(id); setTab("room"); } });
    } else if (tab === "lanes") {
      content = h(LanesPanel, { board: board, runs: lanesRuns, onOpenTask: openTask, onStart: function (t) { setDialog({ kind: "lane", template: t }); } });
    } else if (tab === "rooms") {
      content = h(RoomsEditor, { onSaved: function () { flash("Room definitions saved."); office.refresh().catch(function () {}); } });
    } else if (tab === "room") {
      content = h(RoomPanel, { room: room, tasks: tasks, agents: (os && os.agents) || [], palettes: palettes, query: q, selectedTask: taskId,
        onOpenTask: function (t) { openTask(t.id); }, onAgent: selectOfficeAgent,
        onGive: function (r, residents) { setDialog({ kind: "give", room: r, residents: residents }); } });
    } else if (tab === "task") {
      content = h(TaskPanel, { taskId: taskId, board: board, subscribe: office.subscribe, onClose: function () { setTaskId(null); setTab("room"); },
        onDecided: decided, onMission: function (m) { setMissionFocus(m); setTab("missions"); } });
    } else if (tab === "approvals") {
      var oq = (os && os.operator_queue) || [];
      var qd = office.queueDecided || [];
      var decidedList = qd.length ? h("details", { className: "oa-sub" },
        h("summary", { className: "oa-sub-title" }, "Recently decided · " + qd.length),
        qd.map(function (d) {
          var v = (d.decided_via || "office").split(":", 1)[0];
          return h("div", { key: d.id, className: "oa-row oa-row-tight" },
            h("button", { className: "oa-linkish", onClick: function () { openTask(d.task_id); } }, d.task_id),
            h("span", { className: "oa-badge oa-badge-via oa-badge-via-" + v, title: "decided via " + v }, v + (d.decided_by ? " · " + d.decided_by : "")),
            h("span", { className: "oa-badge", title: "final status: " + d.status }, d.status),
            " · ",
            h("span", { className: "oa-dim oa-small" }, fmtTime(d.decided_at || d.created_at)));
        })) : null;
      content = h("div", { className: "oa-pane" },
        h("p", { className: "oa-muted oa-small" }, "Human gates on ", h("b", null, board), ": Kanban blocked/needs_input, review on a human lane, and governed-lane gates (server re-checks independent review before approve). A review owned by a profile is agent review, not yours. Execution adapters (email, publishing, applications, money, infra) are disabled: approving records the decision, nothing external runs."),
        approvals.length ? approvals.map(function (a) { return h(ApprovalBox, { key: a.id + a.params.fingerprint, approval: a, onOpenTask: openTask, onDone: decided }); })
          : h("div", { className: "oa-empty" }, "Nothing awaits your decision on this board."),
        decidedList,
        oq.length ? h("details", { className: "oa-sub" }, h("summary", { className: "oa-sub-title" }, "Operator queue · " + oq.length + " (tasks on human lanes, read-only)"),
          oq.map(function (t) { return h(TaskCard, { key: t.id, task: t, onOpen: function (x) { openTask(x.id); } }); })) : null);
    } else if (tab === "incidents") {
      content = h(IncidentsPanel, { incidents: incidents, onOpenTask: openTask });
    } else if (tab === "events") {
      content = h(EventsPanel, { feed: office.feed, tasks: allTasks, board: board, onOpenTask: openTask });
    } else if (tab === "missions") {
      content = h(MissionsPanel, { query: q, focus: missionFocus });
    } else if (tab === "agents") {
      content = h("div", null,
        sel ? h(Inspector, { agent: sel, teams: teams, onClose: function () { setSelected(null); },
          onTask: function (a) { setDialog({ kind: "task", path: "/agents/" + encodeURIComponent(a.key) + "/task", title: "Task for " + a.name,
            subtitle: "Creates a Kanban task assigned to " + a.key + " on board " + board + ". The dispatcher picks it up as usual.", board: board }); },
          onSetTeam: setTeam, onHide: hide }) : null,
        sorted.map(function (a) {
          return h("div", { key: a.key, className: "oa-agent-row" },
            h(AgentRow, { agent: a, selected: a.key === selected, onSelect: select }),
            presence[a.key] ? h(StateTag, { state: presence[a.key].state, title: (presence[a.key].evidence || []).join(" · ") }) : null);
        }),
        (os && os.orphan_assignees && os.orphan_assignees.length) ? h("div", { className: "oa-dim oa-small oa-pad" },
          "Assignees without a profile (operator or external lanes): " + os.orphan_assignees.join(", ")) : null,
        hiddenAgents.length ? h("button", { className: "po-linkbtn", onClick: function () { setShowHidden(!showHidden); } },
          (showHidden ? "▾ " : "▸ ") + "Hidden · " + hiddenAgents.length) : null,
        showHidden ? hiddenAgents.map(function (a) {
          return h("div", { key: a.key, className: "po-row po-row-hidden" }, h("span", { className: "po-row-main" }, a.name),
            h(Btn, { small: true, onClick: function () { hide(a, false); } }, "Show"));
        }) : null);
    } else {
      content = h(TeamsPanel, { teams: teams, agents: agents, onSelect: select,
        onEdit: function (t) { setDialog({ kind: "team", team: t }); },
        onDelete: function (t) { if (window.confirm("Delete team " + t.name + "? Agents stay.")) call("/teams/" + t.id, "DELETE").then(function () { flash("Team deleted."); poke(); }); },
        onTask: function (t) { setDialog({ kind: "task", path: "/teams/" + t.id + "/task", title: "Task for team " + t.name, board: board,
          subtitle: "Goes to the lead (" + (t.lead || "first member") + ") as a Kanban task with the team roster attached." }); } });
    }

    var side = h("aside", { className: "po-side oa-side", "aria-label": "Office panel" },
      h("div", { className: "oa-tabs", role: "tablist" }, TABS.map(function (t) {
        return h("button", { key: t[0], role: "tab", "aria-selected": tab === t[0], className: "oa-tab" + (tab === t[0] ? " oa-tab-on" : "") +
          (t[0] === "approvals" && approvals.length ? " oa-tab-alert" : "") + (t[0] === "incidents" && incidents.length ? " oa-tab-warn" : ""),
          onClick: function () { setTab(t[0]); } }, t[1]);
      })),
      h("div", { className: "po-side-scroll" }, content));

    var dlg = null;
    if (dialog && dialog.kind === "lane") {
      dlg = h(LaneDialog, { template: dialog.template, board: board, onClose: function () { setDialog(null); },
        onDone: function (r) { setDialog(null); flash("Lane " + r.template + " run " + r.run + ": " + Object.keys(r.tasks).length + " Kanban tasks on " + r.board + "."); setTab("lanes"); office.refresh().catch(function () {}); } });
    } else if (dialog && dialog.kind === "team") {
      dlg = h(TeamDialog, { team: dialog.team, teams: teams, agents: agents, onClose: function () { setDialog(null); },
        onDone: function () { setDialog(null); setTab("teams"); flash("Team saved. Tip: Layout → Areas, map an area to the team to seat it together."); poke(); } });
    } else if (dialog && dialog.kind === "task") {
      dlg = h(TaskDialog, { path: dialog.path, title: dialog.title, subtitle: dialog.subtitle, board: dialog.board, onClose: function () { setDialog(null); },
        onDone: function (r) { setDialog(null); var id = (r && (r.id || r.task_id || (r.task && r.task.id))) || ""; flash("Kanban task " + id + " created."); poke(); if (id) openTask(id); } });
    } else if (dialog && dialog.kind === "give") {
      dlg = h(GiveTaskDialog, { room: dialog.room, residents: dialog.residents, board: board, onClose: function () { setDialog(null); },
        onDone: function (r) { setDialog(null); var id = (r && (r.id || r.task_id || (r.task && r.task.id))) || ""; flash("Kanban task " + id + " created on " + board + "."); poke(); if (id) openTask(id); } });
    }

    var sources = (os && os.sources) || {};
    var downs = Object.keys(sources).filter(function (k) { return sources[k] === false; });

    return h("div", { className: "po-root oa-root", ref: rootRef, style: height ? { height: height + "px" } : undefined },
      h("div", { className: "po-toolbar oa-toolbar" },
        h("div", { className: "oa-brand" }, h("span", { className: "oa-brand-mark" }, "▚"), "OPERATOR OFFICE"),
        h("select", { className: "po-input po-input-sm oa-board" + (board && clientBoards.indexOf(board) >= 0 ? " oa-board-confidential" : ""), value: board || "", "aria-label": "Kanban board",
          onChange: function (e) { if (e.target.value) chooseBoard(e.target.value); } },
          h("option", { value: "" }, "board…"),
          boards.map(function (b) { return h("option", { key: b.slug, value: b.slug }, b.is_client ? "▣ " + b.slug + " (CONFIDENTIAL)" : b.slug); })),
        board && clientBoards.indexOf(board) >= 0 ? h("span", { className: "oa-confidential-badge", title: "Client Work board: content is shown only to authenticated operator requests" }, "▣ CONFIDENTIAL") : null,
        h(ConnPill, { conn: board ? office.conn : (boards.length ? "idle" : "connecting"), cursor: os && os.cursor, lastOk: office.lastOk }),
        h("div", { className: "oa-stats" + (office.err ? " oa-stale" : ""), title: office.err ? "stale: last good snapshot" : undefined },
          ["working", "blocked", "awaiting_review", "completed", "idle", "offline", "unknown"].map(function (s) {
            return stateCounts[s] ? h("span", { key: s, className: "oa-stat oa-st-" + s }, h("i", { className: "oa-led" }), stateCounts[s] + " " + STATE_LABEL[s]) : null;
          }),
          approvals.length ? h("button", { className: "oa-stat oa-stat-btn oa-st-awaiting_review", onClick: function () { setTab("approvals"); } }, "◎ " + approvals.length + " to approve") : null,
          incidents.length ? h("button", { className: "oa-stat oa-stat-btn oa-st-blocked", onClick: function () { setTab("incidents"); } }, "! " + incidents.length + " incidents") : null,
          os && !os.client_mode && os.withheld_client_tasks ? h("span", { className: "oa-stat oa-dim", title: os.withheld_client_tasks_note || "Tasks matching client markers are withheld from this personal office" }, "▣ " + os.withheld_client_tasks + " withheld") : null),
        h("input", { ref: searchRef, className: "po-input po-input-sm oa-search", placeholder: "Search tasks  /", value: query, "aria-label": "Search tasks",
          onChange: function (e) { setQuery(e.target.value); }, onKeyDown: function (e) { if (e.key === "Escape") { setQuery(""); e.target.blur(); } } }),
        h("div", { className: "po-toolbar-actions" },
          h(Btn, { small: true, onClick: function () { setDialog({ kind: "team", team: null }); } }, "+ Team"),
          h(Btn, { small: true, onClick: function () { applyPreset("operator-office", "Operator Office (9 rooms)"); }, title: "Nine functional rooms, every profile seated in its own room" }, "9-room layout"),
          h(Btn, { small: true, onClick: function () { applyPreset("hermes-hq", "Hermes HQ"); }, title: "Big office: gym, game room, cafe, server room, lounge, library" }, "HQ layout"),
          h(Btn, { small: true, onClick: function () { setStageOpen(!stageOpen); }, title: "Toggle the pixel office canvas" }, stageOpen ? "Hide office" : "Show office"),
          h(Btn, { small: true, onClick: function () { setSideOpen(!sideOpen); }, title: "Toggle side panel" }, sideOpen ? "Hide panel" : "Show panel"))),
      (notice || loadErr || office.err || downs.length) ? h("div", { className: "oa-banners" },
        notice ? h("div", { className: "po-notice" }, notice) : null,
        office.err ? h("div", { className: "oa-banner oa-banner-err", role: "alert" }, "Office state unavailable: " + office.err + (os ? " · showing last good snapshot (" + ago(office.lastOk / 1000) + " old)" : "")) : null,
        downs.length ? h("div", { className: "oa-banner oa-banner-err", role: "alert" }, "Source down: " + downs.join(", ") + ". Affected values show as unknown, not idle.") : null,
        loadErr ? h("div", { className: "po-error po-small" }, loadErr) : null) : null,
      h("div", { className: "po-body oa-body" + (stageOpen ? "" : " oa-nostage") },
        h(FloorPlan, { rooms: (os && os.rooms) || [], agents: (os && os.agents) || [], palettes: palettes, selected: tab === "room" ? roomId : null, board: board,
          matchCount: matchCount, onSelect: function (id) { setRoomId(id); setTab("room"); setSideOpen(true); } }),
        stageOpen ? h("div", { className: "po-stage" },
          h("iframe", { key: frameKey, ref: frameRef, src: APP_URL, className: "po-frame", title: "Pixel Office: live characters for Hermes profiles", allow: "autoplay" })) : null,
        sideOpen ? side : null),
      dlg);
  }

  REG.register(NAME, PixelOffice);
})();
