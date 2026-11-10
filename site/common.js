// Shared helpers for every page.
export const $ = (sel, el = document) => el.querySelector(sel);
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const money = (n) => (n == null ? "—" : n >= 1e6 ? `$${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `$${Math.round(n / 1e3)}K` : `$${Math.round(n)}`);
export const moneyFull = (n) => (n == null ? "—" : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }));
export const pct = (n) => (n == null ? "—" : `${n}%`);
export const partyLetter = (p) => (p || "?")[0];
export const seat = (m) => (m.chamber === "senate" ? `Senator, ${m.state}` : `Rep., ${m.state}${m.district ? "-" + m.district : m.district === 0 ? "-AL" : ""}`);

const cache = new Map();
export async function getJSON(path) {
  if (!cache.has(path)) {
    const load = async (tries) => {
      try {
        const r = await fetch(path);
        if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
        return await r.json();
      } catch (e) {
        if (tries > 0 && !String(e.message).includes("HTTP 404")) return load(tries - 1);
        cache.delete(path);
        throw e;
      }
    };
    cache.set(path, load(2));
  }
  return cache.get(path);
}

export async function footer() {
  const el = $("footer .wrap");
  if (!el) return;
  try {
    const meta = await getJSON("data/meta.json");
    el.innerHTML = `Data built ${esc(new Date(meta.built).toLocaleString())}. Every figure links to its public source.
      Read the <a href="methodology.html">methodology</a> to see exactly how each number is calculated.`;
  } catch { /* ignore */ }
}
