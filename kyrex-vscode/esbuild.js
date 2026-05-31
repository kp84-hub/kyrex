const esbuild = require("esbuild");

const watch = process.argv.includes("--watch");

/** @type {esbuild.BuildOptions} */
const config = {
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outdir: "dist",
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  target: "node18",
  sourcemap: true,
  minify: false,
  logLevel: "info",
};

if (watch) {
  esbuild.context(config).then((ctx) => ctx.watch());
} else {
  esbuild.build(config).catch(() => process.exit(1));
}
