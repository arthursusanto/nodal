/* ---------------------------------------------------------------------------
   The check every panel gets, whatever state put it on screen.

   This module MEASURES; `vetPanel` in drive.mjs reports what it measures.

   Bespoke checks only ever cover the states somebody thought to open. The
   880px occupancy bars that painted over a CLOSED facility's popover were
   never going to be caught that way: no step had ever opened that popover.
   So every panel and popover this harness opens goes through here as well, and
   the rules below hold for all of them:

     paints outside  every descendant's VISIBLE box — its own box intersected
                     with each clipping ancestor up to the panel — lies inside
                     the panel. Content scrolled out of a scroller is not
                     painted and does not count; a bar, badge or row that
                     escapes its panel is.
     hides content   nothing that CLIPS overflow is cutting its own children
                     off. The 880px bars never left the panel — they buried it,
                     and the chart silently cropped them.
     clipped text    no leaf, cell, or overflow-hidden box holds text wider (or
                     taller) than itself unless it can be scrolled to the rest.
                     Ellipsized header copy IS clipped text.
     forbidden text  no raw ISO stamp, no NaN / undefined / null / Infinity as
                     a word, no "[object Object]" — in text or in a title.
     overlap         no two interactive controls' boxes intersect.
     occluded        every enabled control's visible centre belongs to itself,
                     so nothing sits over something that must be clickable.
     hit area        a control the stylesheet pads with an invisible ::after —
                     its promise of a 44px target — measures 44 CSS px or more
                     with that pad counted.
     contrast        a badge or chip reads at 3:1 or better against whatever is
                     really behind it, semi-transparent panels composited.

   `label` names the screen AND the state, because that pair is what a failure
   has to send someone back to. Pass `{ modal: true }` for a panel a catch
   layer deliberately covers: the occlusion rule is the one that cannot hold
   there, and only it is dropped.
--------------------------------------------------------------------------- */
const FORBIDDEN = [
  ["raw ISO timestamp", "\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}"],
  ["NaN", "\\bNaN\\b"],
  ["undefined", "\\bundefined\\b"],
  ["null", "\\bnull\\b"],
  ["Infinity", "\\bInfinity\\b"],
  ["[object Object]", "\\[object Object\\]"],
];

export const VET_KINDS = ["outside", "hidden", "clipped", "forbidden", "overlap", "occluded", "hitArea", "contrast"];
export const VET_SAYS = {
  outside: "paints outside the panel",
  hidden: "is cut off by a box that clips it",
  clipped: "is clipped",
  forbidden: "shows machine text",
  overlap: "controls overlap",
  occluded: "is covered",
  hitArea: "misses the 44px target the stylesheet promises it",
  contrast: "reads under 3:1",
};

