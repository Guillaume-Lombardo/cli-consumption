import "server-only";

const SECRET_BYTES = 32;

export interface DashboardServerConfig {
  apiUrl: URL;
  dashboardOrigin: string | null;
  exportToken: string | null;
  password: string;
  readToken: string;
  sessionSecret: string;
}

function required(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error("dashboard_configuration_invalid");
  return value;
}

/** Read server-only configuration without returning secret values in errors. */
export function dashboardServerConfig(): DashboardServerConfig {
  const password = required("CLI_CONSUMPTION_DASHBOARD_PASSWORD");
  const readToken = required("CLI_CONSUMPTION_READ_TOKEN");
  const sessionSecret = required("CLI_CONSUMPTION_SESSION_SECRET");
  const dashboardOrigin = process.env.CLI_CONSUMPTION_DASHBOARD_ORIGIN ?? null;
  const exportToken = process.env.CLI_CONSUMPTION_EXPORT_TOKEN || null;
  let apiUrl: URL;
  try {
    apiUrl = new URL(required("CLI_CONSUMPTION_API_URL"));
  } catch {
    throw new Error("dashboard_configuration_invalid");
  }
  if (
    !["http:", "https:"].includes(apiUrl.protocol) ||
    apiUrl.username !== "" ||
    apiUrl.password !== "" ||
    apiUrl.pathname !== "/" ||
    apiUrl.search !== "" ||
    apiUrl.hash !== "" ||
    password.length < 12 ||
    Buffer.byteLength(sessionSecret, "utf8") < SECRET_BYTES
  ) {
    throw new Error("dashboard_configuration_invalid");
  }
  if (dashboardOrigin !== null) {
    try {
      const parsed = new URL(dashboardOrigin);
      if (
        parsed.origin !== dashboardOrigin ||
        !["http:", "https:"].includes(parsed.protocol)
      ) {
        throw new Error("dashboard_configuration_invalid");
      }
    } catch {
      throw new Error("dashboard_configuration_invalid");
    }
  }
  return {
    apiUrl,
    dashboardOrigin,
    exportToken,
    password,
    readToken,
    sessionSecret,
  };
}
