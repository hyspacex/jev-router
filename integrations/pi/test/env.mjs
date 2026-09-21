// Import this before the extension. It points the extension at a fixture
// config, so a test run can never read the developer's own
// ~/.pi/agent/jev-router.json, and it never talks to a real router: the
// fixture origin is a .invalid name that cannot resolve.
import { fileURLToPath } from "node:url";

export const FIXTURE_CONFIG = fileURLToPath(new URL("./fixtures/jev-router.json", import.meta.url));
export const FIXTURE_ORIGIN = "http://router.test.invalid";
export const FIXTURE_MAX_OUTPUT_TOKENS = 4096;

process.env.JEV_ROUTER_PI_CONFIG = FIXTURE_CONFIG;

// Nothing in this suite may reach the network. A test that wants HTTP
// installs its own stub and puts this one back afterwards.
globalThis.fetch = async () => {
  throw new Error("this test did not install a fetch stub; the suite never makes real requests");
};

/** Run `body` with `stub` as the transport and a throwaway admin credential. */
export async function withRouter(stub, body) {
  const previousFetch = globalThis.fetch;
  const previousToken = process.env.JEV_ROUTER_ADMIN_TOKEN;
  globalThis.fetch = stub;
  process.env.JEV_ROUTER_ADMIN_TOKEN = "control-test";
  try { return await body(); }
  finally {
    globalThis.fetch = previousFetch;
    if (previousToken === undefined) delete process.env.JEV_ROUTER_ADMIN_TOKEN;
    else process.env.JEV_ROUTER_ADMIN_TOKEN = previousToken;
  }
}