export async function vetFindings(page, selector, options = {}) {
  return page.evaluate(
    ({ selector, forbidden, modal }) => {
      const panel = document.querySelector(selector);
      if (!panel) return { missing: true };
      const pr = panel.getBoundingClientRect();
      const out = { outside: [], hidden: [], clipped: [], forbidden: [], overlap: [], occluded: [], hitArea: [], contrast: [], counted: 0 };

      const name = (el) => {
        const cls = typeof el.className === "string" && el.className.trim()
          ? `.${el.className.trim().split(/\s+/).join(".")}`
          : "";
        const text = (el.textContent || "").replace(/\s+/g, " ").trim().slice(0, 46);
        return `${el.tagName.toLowerCase()}${cls}${text ? ` "${text}"` : ""}`;
      };
      const shown = (el) => {
        const cs = getComputedStyle(el);
        return cs.display !== "none" && cs.visibility !== "hidden" && Number(cs.opacity) > 0;
      };
      // The box an element really PAINTS: its own, clipped by every ancestor
      // up to the panel that hides overflow.
      const visible = (el) => {
        const r = el.getBoundingClientRect();
        let box = { left: r.left, right: r.right, top: r.top, bottom: r.bottom };
        let p = el.parentElement;
        while (p) {
          const cs = getComputedStyle(p);
          if (cs.overflowX !== "visible" || cs.overflowY !== "visible") {
            const q = p.getBoundingClientRect();
            box = {
              left: Math.max(box.left, q.left),
              right: Math.min(box.right, q.right),
              top: Math.max(box.top, q.top),
              bottom: Math.min(box.bottom, q.bottom),
            };
          }
          if (p === panel) break;
          p = p.parentElement;
        }
        return box.right - box.left > 0.5 && box.bottom - box.top > 0.5 ? box : null;
      };
      // How much of an element's own TEXT does not fit the content box that is
      // supposed to hold it. A Range over the contents measures the laid-out
      // text and nothing else — no pseudo-elements, no positioned children.
      const range = document.createRange();
      const textFit = (el, cs) => {
        range.selectNodeContents(el);
        const rects = [...range.getClientRects()].filter((r) => r.width > 0 || r.height > 0);
        if (!rects.length) return null;
        const r = el.getBoundingClientRect();
        const inset = (a, b) =>
          (parseFloat(cs[`padding${a}`]) || 0) +
          (parseFloat(cs[`padding${b}`]) || 0) +
          (parseFloat(cs[`border${a}Width`]) || 0) +
          (parseFloat(cs[`border${b}Width`]) || 0);
        const boxWidth = r.width - inset("Left", "Right");
        const boxHeight = r.height - inset("Top", "Bottom");
        const textWidth = Math.max(...rects.map((x) => x.right)) - Math.min(...rects.map((x) => x.left));
        const textHeight = Math.max(...rects.map((x) => x.bottom)) - Math.min(...rects.map((x) => x.top));
        return {
          textWidth,
          textHeight,
          boxWidth,
          boxHeight,
          overflowX: textWidth - boxWidth,
          overflowY: textHeight - boxHeight,
        };
      };

      const all = [...panel.querySelectorAll("*")];
      out.counted = all.length;
      for (const el of all) {
        if (!shown(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        const box = visible(el);
        if (box) {
          const over = Math.max(pr.left - box.left, box.right - pr.right, pr.top - box.top, box.bottom - pr.bottom);
          if (over > 1) out.outside.push(`${name(el)} by ${Math.round(over)}px`);
        }
        const tag = el.tagName.toLowerCase();
        const cs = getComputedStyle(el);
        // A box that CLIPS is a box that hides: the 880px occupancy bars in a
        // 2.75rem chart did not escape their panel, they buried it, and the
        // chart quietly cut them off. Measured off real children, so the
        // invisible ::after hit pads never register as spill.
        const clips = (axis) => cs[`overflow${axis}`] === "hidden" || cs[`overflow${axis}`] === "clip";
        if (clips("X") || clips("Y")) {
          let worst = 0;
          let culprit = null;
          for (const child of el.children) {
            const cr = child.getBoundingClientRect();
            if (cr.width <= 0 && cr.height <= 0) continue;
            const spill = Math.max(
              clips("Y") ? Math.max(cr.bottom - r.bottom, r.top - cr.top) : 0,
              clips("X") ? Math.max(cr.right - r.right, r.left - cr.left) : 0,
            );
            if (spill > worst) {
              worst = spill;
              culprit = child;
            }
          }
          if (worst > 2) out.hidden.push(`${name(el)} cuts ${Math.round(worst)}px off ${name(culprit)}`);
        }
        // A container is wider than its box whenever any child is: only a box
        // that HOLDS the text (a leaf, a cell) or one that clips it can say
        // anything about the text being cut off.
        const holdsText =
          (el.textContent || "").trim() !== "" &&
          !["input", "textarea", "select", "svg", "canvas"].includes(tag) &&
          (el.childElementCount === 0 || tag === "td" || tag === "th" || cs.overflowX === "hidden" || cs.overflowX === "clip");
        if (holdsText) {
          const scrollsX = cs.overflowX === "auto" || cs.overflowX === "scroll";
          const scrollsY = cs.overflowY === "auto" || cs.overflowY === "scroll";
          // Measured off the TEXT, never off scrollWidth: this stylesheet pads
          // every button's hit area with an absolutely-positioned ::after that
          // hangs 0.4rem past the box, and scroll overflow counts that — which
          // would report every button in the app as clipped by 7px.
          const fit = textFit(el, cs);
          if (fit) {
            if (!scrollsX && fit.overflowX > 1) {
              out.clipped.push(`${name(el)} (text ${Math.round(fit.textWidth)}px in ${Math.round(fit.boxWidth)}px)`);
            }
            if (!scrollsY && cs.overflowY !== "visible" && fit.overflowY > 1) {
              out.clipped.push(`${name(el)} (text ${Math.round(fit.textHeight)}px tall in ${Math.round(fit.boxHeight)}px)`);
            }
          }
        }
      }

      // Machine text, in copy and in the titles that explain it. Scanned per
      // element, never over one concatenated blob: "…PLANNED" running into
      // "NaN" destroys the word boundary that makes NaN findable at all.
      const texts = [];
      for (const el of all) {
        const own = [...el.childNodes]
          .filter((n) => n.nodeType === 3)
          .map((n) => n.textContent)
          .join(" ")
          .trim();
        if (own) texts.push([own, el]);
        if (el.title) texts.push([el.title, el]);
        const aria = el.getAttribute && el.getAttribute("aria-label");
        if (aria) texts.push([aria, el]);
      }
      for (const [why, source] of forbidden) {
        const re = new RegExp(source);
        const hit = texts.find(([text]) => re.test(text));
        if (hit) out.forbidden.push(`${why}: "${re.exec(hit[0])[0]}" in ${name(hit[1])}`);
      }

      const controls = [...panel.querySelectorAll("button, input, select, textarea, a[href], [role=button]")]
        .filter((el) => {
          if (!shown(el)) return false;
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
      // Painted boxes, not laid-out ones: a queue row scrolled up under the
      // action bar, or down behind the disruption board, is clipped away and
      // overlaps nothing an operator can see or hit.
      const painted = controls.map((el) => ({ el, box: visible(el) })).filter((c) => c.box);
      for (let i = 0; i < painted.length; i++) {
        for (let j = i + 1; j < painted.length; j++) {
          const a = painted[i];
          const b = painted[j];
          if (a.el.contains(b.el) || b.el.contains(a.el)) continue;
          const w = Math.min(a.box.right, b.box.right) - Math.max(a.box.left, b.box.left);
          const h = Math.min(a.box.bottom, b.box.bottom) - Math.max(a.box.top, b.box.top);
          if (w > 1 && h > 1) {
            out.overlap.push(`${name(a.el)} over ${name(b.el)} (${Math.round(w)}x${Math.round(h)}px)`);
          }
        }
      }
      for (const el of controls) {
        if (!modal) {
          if (!el.disabled) {
            const box = visible(el);
            if (box) {
              const hit = document.elementFromPoint((box.left + box.right) / 2, (box.top + box.bottom) / 2);
              if (hit && hit !== el && !el.contains(hit)) out.occluded.push(`${name(el)} <- ${name(hit)}`);
            }
          }
        }
        // The stylesheet's invisible pad IS the promise; measure it.
        const after = getComputedStyle(el, "::after");
        if (!after || after.content === "none" || after.position !== "absolute") continue;
        const pad = ["top", "right", "bottom", "left"].map((side) => -parseFloat(after[side]));
        if (!pad.every((v) => Number.isFinite(v)) || !pad.some((v) => v > 0)) continue;
        const r = el.getBoundingClientRect();
        const w = r.width + pad[1] + pad[3];
        const h = r.height + pad[0] + pad[2];
        if (w < 44 || h < 44) out.hitArea.push(`${name(el)} ${Math.round(w)}x${Math.round(h)}px`);
      }

      const parse = (value) => {
        const m = /rgba?\(([^)]+)\)/.exec(value || "");
        if (!m) return null;
        const p = m[1].split(",").map((v) => parseFloat(v));
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
      };
      const over = (fg, bg) => ({
        r: fg.r * fg.a + bg.r * (1 - fg.a),
        g: fg.g * fg.a + bg.g * (1 - fg.a),
        b: fg.b * fg.a + bg.b * (1 - fg.a),
        a: 1,
      });
      const lum = (c) =>
        [c.r, c.g, c.b]
          .map((v) => {
            const s = v / 255;
            return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
          })
          .reduce((acc, v, i) => acc + [0.2126, 0.7152, 0.0722][i] * v, 0);
      // Panels are painted at 95% opacity over the map: what is behind a badge
      // is the whole stack, composited, not the nearest declared colour.
      const backdrop = (el) => {
        const layers = [];
        for (let p = el; p; p = p.parentElement) {
          const c = parse(getComputedStyle(p).backgroundColor);
          if (c && c.a > 0) layers.push(c);
          if (c && c.a >= 1) break;
        }
        layers.push({ r: 23, g: 18, b: 13, a: 1 }); // --bg-page, the floor
        let acc = layers[layers.length - 1];
        for (let i = layers.length - 2; i >= 0; i--) acc = over(layers[i], acc);
        return acc;
      };
      for (const el of panel.querySelectorAll(".badge, .chip")) {
        if (!shown(el) || (el.textContent || "").trim() === "") continue;
        const raw = parse(getComputedStyle(el).color);
        if (!raw) continue;
        const bg = backdrop(el);
        const l1 = lum(over(raw, bg));
        const l2 = lum(bg);
        const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
        if (ratio < 3) out.contrast.push(`${name(el)} ${ratio.toFixed(2)}:1`);
      }
      return out;
    },
    { selector, forbidden: FORBIDDEN, modal: !!options.modal },
  );
}
