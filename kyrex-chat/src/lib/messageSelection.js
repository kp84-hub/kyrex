const SCOPE = '[data-message-selection]';
const CONTROL = 'input, textarea, select, button, [contenteditable]:not([contenteditable="false"])';

const elementFor = node => node?.nodeType === 1 ? node : node?.parentElement;

// Native mobile Select all changes the document selection, without a keyboard
// event. Remember where selection began and clamp that range to this message.
// The reply stays ordinary, read-only DOM: no editable surface or touch/menu
// replacement is needed. One listener set belongs to the mounted transcript.
export function bindMessageSelection(container, onSelection = () => {}) {
  const doc = container.ownerDocument;
  let active = null;
  let notified = false;
  const scopeFor = node => {
    const scope = elementFor(node)?.closest(SCOPE);
    return scope && container.contains(scope) ? scope : null;
  };
  const isControl = node => Boolean(elementFor(node)?.closest(CONTROL));
  const activate = scope => { active = scope; notified = false; };

  const start = event => {
    // Clicking/tapping outside a reply releases its selection boundary. Form
    // controls retain native editing/Select all, even inside an approval card.
    activate(isControl(event.target) ? null : scopeFor(event.target));
  };
  const contain = () => {
    const selection = doc.getSelection();
    if (!selection?.rangeCount || selection.isCollapsed) {
      notified = false;
      return;
    }
    if (!active && isControl(doc.activeElement)) return;
    if (active && !container.contains(active)) activate(null);
    if (!active) activate(scopeFor(selection.anchorNode));
    if (!active) return;
    const range = selection.getRangeAt(0);
    if (!range.intersectsNode(active)) {
      activate(scopeFor(selection.anchorNode));
      return;
    }
    if (!notified) { notified = true; onSelection(); }
    if (selection.rangeCount === 1 && active.contains(range.startContainer)
        && active.contains(range.endContainer)) return;

    const bounds = doc.createRange();
    bounds.selectNodeContents(active);
    const clipped = range.cloneRange();
    if (clipped.compareBoundaryPoints(0 /* START_TO_START */, bounds) < 0) {
      clipped.setStart(bounds.startContainer, bounds.startOffset);
    }
    if (clipped.compareBoundaryPoints(2 /* END_TO_END */, bounds) > 0) {
      clipped.setEnd(bounds.endContainer, bounds.endOffset);
    }
    const backward = selection.anchorNode === range.endContainer
      && selection.anchorOffset === range.endOffset;
    if (selection.setBaseAndExtent) {
      const from = backward ? 'end' : 'start';
      const to = backward ? 'start' : 'end';
      selection.setBaseAndExtent(clipped[`${from}Container`], clipped[`${from}Offset`],
        clipped[`${to}Container`], clipped[`${to}Offset`]);
    } else {
      selection.removeAllRanges();
      selection.addRange(clipped);
    }
  };
  const selectstart = event => {
    const scope = scopeFor(event.target);
    if (isControl(event.target)) activate(null);
    else if (scope && scope !== active) activate(scope);
    // Android's native Select all can originate on the document/body. Keep
    // the message remembered from the long press instead of losing its scope.
  };
  const keydown = event => {
    if (event.key.toLowerCase() !== 'a' || !(event.ctrlKey || event.metaKey)
        || event.altKey || event.shiftKey || isControl(event.target)) return;
    const selection = doc.getSelection();
    const scope = scopeFor(event.target) || (active && container.contains(active) ? active : null)
      || scopeFor(selection?.anchorNode);
    if (!scope || !selection) return;
    event.preventDefault();
    activate(scope);
    const range = doc.createRange();
    range.selectNodeContents(scope);
    selection.removeAllRanges();
    selection.addRange(range);
    contain();
  };

  // selectstart also covers keyboard-initiated selections. Pointer/mouse/touch
  // capture remembers the scope before native selection begins on each browser.
  const starts = ['pointerdown', 'mousedown', 'touchstart', 'focusin'];
  starts.forEach(type => doc.addEventListener(type, start, true));
  doc.addEventListener('selectstart', selectstart, true);
  doc.addEventListener('selectionchange', contain);
  doc.addEventListener('keydown', keydown, true);
  return () => {
    starts.forEach(type => doc.removeEventListener(type, start, true));
    doc.removeEventListener('selectstart', selectstart, true);
    doc.removeEventListener('selectionchange', contain);
    doc.removeEventListener('keydown', keydown, true);
    active = null;
  };
}
