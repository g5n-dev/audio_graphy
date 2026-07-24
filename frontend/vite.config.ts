import { gzipSync } from "node:zlib";
import { defineConfig, loadEnv, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";
import { resolveApiProxyTarget } from "./src/config/apiProxy";

const INITIAL_JS_GZIP_BUDGET_KIB = 250;

function initialBundleBudgetPlugin(): Plugin {
  return {
    name: "initial-js-gzip-budget",
    apply: "build",
    generateBundle(_options, bundle) {
      const entryChunks = Object.values(bundle).filter(
        (output) => output.type === "chunk" && output.isEntry,
      );

      for (const entryChunk of entryChunks) {
        const initialFiles = new Set<string>();

        function collectStaticImports(fileName: string): void {
          if (initialFiles.has(fileName)) return;
          const output = bundle[fileName];
          if (!output || output.type !== "chunk") return;

          initialFiles.add(fileName);
          output.imports.forEach(collectStaticImports);
        }

        collectStaticImports(entryChunk.fileName);

        const gzipBytes = [...initialFiles].reduce((total, fileName) => {
          const output = bundle[fileName];
          return output?.type === "chunk"
            ? total + gzipSync(output.code).byteLength
            : total;
        }, 0);
        const gzipKiB = gzipBytes / 1024;

        if (gzipKiB > INITIAL_JS_GZIP_BUDGET_KIB) {
          this.error(
            `Initial JS for "${entryChunk.name}" is ${gzipKiB.toFixed(2)} KiB gzip; ` +
              `budget is ${INITIAL_JS_GZIP_BUDGET_KIB} KiB.`,
          );
        }
      }
    },
  };
}

function manualChunks(id: string): string | undefined {
  const moduleId = id.replaceAll("\\", "/");

  if (
    /\/node_modules\/(?:react|react-dom|react-router|react-router-dom|scheduler|zustand)\//.test(
      moduleId,
    )
  ) {
    return "vendor-react";
  }
  if (moduleId.includes("/node_modules/@tanstack/")) {
    return "vendor-query";
  }
  if (moduleId.includes("/node_modules/axios/")) {
    return "vendor-http";
  }
  // Keep Arco route modules under Rollup's automatic splitting. A package-wide
  // manual chunk pulls components used only by lazy pages back into the entry.
  if (
    moduleId.includes("/node_modules/@antv/layout/") ||
    /\/node_modules\/d3-[^/]+\//.test(moduleId)
  ) {
    return "vendor-graph-layout";
  }
  if (
    /\/node_modules\/@antv\/(?:g|g-canvas|g-lite|g-math|g-plugin-dragndrop|component)\//.test(
      moduleId,
    )
  ) {
    return "vendor-graph-render";
  }
  if (
    /\/node_modules\/@antv\/(?:g6|graphlib|algorithm)\//.test(moduleId)
  ) {
    return "vendor-graph-core";
  }
  if (moduleId.includes("/node_modules/@antv/")) {
    return "vendor-graph-utils";
  }

  return undefined;
}

// https://vitejs.dev/config/
export default defineConfig(async ({ mode }) => {
  const environment = {
    ...loadEnv(mode, process.cwd(), ""),
    ...process.env,
  };
  const plugins = [react(), initialBundleBudgetPlugin()];

  if (mode === "sites") {
    const { cloudflare } = await import("@cloudflare/vite-plugin");
    plugins.push(
      cloudflare({
        config: {
          name: "server",
          main: "./worker/index.ts",
          compatibility_flags: ["nodejs_compat"],
        },
      }),
    );
  }

  return {
    plugins,
    resolve: {
      alias: {
        "@": path.resolve(__dirname, "./src"),
      },
    },
    server: {
      host: true,
      port: 5173,
      strictPort: true,
      proxy:
        mode === "sites"
          ? undefined
          : {
              "/api": {
                target: resolveApiProxyTarget(environment),
                changeOrigin: true,
              },
            },
    },
    css: {
      preprocessorOptions: {
        less: {
          javascriptEnabled: true,
        },
      },
    },
    build: {
      rollupOptions: {
        output: {
          onlyExplicitManualChunks: true,
          manualChunks,
        },
      },
    },
  };
});
