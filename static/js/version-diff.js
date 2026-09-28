// "What changed" for versioned things (scenario sets, judges): word-level text
// diffs, field-by-field comparisons and the diff modal
// (templates/partials/version_diff_modal.html). Needs diff.min.js (jsdiff) and
// the global esc() from ui_js.html.
(function () {
  const text = v => Array.isArray(v) ? v.join('\n') : String(v ?? '');

  /** Word-level diff of two texts as HTML: insertions green, deletions red. */
  function words(a, b) {
    if (!a && !b) return '<span class="text-gray-600 italic">empty</span>';
    return Diff.diffWordsWithSpace(a, b).map(p =>
      p.added ? `<ins class="no-underline bg-green-900/50 text-green-300 rounded-sm">${esc(p.value)}</ins>`
      : p.removed ? `<del class="bg-red-900/50 text-red-300 rounded-sm">${esc(p.value)}</del>`
      : esc(p.value)).join('');
  }

  /**
   * Compare named fields. rows: [{label, from, to, collapsed?, hint?}], values
   * are strings or lists (one item per line). Changed fields are shown as diffs
   * (collapsed ones in a <details>); unchanged ones are listed in one line.
   */
  function fields(rows, { unchanged = true } = {}) {
    let html = '';
    const kept = [];
    rows.forEach(({ label, from, to, collapsed, hint }) => {
      const a = text(from), b = text(to);
      if (a === b) {
        if (a) kept.push(esc(label) + (a.length < 40 && !a.includes('\n') ? ` (${esc(a)})` : ''));
        return;
      }
      const block = `<pre class="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-overlay p-2 text-[11px] leading-relaxed text-gray-300 font-mono">${words(a, b)}</pre>`;
      const note = hint ? ` <span class="text-gray-600">(${esc(hint)})</span>` : '';
      html += collapsed
        ? `<details class="mb-3"><summary class="cursor-pointer select-none text-gray-400 hover:text-gray-200">${esc(label)}${note}</summary>${block}</details>`
        : `<div class="mb-3"><p class="font-medium text-amber-300">${esc(label)}${note}</p>${block}</div>`;
    });
    if (unchanged && kept.length) html += `<p class="mt-2 text-gray-500">Unchanged: ${kept.join(', ')}.</p>`;
    return html;
  }

  /** Count chips: {added, removed, changed, unchanged}. */
  function counts({ added = 0, removed = 0, changed = 0, unchanged = 0 }) {
    const chip = (cls, label) => `<span class="px-2 py-0.5 rounded font-medium ${cls}">${label}</span>`;
    return `<div class="flex flex-wrap gap-2 mb-4">${
      chip('bg-green-900/40 text-green-400', `+${added} added`)}${
      chip('bg-red-900/40 text-red-400', `−${removed} removed`)}${
      chip('bg-yellow-900/40 text-yellow-400', `~${changed} changed`)}${
      chip('bg-gray-800 text-gray-500', `${unchanged} unchanged`)}</div>`;
  }

  /**
   * Wire a diff modal. render(api) fills api.body; it runs on open and when
   * the From / to pickers change. Returns {open(from, to), close(), body,
   * from, to, loading(), error(message)}.
   */
  function modal(id, render) {
    const root = document.getElementById(id);
    if (!root) throw new Error(`VersionDiff.modal: no #${id} on the page (include version_diff_modal.html before the script that wires it)`);
    const from = root.querySelector('[data-diff-from]');
    const to = root.querySelector('[data-diff-to]');
    const api = {
      root, from, to,
      body: root.querySelector('[data-diff-body]'),
      open(a, b) {
        if (from && a != null) from.value = a;
        if (to && b != null) to.value = b;
        root.classList.remove('hidden');
        (to || root.querySelector('[data-diff-close]')).focus();
        render(api);
      },
      close() { root.classList.add('hidden'); },
      loading() { api.body.innerHTML = '<p class="text-gray-400 text-sm py-8 text-center">Loading…</p>'; },
      error(message) { api.body.innerHTML = `<p class="text-red-400">${esc(message)}</p>`; },
    };
    [from, to].forEach(select => select && select.addEventListener('change', () => render(api)));
    root.addEventListener('click', e => {
      if (e.target === root || e.target.closest('[data-diff-close]')) api.close();
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !root.classList.contains('hidden')) api.close();
    });
    return api;
  }

  window.VersionDiff = { words, fields, counts, modal };
})();
