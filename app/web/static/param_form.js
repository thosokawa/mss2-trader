/* 戦略パラメータを生のJSONではなく、項目ごとの入力欄で編集できるようにする。
 * default_params（既定値）と param_meta（ラベル/選択肢/説明、任意）から
 * フォームを組み立て、変更のたびに隠しinput(params_json)へJSONを書き戻す。
 * 「JSONを直接編集」の <details> と双方向に同期する。
 *
 * 使い方:
 *   const pf = ParamForm.mount({ containerEl, hiddenEl, jsonAreaEl, allowRanges });
 *   pf.rebuild(defaults, meta, initialValues);  // 戦略を切り替えるたびに呼ぶ
 */
const ParamForm = (() => {
  function inferType(value) {
    if (typeof value === "boolean") return "bool";
    if (typeof value === "number") return "number";
    return "text";
  }

  function coerce(raw, type) {
    if (type === "number") {
      const n = Number(raw);
      return Number.isNaN(n) ? raw : n;
    }
    if (type === "bool") {
      // allowRanges 時は bool もテキスト欄になる（"true,false" 等でスイープ可能にするため）
      const s = String(raw).trim().toLowerCase();
      if (s === "true") return true;
      if (s === "false") return false;
    }
    return raw;
  }

  // allowRanges のとき、カンマ区切りは配列（=最適化の探索対象）として扱う
  function parseFieldValue(raw, type, allowRanges) {
    const trimmed = String(raw).trim();
    if (allowRanges && trimmed.includes(",")) {
      return trimmed.split(",").map((v) => coerce(v.trim(), type));
    }
    return coerce(trimmed, type);
  }

  function mount({ containerEl, hiddenEl, jsonAreaEl, detailsEl, allowRanges }) {
    let defaults = {};
    let meta = {};

    function sync() {
      const out = {};
      containerEl.querySelectorAll("[data-key]").forEach((input) => {
        const key = input.dataset.key;
        const type = input.dataset.type;
        if (input.type === "checkbox") {
          out[key] = input.checked;
        } else {
          out[key] = parseFieldValue(input.value, type, allowRanges);
        }
      });
      const json = JSON.stringify(out);
      hiddenEl.value = json;
      if (jsonAreaEl) jsonAreaEl.value = JSON.stringify(out, null, 2);
    }

    function buildFields(values) {
      containerEl.innerHTML = "";
      for (const key of Object.keys(defaults)) {
        const m = meta[key] || {};
        const def = defaults[key];
        const type = inferType(def);
        const val = values && Object.prototype.hasOwnProperty.call(values, key) ? values[key] : def;

        const wrap = document.createElement("label");
        wrap.className = "pf-field";
        wrap.textContent = (m.label ? `${m.label} ` : "") + `(${key})`;
        if (m.help) wrap.title = m.help;

        let input;
        if (m.choices) {
          input = document.createElement("select");
          for (const c of m.choices) {
            const opt = document.createElement("option");
            opt.value = c;
            opt.textContent = c;
            if (String(val) === String(c)) opt.selected = true;
            input.appendChild(opt);
          }
        } else if (type === "bool" && !allowRanges) {
          input = document.createElement("input");
          input.type = "checkbox";
          input.checked = !!val;
        } else {
          input = document.createElement("input");
          input.type = type === "number" && !allowRanges ? "number" : "text";
          if (type === "number") input.step = "any";
          input.value = Array.isArray(val) ? val.join(",") : val;
        }
        input.dataset.key = key;
        input.dataset.type = type;
        input.addEventListener("input", sync);
        input.addEventListener("change", sync);

        wrap.appendChild(document.createElement("br"));
        wrap.appendChild(input);
        containerEl.appendChild(wrap);
      }
      sync();
    }

    function rebuild(newDefaults, newMeta, initialValues) {
      defaults = newDefaults || {};
      meta = newMeta || {};
      buildFields(initialValues || null);
    }

    // JSON 直接編集 → フォームへ反映（壊れたJSONの間は無視して編集を続けさせる）
    if (jsonAreaEl) {
      jsonAreaEl.addEventListener("change", () => {
        try {
          const parsed = JSON.parse(jsonAreaEl.value);
          buildFields(parsed);
        } catch (e) {
          /* 入力中の可能性があるので黙って無視 */
        }
      });
    }

    return { rebuild, sync };
  }

  return { mount };
})();
