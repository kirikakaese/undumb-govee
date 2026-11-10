/* Undumb Govee card: shows every light of a multi-light Govee lamp side by side.
 *
 *   type: custom:undumb-govee-card
 *   entity: light.floor_lamp      # the whole-lamp entity
 *   swatches: ["#ffc77a", "#ffffff", "#ff5a5a", "#5a8cff", "#c255e6"]   # optional
 */
const DEFAULT_SWATCHES = ["#ffc77a", "#ffffff", "#ff5a5a", "#5a8cff", "#c255e6"];

const hexToRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const rgbToHex = (rgb) => "#" + rgb.map((c) => c.toString(16).padStart(2, "0")).join("");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

class UndumbGoveeCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) throw new Error("Set `entity` to the lamp's whole-lamp light, e.g. light.floor_lamp");
    this._config = { swatches: DEFAULT_SWATCHES, ...config };
    this._sig = null;
  }

  static getStubConfig(hass) {
    const lamp = Object.values(hass.states).find((s) => s.attributes.undumb_govee_role === "lamp");
    return { entity: lamp ? lamp.entity_id : "light.my_govee_lamp" };
  }

  getCardSize() {
    return 4;
  }

  set hass(hass) {
    this._hass = hass;
    const lamp = hass.states[this._config.entity];
    const lampId = lamp?.attributes.undumb_govee_lamp;
    const zones = Object.values(hass.states)
      .filter((s) => lampId && s.attributes.undumb_govee_lamp === lampId && s.attributes.undumb_govee_role === "zone")
      .sort((a, b) => (a.attributes.segments || "z").localeCompare(b.attributes.segments || "z", undefined, { numeric: true }));
    const sig = JSON.stringify([lamp?.state, lamp?.attributes, zones.map((z) => [z.entity_id, z.state, z.attributes])]);
    if (sig === this._sig) return;
    this._sig = sig;
    this._render(lamp, zones);
  }

  _call(service, entity_id, data = {}) {
    this._hass.callService("light", service, { entity_id, ...data });
  }

  _render(lamp, zones) {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (!lamp) {
      this.shadowRoot.innerHTML = `<ha-card><div class="msg">Entity ${esc(this._config.entity)} not found.</div></ha-card>`;
      return;
    }
    const title = this._config.title || lamp.attributes.friendly_name;
    const lampOn = lamp.state === "on";
    const tiles = zones.length
      ? zones.map((z) => this._zoneHtml(z, lamp)).join("")
      : `<div class="msg">No separate lights found for this lamp. Map them under Settings → Devices → Undumb Govee → Configure.</div>`;
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 16px; }
        .head { display: flex; align-items: center; gap: 12px; margin-bottom: 14px; }
        .head .name { font-size: 1.15em; font-weight: 600; flex: 1; min-width: 0; }
        .zones { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; }
        .zone { border-radius: 14px; padding: 12px; display: grid; gap: 12px;
                background: var(--secondary-background-color); }
        .top { display: flex; align-items: center; gap: 10px; }
        .dot { width: 14px; height: 14px; border-radius: 50%; flex: none; box-shadow: 0 0 0 1px var(--divider-color); }
        .top .lbl { flex: 1; min-width: 0; }
        .top b { display: block; font-weight: 600; }
        .top small { color: var(--secondary-text-color); }
        input[type=range] { width: 100%; margin: 0; accent-color: var(--zone-color, var(--primary-color)); }
        .sw { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
        .sw button { width: 26px; height: 26px; border-radius: 50%; border: 2px solid var(--card-background-color);
                     box-shadow: 0 0 0 1px var(--divider-color); cursor: pointer; padding: 0; }
        .sw button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
        .sw input[type=color] { width: 26px; height: 26px; border: 0; padding: 0; background: none; cursor: pointer; border-radius: 50%; overflow: hidden; }
        .sw input[type=color]::-webkit-color-swatch-wrapper { padding: 0; }
        .sw input[type=color]::-webkit-color-swatch { border: 0; border-radius: 50%; }
        .sw input[type=color]::-moz-color-swatch { border: 0; border-radius: 50%; }
        .msg { color: var(--secondary-text-color); padding: 8px 0; }
        .off { opacity: .6; }
      </style>
      <ha-card>
        <div class="head">
          <div class="name">${esc(title)}</div>
          <ha-switch data-toggle="${esc(lamp.entity_id)}" ${lampOn ? "checked" : ""}></ha-switch>
        </div>
        <div class="zones">${tiles}</div>
      </ha-card>`;
    this._bind();
  }

  _zoneHtml(z, lamp) {
    const on = z.state === "on";
    const a = z.attributes;
    const pct = on && a.brightness != null ? Math.round((a.brightness / 255) * 100) : 0;
    const rgb = a.rgb_color || (on ? [255, 214, 150] : null);
    const color = rgb ? `rgb(${rgb.join(",")})` : "var(--disabled-text-color)";
    const name = esc(a.zone_name || a.friendly_name);
    const supportsColor = (a.supported_color_modes || []).some((m) => ["rgb", "hs", "xy", "rgbw", "rgbww"].includes(m));
    const supportsBri = (a.supported_color_modes || []).some((m) => m !== "onoff");
    const status = z.state === "unavailable" ? "Unavailable" : on ? (supportsBri ? `On \u00b7 ${pct}%` : "On") : "Off";
    return `
      <div class="zone ${on ? "" : "off"}" style="--zone-color:${color}">
        <div class="top">
          <span class="dot" style="background:${color}"></span>
          <span class="lbl"><b>${name}</b><small>${status}</small></span>
          <ha-switch data-toggle="${esc(z.entity_id)}" ${on ? "checked" : ""}></ha-switch>
        </div>
        ${supportsBri ? `<input type="range" min="1" max="100" value="${pct || 1}" aria-label="${name} brightness" data-bri="${esc(z.entity_id)}">` : ""}
        ${supportsColor ? `<div class="sw">
          ${this._config.swatches.map((h) => `<button type="button" title="${h}" aria-label="Set ${name} to ${h}" style="background:${h}" data-rgb="${esc(z.entity_id)}" data-hex="${h}"></button>`).join("")}
          <input type="color" aria-label="Pick a colour for ${name}" value="${rgb ? rgbToHex(rgb) : "#ffffff"}" data-pick="${esc(z.entity_id)}">
        </div>` : ""}
      </div>`;
  }

  _bind() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-toggle]").forEach((el) =>
      el.addEventListener("change", (e) => this._call(e.target.checked ? "turn_on" : "turn_off", el.dataset.toggle))
    );
    root.querySelectorAll("[data-bri]").forEach((el) =>
      el.addEventListener("change", () => this._call("turn_on", el.dataset.bri, { brightness_pct: Number(el.value) }))
    );
    root.querySelectorAll("[data-rgb]").forEach((el) =>
      el.addEventListener("click", () => this._call("turn_on", el.dataset.rgb, { rgb_color: hexToRgb(el.dataset.hex) }))
    );
    root.querySelectorAll("[data-pick]").forEach((el) =>
      el.addEventListener("change", () => this._call("turn_on", el.dataset.pick, { rgb_color: hexToRgb(el.value) }))
    );
  }
}

customElements.define("undumb-govee-card", UndumbGoveeCard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "undumb-govee-card",
  name: "Undumb Govee lamp",
  description: "Control every light of a multi-light Govee lamp.",
});
