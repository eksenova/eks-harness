import { build } from "esbuild";

await build({
  entryPoints: ["src/global.ts"],
  bundle: true,
  format: "iife",
  target: "es2022",
  outfile: "dist/scene-runtime.js",
});
