import { readFileSync } from "node:fs";

import { dispatchBridgeRequest } from "./src/dispatch.mjs";

function writeJson(payload) {
  process.stdout.write(JSON.stringify(payload));
}

async function main() {
  if (process.argv.includes("--self-check")) {
    writeJson({
      ok: true,
      result: {
        status: "ok",
        message: "weixin transport bridge is ready"
      }
    });
    return;
  }

  const raw = readFileSync(0, "utf8");
  const request = JSON.parse(raw || "{}");
  const result = await dispatchBridgeRequest(request);
  writeJson({
    ok: true,
    result
  });
}

main().catch((error) => {
  writeJson({
    ok: false,
    error: {
      code: error?.code || "bridge_protocol_error",
      message: error?.message || "Unknown Node bridge failure",
      field: typeof error?.field === "string" ? error.field : null
    }
  });
});
