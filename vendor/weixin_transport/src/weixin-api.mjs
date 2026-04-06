import { randomBytes } from "node:crypto";

export const DEFAULT_API_BASE_URL = "https://ilinkai.weixin.qq.com";
export const DEFAULT_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c";
export const DEFAULT_LONG_POLL_TIMEOUT_MS = 35_000;
export const DEFAULT_API_TIMEOUT_MS = 20_000;

export async function fetchUpdates({ apiBaseUrl, token, cursor }) {
  return postJson({
    url: new URL("ilink/bot/getupdates", ensureTrailingSlash(apiBaseUrl)),
    body: {
      get_updates_buf: typeof cursor === "string" ? cursor : "",
      base_info: buildBaseInfo()
    },
    token,
    timeoutMs: DEFAULT_LONG_POLL_TIMEOUT_MS
  });
}

export async function fetchUploadUrl({
  apiBaseUrl,
  token,
  fileKey,
  mediaType,
  toUserId,
  rawSize,
  rawFileMd5,
  encryptedSize,
  aesKeyHex
}) {
  return postJson({
    url: new URL("ilink/bot/getuploadurl", ensureTrailingSlash(apiBaseUrl)),
    body: {
      filekey: fileKey,
      media_type: mediaType,
      to_user_id: toUserId,
      rawsize: rawSize,
      rawfilemd5: rawFileMd5,
      filesize: encryptedSize,
      no_need_thumb: true,
      aeskey: aesKeyHex,
      base_info: buildBaseInfo()
    },
    token,
    timeoutMs: DEFAULT_API_TIMEOUT_MS
  });
}

export async function sendWeixinMessage({ apiBaseUrl, token, message }) {
  await postJson({
    url: new URL("ilink/bot/sendmessage", ensureTrailingSlash(apiBaseUrl)),
    body: {
      msg: message,
      base_info: buildBaseInfo()
    },
    token,
    timeoutMs: DEFAULT_API_TIMEOUT_MS
  });
}

async function postJson({ url, body, token, timeoutMs }) {
  const serialized = JSON.stringify(body);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url.toString(), {
      method: "POST",
      headers: buildHeaders({ token, body: serialized }),
      body: serialized,
      signal: controller.signal
    });
    const rawText = await response.text();
    if (!response.ok) {
      throw new Error(`Weixin transport request failed: ${response.status} ${response.statusText}`);
    }
    return rawText ? JSON.parse(rawText) : {};
  } finally {
    clearTimeout(timer);
  }
}

function buildHeaders({ token, body }) {
  return {
    "Content-Type": "application/json",
    AuthorizationType: "ilink_bot_token",
    Authorization: `Bearer ${token}`,
    "Content-Length": String(Buffer.byteLength(body, "utf-8")),
    "X-WECHAT-UIN": Buffer.from(String(randomBytes(4).readUInt32BE(0)), "utf-8").toString("base64")
  };
}

function buildBaseInfo() {
  return {
    channel_version: "familyclaw-plugin-dev"
  };
}

function ensureTrailingSlash(value) {
  return value.endsWith("/") ? value : `${value}/`;
}
