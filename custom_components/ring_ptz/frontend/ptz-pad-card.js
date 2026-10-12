// ptz-pad-card: ◀ ▲ ⌂ ▼ ▶ pad for Ring Pan-Tilt (ring_ptz), served by the integration.
// config: { type: custom:ptz-pad-card, entity: <any ring_ptz entity of the camera>, show_angles?: bool }
//         (or device_id instead of entity)
// Tap = one step. Hold (>350 ms) = continuous move until released: ring_ptz.move is renewed every
// 1.5 s while held and the integration stops 3 s after the last renewal, so a lost release can't
// leave the camera turning. Press also calls ring_ptz.prepare so the Ring session is open in time.
const HOLD_MS = 350;
const RENEW_MS = 1500;
const BUTTONS = [
  ["left", "mdi:chevron-left", "Pan left"],
  ["up", "mdi:chevron-up", "Tilt up"],
  ["home", "mdi:home-outline", "Home position"],
  ["down", "mdi:chevron-down", "Tilt down"],
  ["right", "mdi:chevron-right", "Pan right"],
];

class PtzPadCard extends HTMLElement {
  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { filter: { integration: "ring_ptz" } } } },
        { name: "show_angles", selector: { boolean: {} } },
      ],
    };
  }

  static getStubConfig(hass) {
    const entity = Object.values(hass.entities || {}).find((e) => e.platform === "ring_ptz");
    return { entity: entity ? entity.entity_id : "", show_angles: true };
  }

  setConfig(config) {
    if (!config.entity && !config.device_id) throw new Error("ptz-pad-card: set 'entity' (a ring_ptz entity) or 'device_id'");
    this._config = config;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._updateAngles();
  }

  get _deviceId() {
    if (this._config.device_id) return this._config.device_id;
    const entry = this._hass && this._hass.entities && this._hass.entities[this._config.entity];
    return entry && entry.device_id;
  }

  _angleEntities() {
    const device = this._deviceId;
    const found = {};
    for (const e of Object.values((this._hass && this._hass.entities) || {})) {
      if (e.platform !== "ring_ptz" || e.device_id !== device || !e.entity_id.startsWith("sensor.")) continue;
      found[e.translation_key || (e.entity_id.includes("tilt") ? "tilt" : "pan")] = e.entity_id;
    }
    return found;
  }

  _updateAngles() {
    const el = this.shadowRoot && this.shadowRoot.querySelector(".angles");
    if (!el || !this._hass) return;
    const ids = this._angleEntities();
    if (this._config.show_angles === false || (!ids.pan && !ids.tilt)) {
      el.hidden = true;
      return;
    }
    const fmt = (id) => {
      const s = id && this._hass.states[id];
      return s && !isNaN(parseFloat(s.state)) ? `${Math.round(parseFloat(s.state))}°` : "–";
    };
    const text = `Pan ${fmt(ids.pan)} · Tilt ${fmt(ids.tilt)}`;
    if (el.textContent !== text) el.textContent = text;
    el.hidden = false;
  }

  _call(service, data = {}) {
    const device_id = this._deviceId;
    if (!this._hass || !device_id) {
      this._flashError();
      return Promise.resolve();
    }
    return this._hass.callService("ring_ptz", service, { device_id, ...data }).catch((err) => {
      console.warn("ptz-pad-card", service, err);
      this._flashError();
    });
  }

  _flashError() {
    const card = this.shadowRoot.querySelector("ha-card");
    card.classList.add("error");
    setTimeout(() => card.classList.remove("error"), 1500);
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 8px; transition: box-shadow 0.2s; }
        ha-card.error { box-shadow: 0 0 0 2px var(--error-color); }
        .pad { display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; }
        button {
          display: flex; align-items: center; justify-content: center;
          height: 48px; border: none; border-radius: 12px; cursor: pointer;
          background: var(--secondary-background-color); color: var(--primary-text-color);
          touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
          transition: background 0.15s, color 0.15s;
        }
        button.active { background: var(--primary-color); color: var(--text-primary-color, #fff); }
        ha-icon { --mdc-icon-size: 28px; pointer-events: none; }
        .angles { margin-top: 6px; text-align: center; font-size: 12px; color: var(--secondary-text-color); }
      </style>
      <ha-card>
        <div class="pad">
          ${BUTTONS.map(([cmd, icon, label]) =>
            `<button data-cmd="${cmd}" title="${label}" aria-label="${label}"><ha-icon icon="${icon}"></ha-icon></button>`
          ).join("")}
        </div>
        <div class="angles" hidden></div>
      </ha-card>`;
    this.shadowRoot.querySelectorAll("button").forEach((btn) => this._bind(btn, btn.dataset.cmd));
    this._updateAngles();
  }

  _bind(btn, cmd) {
    let holdTimer = null;
    let renew = null;
    let held = false;
    let pressed = false;

    const press = (ev) => {
      ev.preventDefault();
      if (pressed) return;
      pressed = true;
      held = false;
      if (btn.setPointerCapture) btn.setPointerCapture(ev.pointerId);
      btn.classList.add("active");
      if (cmd === "home") return;
      this._call("prepare");
      holdTimer = setTimeout(() => {
        held = true;
        this._call("move", { direction: cmd });
        renew = setInterval(() => this._call("move", { direction: cmd }), RENEW_MS);
      }, HOLD_MS);
    };

    const release = () => {
      if (!pressed) return;
      pressed = false;
      clearTimeout(holdTimer);
      clearInterval(renew);
      btn.classList.remove("active");
      if (cmd === "home") this._call("home");
      else if (held) this._call("stop");
      else this._call("step", { direction: cmd });
    };

    btn.addEventListener("pointerdown", press);
    btn.addEventListener("pointerup", release);
    btn.addEventListener("pointercancel", release);
    btn.addEventListener("lostpointercapture", release);
    btn.addEventListener("contextmenu", (ev) => ev.preventDefault());
  }

  getCardSize() {
    return 2;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }
}

if (!customElements.get("ptz-pad-card")) {
  customElements.define("ptz-pad-card", PtzPadCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "ptz-pad-card",
    name: "Ring Pan-Tilt pad",
    description: "Tap to step, hold to pan/tilt a Ring Pan-Tilt Indoor Cam",
  });
}
