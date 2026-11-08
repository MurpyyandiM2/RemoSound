(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const state = {
    sounds: [],
    categories: [],
    available: [],
    providers: [],
    provider: "mixkit",
    dmode: "category",
    query: "",
    category: "all",
    selectedIds: new Set(),
    source: { mode: "trending", target: "" },
    jobId: null,
    jobTimer: null,
    activeId: null,
    pendingDeleteIds: [],
    muted: false,
  };

  const audio = $("#audio");
  audio.volume = 1;

  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);

  const svg = (body, size = 18) =>
    `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;

  const ICONS = {
    play: svg('<path d="M8 5.4v13.2L19 12Z" fill="currentColor" stroke="none"/>'),
    pause: svg('<rect x="7" y="5" width="3.6" height="14" rx="1" fill="currentColor" stroke="none"/><rect x="13.4" y="5" width="3.6" height="14" rx="1" fill="currentColor" stroke="none"/>'),
    check: svg('<path d="m5 12 4.5 4.5L19 7"/>'),
    alert: svg('<circle cx="12" cy="12" r="9"/><path d="M12 8v5"/><path d="M12 16h.01"/>'),
    copy: svg('<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>', 15),
    disc: svg('<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="2"/>', 17),
    fire: svg('<path d="M12 3c3 3.5 5 6 5 9a5 5 0 0 1-10 0c0-1.6.7-3 2-4.5.3 1.4 1 2.2 2 2.7C11.3 8.2 11.5 5.6 12 3Z"/>', 17),
    trash: svg('<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>', 15),
  };

  const prettyCategory = (name) => {
    const known = state.available.find((item) => item.slug === name);
    if (known) return known.name;
    if (!name) return "Uncategorized";
    return name.replace(/\b[a-z]/g, (letter) => letter.toUpperCase());
  };

  const hueFor = (name) => {
    let hash = 0;
    for (const character of String(name)) hash = (hash * 31 + character.charCodeAt(0)) % 360;
    return hash;
  };

  const formatTime = (seconds) => {
    if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
    const minutes = Math.floor(seconds / 60);
    const rest = Math.floor(seconds % 60);
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  };

  // --- API ---------------------------------------------------------------

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (error) {
      payload = null;
    }
    if (!response.ok) {
      throw new Error((payload && payload.error) || `Request failed (${response.status})`);
    }
    return payload;
  }

  function toast(message, kind = "success") {
    const node = document.createElement("div");
    node.className = `toast ${kind === "error" ? "error" : ""}`;
    node.innerHTML = (kind === "error" ? ICONS.alert : ICONS.check) + "<span></span>";
    node.querySelector("span").textContent = message;
    $("#toasts").appendChild(node);
    setTimeout(() => node.remove(), 3400);
  }

  // --- tabs --------------------------------------------------------------

  function selectTab(name) {
    $$(".tab").forEach((tab) => {
      const selected = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    $$("[role='tabpanel']").forEach((panel) => {
      panel.hidden = panel.id !== `panel-${name}`;
    });
  }

  $$(".tab").forEach((tab) => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));

  // --- bank --------------------------------------------------------------

  async function loadBank() {
    const data = await api("/api/bank");
    state.sounds = data.sounds;
    state.categories = data.categories;
    state.available = data.available;
    state.providers = data.providers || [];
    renderProviders();
    $("#stat-total").textContent = String(data.total);
    $("#stat-categories").textContent = String(data.categories.length);
    renderFilters();
    renderSources();
    renderSounds();
  }

  function renderFilters() {
    const counts = new Map(state.categories.map((item) => [item.name, item.count]));
    const pills = [`<button class="pill" type="button" data-category="all" aria-pressed="${state.category === "all"}">All <span>${state.sounds.length}</span></button>`];
    for (const category of state.categories) {
      pills.push(
        `<button class="pill" type="button" data-category="${esc(category.name)}" aria-pressed="${state.category === category.name}">${esc(prettyCategory(category.name))} <span>${counts.get(category.name) || 0}</span></button>`
      );
    }
    $("#filters").innerHTML = pills.join("");
  }

  function filteredSounds() {
    const query = state.query.trim().toLowerCase();
    return state.sounds.filter((sound) => {
      if (state.category !== "all" && sound.category !== state.category) return false;
      if (!query) return true;
      return [sound.title, sound.category, sound.id, sound.path, sound.provider].some((field) =>
        String(field || "").toLowerCase().includes(query)
      );
    });
  }

  function soundCard(sound) {
    const playing = state.activeId === sound.id && !audio.paused;
    const selected = state.selectedIds.has(sound.id);
    const providerBadge = badgeFor(sound.provider);
    return `
      <div class="sound-card ${playing ? "is-playing" : ""} ${selected ? "is-selected" : ""}" data-id="${esc(sound.id)}">
        <input type="checkbox" class="sound-select-chk" data-select-id="${esc(sound.id)}" ${selected ? "checked" : ""} aria-label="Select ${esc(sound.title)}">
        <button class="card-play" type="button" data-play-id="${esc(sound.id)}" aria-pressed="${playing}" aria-label="${playing ? "Pause" : "Play"} ${esc(sound.title)}">
          ${playing ? ICONS.pause : ICONS.play}
        </button>
        <span class="sound-body" data-play-id="${esc(sound.id)}" style="cursor:pointer">
          <span class="sound-title">${esc(sound.title)}</span>
          <span class="sound-sub">
            <span class="badge">${esc(prettyCategory(sound.category))}</span>
            ${providerBadge}
          </span>
        </span>
      </div>`;
  }

  function renderSounds() {
    const sounds = filteredSounds();
    const grid = $("#sound-grid");
    const empty = $("#board-empty");
    grid.innerHTML = sounds.map(soundCard).join("");
    grid.hidden = sounds.length === 0;
    empty.hidden = sounds.length !== 0;

    if (sounds.length === 0) {
      const searching = Boolean(state.query.trim()) || state.category !== "all";
      $("#empty-title").textContent = searching ? "No sound matches that" : "Your bank is empty";
      $("#empty-copy").textContent = searching
        ? "Try a different word or switch back to All categories."
        : "Download a first batch and it lands here, ready to preview and drop into a video.";
      $("#empty-cta").hidden = searching;
    }
    updateSelectionBar();
  }

  function updateSelectionBar() {
    const displayed = filteredSounds();
    const count = state.selectedIds.size;
    const btnDelete = $("#btn-delete-selected");
    const btnCancel = $("#btn-cancel-selection");
    const chkAll = $("#chk-select-all");
    const countSpan = $("#selected-count");

    if (countSpan) countSpan.textContent = String(count);
    if (btnDelete) btnDelete.hidden = count === 0;
    if (btnCancel) btnCancel.hidden = count === 0;

    if (chkAll) {
      const allDisplayedSelected = displayed.length > 0 && displayed.every((s) => state.selectedIds.has(s.id));
      chkAll.checked = allDisplayedSelected;
      chkAll.indeterminate = count > 0 && !allDisplayedSelected;
    }
  }

  $("#chk-select-all").addEventListener("change", (event) => {
    const checked = event.target.checked;
    const displayed = filteredSounds();
    if (checked) {
      displayed.forEach((sound) => state.selectedIds.add(sound.id));
    } else {
      displayed.forEach((sound) => state.selectedIds.delete(sound.id));
    }
    renderSounds();
  });

  $("#btn-cancel-selection").addEventListener("click", () => {
    state.selectedIds.clear();
    renderSounds();
  });

  $("#btn-delete-selected").addEventListener("click", () => {
    const ids = Array.from(state.selectedIds);
    if (ids.length === 0) return;
    promptDelete(ids);
  });

  $("#filters").addEventListener("click", (event) => {
    const pill = event.target.closest(".pill");
    if (!pill) return;
    state.category = pill.dataset.category;
    renderFilters();
    renderSounds();
  });

  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderSounds();
  });

  $("#empty-cta").addEventListener("click", () => selectTab("download"));

  $("#sound-grid").addEventListener("click", (event) => {
    const chk = event.target.closest(".sound-select-chk");
    if (chk) {
      const id = chk.dataset.selectId;
      if (chk.checked) {
        state.selectedIds.add(id);
      } else {
        state.selectedIds.delete(id);
      }
      const card = chk.closest(".sound-card");
      if (card) card.classList.toggle("is-selected", chk.checked);
      updateSelectionBar();
      event.stopPropagation();
      return;
    }

    const playTrigger = event.target.closest("[data-play-id]");
    if (playTrigger) {
      const sound = state.sounds.find((item) => item.id === playTrigger.dataset.playId);
      if (sound) toggleSound(sound);
      return;
    }

    const card = event.target.closest(".sound-card");
    if (!card) return;
    const sound = state.sounds.find((item) => item.id === card.dataset.id);
    if (sound) toggleSound(sound);
  });

  function promptDelete(ids) {
    if (!Array.isArray(ids) || ids.length === 0) return;
    state.pendingDeleteIds = ids;
    if (ids.length === 1) {
      const sound = state.sounds.find((s) => s.id === ids[0]);
      const title = sound ? sound.title : ids[0];
      $("#delete-modal-msg").innerHTML = `Are you sure you want to delete <strong>${esc(title)}</strong>? This will permanently remove the audio file from your sound bank and manifest.`;
    } else {
      $("#delete-modal-msg").innerHTML = `Are you sure you want to delete <strong>${ids.length} selected sounds</strong>? This will permanently remove the audio files from your sound bank and manifest.`;
    }
    const modal = $("#delete-modal");
    if (typeof modal.showModal === "function") {
      modal.showModal();
    } else {
      modal.setAttribute("open", "");
    }
  }

  function closeDeleteModal() {
    const modal = $("#delete-modal");
    if (typeof modal.close === "function") {
      modal.close();
    } else {
      modal.removeAttribute("open");
    }
    state.pendingDeleteIds = [];
  }

  $("#delete-modal-cancel").addEventListener("click", closeDeleteModal);

  $("#delete-modal-confirm").addEventListener("click", async () => {
    const ids = state.pendingDeleteIds;
    if (!ids || ids.length === 0) return;
    try {
      await api("/api/delete", {
        method: "POST",
        body: JSON.stringify({ ids }),
      });
      if (state.activeId && ids.includes(state.activeId)) {
        audio.pause();
        audio.removeAttribute("src");
        audio.load();
        state.activeId = null;
        $("#player").hidden = true;
      }
      ids.forEach((id) => state.selectedIds.delete(id));
      closeDeleteModal();
      toast(ids.length === 1 ? "Sound deleted from sound bank." : `${ids.length} sounds deleted from sound bank.`);
      await loadBank();
      ids.forEach((id) => {
        $$(`.match[data-id="${id}"]`).forEach((el) => el.remove());
      });
    } catch (error) {
      toast(error.message, "error");
      closeDeleteModal();
    }
  });

  // --- audio player ------------------------------------------------------

  function toggleSound(sound) {
    if (state.activeId === sound.id) {
      if (audio.paused) {
        audio.play().catch(() => toast("This file could not be played.", "error"));
      } else {
        audio.pause();
      }
      return;
    }

    state.activeId = sound.id;
    audio.src = sound.url;
    audio.play().catch(() => toast("This file could not be played.", "error"));
    $("#player").hidden = false;
    $("#player-name").textContent = sound.title;
    $("#player-category").textContent = prettyCategory(sound.category);
    refreshCards();
  }

  function refreshCards() {
    $$(".sound-card").forEach((card) => {
      const playing = card.dataset.id === state.activeId && !audio.paused;
      card.classList.toggle("is-playing", playing);
      card.setAttribute("aria-pressed", String(playing));
      const icon = card.querySelector(".card-play");
      if (icon) icon.innerHTML = playing ? ICONS.pause : ICONS.play;
    });
    $("#player-icon").innerHTML = audio.paused
      ? '<path d="M8 5.4v13.2L19 12Z" fill="currentColor" stroke="none"/>'
      : '<rect x="7" y="5" width="3.6" height="14" rx="1" fill="currentColor"/><rect x="13.4" y="5" width="3.6" height="14" rx="1" fill="currentColor"/>';
    $("#player-toggle").setAttribute("aria-label", audio.paused ? "Play" : "Pause");
  }

  audio.addEventListener("play", refreshCards);
  audio.addEventListener("pause", refreshCards);
  audio.addEventListener("ended", () => {
    audio.currentTime = 0;
    refreshCards();
  });
  audio.addEventListener("loadedmetadata", () => {
    $("#player-end").textContent = formatTime(audio.duration);
  });
  audio.addEventListener("timeupdate", () => {
    const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
    $("#player-now").textContent = formatTime(audio.currentTime);
    $("#player-seek").max = String(duration || 0);
    $("#player-seek").value = String(audio.currentTime || 0);
  });
  audio.addEventListener("error", () => {
    if (audio.getAttribute("src")) toast("This file could not be played.", "error");
  });

  $("#player-toggle").addEventListener("click", () => {
    if (audio.paused) audio.play().catch(() => toast("Playback failed.", "error"));
    else audio.pause();
  });
  $("#player-seek").addEventListener("input", (event) => {
    audio.currentTime = Number(event.target.value);
  });
  $("#volume").addEventListener("input", (event) => {
    audio.volume = Number(event.target.value);
  });
  $("#player-close").addEventListener("click", () => {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    state.activeId = null;
    $("#player").hidden = true;
    refreshCards();
  });

  // --- downloader --------------------------------------------------------

  function renderSources() {
    const currentProv = state.providers.find((p) => p.id === state.provider);
    const catList = (currentProv && currentProv.categories) || state.available;
    const sources = [
      { mode: "trending", target: "", name: "Trending", tag: "top listed", hue: 150 },
      ...catList.map((category) => ({
        mode: "category",
        target: category.slug,
        name: category.name,
        tag: "category",
        hue: hueFor(category.slug),
      })),
    ];
    $("#sources").innerHTML = sources
      .map((source) => {
        const selected = state.source.mode === source.mode && state.source.target === source.target;
        return `
          <button class="source" type="button" data-mode="${esc(source.mode)}" data-target="${esc(source.target)}" aria-pressed="${selected}" style="--tile-h:${source.hue}">
            <span class="source-icon" aria-hidden="true">${source.mode === "trending" ? ICONS.fire : ICONS.disc}</span>
            <span>
              <span class="source-name">${esc(source.name)}</span>
              <span class="source-tag">${esc(source.tag)}</span>
            </span>
          </button>`;
      })
      .join("");
  }

  function badgeFor(providerId) {
    const known = state.providers.find((p) => p.id === providerId);
    const cls = providerId === "mixkit" ? "badge-mixkit" : "badge-other";
    return `<span class="badge ${cls}">${esc(known ? known.badge : providerId)}</span>`;
  }

  function renderProviders() {
    $("#providers").innerHTML = state.providers
      .map((p) => `
        <button class="provider-btn" type="button" data-provider="${esc(p.id)}" aria-selected="${p.id === state.provider}">
          ${esc(p.name)} ${badgeFor(p.id)}
        </button>`)
      .join("");
  }

  $("#providers").addEventListener("click", (event) => {
    const button = event.target.closest("[data-provider]");
    if (!button) return;
    state.provider = button.dataset.provider;
    const current = state.providers.find((p) => p.id === state.provider);
    $("#download-query").placeholder = `Search sounds on ${current ? current.name : state.provider} (e.g. whoosh, transition, bell)...`;
    state.source = { mode: "trending", target: "" };
    renderProviders();
    renderSources();
  });

  $("#btn-mode-cat").addEventListener("click", () => {
    state.dmode = "category";
    $("#btn-mode-cat").classList.add("active");
    $("#btn-mode-search").classList.remove("active");
    $("#sources").hidden = false;
    $("#downloader-search-wrap").hidden = true;
  });

  $("#btn-mode-search").addEventListener("click", () => {
    state.dmode = "search";
    $("#btn-mode-search").classList.add("active");
    $("#btn-mode-cat").classList.remove("active");
    $("#sources").hidden = true;
    $("#downloader-search-wrap").hidden = false;
    $("#download-query").focus();
  });

  $("#sources").addEventListener("click", (event) => {
    const button = event.target.closest(".source");
    if (!button) return;
    state.source = { mode: button.dataset.mode, target: button.dataset.target };
    revealSourceSelection();
  });

  function revealSourceSelection() {
    $$(".source").forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.mode === state.source.mode && button.dataset.target === state.source.target)
      );
    });
  }

  $$(".chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      $("#limit").value = chip.dataset.limit;
    })
  );

  $("#limit").addEventListener("input", (event) => {
    if (Number.parseInt(event.target.value, 10) > 250) event.target.value = "250";
  });

  $("#start").addEventListener("click", async () => {
    const limit = Math.max(1, Math.min(250, Number.parseInt($("#limit").value, 10) || 50));
    $("#limit").value = String(limit);

    let mode = state.source.mode;
    let target = state.source.target;
    if (state.dmode === "search") {
      const q = $("#download-query").value.trim();
      if (!q) {
        $("#download-query").focus();
        toast("Please enter a search query first.", "error");
        return;
      }
      mode = "search";
      target = q;
    }

    $("#start").disabled = true;
    $("#job").hidden = false;
    $("#job").classList.remove("error");
    setJob({ state: "queued", found: 0, total: 0, completed: 0, downloaded: 0, skipped: 0, failed: 0, current: "" });

    try {
      const data = await api("/api/download", {
        method: "POST",
        body: JSON.stringify({ mode, target, limit, provider: state.provider }),
      });
      state.jobId = data.job.id;
      pollJob();
    } catch (error) {
      $("#start").disabled = false;
      $("#job").classList.add("error");
      setJob({ state: "error", error: error.message });
      toast(error.message, "error");
    }
  });

  async function pollJob() {
    if (!state.jobId) return;
    clearTimeout(state.jobTimer);
    try {
      const data = await api(`/api/jobs/${state.jobId}`);
      const job = data.job;
      setJob(job);
      if (job.state === "queued" || job.state === "running") {
        state.jobTimer = setTimeout(pollJob, 700);
        return;
      }
      state.jobId = null;
      $("#start").disabled = false;
      await loadBank();
      if (job.state === "done") {
        const parts = [`${job.downloaded} new`];
        if (job.skipped) parts.push(`${job.skipped} already owned`);
        if (job.failed) parts.push(`${job.failed} failed`);
        toast(`Download finished — ${parts.join(", ")}.`, job.failed ? "error" : "success");
      } else {
        toast(job.error || "Download failed.", "error");
      }
    } catch (error) {
      state.jobId = null;
      $("#start").disabled = false;
      toast(error.message, "error");
    }
  }

  function setJob(job) {
    const total = job.total || 0;
    const completed = total > 0 ? Math.min(job.completed || 0, total) : (job.completed || 0);
    let percent = 0;
    if (total > 0) percent = Math.round((completed / total) * 100);
    else if (job.state === "done") percent = 100;

    const labels = {
      queued: "Starting…",
      running: total ? `Downloading ${completed} of ${total}` : "Scanning the listing…",
      done: total ? "Finished" : "Nothing new to download",
      error: "Failed",
    };

    $("#progress-fill").style.width = `${percent}%`;
    $("#progress").setAttribute("aria-valuenow", String(percent));
    $("#job-percent").textContent = `${percent}%`;
    $("#job-state").textContent = labels[job.state] || job.state;
    $("#job-current").textContent = job.state === "error" ? (job.error || "") : (job.current || "");
    $("#job-found").textContent = String(job.found || 0);
    $("#job-downloaded").textContent = String(job.downloaded || 0);
    $("#job-skipped").textContent = String(job.skipped || 0);
    $("#job-failed").textContent = String(job.failed || 0);
  }

  // --- studio ------------------------------------------------------------

  async function runMatch() {
    const text = $("#scene").value.trim();
    if (!text) {
      $("#scene").focus();
      toast("Describe the scene first.", "error");
      return;
    }
    $("#match").disabled = true;
    try {
      const data = await api("/api/match", { method: "POST", body: JSON.stringify({ text }) });
      renderMatches(data.matches);
      if (!data.matches.length) toast("No sound in the bank matches that scene yet.");
    } catch (error) {
      toast(error.message, "error");
    } finally {
      $("#match").disabled = false;
    }
  }

  function renderMatches(matches) {
    const list = $("#matches");
    const empty = $("#match-empty");
    empty.hidden = matches.length > 0;
    list.innerHTML = matches
      .map((match) => {
        const code = `<Audio src={staticFile(${JSON.stringify(match.src)})} />`;
        const providerBadge = badgeFor(match.provider);
        return `
          <article class="match" data-id="${esc(match.id)}">
            <div class="match-top">
              <span class="score">score ${Number(match.score) || 0}</span>
              <span class="match-title">${esc(match.title)}</span>
              <span class="badge">${esc(prettyCategory(match.category))}</span>
              ${providerBadge}
            </div>
            <div class="code-row">
              <code class="code">${esc(code)}</code>
              <button class="copy" type="button" data-copy="${esc(code)}" aria-label="Copy Remotion code for ${esc(match.title)}">${ICONS.copy} Copy</button>
            </div>
            <div class="match-actions">
              <button class="btn btn-ghost" type="button" data-play="${esc(match.id)}" aria-label="Play ${esc(match.title)}">${ICONS.play} Preview</button>
            </div>
          </article>`;
      })
      .join("");
  }

  $("#matches").addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const code = copy.dataset.copy;
      try {
        await navigator.clipboard.writeText(code);
      } catch (error) {
        const helper = document.createElement("textarea");
        helper.value = code;
        document.body.appendChild(helper);
        helper.select();
        document.execCommand("copy");
        helper.remove();
      }
      toast("Remotion code copied to clipboard.");
      return;
    }

    const play = event.target.closest("[data-play]");
    if (play) {
      const sound = state.sounds.find((item) => item.id === play.dataset.play);
      if (sound) {
        toggleSound(sound);
      } else {
        toast("Refresh the bank and preview it from the soundboard.", "error");
      }
    }
  });

  $("#match").addEventListener("click", runMatch);
  $("#scene").addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") runMatch();
  });
  $("#clear-match").addEventListener("click", () => {
    $("#scene").value = "";
    $("#matches").innerHTML = "";
    $("#match-empty").hidden = false;
    $("#scene").focus();
  });
  $$(".example").forEach((example) =>
    example.addEventListener("click", () => {
      $("#scene").value = example.dataset.scene;
      runMatch();
    })
  );

  // --- keyboard ----------------------------------------------------------

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      selectTab("board");
      $("#search").select();
    }
  });

  // --- boot --------------------------------------------------------------

  loadBank().catch((error) => toast(error.message, "error"));
})();
