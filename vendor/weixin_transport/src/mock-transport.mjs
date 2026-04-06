function fail(code, message, field = null) {
  const error = new Error(message);
  error.code = code;
  error.field = field;
  throw error;
}

export async function runMockTransport(request) {
  const payload = isObject(request.payload) ? request.payload : {};
  const testing = isObject(payload.testing) ? payload.testing : {};
  if (testing.force_error === true) {
    fail(
      asText(testing.error_code, "transport_mock_failure"),
      asText(testing.message, "forced mock failure"),
      typeof testing.field === "string" ? testing.field : null
    );
  }

  if (request.kind === "channel") {
    return handleChannelRequest(request.action, payload);
  }
  if (request.kind === "action") {
    return handleActionRequest(request.action, payload);
  }
  fail("bridge_request_invalid", `Unsupported request kind: ${request.kind}`, "kind");
}

function handleChannelRequest(action, payload) {
  if (action === "poll") {
    const pollState = isObject(payload.poll_state) ? payload.poll_state : {};
    return {
      events: [],
      next_cursor: typeof pollState.cursor === "string" ? pollState.cursor : null,
      message: "weixin mock bridge poll is wired"
    };
  }
  if (action === "send") {
    const delivery = isObject(payload.delivery) ? payload.delivery : {};
    const text = asText(delivery.text, "");
    if (!text) {
      fail("invalid_delivery", "delivery.text is required", "delivery.text");
    }
    return {
      provider_message_ref: "mock-provider-message-ref",
      message: "weixin mock bridge send is wired"
    };
  }
  if (action === "probe") {
    return {
      probe_status: "ok",
      message: "weixin mock bridge probe is ready"
    };
  }
  if (action === "webhook") {
    return {
      message: "weixin claw channel currently runs in polling mode",
      http_response: {
        status_code: 202,
        body_text: "polling-only",
        media_type: "text/plain"
      }
    };
  }
  fail("unsupported_action", `Unsupported channel action: ${action}`, "action");
}

function handleActionRequest(action, payload) {
  const accountId = resolveAccountId(payload);
  if (action === "start_login") {
    return {
      action_name: action,
      channel_account_id: accountId,
      login_status: "not_logged_in",
      qr_code_url: null,
      message: "weixin mock bridge action is wired"
    };
  }
  if (action === "get_login_status") {
    return {
      action_name: action,
      channel_account_id: accountId,
      login_status: "not_logged_in",
      message: "weixin mock bridge action is wired"
    };
  }
  if (action === "logout" || action === "purge_runtime_state") {
    return {
      action_name: action,
      channel_account_id: accountId,
      status: "accepted",
      message: "weixin mock bridge action is wired"
    };
  }
  fail("unsupported_action", `Unsupported action request: ${action}`, "action");
}

function resolveAccountId(payload) {
  const account = isObject(payload.account) ? payload.account : {};
  return asText(account.id, "unknown-account");
}

function asText(value, fallback) {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  return fallback;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
