/* ローソク足チャート＋エントリー/決済の印（TradingView Lightweight Charts v4）。
 * データは /api/chart/...（app/web/chart.py）が返す JSON:
 *   { title, candles:[{time,open,high,low,close}], volume:[{time,value,color}],
 *     markers:[{time,position,color,shape,text,price}], overlays:[{name,color,data:[{time,value}]}],
 *     rsi:{name,data:[{time,value?}],levels:[{value,label,color}]}, initial_bars }
 * rsi があれば opts.rsiEl に RSI の枠を作り、ローソク足の枠と表示範囲・カーソルを連動させる。
 * time は JST の壁時計を UTC とみなした UNIX 秒（軸・カーソルがそのまま JST で読める）。
 *
 * 使い方:
 *   const c = TradeChart.mount(document.getElementById('chart'), '/api/chart/backtest/12',
 *                              { legendEl: document.getElementById('chart-legend') });
 *   c.focus(entrySec, exitSec);   // 取引の行をクリックしたとき（前後に30本の余白）
 *   c.load(otherUrl);          // 銘柄・期間を変えたとき
 */
const TradeChart = (() => {
  const css = (name, fallback) =>
    getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  const fmt = (v) => (v == null ? "—" : Number(v).toLocaleString("ja-JP", { maximumFractionDigits: 2 }));
  const fmtTime = (t) => {
    const d = new Date(t * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getUTCFullYear()}/${p(d.getUTCMonth() + 1)}/${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())}`;
  };

  function mount(el, url, opts = {}) {
    const LC = window.LightweightCharts;
    const legend = opts.legendEl || null;
    if (!LC) {
      el.textContent = "チャートのライブラリを読み込めませんでした（ネットワークを確認してください）。";
      return { focus() {}, load() {} };
    }
    const baseOptions = (extra = {}) => ({
      autoSize: true,
      layout: { background: { color: css("--c-surface", "#10141B") }, textColor: css("--c-sub", "#A3AEBD"),
                fontFamily: css("--font-mono", "monospace") },
      grid: { vertLines: { color: "rgba(255,255,255,.04)" }, horzLines: { color: "rgba(255,255,255,.04)" } },
      crosshair: { mode: LC.CrosshairMode.Normal },
      // 上下の枠で右の目盛りの幅を揃える（揃えないと足の位置が上下でずれる）
      rightPriceScale: { borderColor: "rgba(255,255,255,.12)", minimumWidth: 72 },
      timeScale: { borderColor: "rgba(255,255,255,.12)", timeVisible: true, secondsVisible: false },
      localization: { locale: "ja-JP", timeFormatter: fmtTime },
      ...extra,
    });
    const chart = LC.createChart(el, baseOptions());
    // 陽線=赤 / 陰線=青（サイト全体の プラス=赤・マイナス=青 に合わせる）
    const candles = chart.addCandlestickSeries({
      upColor: "#FF5A5A", downColor: "#5AA9FF", borderVisible: false,
      wickUpColor: "#FF5A5A", wickDownColor: "#5AA9FF",
    });
    const volume = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" } });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    let lines = [];
    let data = null;

    // ---- RSI（下の小さな枠）。ローソク足の枠と「何本目か」で表示範囲とカーソルを連動させる ----
    let rsiChart = null, rsiSeries = null, rsiLevels = [];
    // どちらの枠が「動かす側」か: マウスが RSI の枠の上にあるときだけ RSI → ローソク足、それ以外は逆向き。
    // （両方向を常に連動させると、RSI の枠がデータを入れた直後の「全体表示」でローソク足の枠を上書きする）
    let overRsi = false;
    const syncRsi = () => {
      const r = chart.timeScale().getVisibleLogicalRange();
      if (rsiChart && r) rsiChart.timeScale().setVisibleLogicalRange(r);
    };
    function ensureRsi() {
      if (rsiChart || !opts.rsiEl) return;
      rsiChart = LC.createChart(opts.rsiEl, baseOptions({
        timeScale: { visible: false },
        handleScroll: true, handleScale: true,
      }));
      rsiSeries = rsiChart.addLineSeries({ color: "#C98AD8", lineWidth: 1.5, priceLineVisible: false,
                                           lastValueVisible: true });
      rsiSeries.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }) });
      rsiChart.priceScale("right").applyOptions({ scaleMargins: { top: 0.06, bottom: 0.06 } });
      opts.rsiEl.addEventListener("mouseenter", () => { overRsi = true; });
      opts.rsiEl.addEventListener("mouseleave", () => { overRsi = false; });
      chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
        if (!overRsi && r) rsiChart.timeScale().setVisibleLogicalRange(r);
      });
      rsiChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
        if (!r) return;
        if (overRsi) { chart.timeScale().setVisibleLogicalRange(r); return; }
        // RSI の枠が自分で範囲を変えた（データを入れた直後の全体表示など）→ ローソク足の枠に合わせ直す
        const m = chart.timeScale().getVisibleLogicalRange();
        if (m && (Math.abs(m.from - r.from) > 0.5 || Math.abs(m.to - r.to) > 0.5)) syncRsi();
      });
      chart.subscribeCrosshairMove((param) => {
        if (!param || param.time == null) { rsiChart.clearCrosshairPosition(); return; }
        const v = rsiValueAt(param.time);
        if (v != null) rsiChart.setCrosshairPosition(v, param.time, rsiSeries);
      });
      rsiChart.subscribeCrosshairMove((param) => {
        if (!param || param.time == null) { chart.clearCrosshairPosition(); showLegend(null); return; }
        const bar = data && data.candles.find((c) => c.time === param.time);
        if (bar) chart.setCrosshairPosition(bar.close, param.time, candles);
      });
    }
    function rsiValueAt(t) {
      if (!data || !data.rsi) return null;
      const p = data.rsi.data.find((d) => d.time === t);
      return p && p.value != null ? p.value : null;
    }
    function setRsi() {
      if (!data.rsi || !opts.rsiEl) {
        if (opts.rsiEl) opts.rsiEl.style.display = "none";
        return;
      }
      opts.rsiEl.style.display = "";
      ensureRsi();
      rsiSeries.setData(data.rsi.data);
      rsiLevels.forEach((l) => rsiSeries.removePriceLine(l));
      rsiLevels = data.rsi.levels.map((l) => rsiSeries.createPriceLine({
        price: l.value, color: l.color, lineWidth: 1, lineStyle: LC.LineStyle.Dashed,
        axisLabelVisible: true, title: l.label,
      }));
    }

    function showLegend(param) {
      if (!legend || !data) return;
      let bar = param && param.time != null ? param.seriesData.get(candles) : null;
      let t = param && param.time;
      if (!bar && data.candles.length) {
        bar = data.candles[data.candles.length - 1];
        t = bar.time;
      }
      if (!bar) { legend.textContent = "足のデータがありません"; return; }
      const ma = lines.map(({ name, series, color, last }) => {
        // カーソルが無いとき（読み込み直後）は最新の値
        const v = param && param.seriesData ? param.seriesData.get(series) : last;
        return `<span style="color:${color}">${name} ${fmt(v && v.value)}</span>`;
      });
      if (data.rsi) {
        const v = rsiValueAt(t);
        ma.push(`<span style="color:#C98AD8">${data.rsi.name} ${fmt(v)}</span>`);
      }
      const mk = data.markers.filter((m) => m.time === t)
        .map((m) => `<span style="color:${m.color}">${m.text}${m.price ? " @" + fmt(m.price) : ""}</span>`);
      legend.innerHTML = [
        `<span>${fmtTime(t)}</span>`,
        `<span>始 ${fmt(bar.open)} 高 ${fmt(bar.high)} 安 ${fmt(bar.low)} 終 ${fmt(bar.close)}</span>`,
        ...ma, ...mk,
      ].join("　");
    }
    chart.subscribeCrosshairMove(showLegend);

    async function load(u) {
      el.classList.add("is-loading");
      try {
        const r = await fetch(u);
        data = await r.json();
      } catch (e) {
        data = null;
      } finally {
        el.classList.remove("is-loading");
      }
      if (!data || data.error) {
        if (legend) legend.textContent = "チャートのデータを取得できませんでした。";
        return;
      }
      candles.setData(data.candles);
      volume.setData(data.volume);
      candles.setMarkers(data.markers.map(({ price, ...m }) => m));
      lines.forEach(({ series }) => chart.removeSeries(series));
      lines = (data.overlays || []).map((o) => {
        const series = chart.addLineSeries({ color: o.color, lineWidth: 1, priceLineVisible: false,
                                             lastValueVisible: false, crosshairMarkerVisible: false });
        series.setData(o.data);
        return { name: o.name, color: o.color, series, last: o.data[o.data.length - 1] };
      });
      setRsi();
      const n = data.candles.length;
      if (data.initial_bars && n > data.initial_bars) {
        chart.timeScale().setVisibleLogicalRange({ from: n - data.initial_bars, to: n + 3 });
      } else {
        chart.timeScale().fitContent();
      }
      syncRsi();
      showLegend(null);
    }

    // from〜to（建て〜決済の時刻）を、前後に pad 本の足を付けて表示する。時間ではなく本数で余白を
    // 取るので、寄り付き直後の建てでも前日の足が見える
    function focus(from, to, pad = 30) {
      if (!data || !data.candles.length) return;
      const idx = (t) => {
        let lo = 0, hi = data.candles.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (data.candles[mid].time < t) lo = mid + 1; else hi = mid;
        }
        return lo;
      };
      chart.timeScale().setVisibleLogicalRange({ from: idx(from) - pad, to: idx(to) + pad });
      syncRsi();
    }

    load(url);
    return { focus, load, chart, rsi: () => rsiChart };
  }

  // data-chart-from / data-chart-to（chart.py の時刻）を持つ行をクリックすると、その範囲を表示する
  function bindRows(ctrl, chartEl, root = document) {
    root.querySelectorAll("[data-chart-from]").forEach((row) => {
      row.classList.add("chart-row");
      row.title = "クリックでチャートのこの取引を表示";
      row.addEventListener("click", () => {
        ctrl.focus(Number(row.dataset.chartFrom), Number(row.dataset.chartTo));
        chartEl.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    });
  }

  return { mount, bindRows };
})();
