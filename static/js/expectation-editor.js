/* Expected-behaviour row editor (templates/partials/expectation_editor.html).
 *
 * Each editor keeps its rows in a hidden textarea as JSON
 * ([{id, text, required}, ...]); the server parses that field. Writing the
 * textarea fires an "input" event so listeners (the "will publish" hint) see
 * edits. Ids are kept as they were loaded and never shown or edited here.
 */
(function () {
  function rowsOf(editor) {
    return Array.from(editor.querySelectorAll('[data-row]')).map(function (row) {
      var out = { text: row.querySelector('[data-text]').value.trim(),
                  required: row.querySelector('[data-required]').checked };
      if (row.dataset.id) out.id = row.dataset.id;
      return out;
    }).filter(function (r) { return r.text; });
  }

  function sync(editor) {
    var field = editor.querySelector('textarea[hidden]');
    var rows = rowsOf(editor);
    var value = JSON.stringify(rows.map(function (r) {
      return { id: r.id || '', text: r.text, required: r.required };
    }));
    if (field.value !== value) {
      field.value = value;
      field.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  function addRow(editor, data) {
    var tpl = editor.querySelector('template[data-row-template]');
    var row = tpl.content.firstElementChild.cloneNode(true);
    row.querySelector('[data-text]').value = (data && data.text) || '';
    row.querySelector('[data-required]').checked = !data || data.required !== false;
    if (data && data.id) row.dataset.id = data.id;
    row.querySelector('[data-remove]').addEventListener('click', function () {
      row.remove();
      sync(editor);
    });
    row.addEventListener('input', function () { sync(editor); });
    row.addEventListener('change', function () { sync(editor); });
    editor.querySelector('[data-rows]').appendChild(row);
    return row;
  }

  function init(editor) {
    if (editor.dataset.ready) return;
    editor.dataset.ready = '1';
    var field = editor.querySelector('textarea[hidden]');
    var rows = [];
    try { rows = JSON.parse(field.value || '[]'); } catch (e) { rows = []; }
    // Normalise the saved value so an untouched editor compares equal to it.
    field.value = JSON.stringify(rows.map(function (r) {
      return { id: r.id || '', text: r.text, required: r.required !== false };
    }));
    field.defaultValue = field.value;
    rows.forEach(function (r) { addRow(editor, r); });
    if (!rows.length) addRow(editor);
    editor.querySelector('[data-add]').addEventListener('click', function () {
      addRow(editor).querySelector('[data-text]').focus();
    });
  }

  window.initExpectationEditors = function (root) {
    (root || document).querySelectorAll('[data-expectation-editor]').forEach(init);
  };
  document.addEventListener('DOMContentLoaded', function () { window.initExpectationEditors(); });
})();
