(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const Bot = (p) => h("span", { className: "le-bot" }, p.children);
  const Button = (p) => h("button", Object.assign({ type: "button", className: "le-primary" }, p));
  const h = React.createElement;
  const API = "/api/plugins/loose-ends";

  const ACTIONS = [
    { key: "done", label: "Dealt with" },
    { key: "tomorrow", label: "Remind me tomorrow" },
    { key: "next", label: "Next steps…" },
  ];

  function ago(iso) {
    if (!iso) return "";
    try { return SDK.utils.isoTimeAgo(iso); } catch (e) { return iso; }
  }

  function fmtWhen(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    return d.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }

  function Item(props) {
    const { item, choice, setChoice } = props;
    const action = choice && choice.action;
    const note = (choice && choice.note) || "";
    return h("div", { className: "le-item" + (action ? " le-chosen" : "") },
      h("div", { className: "le-item-head" },
        h(Bot, null, "@" + item.bot),
        h("span", { className: "le-title" }, item.title),
        h("span", { className: "le-when" }, item.source_time ? fmtWhen(item.source_time) : ago(item.first_seen)),
      ),
      item.ask ? h("div", { className: "le-ask" }, item.ask) : null,
      item.detail ? h("div", { className: "le-detail" }, item.detail) : null,
      h("div", { className: "le-actions" },
        ACTIONS.map((a) => h("button", {
          key: a.key, type: "button",
          className: "le-btn" + (action === a.key ? " le-btn-on" : ""),
          onClick: () => setChoice(action === a.key ? null : { action: a.key, note }),
        }, a.label)),
      ),
      action ? h("textarea", {
        className: "le-note",
        rows: action === "next" ? 3 : 2,
        placeholder: action === "next"
          ? "What should @" + item.bot + " do next? It will act on this right away."
          : "Optional note",
        value: note,
        onChange: (e) => setChoice({ action, note: e.target.value }),
      }) : null,
    );
  }

  function Section(props) {
    if (!props.items || !props.items.length) return null;
    return h("section", { className: "le-card" },
      h("h2", { className: "le-section-title" }, props.title + " (" + props.items.length + ")"),
      h("div", null, props.items.map(props.render)),
    );
  }

  function LooseEnds() {
    const [view, setView] = useState(null);
    const [error, setError] = useState(null);
    const [choices, setChoices] = useState({});
    const [busy, setBusy] = useState(false);
    const [flash, setFlash] = useState(null);

    const load = useCallback(() => {
      SDK.fetchJSON(API + "/items").then((v) => { setView(v); setError(null); })
        .catch((e) => setError(String(e && e.message || e)));
    }, []);

    useEffect(() => {
      load();
      const t = setInterval(load, 30000);
      return () => clearInterval(t);
    }, [load]);

    // The dashboard ships no viewport tag, so phones render it as a shrunken 980px page.
    // Add one while this tab is open; put things back when leaving it.
    useEffect(() => {
      let meta = document.querySelector('meta[name="viewport"]');
      const prev = meta ? meta.getAttribute("content") : null;
      if (!meta) { meta = document.createElement("meta"); meta.name = "viewport"; document.head.appendChild(meta); }
      meta.setAttribute("content", "width=device-width, initial-scale=1, viewport-fit=cover");
      document.documentElement.classList.add("le-mobile-ok");
      return () => {
        document.documentElement.classList.remove("le-mobile-ok");
        if (prev === null) meta.remove(); else meta.setAttribute("content", prev);
      };
    }, []);

    const chosen = Object.entries(choices).filter(([, c]) => c && c.action);
    const missingNote = chosen.some(([, c]) => c.action === "next" && !String(c.note || "").trim());

    function submit() {
      if (!chosen.length || missingNote) return;
      setBusy(true);
      const decisions = chosen.map(([id, c]) => ({ id, action: c.action, note: c.note || "" }));
      SDK.fetchJSON(API + "/decide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decisions }),
      }).then((res) => {
        setView(res.view);
        setChoices({});
        const handed = decisions.filter((d) => d.action === "next").length;
        setFlash("Saved " + decisions.length + " decision" + (decisions.length === 1 ? "" : "s") +
          (handed ? ". " + handed + " handed to the bots; their replies show up below." : "."));
      }).catch((e) => setError(String(e && e.message || e))).finally(() => setBusy(false));
    }

    if (error && !view) return h("div", { className: "le-wrap" }, h("p", { className: "le-error" }, "Couldn't load: " + error));
    if (!view) return h("div", { className: "le-wrap" }, h("p", { className: "le-muted" }, "Loading…"));

    const waiting = view.waiting || [];
    return h("div", { className: "le-wrap" },
      h("div", { className: "le-top" },
        h("h1", { className: "le-h1" }, waiting.length ? waiting.length + " waiting on you" : "Nothing waiting on you"),
        h("span", { className: "le-muted" }, view.last_sweep ? "Last sweep " + fmtWhen(view.last_sweep) : "No sweep yet"),
      ),
      flash ? h("div", { className: "le-flash" }, flash) : null,
      error ? h("div", { className: "le-error" }, error) : null,
      waiting.map((item) => h(Item, {
        key: item.id, item, choice: choices[item.id],
        setChoice: (c) => setChoices(Object.assign({}, choices, { [item.id]: c })),
      })),
      waiting.length ? h("div", { className: "le-submit" },
        h(Button, { onClick: submit, disabled: busy || !chosen.length || missingNote },
          busy ? "Saving…" : chosen.length ? "Submit " + chosen.length + " decision" + (chosen.length === 1 ? "" : "s") : "Pick an action above"),
        missingNote ? h("span", { className: "le-muted" }, "Next steps need a note.") : null,
      ) : null,
      h(Section, {
        title: "With the bots now", items: view.working,
        render: (i) => h("div", { key: i.id, className: "le-row" },
          h(Bot, null, "@" + i.bot), " ", h("b", null, i.title),
          h("div", { className: "le-muted" }, "Your instructions: " + ((i.handoff && i.handoff.note) || "")),
          h("div", { className: "le-muted" }, "Working on it since " + fmtWhen(i.handoff && i.handoff.at) + "…")),
      }),
      h(Section, {
        title: "Snoozed", items: view.snoozed,
        render: (i) => h("div", { key: i.id, className: "le-row" },
          h(Bot, null, "@" + i.bot), " ", i.title,
          h("span", { className: "le-muted" }, "  · back " + fmtWhen(i.snooze_until))),
      }),
      h(Section, {
        title: "Closed this week", items: view.closed,
        render: (i) => h("details", { key: i.id, className: "le-row" },
          h("summary", null, h(Bot, null, "@" + i.bot), " ", i.title,
            h("span", { className: "le-muted" }, "  · " + fmtWhen(i.closed_at))),
          h("div", { className: "le-detail" }, i.resolution || "")),
      }),
    );
  }

  window.__HERMES_PLUGINS__.register("loose-ends", LooseEnds);
})();
