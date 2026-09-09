"""The New England leaf-on mosaics, one map, from the local pyramid.

A cut of s2-ctrees-pair.py with the pair, the Sentinel-2 and the CTrees
sides gone: one maplibre map, the Landsat year as live tiles the kernel
renders from data/pyramid_v1 (src/landsat_mosaic/tiles.py), the plain Zarr
multiscales pyramid: each zoom reads the level nearest its pixel size, so
every zoom is a few chunks. Read only. `R` (or the button) re-opens the
store.

  YEAR   the slider with an arrow button either side, `[` `]` or the
         arrow keys, 2000..2025; a label every five years under the
         track, a taller tick where the store has the year, and the
         tiles the store has for the year counted in the header
  SHOW   true colour (red green blue), false colour (SWIR2 NIR red), NDVI
         (Carto Emrld, dark low, bright high, fixed -0.1..0.9, the ramp
         of the S2 x CTrees pair), the clear observations used per
         pixel (viridis, 0 to 20; the block mean at levels 1-7), the
         share of pixels borrowed from the year before or after (Blues,
         0 to 100%, from the source pass), or the source class of every
         pixel (own year grey, year before blue, year after orange,
         own-year Landsat 7 purple, unmatched yellow; zoom 10 and up,
         read from level 0). The two provenance modes draw nothing until
         source_pyramid.py has put the planes in the pyramid. A legend
         for each ramp and the class swatches.
  PIXEL  click the map: the level 0 pixel under the cursor is read and
         shown in the header (tile, row, col, clear looks, source class,
         the pick's day of year and date, borrowed share, NDVI, the six
         reflectances), with a ring on the map at the pixel centre. Click
         the readout's x, or press Escape, to clear it. The readout
         follows the year slider.
  SCALE  a gain on the two colour modes (double-click for 1.0)
  GRID   the 7x7 tile outlines with ids; land tiles without data for the
         year are drawn thicker and labelled "not yet"; the 16 all-water
         tiles the planner skipped are grey
  L labels · F full screen · R refresh

Run: uv run marimo edit mosaic-viewer.py   (marimo, anywidget are dev deps)
"""

import marimo

__generated_with = "0.24.0"
app = marimo.App(width="full")


