const DEFAULT_API_PROXY_TARGET = "http://localhost:8000";

export function resolveApiProxyTarget(
  environment: Readonly<Record<string, string | undefined>>,
): string {
  const configuredTarget = environment.VITE_API_PROXY_TARGET?.trim();
  if (!configuredTarget) return DEFAULT_API_PROXY_TARGET;

  let target: URL;
  try {
    target = new URL(configuredTarget);
  } catch {
    throw new Error(
      "VITE_API_PROXY_TARGET must be an absolute http(s) URL",
    );
  }
  if (target.protocol !== "http:" && target.protocol !== "https:") {
    throw new Error(
      "VITE_API_PROXY_TARGET must use the http or https protocol",
    );
  }
  return configuredTarget.replace(/\/+$/, "");
}
