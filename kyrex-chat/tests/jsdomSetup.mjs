// jsdomSetup.mjs — install a DOM + browser-ish globals BEFORE React, ReactDOM,
// or the component under test is imported. Loaded with `node --import`.
//
// Kept separate from the assertion file so the globals exist before the
// bundled test module (which imports react-dom/client) is evaluated.
import { JSDOM } from "jsdom";

const dom = new JSDOM(
  "<!doctype html><html><body></body></html>",
  { url: "http://localhost/" },
);
const { window } = dom;

function define(name, value) {
  Object.defineProperty(globalThis, name, {
    value, configurable: true, writable: true,
  });
}

define("window", window);
define("document", window.document);
// Node >= 21 exposes a read-only `navigator`; redefine it for jsdom.
define("navigator", window.navigator);
define("HTMLElement", window.HTMLElement);
define("Node", window.Node);
define("Event", window.Event);
define("CustomEvent", window.CustomEvent);
define("getComputedStyle", window.getComputedStyle.bind(window));
define("requestAnimationFrame", (cb) => setTimeout(() => cb(Date.now()), 0));
define("cancelAnimationFrame", (id) => clearTimeout(id));
// React 19: required so act() runs without the "not configured" warning.
define("IS_REACT_ACT_ENVIRONMENT", true);
