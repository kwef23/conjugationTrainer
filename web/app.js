(() => {
  "use strict";

  // Resolved relative to this script's own URL (not the page URL, which may or
  // may not have a trailing slash) so the app works whether it's served at a
  // domain root or under a subpath (e.g. GitHub Pages project sites live at
  // github.io/<repo>/, not the domain root).
  const DATA_ROOT = new URL("../export", document.currentScript.src).href.replace(/\/$/, "");

  const MOOD_ORDER = ["indicativo", "congiuntivo", "condizionale", "imperativo", "infinito", "participio"];
  // "infinito" only ever contains the (data-quirky) gerundio bucket in this data
  // set -- traditional grammar treats gerundio as its own mood, not a tense of
  // infinito, so it's displayed as "Gerundio" rather than "Infinito" everywhere.
  const PRIMARY_MOODS = ["indicativo", "congiuntivo", "condizionale"];
  const SECONDARY_MOODS = ["imperativo", "infinito", "participio"];
  function moodLabel(mood) {
    return mood === "infinito" ? "Gerundio" : titleCase(mood);
  }
  const TENSE_ORDER = {
    indicativo: ["presente", "imperfetto", "passato-remoto", "futuro", "passato-prossimo", "trapassato-prossimo", "trapassato-remoto", "futuro-anteriore"],
    congiuntivo: ["presente", "imperfetto", "passato", "trapassato"],
    condizionale: ["presente", "passato"],
    imperativo: ["affermativo", "negativo"],
    infinito: ["gerundio"],
    participio: ["participio-presente", "participio-passato"],
  };

  // ---- shard resolution (must mirror export.py's shard_key exactly) ----

  const MAX_SHARD_DEPTH = 12;

  function shardKey(form, length) {
    const chars = [];
    const slice = form.slice(0, length).toLowerCase();
    for (let i = 0; i < slice.length; i++) {
      const decomposed = slice[i].normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
      const isLetter = decomposed.length === 1 && /[a-z]/.test(decomposed);
      if (isLetter) {
        chars.push(decomposed);
      } else if (i === 0) {
        return "_other";
      } else {
        chars.push("_");
      }
    }
    return chars.length ? chars.join("") : "_other";
  }

  function resolveShardKey(form, manifest) {
    for (let length = 1; length <= MAX_SHARD_DEPTH; length++) {
      const key = shardKey(form, length);
      if (manifest.has(key)) return key;
    }
    return null;
  }

  // ---- data loading (small lookups loaded once, shards/verb files cached on demand) ----

  const cache = {
    manifest: null,
    verbsById: null, // array, index = verb_id
    translationsById: null, // array, index = verb_id, each entry null or a list of up to 3 glosses
    infinitiveToId: null, // Map, infinitive -> verb_id (for infinitive-as-such search matches)
    slots: null, // array of [mood, tense, person, number, gender]
    shards: new Map(), // shardKey -> {form: [[verb_id, slot_id], ...]}
    verbDetail: new Map(), // infinitive -> array of rows
  };

  async function fetchJson(path) {
    const res = await fetch(path);
    if (!res.ok) throw new Error(`Failed to fetch ${path}: ${res.status}`);
    return res.json();
  }

  async function loadCoreData() {
    const [manifestArr, verbsById, slots, translationsById] = await Promise.all([
      fetchJson(`${DATA_ROOT}/idx/_manifest.json`),
      fetchJson(`${DATA_ROOT}/idx/verbs.json`),
      fetchJson(`${DATA_ROOT}/idx/slots.json`),
      fetchJson(`${DATA_ROOT}/idx/translations.json`),
    ]);
    cache.manifest = new Set(manifestArr);
    cache.verbsById = verbsById;
    cache.slots = slots;
    cache.translationsById = translationsById;
    cache.infinitiveToId = new Map();
    verbsById.forEach((infinitive, id) => {
      if (infinitive) cache.infinitiveToId.set(infinitive, id);
    });
  }

  async function loadShard(key) {
    if (cache.shards.has(key)) return cache.shards.get(key);
    const data = await fetchJson(`${DATA_ROOT}/idx/${key}.json`);
    cache.shards.set(key, data);
    return data;
  }

  async function loadVerbDetail(infinitive) {
    if (cache.verbDetail.has(infinitive)) return cache.verbDetail.get(infinitive);
    const data = await fetchJson(`${DATA_ROOT}/verbs/${encodeURIComponent(infinitive)}.json`);
    cache.verbDetail.set(infinitive, data);
    return data;
  }

  // ---- display helpers ----

  function titleCase(s) {
    return s.charAt(0).toUpperCase() + s.slice(1).replace(/-/g, " ");
  }

  function translationText(verbId) {
    const glosses = cache.translationsById[verbId];
    return glosses && glosses.length ? glosses.join("; ") : "";
  }

  // Ordinal person/number labels ("1. Sg.", "3. Pl.") instead of pronoun words.
  // Gender is only appended when it actually distinguishes two different forms
  // (e.g. essere-compound "3. Sg. (m.)" vs "3. Sg. (f.)") -- callers that have
  // already merged identical-form gender pairs should pass gender=null.
  function personNumberLabel(person, number, gender) {
    const numLabel = number === "s" ? "Sg." : number === "p" ? "Pl." : "";
    if (person !== null) {
      const base = numLabel ? `${person}. ${numLabel}` : `${person}.`;
      return gender ? `${base} (${gender}.)` : base;
    }
    // Participle-style rows: no grammatical person, just gender/number.
    const parts = [];
    if (numLabel) parts.push(numLabel);
    if (gender) parts.push(`(${gender}.)`);
    return parts.length ? parts.join(" ") : "Invariant";
  }

  // Canonical io/tu/lui-lei/noi/voi/loro ordering, independent of whatever
  // order the JSON array happens to list rows in.
  const PERSON_RANK = { 1: 0, 2: 1, 3: 2 };
  function personNumberRank(person, number, gender) {
    if (person !== null) {
      return PERSON_RANK[person] + (number === "p" ? 3 : 0);
    }
    // participle-style: after all person rows, singular before plural, m before f
    return 10 + (number === "p" ? 2 : 0) + (gender === "f" ? 1 : 0);
  }

  function sortByOrder(items, order, keyFn) {
    return [...items].sort((a, b) => {
      const ia = order.indexOf(keyFn(a));
      const ib = order.indexOf(keyFn(b));
      return (ia === -1 ? order.length : ia) - (ib === -1 ? order.length : ib);
    });
  }

  // ---- search view ----

  const searchInput = document.getElementById("search-input");
  const searchStatus = document.getElementById("search-status");
  const searchResults = document.getElementById("search-results");

  let searchDebounce = null;

  searchInput.addEventListener("input", () => {
    clearTimeout(searchDebounce);
    const query = searchInput.value.trim();
    if (query.length < 2) {
      searchResults.innerHTML = "";
      searchStatus.textContent = "";
      return;
    }
    searchDebounce = setTimeout(() => runSearch(query), 200);
  });

  async function runSearch(query) {
    searchStatus.textContent = "Searching…";
    try {
      const form = query.toLowerCase();
      const results = [];

      // A word can be both a dictionary infinitive and a conjugated form of
      // some other verb (or itself) -- check for the infinitive match first,
      // "possibly among others" per the conjugated-form matches below.
      const infId = cache.infinitiveToId.get(form);
      if (infId !== undefined) {
        results.push({ kind: "infinitive", verbId: infId });
      }

      const key = resolveShardKey(form, cache.manifest);
      if (key) {
        const shard = await loadShard(key);
        const refs = shard[form] || [];
        for (const [verbId, slotId] of refs) {
          results.push({ kind: "conjugation", verbId, slotId });
        }
      }

      if (results.length === 0) {
        searchStatus.textContent = "No matches.";
        searchResults.innerHTML = "";
        return;
      }
      renderResults(results);
      searchStatus.textContent = `${results.length} match${results.length === 1 ? "" : "es"}`;
    } catch (err) {
      console.error(err);
      searchStatus.textContent = "Search failed.";
    }
  }

  function resultSortKey([verbId, slotId]) {
    const [mood, tense, person, number, gender] = cache.slots[slotId];
    const moodRank = MOOD_ORDER.indexOf(mood);
    const tenseRank = (TENSE_ORDER[mood] || []).indexOf(tense);
    return [
      moodRank === -1 ? MOOD_ORDER.length : moodRank,
      tenseRank === -1 ? 99 : tenseRank,
      personNumberRank(person, number, gender),
    ];
  }

  function compareArrays(a, b) {
    for (let i = 0; i < a.length; i++) {
      if (a[i] !== b[i]) return a[i] - b[i];
    }
    return 0;
  }

  function renderResults(results) {
    searchResults.innerHTML = "";
    const infinitiveResults = results.filter((r) => r.kind === "infinitive");
    const conjugationResults = results
      .filter((r) => r.kind === "conjugation")
      .sort((a, b) => compareArrays(resultSortKey([a.verbId, a.slotId]), resultSortKey([b.verbId, b.slotId])));

    for (const r of [...infinitiveResults, ...conjugationResults]) {
      const infinitive = cache.verbsById[r.verbId];
      const li = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";

      let metaLine;
      let targetMood;
      let targetTense;
      if (r.kind === "infinitive") {
        metaLine = "Infinito";
        targetMood = "indicativo";
        targetTense = "presente";
      } else {
        const [mood, tense, person, number, gender] = cache.slots[r.slotId];
        const moodTense = mood === "infinito" ? moodLabel(mood) : `${moodLabel(mood)} — ${titleCase(tense)}`;
        metaLine = `${personNumberLabel(person, number, gender)} · ${moodTense}`;
        targetMood = mood;
        targetTense = tense;
      }

      const translation = translationText(r.verbId);
      button.innerHTML = `
        <span class="infinitive">${infinitive}</span>
        ${translation ? `<span class="meta">— ${translation}</span>` : ""}
        <span class="meta">${metaLine}</span>
      `;
      button.addEventListener("click", () => {
        location.hash = `#/verb/${encodeURIComponent(infinitive)}/${targetMood}/${targetTense}`;
      });
      li.appendChild(button);
      searchResults.appendChild(li);
    }
  }

  // ---- verb detail view ----

  const searchView = document.getElementById("search-view");
  const verbView = document.getElementById("verb-view");
  const verbTitle = document.getElementById("verb-title");
  const verbTranslation = document.getElementById("verb-translation");
  const moodTabs = document.getElementById("mood-tabs");
  const tenseTabsWrap = document.getElementById("tense-tabs-wrap");
  const tenseTabsLabel = document.getElementById("tense-tabs-label");
  const tenseTabs = document.getElementById("tense-tabs");
  const verbTableBody = document.getElementById("verb-table-body");
  const backButton = document.getElementById("back-button");

  let currentVerbRows = null;
  let currentMood = null;
  let currentTense = null;

  backButton.addEventListener("click", () => {
    location.hash = "#/";
  });

  async function showVerb(infinitive, preferredMood, preferredTense) {
    searchView.hidden = true;
    verbView.hidden = false;
    verbTitle.textContent = infinitive;
    const verbId = cache.infinitiveToId.get(infinitive);
    verbTranslation.textContent = verbId !== undefined ? translationText(verbId) : "";
    verbTableBody.innerHTML = "";
    moodTabs.innerHTML = "";
    tenseTabs.innerHTML = "";

    let rows;
    try {
      rows = await loadVerbDetail(infinitive);
    } catch (err) {
      console.error(err);
      verbTableBody.innerHTML = `<tr><td colspan="2">Could not load "${infinitive}".</td></tr>`;
      return;
    }
    currentVerbRows = rows;

    const moods = sortByOrder([...new Set(rows.map((r) => r.mood))], MOOD_ORDER, (m) => m);
    const primaryMoods = moods.filter((m) => PRIMARY_MOODS.includes(m));
    const secondaryMoods = moods.filter((m) => SECONDARY_MOODS.includes(m));

    function addMoodButton(mood, isFirstSecondary) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = moodLabel(mood);
      btn.dataset.mood = mood;
      if (isFirstSecondary) btn.classList.add("divider-before");
      btn.addEventListener("click", () => selectMood(mood));
      moodTabs.appendChild(btn);
    }
    for (const mood of primaryMoods) addMoodButton(mood, false);
    secondaryMoods.forEach((mood, i) => addMoodButton(mood, i === 0));

    selectMood(preferredMood && moods.includes(preferredMood) ? preferredMood : moods[0], preferredTense);
  }

  function selectMood(mood, preferredTense) {
    currentMood = mood;
    [...moodTabs.children].forEach((btn) => btn.classList.toggle("active", btn.dataset.mood === mood));

    const tenses = sortByOrder(
      [...new Set(currentVerbRows.filter((r) => r.mood === mood).map((r) => r.tense))],
      TENSE_ORDER[mood] || [],
      (t) => t
    );

    tenseTabsLabel.textContent = mood === "imperativo" ? "Form" : "Tense";
    // A mood with only one tense (e.g. gerundio) doesn't need a selector at all.
    tenseTabsWrap.hidden = tenses.length <= 1;

    tenseTabs.innerHTML = "";
    for (const tense of tenses) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = titleCase(tense);
      btn.addEventListener("click", () => selectTense(tense));
      tenseTabs.appendChild(btn);
    }

    selectTense(preferredTense && tenses.includes(preferredTense) ? preferredTense : tenses[0]);
  }

  // Merge two strings that share a common leading "word part" (split on the
  // last space) into one slashed string, e.g. "è andato" + "è andata" ->
  // "è andato/andata"; "andato" + "andata" -> "andato/andata" (no shared
  // prefix to factor out); "lui" + "lei" -> "lui/lei". Equal strings collapse
  // to themselves with no slash at all.
  function mergeWithSlash(a, b) {
    if (a === b) return a;
    const cutA = a.lastIndexOf(" ");
    const cutB = b.lastIndexOf(" ");
    if (cutA !== -1 && cutA === cutB && a.slice(0, cutA) === b.slice(0, cutA)) {
      return `${a.slice(0, cutA + 1)}${a.slice(cutA + 1)}/${b.slice(cutB + 1)}`;
    }
    return `${a}/${b}`;
  }

  const MERGED_PERSON_RANK = { 1: 0, 2: 1, 3: 2 };
  function mergedRowRank(person, number) {
    if (person !== null) return MERGED_PERSON_RANK[person] + (number === "p" ? 3 : 0);
    return 10 + (number === "p" ? 1 : 0);
  }

  // One display row per (person, number): pronoun and form are shown as-is
  // when the gender pair agrees, or slashed together (lui/lei, andato/andata)
  // when they genuinely differ -- always one row, per the user's request.
  function buildDisplayRows(rows) {
    const groups = new Map();
    for (const row of rows) {
      const key = `${row.person}|${row.number}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    const display = [];
    for (const group of groups.values()) {
      const sorted = [...group].sort((a, b) => (a.gender === b.gender ? 0 : a.gender === "m" ? -1 : 1));
      const { person, number } = sorted[0];
      const form = sorted.map((r) => r.form).reduce((acc, f) => mergeWithSlash(acc, f));
      const label =
        person !== null
          ? sorted.map((r) => r.pronoun).reduce((acc, p) => mergeWithSlash(acc, p))
          : number === "s"
          ? "Sg."
          : number === "p"
          ? "Pl."
          : "—";
      display.push({ person, number, label, form });
    }
    display.sort((a, b) => mergedRowRank(a.person, a.number) - mergedRowRank(b.person, b.number));
    return display;
  }

  function selectTense(tense) {
    currentTense = tense;
    [...tenseTabs.children].forEach((btn) => btn.classList.toggle("active", btn.textContent === titleCase(tense)));

    const rows = currentVerbRows.filter((r) => r.mood === currentMood && r.tense === tense);
    const displayRows = buildDisplayRows(rows);
    verbTableBody.innerHTML = "";
    for (const row of displayRows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${row.label}</td><td>${row.form}</td>`;
      verbTableBody.appendChild(tr);
    }
  }

  // ---- routing ----

  function showSearchView() {
    verbView.hidden = true;
    searchView.hidden = false;
  }

  function handleRoute() {
    const hash = location.hash;
    const verbMatch = hash.match(/^#\/verb\/([^/]+)(?:\/([^/]+)\/([^/]+))?$/);
    if (verbMatch) {
      const [, infinitive, mood, tense] = verbMatch;
      showVerb(decodeURIComponent(infinitive), mood, tense);
    } else {
      showSearchView();
    }
  }

  window.addEventListener("hashchange", handleRoute);

  // ---- init ----

  loadCoreData()
    .then(handleRoute)
    .catch((err) => {
      console.error(err);
      searchStatus.textContent = "Failed to load app data.";
    });
})();
