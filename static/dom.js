// Redrawing by rebuilding innerHTML is fine for a timeline track and hostile to
// a text field: the element you are typing in is destroyed and replaced between
// keystrokes, taking focus, caret position, the browser's own undo history and
// any in-flight IME composition with it. Everything here exists so a redraw is
// invisible to someone in the middle of a word.

// Identifies a field across a rebuild. The replacement is a different element
// with the same id / data-attributes, so that tuple is what we match on.
const keyOf = (el) =>
  el ? [el.id, el.dataset?.t, el.dataset?.k, el.dataset?.i].join("|") : null;

/** True when the user is actively typing inside `scope`. */
export function isTyping(scope) {
  const a = document.activeElement;
  return !!a && scope.contains(a) && (a.tagName === "INPUT" || a.tagName === "TEXTAREA");
}

/**
 * Run `rebuild`, then put the user back where they were.
 *
 * Restores the raw string as well as the caret: a half-typed "0." reads back as
 * the number 0, so re-rendering from the manifest mid-edit would erase the
 * decimal point the moment you typed it.
 */
export function preserveFocus(scope, rebuild) {
  const a = document.activeElement;
  if (!a || !scope.contains(a) || !a.matches("input,textarea,select")) return rebuild();
  const k = keyOf(a);
  const { selectionStart: start, selectionEnd: end, value: raw } = a;
  rebuild();
  const back = [...scope.querySelectorAll("input,textarea,select")]
    .find((el) => keyOf(el) === k);
  if (!back) return;
  if (back.tagName !== "SELECT" && back.value !== raw) back.value = raw;
  back.focus();
  // number inputs and selects throw on setSelectionRange; nothing to restore there
  try { if (start != null) back.setSelectionRange(start, end); } catch { /* not a text field */ }
}

/** Assign a value unless the user is currently editing that field. */
export function setValue(el, v) {
  if (el !== document.activeElement) el.value = v;
}
