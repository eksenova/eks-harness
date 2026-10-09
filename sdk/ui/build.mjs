import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
mkdirSync(resolve(here, "dist"), { recursive: true });
copyFileSync(resolve(here, "../../web/src/styles/tokens.css"), resolve(here, "dist/tokens.css"));
