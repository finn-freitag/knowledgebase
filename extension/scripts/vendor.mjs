// Copies third-party libraries and the shared map code into the extension (no remote code at runtime).
import { cpSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const nm = join(root, "node_modules");
const v = join(root, "vendor");
rmSync(v, { recursive: true, force: true });
mkdirSync(join(v, "leaflet"), { recursive: true });
mkdirSync(join(v, "geoman"), { recursive: true });
mkdirSync(join(v, "diff"), { recursive: true });

cpSync(join(nm, "leaflet/dist/leaflet.js"), join(v, "leaflet/leaflet.js"));
cpSync(join(nm, "leaflet/dist/leaflet.css"), join(v, "leaflet/leaflet.css"));
cpSync(join(nm, "leaflet/dist/images"), join(v, "leaflet/images"), { recursive: true });
cpSync(join(nm, "@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.min.js"), join(v, "geoman/leaflet-geoman.min.js"));
cpSync(join(nm, "@geoman-io/leaflet-geoman-free/dist/leaflet-geoman.css"), join(v, "geoman/leaflet-geoman.css"));
cpSync(join(nm, "diff/dist/diff.min.js"), join(v, "diff/diff.min.js"));
cpSync(join(root, "../shared/pkdb-map.js"), join(root, "lib/pkdb-map.js"));
console.log("Vendor files copied.");
