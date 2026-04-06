import { handleChannelRequest } from "./channel-transport.mjs";
import { handleLoginActionRequest } from "./login-transport.mjs";
import { runMockTransport } from "./mock-transport.mjs";

export async function dispatchBridgeRequest(request) {
  const kind = typeof request?.kind === "string" ? request.kind.trim() : "";
  const action = typeof request?.action === "string" ? request.action.trim() : "";
  if (!kind) {
    const error = new Error("Bridge request kind is required");
    error.code = "bridge_request_invalid";
    error.field = "kind";
    throw error;
  }
  if (!action) {
    const error = new Error("Bridge request action is required");
    error.code = "bridge_request_invalid";
    error.field = "action";
    throw error;
  }
  if (kind === "action" && isLoginAction(action)) {
    return handleLoginActionRequest({
      action,
      payload: request?.payload ?? {},
      runtime: request?.runtime ?? {}
    });
  }
  if (kind === "channel" && shouldUseChannelTransport(action, request?.payload ?? {})) {
    return handleChannelRequest({
      action,
      payload: request?.payload ?? {},
      runtime: request?.runtime ?? {}
    });
  }
  return runMockTransport({
    kind,
    action,
    payload: request?.payload ?? {},
    runtime: request?.runtime ?? {}
  });
}

function isLoginAction(action) {
  return (
    action === "start_login" ||
    action === "get_login_status" ||
    action === "logout" ||
    action === "purge_runtime_state"
  );
}

function shouldUseChannelTransport(action, payload) {
  if (!(action === "poll" || action === "send" || action === "probe")) {
    return false;
  }
  if (payload?.transport && typeof payload.transport === "object") {
    return true;
  }
  const responses = payload?.testing?.transport_responses;
  return responses && typeof responses === "object" && responses[action] && typeof responses[action] === "object";
}