@app.cell
def _():
    import asyncio
    import json
    import sys
    import time
    from pathlib import Path

    import marimo as mo
    import anywidget
    import traitlets

    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from landsat_mosaic import tiles
    return anywidget, asyncio, json, mo, tiles, time, traitlets


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # New England leaf-on mosaics

    One map, one year at a time, from the multiscales pyramid `data/pyramid_v1`.
    Zooms 4 to 13 read levels 7 to 0 (3840 m to 30 m): at each zoom the level
    whose pixel is the finest not smaller than the screen pixel. The header
    counts the tiles with data for the year; **grid** draws the 7 x 7 tile
    outlines, thicker where a land tile is empty. **R** re-opens the store.
    """)
    return


@app.cell
def _(tiles):
    YEARS = list(tiles.YEARS)
    YEAR0 = 2015
    SCALE0 = 1.0
    MODES = (("tc", "true colour"), ("fcc", "false colour"), ("ndvi", "NDVI"), ("count", "looks"),
             ("borrowed", "borrowed"), ("source", "source"))
    HOME = {"longitude": -71.4, "latitude": 44.0, "zoom": 6.4}
    VIEW_H = 780
    LABELS_SLOT = "watername_ocean"
    return HOME, LABELS_SLOT, MODES, SCALE0, VIEW_H, YEAR0, YEARS


@app.cell
def _(anywidget, asyncio, traitlets):
    class MosaicMap(anywidget.AnyWidget):
        """One maplibre map with a deck TileLayer whose tiles the kernel
        renders (custom `tile` messages, PNG bytes back).
        Kernel -> browser: `config` (JSON), `status` (string).
        Browser -> kernel: `view` (JSON lon/lat/zoom on moveend), `ctl`
        (JSON: year, mode, scale, labels, grid, refresh).
        Custom `probe` messages (lon, lat, year) read one level 0 pixel
        through `probe_fn` and answer with its values (JSON back)."""

        config = traitlets.Unicode("{}").tag(sync=True)
        status = traitlets.Unicode("").tag(sync=True)
        view = traitlets.Unicode("").tag(sync=True)
        ctl = traitlets.Unicode("").tag(sync=True)

        def __init__(self, **kw):
            super().__init__(**kw)
            self.tile_fn = None
            self.probe_fn = None
            self.on_msg(self._on_custom)

        def _on_custom(self, widget, content, buffers):
            if not isinstance(content, dict) or content.get("kind") not in ("tile", "probe"):
                return
            kind = content["kind"]
            try:
                asyncio.get_running_loop().create_task((self._tile if kind == "tile" else self._probe)(content))
            except RuntimeError as e:
                self.send({"kind": kind, "id": content.get("id"), "err": f"no loop: {e}"})

        async def _probe(self, c):
            if self.probe_fn is None:
                self.send({"kind": "probe", "id": c["id"], "err": "no probe_fn (re-run the wiring cell)"})
                return
            try:
                got = await self.probe_fn(float(c["lon"]), float(c["lat"]), int(c["year"]))
            except Exception as e:
                self.send({"kind": "probe", "id": c["id"], "err": f"{type(e).__name__}: {e}"})
                return
            self.send({"kind": "probe", "id": c["id"], "pixel": got})

        async def _tile(self, c):
            if self.tile_fn is None:
                self.send({"kind": "tile", "id": c["id"], "err": "no tile_fn (re-run the wiring cell)"})
                return
            try:
                png = await self.tile_fn(int(c["z"]), int(c["x"]), int(c["y"]), int(c["year"]), c.get("mode", "tc"))
            except Exception as e:
                self.send({"kind": "tile", "id": c["id"], "err": f"{type(e).__name__}: {e}"})
                return
            if png is None:
                self.send({"kind": "tile", "id": c["id"], "empty": True})
            else:
                self.send({"kind": "tile", "id": c["id"]}, buffers=[png])

        _esm = r"""
        import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@5.24.0/+esm";
        // deck.gl as its own self-contained dist bundle (one luma.gl inside),
        // with h3-js loaded first as the global `h3` the bundle looks up when it
        // evaluates (its one external). Per-package ESM builds do not hold
        // together from a CDN: esm.sh hangs in its build queue on the @loaders.gl
        // ranges geo-layers pulls in, and jsdelivr +esm resolves
        // @luma.gl/shadertools to two versions, which luma refuses to start.
        // Two static files, one version each.
        const H3_URL = "https://cdn.jsdelivr.net/npm/h3-js@4.5.0/dist/h3-js.umd.js";
        const DECK_URL = "https://cdn.jsdelivr.net/npm/deck.gl@9.3.10/dist.min.js";
        function loadScript(url, ready) {
          if (ready()) return Promise.resolve();
          return new Promise((ok, bad) => {
            let s = document.querySelector(`script[src="${url}"]`);
            if (!s) { s = document.createElement("script"); s.src = url; s.async = false; document.head.appendChild(s); }
            if (ready()) return ok();
            s.addEventListener("load", () => ok(), {once: true});
            s.addEventListener("error", () => bad(new Error("failed to load " + url)), {once: true});
          });
        }
        await loadScript(H3_URL, () => !!globalThis.h3?.cellToBoundary);
        await loadScript(DECK_URL, () => !!globalThis.deck?.MapboxOverlay);
        const {MapboxOverlay, BitmapLayer, PathLayer, ScatterplotLayer, TextLayer, TileLayer} = globalThis.deck;

        const STYLE = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";

        function bytesOf(v) {
          if (!v) return null;
          if (v instanceof DataView) return new Uint8Array(v.buffer, v.byteOffset, v.byteLength);
          if (v instanceof ArrayBuffer) return new Uint8Array(v);
          if (v.buffer) return new Uint8Array(v.buffer, v.byteOffset || 0, v.byteLength);
          return null;
        }

        function render({model, el}) {
          let cfg = {};
          try { cfg = JSON.parse(model.get("config") || "{}"); } catch (e) { cfg = {}; }
          const css = document.createElement("link");
          css.rel = "stylesheet"; css.href = "https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.css";
          const font = "font:12px ui-sans-serif,system-ui,sans-serif";
          const mono = "font:11px ui-monospace,Menlo,monospace";
          const root = document.createElement("div");
          root.style.cssText = "width:100%;background:#fff;color:#222;" + font;
          const pane = document.createElement("div");
          pane.style.cssText = "position:relative;width:100%;height:" + (cfg.height || 720) + "px;background:#f4f2ee";
          const mapEl = document.createElement("div");
          mapEl.style.cssText = "position:absolute;inset:0";
          const head = document.createElement("div");
          head.style.cssText = "position:absolute;left:8px;top:8px;z-index:5;display:flex;flex-direction:column;gap:.3rem;align-items:flex-start;" +
            "max-width:calc(100% - 72px);background:rgba(255,255,255,.94);color:#1d1d1b;padding:4px 8px;border-radius:6px;" +
            "box-shadow:0 1px 3px rgba(0,0,0,.18);white-space:nowrap;font-variant-numeric:tabular-nums";
          pane.append(mapEl, head);
          const strip = document.createElement("div");
          strip.style.cssText = "display:flex;flex-direction:column;gap:.25rem;padding:.35rem .4rem;background:#fff;color:#222";
          const status = document.createElement("div");
          status.style.cssText = "font:13px ui-sans-serif,system-ui,sans-serif;color:#444;white-space:pre-wrap";
          const hint = document.createElement("div");
          hint.style.cssText = mono + ";color:#666;opacity:.7";
          hint.textContent = "keys: \u2190 \u2192 or [ ] year · ; ' scale · 1-6 show · G grid · L labels · R refresh · F full screen · click the map for the pixel · Esc clears it";
          strip.append(status, hint);
          root.append(pane, strip);
          el.append(css, root);

          // ---- the controls ------------------------------------------------
          const ACCENT = "#2a5db0";
          const btnCss = font + ";padding:.15rem .55rem;border:0;background:transparent;color:#1d1d1b;cursor:pointer;line-height:1.4;font-variant-numeric:tabular-nums";
          const onCss = (b, on) => { b.style.background = on ? ACCENT : "transparent"; b.style.color = on ? "#fff" : "#1d1d1b"; };
          const labCss = "font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:#6b6b68";
          let year = cfg.year, mode = cfg.mode || "tc", scale = Number(cfg.scale) || 1;
          let labelsOn = cfg.labels !== false, gridOn = cfg.grid !== false;
          const years = cfg.years || [];
          const step = (arr, cur, d) => { const i = arr.indexOf(cur); return arr[Math.max(0, Math.min(arr.length - 1, (i < 0 ? 0 : i) + d))]; };
          const send = (act) => {
            model.set("ctl", JSON.stringify({act, year, mode, scale, labels: labelsOn, grid: gridOn, n: Date.now()}));
            model.save_changes();
          };
          const newRow = () => { const r = document.createElement("span"); r.style.cssText = "display:inline-flex;gap:.8rem;align-items:center"; head.appendChild(r); return r; };
          const rowOf = () => (head.lastElementChild && head.lastElementChild.tagName === "SPAN") ? head.lastElementChild : newRow();
          const mkGroup = (title, values, get, set, act) => {
            const wrap = document.createElement("span");
            wrap.style.cssText = "display:inline-flex;align-items:center;gap:.4rem";
            const lab = document.createElement("span"); lab.textContent = title; lab.style.cssText = labCss;
            const seg = document.createElement("span");
            seg.style.cssText = "display:inline-flex;border:1px solid rgba(29,29,27,.28);border-radius:5px;overflow:hidden";
            const btns = values.map((v, i) => {
              const b = document.createElement("button"); b.textContent = v.label; b.style.cssText = btnCss;
              if (i) b.style.borderLeft = "1px solid rgba(29,29,27,.18)";
              b.dataset.value = v.value;
              b.onclick = () => { set(v.value); style(); update(); send(act); };
              seg.appendChild(b); return b;
            });
            wrap.append(lab, seg);
            rowOf().appendChild(wrap);
            const style = () => btns.forEach((b) => onCss(b, b.dataset.value === String(get())));
            style();
            return style;
          };

          const sty = document.createElement("style");
          sty.textContent = [
            ".mv-scale{position:relative;display:inline-block;width:260px;height:22px}",
            ".mv-scale .trk{position:absolute;left:8px;right:8px;top:9px;height:4px;border-radius:2px;background:rgba(29,29,27,.18)}",
            ".mv-scale .spn{position:absolute;left:8px;top:9px;height:4px;border-radius:2px;background:" + ACCENT + "}",
            ".mv-scale .tks{position:absolute;left:8px;right:8px;top:14px;height:8px;display:flex;justify-content:space-between;pointer-events:none}",
            ".mv-scale .tks span{width:1px;height:5px;background:rgba(29,29,27,.35)}",
            ".mv-scale .tks span.has{background:" + ACCENT + ";height:7px}",
            ".mv-year{height:34px;width:340px}",
            ".mv-year .tks{top:14px;height:16px}",
            ".mv-year .tks span{position:relative;width:1px;height:5px;overflow:visible}",
            ".mv-year .tks span i{position:absolute;top:7px;left:0;transform:translateX(-50%);font-style:normal;font-size:9px;color:#6b6b68;line-height:1;white-space:nowrap}",
            ".mv-year .tks span.has i{color:#1d1d1b}",
            ".mv-arrow{" + btnCss + ";width:26px;height:26px;padding:0;border:1px solid rgba(29,29,27,.28);border-radius:5px;font-size:13px;line-height:1;display:inline-flex;align-items:center;justify-content:center}",
            ".mv-arrow:hover{background:rgba(42,93,176,.12)}",
            ".mv-arrow:disabled{opacity:.3;cursor:default;background:transparent}",
            ".mv-scale input{position:absolute;left:0;top:0;width:100%;height:22px;margin:0;background:none;-webkit-appearance:none;appearance:none}",
            ".mv-scale input:focus{outline:none}",
            ".mv-scale input::-webkit-slider-runnable-track{background:transparent}",
            ".mv-scale input::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:#fff;border:2px solid " + ACCENT + ";margin-top:3px;cursor:pointer}",
            ".mv-scale input::-moz-range-track{background:transparent}",
            ".mv-scale input::-moz-range-thumb{width:12px;height:12px;border-radius:50%;background:#fff;border:2px solid " + ACCENT + ";cursor:pointer}",
          ].join("\n");
          head.appendChild(sty);

          // the year slider: arrows either side, a labelled tick every five
          // years, a taller tick under the years the store has
          const yrWrap = document.createElement("span");
          yrWrap.style.cssText = "display:inline-flex;align-items:center;gap:.4rem";
          const yrLab = document.createElement("span"); yrLab.textContent = "year"; yrLab.style.cssText = labCss;
          const mkArrow = (txt, title, d) => {
            const b = document.createElement("button"); b.className = "mv-arrow"; b.textContent = txt; b.title = title;
            b.onclick = () => { stepYear(d); };
            return b;
          };
          const yrPrev = mkArrow("\u25C0", "previous year ([ or the left arrow)", -1);
          const yrNext = mkArrow("\u25B6", "next year (] or the right arrow)", 1);
          const yr = document.createElement("span"); yr.className = "mv-scale mv-year";
          const yrTrk = document.createElement("span"); yrTrk.className = "trk";
          const yrSpn = document.createElement("span"); yrSpn.className = "spn";
          const yrTks = document.createElement("span"); yrTks.className = "tks";
          const tickLabel = (y, i, arr) => (y % 5 === 0 || i === 0 || i === arr.length - 1) ? String(y) : "";
          const ticks = years.map((y, i, arr) => {
            const t = document.createElement("span"); const l = document.createElement("i"); l.textContent = tickLabel(y, i, arr);
            t.appendChild(l); yrTks.appendChild(t); return t;
          });
          const yri = document.createElement("input"); yri.type = "range"; yri.min = 0; yri.max = Math.max(0, years.length - 1); yri.step = 1;
          yri.title = "which year is drawn (the arrows, [ ] or the arrow keys)";
          const yrTxt = document.createElement("span");
          yrTxt.style.cssText = "font-variant-numeric:tabular-nums;min-width:11em";
          yr.append(yrTrk, yrSpn, yrTks, yri);
          yrWrap.append(yrLab, yrPrev, yr, yrNext, yrTxt);
          rowOf().appendChild(yrWrap);
          const styleYear = () => {
            const i = Math.max(0, years.indexOf(year)), n = Math.max(1, years.length - 1);
            yri.value = i;
            yrSpn.style.width = Math.max(0, (yr.clientWidth - 16) * i / n) + "px";
            const have = cfg.have || {};
            ticks.forEach((t, k) => { t.className = have[years[k]] ? "has" : ""; });
            yrPrev.disabled = i <= 0;
            yrNext.disabled = i >= years.length - 1;
            const c = have[year];
            yrTxt.textContent = String(year) + (c == null ? "" : " \u00b7 " + c + " of " + (cfg.ntiles || 49) + " tiles");
          };
          let yrSent = year, yrTimer = null;
          const yrRelease = () => { if (yrTimer) { clearTimeout(yrTimer); yrTimer = null; } if (year !== yrSent) { yrSent = year; send("year"); askProbe(); } };
          const stepYear = (d) => { year = step(years, year, d); styleYear(); update(); yrRelease(); };
          yri.addEventListener("input", () => { year = years[Number(yri.value)]; styleYear(); update(); if (yrTimer) clearTimeout(yrTimer); yrTimer = setTimeout(yrRelease, 150); });
          yri.addEventListener("change", yrRelease);
          setTimeout(styleYear, 0);
          try { new ResizeObserver(styleYear).observe(yr); } catch (e) {}

          newRow();
          const modes = (cfg.modes || []).map((m) => ({value: m[0], label: m[1]}));
          const styleMode0 = mkGroup("show", modes, () => mode, (v) => { mode = v; }, "mode");
          // a legend bar for the two ramps: NDVI (Emrld, dark low, bright high) and clear looks (viridis)
          const leg = document.createElement("span");
          leg.style.cssText = "display:inline-flex;align-items:center;gap:.35rem;font-variant-numeric:tabular-nums";
          rowOf().appendChild(leg);
          const styleLegend = () => {
            leg.replaceChildren();
            if (mode === "source") {
              // categorical swatches for the source classes
              leg.title = "where each pixel's value came from (source pass): own year, the year before or after, own-year Landsat 7, or unmatched; nodata is clear. Read from the 30 m plane, so it draws from zoom " + (cfg.source_min_z || 10) + " up; below that use borrowed for the share";
              (cfg.source_legend || []).forEach((c) => {
                const sw = document.createElement("span");
                sw.style.cssText = "display:inline-flex;align-items:center;gap:.25rem;opacity:.9";
                const dot = document.createElement("span");
                dot.style.cssText = "display:inline-block;width:11px;height:11px;border-radius:2px;border:1px solid rgba(0,0,0,.25);background:" + c[2];
                const t = document.createElement("span"); t.textContent = c[1];
                sw.append(dot, t); leg.appendChild(sw);
              });
              const note = document.createElement("span"); note.style.opacity = ".6";
              note.textContent = "zoom " + (cfg.source_min_z || 10) + "+";
              leg.appendChild(note);
              return;
            }
            const ramp = mode === "ndvi" ? cfg.ndvi_ramp : mode.startsWith("count") ? cfg.count_ramp : mode === "borrowed" ? cfg.borrowed_ramp : null;
            if (!ramp) return;
            const lo = document.createElement("span"), bar = document.createElement("span"), hi = document.createElement("span");
            lo.textContent = mode === "ndvi" ? "NDVI " + (cfg.ndvi_lo != null ? cfg.ndvi_lo : -0.1) : mode === "borrowed" ? "borrowed 0%" : "looks 0";
            hi.textContent = mode === "ndvi" ? String(cfg.ndvi_hi != null ? cfg.ndvi_hi : 0.9) : mode === "borrowed" ? "100%" : String(cfg.count_max || 20) + "+";
            lo.style.opacity = hi.style.opacity = ".75";
            bar.style.cssText = "display:inline-block;width:10rem;height:12px;border-radius:2px;border:1px solid rgba(0,0,0,.15);background:linear-gradient(90deg," + ramp.join(",") + ")";
            leg.title = mode === "ndvi" ? "NDVI = (NIR - red) / (NIR + red) of the scaled reflectance, one fixed scale for every year; dark low, bright high (Carto Emrld)"
                      : mode === "borrowed" ? "share of the pixels whose value is a look from the year before or after (source pass, borrowed_pct); at levels 1-7 the share over the block; clear where the pass has not run"
                      : mode === "count" ? "clear observations used per pixel (viridis); below zoom 11 one pixel in 8 is sampled"
                      : "clear observations used per pixel (viridis); below zoom 11 the 8 x 8 block " + mode.slice(6) + ", as a pyramid level would hold it";
            leg.append(lo, bar, hi);
          };
          const styleMode = () => { styleMode0(); styleLegend(); };
          styleLegend();

          // the scale: a gain on the colour modes
          const SC_MIN = 0.2, SC_MAX = 3;
          const scWrap = document.createElement("span");
          scWrap.style.cssText = "display:inline-flex;align-items:center;gap:.4rem";
          const scLab = document.createElement("span"); scLab.textContent = "scale"; scLab.style.cssText = labCss;
          const scr = document.createElement("span"); scr.className = "mv-scale"; scr.style.width = "140px";
          const scTrk = document.createElement("span"); scTrk.className = "trk";
          const scSpn = document.createElement("span"); scSpn.className = "spn";
          const sc = document.createElement("input"); sc.type = "range"; sc.min = SC_MIN; sc.max = SC_MAX; sc.step = 0.1;
          sc.title = "brightness of the colour modes (double-click for 1.0)";
          const scTxt = document.createElement("span");
          scTxt.style.cssText = "font-variant-numeric:tabular-nums;min-width:2.4em";
          scr.append(scTrk, scSpn, sc);
          scWrap.append(scLab, scr, scTxt);
          rowOf().appendChild(scWrap);
          const styleSc = () => {
            sc.value = scale;
            scSpn.style.width = Math.max(0, (scr.clientWidth - 16) * (scale - SC_MIN) / (SC_MAX - SC_MIN)) + "px";
            scTxt.textContent = scale.toFixed(1) + "×";
          };
          let scSent = scale, scTimer = null;
          const scRelease = () => { if (scTimer) { clearTimeout(scTimer); scTimer = null; } if (scale !== scSent) { scSent = scale; send("scale"); } };
          sc.addEventListener("input", () => { scale = Number(sc.value); styleSc(); if (scTimer) clearTimeout(scTimer); scTimer = setTimeout(scRelease, 150); });
          sc.addEventListener("change", scRelease);
          scr.addEventListener("dblclick", (e) => { e.preventDefault(); scale = 1; styleSc(); scRelease(); });
          setTimeout(styleSc, 0);
          try { new ResizeObserver(styleSc).observe(scr); } catch (e) {}

          // toggles: grid, labels, refresh
          const mkBtn = (text, title, on) => {
            const b = document.createElement("button"); b.textContent = text; b.title = title;
            b.style.cssText = btnCss + ";border:1px solid rgba(29,29,27,.28);border-radius:5px";
            onCss(b, on); rowOf().appendChild(b); return b;
          };
          const gridBtn = mkBtn("grid", "the 7 x 7 tile outlines and ids; thicker where a land tile is not in the store yet, grey where the tile is all water and was skipped (G)", gridOn);
          gridBtn.onclick = () => { gridOn = !gridOn; onCss(gridBtn, gridOn); update(); send("grid"); };
          const labBtn = mkBtn("labels", "basemap labels (L)", labelsOn);
          labBtn.onclick = () => { labelsOn = !labelsOn; onCss(labBtn, labelsOn); labels(labelsOn); send("labels"); };
          const refBtn = mkBtn("refresh", "re-open the pyramid store and recount the tiles (R)", false);
          refBtn.onclick = () => { send("refresh"); };

          // ---- the pixel readout: click the map, the kernel reads level 0 ---
          const pix = document.createElement("div");
          pix.style.cssText = mono + ";display:none;align-items:baseline;gap:.6rem;flex-wrap:wrap;color:#1d1d1b;border-top:1px solid rgba(29,29,27,.15);padding-top:.25rem;max-width:100%;white-space:normal";
          pix.title = "the 30 m pixel under the click; the ring on the map marks its centre";
          head.appendChild(pix);
          let probe = null;        // {lon, lat} of the last click, re-read when the year changes
          let pixel = null;        // the kernel's answer: null reading, false outside, {err} or the pixel
          let pseq = 0;
          const ppending = new Map();
          const doyDate = (year, doy) => {
            if (!doy) return "";
            const d = new Date(Date.UTC(year, 0, doy));
            return d.toLocaleDateString(undefined, {month: "short", day: "numeric", timeZone: "UTC"});
          };
          const kv = (k, v, title) => {
            const s = document.createElement("span");
            const a = document.createElement("span"); a.textContent = k + " "; a.style.cssText = "color:#6b6b68";
            const b = document.createElement("span"); b.textContent = v;
            s.append(a, b); if (title) s.title = title; return s;
          };
          const swatch = (hex) => {
            const d = document.createElement("span");
            d.style.cssText = "display:inline-block;width:9px;height:9px;border-radius:2px;border:1px solid rgba(0,0,0,.3);vertical-align:-1px;margin-right:.25rem;background:" + (hex || "transparent");
            return d;
          };
          const stylePix = () => {
            pix.replaceChildren();
            if (!probe) { pix.style.display = "none"; return; }
            pix.style.display = "flex";
            const x = document.createElement("button"); x.textContent = "\u00d7"; x.title = "clear the pixel (Esc)";
            x.style.cssText = btnCss + ";padding:0 .3rem;color:#6b6b68";
            x.onclick = () => { clearProbe(); };
            pix.appendChild(x);
            if (pixel === null) { pix.appendChild(kv("pixel", "reading\u2026")); return; }
            if (pixel === false) { pix.appendChild(kv("pixel", "outside the grid")); return; }
            if (pixel.err) { pix.appendChild(kv("pixel", pixel.err)); return; }
            const p = pixel;
            pix.appendChild(kv("pixel", p.tile + " r" + p.row + " c" + p.col, p.clat.toFixed(5) + ", " + p.clon.toFixed(5) + " (pixel centre)"));
            pix.appendChild(kv("looks", String(p.clear_count), "clear observations used for this pixel in " + p.year));
            const legend = cfg.source_legend || [];
            const cls = legend.find((c) => c[0] === p.source);
            const src = document.createElement("span");
            const a = document.createElement("span"); a.textContent = "source "; a.style.cssText = "color:#6b6b68";
            src.appendChild(a);
            if (p.source == null) src.appendChild(document.createTextNode(p.has_planes === false ? "no plane yet" : "not computed"));
            else { src.appendChild(swatch(cls ? cls[2] : null)); src.appendChild(document.createTextNode(cls ? cls[1] : p.source)); }
            src.title = "which look the value came from: own year, the year before or after (borrowed), own-year Landsat 7, nodata, or unmatched; 'no plane yet' until source_pyramid.py has run, 'not computed' where the pass has not reached";
            pix.appendChild(src);
            if (p.pick_doy) {
              const py = p.source === "before" ? p.year - 1 : p.source === "after" ? p.year + 1 : p.year;
              pix.appendChild(kv("picked", "day " + p.pick_doy + " (" + doyDate(py, p.pick_doy) + (py !== p.year ? " " + py : "") + ")", "day of year of the picked look, in the look's own year"));
            }
            if (p.borrowed_pct != null) pix.appendChild(kv("borrowed", p.borrowed_pct + "%", "borrowed_pct at level 0: 0 or 100 per pixel"));
            const r = p.refl || {};
            if (r.nir != null && r.red != null && (r.nir + r.red) !== 0) pix.appendChild(kv("NDVI", ((r.nir - r.red) / (r.nir + r.red)).toFixed(3)));
            const bands = ["blue", "green", "red", "nir", "swir1", "swir2"];
            const anyBand = bands.some((b) => r[b] != null);
            pix.appendChild(kv("refl", anyBand ? bands.map((b) => b + " " + (r[b] == null ? "\u2013" : r[b].toFixed(3))).join("  ") : "nodata", "surface reflectance of the six bands (scaled), a dash where the band is 0"));
          };
          const askProbe = () => {
            if (!probe) return;
            const id = ++pseq;
            pixel = null; stylePix();
            ppending.set(id, true);
            model.send({kind: "probe", id, lon: probe.lon, lat: probe.lat, year});
          };
          const clearProbe = () => { probe = null; pixel = null; stylePix(); update(); };
          model.on("msg:custom", (msg) => {
            if (!msg || msg.kind !== "probe") return;
            if (!ppending.has(msg.id)) return;
            ppending.delete(msg.id);
            if (msg.id !== pseq) return;                  // an older click's answer
            pixel = msg.err ? {err: msg.err} : (msg.pixel || false);
            stylePix(); update();
          });
          stylePix();

          const toggleFull = () => {
            if (document.fullscreenElement) { document.exitFullscreen && document.exitFullscreen(); return; }
            if (root.requestFullscreen) root.requestFullscreen();
          };
          const paneHeight = () => {
            const h = document.fullscreenElement === root ? window.innerHeight - strip.offsetHeight : (cfg.height || 720);
            pane.style.height = h + "px";
            if (map) setTimeout(() => { try { map.resize(); } catch (e) {} }, 30);
          };
          document.addEventListener("fullscreenchange", () => { setTimeout(paneHeight, 30); });
          root.tabIndex = 0;
          root.addEventListener("pointerup", (e) => {
            if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
            setTimeout(() => { try { root.focus({preventScroll: true}); } catch (err) {} }, 0);
          });
          root.addEventListener("keydown", (e) => {
            if (e.target && /^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
            const k = e.key;
            if (k === "[" || k === "]" || k === "ArrowLeft" || k === "ArrowRight") { stepYear((k === "]" || k === "ArrowRight") ? 1 : -1); }
            else if (k === ";" || k === "'") { scale = Math.round(10 * Math.max(SC_MIN, Math.min(SC_MAX, scale + (k === "'" ? 0.1 : -0.1)))) / 10; styleSc(); scRelease(); }
            else if (k >= "1" && k <= "9") { const m = modes[Number(k) - 1]; if (m) { mode = m.value; styleMode(); update(); send("mode"); } }
            else if (k === "g" || k === "G") { gridBtn.onclick(); }
            else if (k === "l" || k === "L") { labBtn.onclick(); }
            else if (k === "r" || k === "R") { send("refresh"); }
            else if (k === "f" || k === "F") { toggleFull(); }
            else if (k === "Escape") { if (!probe) return; clearProbe(); }
            else return;
            e.preventDefault();
          });

          const say = (t) => { status.textContent = t || ""; };

          // ---- the tiles: ask the kernel ------------------------------------
          const pending = new Map();
          let tseq = 0;
          const tstat = {asked: 0, got: 0, empty: 0, err: 0, abort: 0};
          model.on("msg:custom", (msg, buffers) => {
            if (!msg || msg.kind !== "tile") return;
            const p = pending.get(msg.id);
            if (!p) return;
            pending.delete(msg.id);
            if (msg.err) { tstat.err++; say("tile: " + msg.err); p.reject(new Error(msg.err)); return; }
            if (msg.empty || !buffers || !buffers.length) { tstat.empty++; p.resolve(null); return; }
            const u8 = bytesOf(buffers[0]);
            createImageBitmap(new Blob([u8], {type: "image/png"})).then(
              (b) => { tstat.got++; p.resolve(b); },
              (e) => { tstat.err++; p.reject(e instanceof Error ? e : new Error("decode")); });
          });
          const getTileDataFor = (yr_, md) => ({index, signal}) => new Promise((resolve, reject) => {
            const id = ++tseq;
            tstat.asked++;
            pending.set(id, {resolve, reject});
            model.send({kind: "tile", id, year: yr_, mode: md, x: index.x, y: index.y, z: index.z});
            if (signal) signal.addEventListener("abort", () => {
              pending.delete(id); tstat.abort++;
              const e = new Error("aborted"); e.name = "AbortError"; reject(e);
            });
          });

          // ---- the layers ---------------------------------------------------
          let map = null, ov = null;
          const slot = () => cfg.labels_slot || "watername_ocean";
          const mkRaster = () => new TileLayer({
            id: "ls-" + year + "-" + mode + "-g" + (cfg.gen || 0) + (mode === "tc" || mode === "fcc" ? "-s" + scale : ""),
            getTileData: getTileDataFor(year, mode),
            onTileError: (e) => { if (!e || e.name !== "AbortError") say("tile: " + ((e && e.message) || e)); },
            tileSize: 256,
            minZoom: cfg.min_z || 4, maxZoom: cfg.max_z || 13,
            extent: cfg.bbox || null,
            refinementStrategy: "best-available",
            beforeId: slot(),
            renderSubLayers: (p) => {
              if (!p.data) return null;
              const {west, south, east, north} = p.tile.bbox;
              return new BitmapLayer(p, {data: null, image: p.data, bounds: [west, south, east, north]});
            },
          });
          const gridLayers = () => {
            const tiles = cfg.tiles || [];
            const done = new Set((cfg.done || {})[year] || []);
            const box = (t) => [[t[1], t[2]], [t[3], t[2]], [t[3], t[4]], [t[1], t[4]], [t[1], t[2]]];
            const isDone = (t) => done.has(t[0]);
            const isWater = (t) => t[5] === false;   // no land: never scheduled
            return [
              new PathLayer({id: "grid-lines", data: tiles, getPath: box,
                getColor: (t) => isWater(t) ? [120, 120, 120, 90] : isDone(t) ? [40, 40, 40, 170] : [42, 93, 176, 230],
                widthUnits: "pixels", getWidth: (t) => isDone(t) || isWater(t) ? 1 : 3, widthMinPixels: 1,
                updateTriggers: {getColor: [year, cfg.gen], getWidth: [year, cfg.gen]}}),
              new TextLayer({id: "grid-ids", data: tiles,
                getPosition: (t) => [(t[1] + t[3]) / 2, (t[2] + t[4]) / 2],
                getText: (t) => t[0] + (isWater(t) ? "\nwater, skipped" : isDone(t) ? "" : "\nnot yet"),
                getColor: (t) => isWater(t) ? [110, 110, 110, 200] : isDone(t) ? [30, 30, 30, 220] : [42, 93, 176, 255],
                getSize: 13, background: true, getBackgroundColor: [255, 255, 255, 190], backgroundPadding: [4, 2],
                fontFamily: "ui-monospace, Menlo, monospace",
                updateTriggers: {getText: [year, cfg.gen], getColor: [year, cfg.gen]}}),
            ];
          };
          const probeLayer = () => {
            // a ring at the probed pixel's centre (at the click until the kernel answers)
            const at = pixel && pixel.clon != null ? [pixel.clon, pixel.clat] : [probe.lon, probe.lat];
            return new ScatterplotLayer({id: "probe-ring", data: [at], getPosition: (d) => d,
              radiusUnits: "pixels", getRadius: 9, stroked: true, filled: false, getLineColor: [42, 93, 176, 255],
              lineWidthUnits: "pixels", getLineWidth: 2.5, pickable: false});
          };
          function update() {
            if (!ov) return;
            const layers = [mkRaster()];
            if (gridOn) layers.push(...gridLayers());
            if (probe) layers.push(probeLayer());
            ov.setProps({layers});
          }
          function labels(on) {
            if (!map || !map.isStyleLoaded()) return;
            const st = map.getStyle();
            if (!st || !st.layers) return;
            st.layers.forEach((l) => {
              if (l.layout && l.layout["text-field"] !== undefined)
                map.setLayoutProperty(l.id, "visibility", on ? "visible" : "none");
            });
          }
          let seq = 0;
          function sendView() {
            if (!map) return;
            const c = map.getCenter();
            model.set("view", JSON.stringify({longitude: c.lng, latitude: c.lat, zoom: map.getZoom(), n: ++seq}));
            model.save_changes();
          }
          function boot() {
            const home = cfg.home || {longitude: -71.4, latitude: 44, zoom: 6.4};
            map = new maplibregl.Map({container: mapEl, style: STYLE, center: [home.longitude, home.latitude], zoom: home.zoom,
              attributionControl: {compact: false}});
            map.keyboard.disable();
            map.addControl(new maplibregl.NavigationControl({showCompass: false}), "top-right");
            ov = new MapboxOverlay({interleaved: true, layers: [], onError: (e) => say("deck: " + (e && e.message ? e.message : e))});
            map.addControl(ov);
            map.on("load", () => { labels(labelsOn); update(); sendView(); });
            map.on("click", (ev) => {
              if (!ev || !ev.lngLat) return;
              probe = {lon: ev.lngLat.lng, lat: ev.lngLat.lat};
              askProbe(); update();
            });
            map.on("moveend", sendView);
            map.on("error", (ev) => { if (ev && ev.error && ev.error.message) say("map: " + ev.error.message); });
            new ResizeObserver(() => { try { map.resize(); } catch (e) {} }).observe(mapEl);
            window.__mvTiles = tstat;
          }
          model.on("change:status", () => say(model.get("status")));
          model.on("change:config", () => {
            try { cfg = JSON.parse(model.get("config") || "{}"); } catch (e) { cfg = {}; }
            styleYear();
            update();
          });
          try { say(model.get("status")); boot(); }
          catch (e) { say("boot: " + e.message); console.error(e); }
          return () => { try { map && map.remove(); } catch (e) {} };
        }
        export default {render};
        """

    return (MosaicMap,)


@app.cell
def _(HOME, LABELS_SLOT, MODES, MosaicMap, SCALE0, VIEW_H, YEAR0, YEARS, json, tiles):
    # ---- the map: built ONCE, empty; never re-runs for a parameter ---------------
    from landsat_mosaic import grid as _grid
    _tiles = tiles.tile_grid()
    mv = MosaicMap(config=json.dumps({
        "height": VIEW_H, "home": HOME, "labels": True, "labels_slot": LABELS_SLOT, "grid": True,
        "year": YEAR0, "years": YEARS, "mode": "tc", "modes": [list(m) for m in MODES], "scale": SCALE0,
        "min_z": tiles.MIN_Z, "max_z": tiles.MAX_Z, "gen": 0,
        "ndvi_ramp": tiles.NDVI_HEX, "ndvi_lo": tiles.NDVI_LO, "ndvi_hi": tiles.NDVI_HI,
        "count_ramp": list(tiles.VIRIDIS), "count_max": tiles.COUNT_MAX,
        "borrowed_ramp": tiles.BORROWED_HEX, "source_legend": tiles.SOURCE_LEGEND, "source_min_z": tiles.SOURCE_MIN_Z,
        "bbox": [_grid.WEST, _grid.SOUTH, _grid.EAST, _grid.NORTH],
        "tiles": _tiles, "ntiles": sum(1 for t in _tiles if t[5]), "done": {}, "have": {},
    }))
    HOLD = {"year": YEAR0, "mode": "tc", "scale": SCALE0, "h_ctl": None, "loop": None, "served": 0, "empty": 0}
    mv
    return HOLD, mv


@app.cell
def _(HOLD, YEARS, asyncio, json, mv, tiles, time):
    # ---- wiring: tiles, controls, the header counts. Re-runs freely. --------------
    try:
        HOLD["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass

    def _cfg(**kw):
        c = json.loads(mv.config or "{}")
        c.update(kw)
        mv.config = json.dumps(c)

    def _say(msg):
        try:
            mv.status = msg
        except Exception:
            pass

    async def _tile_fn(z, x, y, year, mode="tc"):
        gain = HOLD["scale"] if mode in ("tc", "fcc") else 1.0
        png = await asyncio.to_thread(tiles.render, z, x, y, year, mode, gain)
        HOLD["served" if png is not None else "empty"] += 1
        return png

    mv.tile_fn = _tile_fn

    async def _probe_fn(lon, lat, year):
        return await asyncio.to_thread(tiles.probe, lon, lat, year)

    mv.probe_fn = _probe_fn

    def _count_year(year):
        """Tile ids with data for the year (one 240 m plane read, well under
        a second); pushes `done` and `have` into the config."""
        t0 = time.time()
        done = tiles.tiles_done(year)
        c = json.loads(mv.config or "{}")
        c.setdefault("done", {})[str(year)] = done
        c.setdefault("have", {})[str(year)] = len(done)
        c["gen"] = tiles.status()["gen"]
        mv.config = json.dumps(c)
        return done, time.time() - t0

    def _status_line(year, done, secs=None):
        st = tiles.status()
        when = time.strftime("%H:%M:%S", time.localtime(st["opened"])) if st["opened"] else "?"
        n = json.loads(mv.config or "{}").get("ntiles", 33)
        line = (f"{year}: {len(done)} of {n} land tiles in the pyramid · store opened {when}"
                f" · tiles {HOLD['served']:,} drawn, {HOLD['empty']:,} empty")
        if secs is not None and secs > 0.5:
            line += f" · counted in {secs:.1f} s"
        return line

    async def _year(year):
        _say(f"{year}: counting tiles…")
        done, secs = await asyncio.to_thread(_count_year, year)
        _say(_status_line(year, done, secs))

    async def _refresh():
        _say("re-opening the store…")
        await asyncio.to_thread(tiles.refresh)
        # every year already counted is recounted so the ticks stay honest
        c = json.loads(mv.config or "{}")
        years = [int(y) for y in (c.get("have") or {})] or [HOLD["year"]]
        if HOLD["year"] not in years:
            years.append(HOLD["year"])
        for y in years:
            await asyncio.to_thread(_count_year, y)
        done = tiles.tiles_done(HOLD["year"])
        _say(_status_line(HOLD["year"], done) + " · refreshed")

    def _spawn(coro):
        try:
            return asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            loop = HOLD.get("loop")
            return asyncio.run_coroutine_threadsafe(coro, loop) if loop else None

    def _on_ctl(change):
        try:
            c = json.loads(change["new"] or "{}")
        except Exception:
            return
        act = c.get("act")
        HOLD["scale"] = float(c.get("scale", HOLD["scale"]))
        HOLD["mode"] = c.get("mode", HOLD["mode"])
        year = int(c.get("year", HOLD["year"]))
        if act == "refresh":
            _spawn(_refresh())
            return
        if year != HOLD["year"] or act == "year":
            HOLD["year"] = year
            have = json.loads(mv.config or "{}").get("have") or {}
            if str(year) not in have:
                _spawn(_year(year))
            else:
                _say(_status_line(year, tiles.tiles_done(year)))

    if HOLD.get("h_ctl") is not None:
        try:
            mv.unobserve(HOLD["h_ctl"], names="ctl")
        except ValueError:
            pass
    mv.observe(_on_ctl, names="ctl")
    HOLD["h_ctl"] = _on_ctl

    # the first year's count, and every year's presence for the slider ticks
    async def _first():
        await _year(HOLD["year"])
        for y in YEARS:
            if y == HOLD["year"]:
                continue
            await asyncio.to_thread(_count_year, y)
        _say(_status_line(HOLD["year"], tiles.tiles_done(HOLD["year"])) + " · all years counted")

    if HOLD.get("loop") is not None:
        _spawn(_first())
    return


if __name__ == "__main__":
    app.run()
