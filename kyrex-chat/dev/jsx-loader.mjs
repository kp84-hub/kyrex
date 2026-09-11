// dev/jsx-loader.mjs — esbuild hook so Node can import the app's .jsx modules.
import { readFile } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { transform } from 'esbuild';
import path from 'node:path';

const JSX_RE = /\.jsx$/;

export async function resolve(specifier, context, next) {
  try {
    const r = await next(specifier, context);
    if (JSX_RE.test(r.url) && !r.url.includes('node_modules')) {
      return { url: r.url, shortCircuit: true, format: 'module' };
    }
    return r;
  } catch (e) {
    // Vite resolves extensionless relative imports; Node does not.
    if (e?.code === 'ERR_MODULE_NOT_FOUND' && /^\.\.?\//.test(specifier)) {
      for (const ext of ['.js', '.jsx']) {
        try {
          return await next(specifier + ext, context);
        } catch {
          /* try next */
        }
      }
    }
    throw e;
  }
}

export async function load(url, context, next) {
  if (JSX_RE.test(url)) {
    const file = fileURLToPath(url);
    const src = await readFile(file, 'utf8');
    const jsx = await transform(src, {
      loader: 'jsx',
      jsx: 'automatic',
      format: 'esm',
      sourcefile: file,
    });
    return { format: 'module', source: jsx.code, shortCircuit: true };
  }
  return next(url, context);
}
