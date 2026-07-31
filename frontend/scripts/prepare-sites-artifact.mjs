import {
  access,
  copyFile,
  cp,
  readFile,
  readdir,
  rm,
} from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);
const distDir = path.join(frontendDir, "dist");
const clientDir = path.join(distDir, "client");
const clientIndex = path.join(clientDir, "index.html");
const clientAssets = path.join(clientDir, "assets");
const rootIndex = path.join(distDir, "index.html");
const rootAssets = path.join(distDir, "assets");
const sourceOpenAiDir = path.join(frontendDir, ".openai");
const distOpenAiDir = path.join(distDir, ".openai");

await access(path.join(distDir, "server", "index.js"));
await access(clientIndex);
await access(clientAssets);

// Sites serves static files from dist/ while the Cloudflare worker manifest
// keeps its own dist/client asset binding. Mirror the same sites-mode build to
// both locations so SPA assets and API worker routes remain available.
await rm(rootAssets, { force: true, recursive: true });
await copyFile(clientIndex, rootIndex);
await cp(clientAssets, rootAssets, { force: true, recursive: true });
await rm(distOpenAiDir, { force: true, recursive: true });
await cp(sourceOpenAiDir, distOpenAiDir, { force: true, recursive: true });

const html = await readFile(rootIndex, "utf8");
const assetRefs = [
  ...html.matchAll(/(?:src|href)="\/?(assets\/[^"]+)"/g),
].map((match) => match[1]);

if (assetRefs.length === 0) {
  throw new Error("Sites index.html does not reference any built assets");
}

await Promise.all(
  assetRefs.map((assetRef) => access(path.join(distDir, assetRef))),
);

const assetCount = (await readdir(rootAssets)).length;
console.log(
  `[sites] prepared root static assets (${assetCount} files, ${assetRefs.length} entry references)`,
);
