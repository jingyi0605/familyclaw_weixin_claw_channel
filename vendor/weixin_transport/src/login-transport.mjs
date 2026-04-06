import { randomUUID } from "node:crypto";

import qrcode from "./package/qrcode.js";

const DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com";
const DEFAULT_BOT_TYPE = "3";
const DEFAULT_TIMEOUT_MS = 15_000;
const QR_STATUS_TIMEOUT_MS = 35_000;
const LOGIN_QR_TTL_MS = 5 * 60_000;

export async function handleLoginActionRequest(request) {
  const payload = isObject(request?.payload) ? request.payload : {};
  const testing = isObject(payload.testing) ? payload.testing : {};
  const channelAccountId = resolveChannelAccountId(payload);
  const stubbed = resolveStubbedResponse(testing, request.action);
  if (stubbed) {
    return {
      action_name: request.action,
      channel_account_id: channelAccountId,
      ...stubbed
    };
  }

  if (request.action === "start_login") {
    return startLogin(payload, channelAccountId);
  }
  if (request.action === "get_login_status") {
    return getLoginStatus(payload, channelAccountId);
  }
  if (request.action === "logout" || request.action === "purge_runtime_state") {
    return {
      action_name: request.action,
      channel_account_id: channelAccountId,
      status: "accepted",
      message: "Login state cleanup is handled by the Python plugin."
    };
  }

  fail("unsupported_action", `Unsupported action request: ${request.action}`, "action");
}

async function startLogin(payload, channelAccountId) {
  const transport = resolveTransportConfig(payload);
  const botType = asText(transport.botType, DEFAULT_BOT_TYPE);
  const url = new URL(
    `ilink/bot/get_bot_qrcode?bot_type=${encodeURIComponent(botType)}`,
    ensureTrailingSlash(transport.apiBaseUrl)
  );
  const response = await fetchJson(url, {
    headers: buildHeaders(transport.routeTag),
    timeoutMs: DEFAULT_TIMEOUT_MS
  });

  const qrCode = asText(response?.qrcode, "");
  const qrTargetUrl = asText(response?.qrcode_img_content, "");
  if (!qrCode || !qrTargetUrl) {
    fail("bridge_protocol_error", "QR code response is missing required fields");
  }
  const qrPreviewUrl = buildQrPreviewDataUrl(qrTargetUrl);

  return {
    action_name: "start_login",
    channel_account_id: channelAccountId,
    login_status: "waiting_scan",
    login_session_key: asText(payload.login_session_key, channelAccountId || randomUUID()),
    login_qrcode: qrCode,
    qr_code_url: qrPreviewUrl,
    api_base_url: transport.apiBaseUrl,
    expires_at: new Date(Date.now() + LOGIN_QR_TTL_MS).toISOString(),
    message: "QR code generated. Scan it with Weixin to continue login."
  };
}

export function buildQrPreviewDataUrl(qrTargetUrl) {
  const normalized = asText(qrTargetUrl, "");
  if (!normalized) {
    fail("bridge_protocol_error", "QR preview target URL is required", "qrcode_img_content");
  }

  try {
    const qr = qrcode(0, "M");
    qr.addData(normalized, "Byte");
    qr.make();
    const svg = qr.createSvgTag({
      cellSize: 6,
      margin: 12,
      scalable: true,
      alt: { text: "Weixin login QR code" },
      title: { text: "Weixin login QR code" }
    });
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error);
    fail("bridge_protocol_error", `Failed to build QR preview artifact: ${detail}`, "qrcode_img_content");
  }
}

async function getLoginStatus(payload, channelAccountId) {
  const loginQrcode = asText(payload.login_qrcode, "");
  const loginSessionKey = asText(payload.login_session_key, channelAccountId);
  if (!loginQrcode) {
    return {
      action_name: "get_login_status",
      channel_account_id: channelAccountId,
      login_status: "not_logged_in",
      login_session_key: loginSessionKey,
      message: "No pending QR login session was found."
    };
  }

  const transport = resolveTransportConfig(payload);
  const url = new URL(
    `ilink/bot/get_qrcode_status?qrcode=${encodeURIComponent(loginQrcode)}`,
    ensureTrailingSlash(transport.apiBaseUrl)
  );
  const response = await fetchJson(url, {
    headers: buildHeaders(transport.routeTag),
    timeoutMs: QR_STATUS_TIMEOUT_MS
  });
  const loginStatus = mapLoginStatus(asText(response?.status, "wait"));

  return {
    action_name: "get_login_status",
    channel_account_id: channelAccountId,
    login_session_key: loginSessionKey,
    login_status: loginStatus,
    token: asNullableText(response?.bot_token),
    provider_account_id: asNullableText(response?.ilink_bot_id),
    api_base_url: asText(response?.baseurl, transport.apiBaseUrl),
    user_id: asNullableText(response?.ilink_user_id),
    message: buildStatusMessage(loginStatus)
  };
}

function resolveTransportConfig(payload) {
  const transport = isObject(payload.transport) ? payload.transport : {};
  return {
    apiBaseUrl: asText(
      transport.api_base_url,
      asText(transport.base_url, DEFAULT_API_BASE_URL)
    ),
    botType: asNullableText(transport.bot_type),
    routeTag: asNullableText(transport.route_tag)
  };
}

function buildHeaders(routeTag) {
  const headers = {
    "iLink-App-ClientVersion": "1"
  };
  if (routeTag) {
    headers.SKRouteTag = routeTag;
  }
  return headers;
}

async function fetchJson(url, { headers, timeoutMs }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url.toString(), {
      headers,
      signal: controller.signal
    });
    const rawText = await response.text();
    if (!response.ok) {
      fail(
        "transport_unavailable",
        `Weixin transport request failed: ${response.status} ${response.statusText}`
      );
    }
    return rawText ? JSON.parse(rawText) : {};
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      fail("transport_unavailable", `Weixin transport request timed out after ${timeoutMs}ms`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function mapLoginStatus(rawStatus) {
  if (rawStatus === "confirmed") {
    return "active";
  }
  if (rawStatus === "scaned") {
    return "scan_confirmed";
  }
  if (rawStatus === "expired") {
    return "expired";
  }
  return "waiting_scan";
}

function buildStatusMessage(loginStatus) {
  if (loginStatus === "active") {
    return "Weixin login succeeded.";
  }
  if (loginStatus === "scan_confirmed") {
    return "QR code scanned. Confirm login in Weixin.";
  }
  if (loginStatus === "expired") {
    return "QR code expired. Generate a new one.";
  }
  return "QR code generated. Waiting for scan.";
}

function resolveStubbedResponse(testing, action) {
  const responses = isObject(testing.transport_responses) ? testing.transport_responses : {};
  const response = responses[action];
  return isObject(response) ? response : null;
}

function resolveChannelAccountId(payload) {
  const account = isObject(payload.account) ? payload.account : {};
  return asText(account.id, asText(payload.channel_account_id, "unknown-account"));
}

function ensureTrailingSlash(value) {
  return value.endsWith("/") ? value : `${value}/`;
}

function asText(value, fallback) {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  return fallback;
}

function asNullableText(value) {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  return null;
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function fail(code, message, field = null) {
  const error = new Error(message);
  error.code = code;
  error.field = field;
  throw error;
}
