import { createHash, randomBytes, randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";

import {
  DEFAULT_API_BASE_URL,
  DEFAULT_API_TIMEOUT_MS,
  DEFAULT_CDN_BASE_URL,
  fetchUpdates,
  fetchUploadUrl,
  sendWeixinMessage
} from "./weixin-api.mjs";
import {
  aesEcbPaddedSize,
  downloadAndDecryptBuffer,
  downloadPlainBuffer,
  encodeAesKeyForMessage,
  inferExtension,
  inferMimeFromFileName,
  sanitizeFileName,
  saveBufferToMediaDir,
  uploadBufferToCdn
} from "./media-transport.mjs";

const UploadMediaType = {
  IMAGE: 1,
  VIDEO: 2,
  FILE: 3
};

export async function handleChannelRequest(request) {
  const payload = isObject(request?.payload) ? request.payload : {};
  const runtime = isObject(request?.runtime) ? request.runtime : {};
  const testing = isObject(payload.testing) ? payload.testing : {};
  const stubbed = resolveStubbedResponse(testing, request.action);
  if (stubbed) {
    return stubbed;
  }

  if (request.action === "poll") {
    return pollMessages(payload, runtime);
  }
  if (request.action === "send") {
    return sendDelivery(payload, runtime);
  }
  if (request.action === "probe") {
    return probeChannel(payload);
  }
  if (request.action === "webhook") {
    return {
      message: "weixin claw channel currently runs in polling mode",
      http_response: {
        status_code: 202,
        body_text: "polling-only",
        media_type: "text/plain"
      }
    };
  }
  fail("unsupported_action", `Unsupported channel action: ${request.action}`, "action");
}

async function pollMessages(payload, runtime) {
  const transport = resolveTransportConfig(payload);
  ensureTokenPresent(transport.token);
  const transportState = isObject(payload.transport_state) ? payload.transport_state : {};
  let response;
  try {
    response = await fetchUpdates({
      apiBaseUrl: transport.apiBaseUrl,
      token: transport.token,
      cursor: transportState.cursor
    });
  } catch (error) {
    fail("transport_unavailable", error instanceof Error ? error.message : String(error));
  }

  if (coerceInt(response?.errcode) === -14) {
    fail("login_expired", asText(response?.errmsg, "weixin login expired"));
  }

  const rawMessages = Array.isArray(response?.msgs) ? response.msgs : [];
  const messages = [];
  for (const rawMessage of rawMessages) {
    if (!isObject(rawMessage)) {
      continue;
    }
    messages.push(
      await enrichInboundMessage({
        message: rawMessage,
        runtime,
        cdnBaseUrl: transport.cdnBaseUrl
      })
    );
  }

  return {
    message: "weixin polling completed",
    messages,
    transport_cursor: asNullableText(response?.get_updates_buf),
    longpolling_timeout_ms: coerceInt(response?.longpolling_timeout_ms)
  };
}

async function sendDelivery(payload, runtime) {
  const transport = resolveTransportConfig(payload);
  ensureTokenPresent(transport.token);

  const delivery = isObject(payload.delivery) ? payload.delivery : {};
  const toUserId = asText(delivery.external_user_id, "");
  const text = asNullableText(delivery.text);
  const contextToken = asText(delivery.context_token, "");
  const attachments = Array.isArray(delivery.attachments) ? delivery.attachments : [];
  if (!toUserId) {
    fail("invalid_delivery", "delivery.external_user_id is required", "delivery.external_user_id");
  }
  if (!text && attachments.length === 0) {
    fail("invalid_delivery", "delivery.text or delivery.attachments is required", "delivery");
  }
  if (!contextToken) {
    fail("context_token_missing", "delivery.context_token is required", "delivery.context_token");
  }

  let providerMessageRef = null;
  if (text) {
    providerMessageRef = await sendTextOnlyMessage({
      transport,
      toUserId,
      contextToken,
      text
    });
  }

  for (const attachment of attachments) {
    providerMessageRef = await sendAttachmentMessage({
      transport,
      runtime,
      toUserId,
      contextToken,
      attachment
    });
  }

  return {
    provider_message_ref: providerMessageRef || asText(delivery.delivery_id, randomUUID()),
    message: "weixin send completed"
  };
}

async function probeChannel(payload) {
  const transport = resolveTransportConfig(payload);
  ensureTokenPresent(transport.token);
  return {
    probe_status: "ok",
    message: "weixin channel session is active"
  };
}

function resolveTransportConfig(payload) {
  const transport = isObject(payload.transport) ? payload.transport : {};
  return {
    apiBaseUrl: asText(
      transport.api_base_url,
      asText(transport.base_url, DEFAULT_API_BASE_URL)
    ),
    cdnBaseUrl: asText(
      transport.cdn_base_url,
      asText(transport.cdnBaseUrl, DEFAULT_CDN_BASE_URL)
    ),
    token: asNullableText(transport.token),
    providerAccountId: asNullableText(transport.provider_account_id)
  };
}

function resolveStubbedResponse(testing, action) {
  const responses = isObject(testing.transport_responses) ? testing.transport_responses : {};
  const response = responses[action];
  return isObject(response) ? response : null;
}

function ensureTokenPresent(token) {
  if (!token) {
    fail("login_required", "weixin transport token is missing", "transport.token");
  }
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

function coerceInt(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.trunc(value);
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number.parseInt(value.trim(), 10);
    return Number.isNaN(parsed) ? null : parsed;
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

async function enrichInboundMessage({ message, runtime, cdnBaseUrl }) {
  const downloaded = [];
  const downloadErrors = [];
  const itemList = Array.isArray(message.item_list) ? message.item_list : [];
  for (const item of itemList) {
    if (!isObject(item)) {
      continue;
    }
    try {
      const attachment = await downloadAttachmentFromItem({
        item,
        runtime,
        cdnBaseUrl
      });
      if (attachment) {
        downloaded.push(attachment);
      }
    } catch (error) {
      downloadErrors.push({
        error_code: "media_download_failed",
        detail: error instanceof Error ? error.message : String(error),
        item_type: coerceInt(item.type)
      });
    }
  }
  return {
    ...message,
    downloaded_attachments: downloaded,
    download_errors: downloadErrors
  };
}

async function downloadAttachmentFromItem({ item, runtime, cdnBaseUrl }) {
  const itemType = coerceInt(item.type);
  if (itemType === 2) {
    return downloadImageAttachment({ item, runtime, cdnBaseUrl });
  }
  if (itemType === 3) {
    return downloadVoiceAttachment({ item, runtime, cdnBaseUrl });
  }
  if (itemType === 4) {
    return downloadFileAttachment({ item, runtime, cdnBaseUrl });
  }
  if (itemType === 5) {
    return downloadVideoAttachment({ item, runtime, cdnBaseUrl });
  }
  return null;
}

async function downloadImageAttachment({ item, runtime, cdnBaseUrl }) {
  const imageItem = isObject(item.image_item) ? item.image_item : {};
  const media = isObject(imageItem.media) ? imageItem.media : {};
  const encryptQueryParam = asNullableText(media.encrypt_query_param);
  if (!encryptQueryParam) {
    return null;
  }
  const aesKeyBase64 = resolveInboundAesKey({
    rawHexKey: asNullableText(imageItem.aeskey),
    encodedKey: asNullableText(media.aes_key)
  });
  const buffer = aesKeyBase64
    ? await downloadAndDecryptBuffer({ encryptedQueryParam: encryptQueryParam, aesKeyBase64, cdnBaseUrl })
    : await downloadPlainBuffer({ encryptedQueryParam: encryptQueryParam, cdnBaseUrl });
  const saved = await saveBufferToMediaDir({
    buffer,
    mediaDir: resolveRuntimeMediaDir(runtime),
    bucket: "inbound",
    preferredName: "image.jpg",
    contentType: "image/jpeg"
  });
  return {
    kind: "image",
    file_name: path.basename(saved.path),
    content_type: "image/jpeg",
    source_path: saved.path,
    size_bytes: saved.sizeBytes,
    metadata: {
      weixin_item_type: "image",
      mid_size: coerceInt(imageItem.mid_size)
    }
  };
}

async function downloadVoiceAttachment({ item, runtime, cdnBaseUrl }) {
  const voiceItem = isObject(item.voice_item) ? item.voice_item : {};
  const media = isObject(voiceItem.media) ? voiceItem.media : {};
  const encryptQueryParam = asNullableText(media.encrypt_query_param);
  const aesKeyBase64 = asNullableText(media.aes_key);
  if (!encryptQueryParam || !aesKeyBase64) {
    return null;
  }
  const buffer = await downloadAndDecryptBuffer({ encryptedQueryParam: encryptQueryParam, aesKeyBase64, cdnBaseUrl });
  const saved = await saveBufferToMediaDir({
    buffer,
    mediaDir: resolveRuntimeMediaDir(runtime),
    bucket: "inbound",
    preferredName: "voice.sil",
    contentType: "audio/silk"
  });
  return {
    kind: "audio",
    file_name: path.basename(saved.path),
    content_type: "audio/silk",
    source_path: saved.path,
    size_bytes: saved.sizeBytes,
    metadata: {
      weixin_item_type: "voice",
      duration_ms: coerceInt(voiceItem.playtime),
      transcript: asNullableText(voiceItem.text)
    }
  };
}

async function downloadFileAttachment({ item, runtime, cdnBaseUrl }) {
  const fileItem = isObject(item.file_item) ? item.file_item : {};
  const media = isObject(fileItem.media) ? fileItem.media : {};
  const encryptQueryParam = asNullableText(media.encrypt_query_param);
  const aesKeyBase64 = asNullableText(media.aes_key);
  if (!encryptQueryParam || !aesKeyBase64) {
    return null;
  }
  const preferredName = asText(fileItem.file_name, "file.bin");
  const contentType = inferMimeFromFileName(preferredName);
  const buffer = await downloadAndDecryptBuffer({ encryptedQueryParam: encryptQueryParam, aesKeyBase64, cdnBaseUrl });
  const saved = await saveBufferToMediaDir({
    buffer,
    mediaDir: resolveRuntimeMediaDir(runtime),
    bucket: "inbound",
    preferredName,
    contentType
  });
  return {
    kind: "file",
    file_name: path.basename(saved.path),
    content_type: contentType,
    source_path: saved.path,
    size_bytes: saved.sizeBytes,
    metadata: {
      weixin_item_type: "file",
      original_file_name: preferredName
    }
  };
}

async function downloadVideoAttachment({ item, runtime, cdnBaseUrl }) {
  const videoItem = isObject(item.video_item) ? item.video_item : {};
  const media = isObject(videoItem.media) ? videoItem.media : {};
  const encryptQueryParam = asNullableText(media.encrypt_query_param);
  const aesKeyBase64 = asNullableText(media.aes_key);
  if (!encryptQueryParam || !aesKeyBase64) {
    return null;
  }
  const buffer = await downloadAndDecryptBuffer({ encryptedQueryParam: encryptQueryParam, aesKeyBase64, cdnBaseUrl });
  const saved = await saveBufferToMediaDir({
    buffer,
    mediaDir: resolveRuntimeMediaDir(runtime),
    bucket: "inbound",
    preferredName: "video.mp4",
    contentType: "video/mp4"
  });
  return {
    kind: "video",
    file_name: path.basename(saved.path),
    content_type: "video/mp4",
    source_path: saved.path,
    size_bytes: saved.sizeBytes,
    metadata: {
      weixin_item_type: "video",
      duration_ms: coerceInt(videoItem.play_length)
    }
  };
}

async function sendTextOnlyMessage({ transport, toUserId, contextToken, text }) {
  const providerMessageRef = randomUUID();
  try {
    await sendWeixinMessage({
      apiBaseUrl: transport.apiBaseUrl,
      token: transport.token,
      message: {
        from_user_id: "",
        to_user_id: toUserId,
        client_id: providerMessageRef,
        message_type: 2,
        message_state: 2,
        context_token: contextToken,
        item_list: [
          {
            type: 1,
            text_item: {
              text
            }
          }
        ]
      }
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      fail("transport_unavailable", `weixin send timed out after ${DEFAULT_API_TIMEOUT_MS}ms`);
    }
    fail("transport_unavailable", error instanceof Error ? error.message : String(error));
  }
  return providerMessageRef;
}

async function sendAttachmentMessage({ transport, runtime, toUserId, contextToken, attachment }) {
  const preparedAttachment = await loadAttachmentSource(attachment, runtime);
  const uploadMediaType = resolveUploadMediaType(preparedAttachment);
  const rawFileMd5 = createHash("md5").update(preparedAttachment.buffer).digest("hex");
  const encryptedSize = aesEcbPaddedSize(preparedAttachment.buffer.length);
  const fileKey = randomBytes(16).toString("hex");
  const aesKey = randomBytes(16);

  let uploadUrlResponse;
  try {
    uploadUrlResponse = await fetchUploadUrl({
      apiBaseUrl: transport.apiBaseUrl,
      token: transport.token,
      fileKey,
      mediaType: uploadMediaType,
      toUserId,
      rawSize: preparedAttachment.buffer.length,
      rawFileMd5,
      encryptedSize,
      aesKeyHex: aesKey.toString("hex")
    });
  } catch (error) {
    fail("media_upload_failed", error instanceof Error ? error.message : String(error), "delivery.attachments");
  }

  const uploadParam = asNullableText(uploadUrlResponse?.upload_param);
  if (!uploadParam) {
    fail("media_upload_failed", "weixin getuploadurl returned no upload_param", "delivery.attachments");
  }

  let uploaded;
  try {
    uploaded = await uploadBufferToCdn({
      buffer: preparedAttachment.buffer,
      uploadParam,
      fileKey,
      cdnBaseUrl: transport.cdnBaseUrl,
      aesKey
    });
  } catch (error) {
    fail("media_upload_failed", error instanceof Error ? error.message : String(error), "delivery.attachments");
  }

  const providerMessageRef = randomUUID();
  try {
    await sendWeixinMessage({
      apiBaseUrl: transport.apiBaseUrl,
      token: transport.token,
      message: {
        from_user_id: "",
        to_user_id: toUserId,
        client_id: providerMessageRef,
        message_type: 2,
        message_state: 2,
        context_token: contextToken,
        item_list: [
          buildOutboundMessageItem({
            preparedAttachment,
            downloadParam: uploaded.downloadParam,
            aesKey
          })
        ]
      }
    });
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      fail("transport_unavailable", `weixin send timed out after ${DEFAULT_API_TIMEOUT_MS}ms`);
    }
    fail("transport_unavailable", error instanceof Error ? error.message : String(error));
  }
  return providerMessageRef;
}

async function loadAttachmentSource(attachment, runtime) {
  if (!isObject(attachment)) {
    fail("invalid_delivery", "delivery attachment item is invalid", "delivery.attachments");
  }
  const kind = asNullableText(attachment.kind);
  if (!kind || !["image", "video", "audio", "file"].includes(kind)) {
    fail("invalid_delivery", "unsupported delivery attachment kind", "delivery.attachments.kind");
  }

  const sourcePath = asNullableText(attachment.source_path);
  const sourceUrl = asNullableText(attachment.source_url);
  if (!sourcePath && !sourceUrl) {
    fail("invalid_delivery", "delivery attachment requires source_path or source_url", "delivery.attachments");
  }

  if (sourcePath) {
    try {
      const buffer = await fs.readFile(sourcePath);
      const preferredName = sanitizeFileName(
        asNullableText(attachment.file_name) || path.basename(sourcePath),
        inferExtension({ fileName: sourcePath, contentType: asNullableText(attachment.content_type) })
      );
      return {
        kind,
        buffer,
        fileName: preferredName,
        contentType: asNullableText(attachment.content_type) || inferMimeFromFileName(preferredName)
      };
    } catch (error) {
      fail(
        "media_upload_failed",
        `failed to read attachment source_path: ${error instanceof Error ? error.message : String(error)}`,
        "delivery.attachments.source_path"
      );
    }
  }

  let response;
  try {
    response = await fetch(sourceUrl);
  } catch (error) {
    fail(
      "media_upload_failed",
      `failed to fetch attachment source_url: ${error instanceof Error ? error.message : String(error)}`,
      "delivery.attachments.source_url"
    );
  }
  if (!response.ok) {
    fail(
      "media_upload_failed",
      `attachment source_url returned ${response.status} ${response.statusText}`,
      "delivery.attachments.source_url"
    );
  }
  const responseContentType = asNullableText(response.headers.get("content-type"));
  const preferredName = sanitizeFileName(
    asNullableText(attachment.file_name),
    inferExtension({ fileName: sourceUrl, contentType: responseContentType || asNullableText(attachment.content_type) })
  );
  const buffer = Buffer.from(await response.arrayBuffer());
  await saveBufferToMediaDir({
    buffer,
    mediaDir: resolveRuntimeMediaDir(runtime),
    bucket: "outbound-cache",
    preferredName,
    contentType: responseContentType || asNullableText(attachment.content_type)
  });
  return {
    kind,
    buffer,
    fileName: preferredName,
    contentType: asNullableText(attachment.content_type) || responseContentType || inferMimeFromFileName(preferredName)
  };
}

function resolveUploadMediaType(preparedAttachment) {
  if (preparedAttachment.kind === "image") {
    return UploadMediaType.IMAGE;
  }
  if (preparedAttachment.kind === "video") {
    return UploadMediaType.VIDEO;
  }
  return UploadMediaType.FILE;
}

function buildOutboundMessageItem({ preparedAttachment, downloadParam, aesKey }) {
  const commonMedia = {
    encrypt_query_param: downloadParam,
    aes_key: encodeAesKeyForMessage(aesKey),
    encrypt_type: 1
  };
  if (preparedAttachment.kind === "image") {
    return {
      type: 2,
      image_item: {
        media: commonMedia,
        mid_size: aesEcbPaddedSize(preparedAttachment.buffer.length)
      }
    };
  }
  if (preparedAttachment.kind === "video") {
    return {
      type: 5,
      video_item: {
        media: commonMedia,
        video_size: aesEcbPaddedSize(preparedAttachment.buffer.length)
      }
    };
  }
  return {
    type: 4,
    file_item: {
      media: commonMedia,
      file_name: preparedAttachment.fileName,
      len: String(preparedAttachment.buffer.length)
    }
  };
}

function resolveInboundAesKey({ rawHexKey, encodedKey }) {
  if (rawHexKey) {
    return Buffer.from(rawHexKey, "utf-8").toString("base64");
  }
  return encodedKey;
}

function resolveRuntimeMediaDir(runtime) {
  const mediaDir = asNullableText(runtime?.media_dir);
  return mediaDir || path.resolve(process.cwd(), "media");
}
