import { createCipheriv, createDecipheriv, randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import path from "node:path";

const EXTENSION_TO_MIME = {
  ".bin": "application/octet-stream",
  ".bmp": "image/bmp",
  ".csv": "text/csv",
  ".doc": "application/msword",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  ".gif": "image/gif",
  ".gz": "application/gzip",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".m4a": "audio/mp4",
  ".mkv": "video/x-matroska",
  ".mov": "video/quicktime",
  ".mp3": "audio/mpeg",
  ".mp4": "video/mp4",
  ".ogg": "audio/ogg",
  ".pdf": "application/pdf",
  ".png": "image/png",
  ".ppt": "application/vnd.ms-powerpoint",
  ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  ".sil": "audio/silk",
  ".txt": "text/plain",
  ".wav": "audio/wav",
  ".webm": "video/webm",
  ".webp": "image/webp",
  ".xls": "application/vnd.ms-excel",
  ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  ".zip": "application/zip"
};

const MIME_TO_EXTENSION = {
  "application/gzip": ".gz",
  "application/msword": ".doc",
  "application/octet-stream": ".bin",
  "application/pdf": ".pdf",
  "application/vnd.ms-excel": ".xls",
  "application/vnd.ms-powerpoint": ".ppt",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
  "application/zip": ".zip",
  "audio/mp4": ".m4a",
  "audio/mpeg": ".mp3",
  "audio/ogg": ".ogg",
  "audio/silk": ".sil",
  "audio/wav": ".wav",
  "image/bmp": ".bmp",
  "image/gif": ".gif",
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/webp": ".webp",
  "text/csv": ".csv",
  "text/plain": ".txt",
  "video/mp4": ".mp4",
  "video/quicktime": ".mov",
  "video/webm": ".webm",
  "video/x-matroska": ".mkv"
};

export function aesEcbPaddedSize(plainSize) {
  return Math.ceil((plainSize + 1) / 16) * 16;
}

export async function uploadBufferToCdn({ buffer, uploadParam, fileKey, cdnBaseUrl, aesKey }) {
  const ciphertext = encryptAesEcb(buffer, aesKey);
  const response = await fetch(buildCdnUploadUrl({ cdnBaseUrl, uploadParam, fileKey }), {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: new Uint8Array(ciphertext)
  });
  if (!response.ok) {
    const errorBody = await safeReadText(response);
    throw new Error(`cdn upload failed: ${response.status} ${response.statusText} ${errorBody}`);
  }
  const downloadParam = response.headers.get("x-encrypted-param");
  if (!downloadParam) {
    throw new Error("cdn upload response missing x-encrypted-param");
  }
  return {
    downloadParam
  };
}

export async function downloadAndDecryptBuffer({ encryptedQueryParam, aesKeyBase64, cdnBaseUrl }) {
  const encrypted = await downloadPlainBuffer({ encryptedQueryParam, cdnBaseUrl });
  return decryptAesEcb(encrypted, decodeAesKey(aesKeyBase64));
}

export async function downloadPlainBuffer({ encryptedQueryParam, cdnBaseUrl }) {
  const response = await fetch(buildCdnDownloadUrl({ cdnBaseUrl, encryptedQueryParam }));
  if (!response.ok) {
    const errorBody = await safeReadText(response);
    throw new Error(`cdn download failed: ${response.status} ${response.statusText} ${errorBody}`);
  }
  return Buffer.from(await response.arrayBuffer());
}

export async function saveBufferToMediaDir({
  buffer,
  mediaDir,
  bucket,
  preferredName,
  contentType
}) {
  const extension = inferExtension({
    fileName: preferredName,
    contentType
  });
  const safeName = sanitizeFileName(preferredName, extension);
  const targetDir = path.join(mediaDir, bucket);
  await fs.mkdir(targetDir, { recursive: true });
  const targetPath = path.join(targetDir, `${randomUUID()}-${safeName}`);
  await fs.writeFile(targetPath, buffer);
  return {
    path: targetPath,
    sizeBytes: buffer.length
  };
}

export function inferMimeFromFileName(fileName) {
  const extension = path.extname(fileName || "").toLowerCase();
  return EXTENSION_TO_MIME[extension] || "application/octet-stream";
}

export function inferExtension({ fileName, contentType }) {
  const fileExtension = path.extname(fileName || "").toLowerCase();
  if (EXTENSION_TO_MIME[fileExtension]) {
    return fileExtension;
  }
  const normalizedContentType = typeof contentType === "string" ? contentType.split(";")[0].trim().toLowerCase() : "";
  return MIME_TO_EXTENSION[normalizedContentType] || ".bin";
}

export function sanitizeFileName(fileName, fallbackExtension = ".bin") {
  const raw = typeof fileName === "string" && fileName.trim() ? fileName.trim() : `media${fallbackExtension}`;
  const baseName = path.basename(raw).replace(/[<>:"/\\|?*\u0000-\u001f]+/g, "-");
  if (path.extname(baseName)) {
    return baseName;
  }
  return `${baseName}${fallbackExtension}`;
}

export function encodeAesKeyForMessage(aesKey) {
  return Buffer.from(aesKey.toString("hex"), "utf-8").toString("base64");
}

function buildCdnUploadUrl({ cdnBaseUrl, uploadParam, fileKey }) {
  return `${stripTrailingSlash(cdnBaseUrl)}/upload?encrypted_query_param=${encodeURIComponent(uploadParam)}&filekey=${encodeURIComponent(fileKey)}`;
}

function buildCdnDownloadUrl({ cdnBaseUrl, encryptedQueryParam }) {
  return `${stripTrailingSlash(cdnBaseUrl)}/download?encrypted_query_param=${encodeURIComponent(encryptedQueryParam)}`;
}

function stripTrailingSlash(value) {
  return value.endsWith("/") ? value.slice(0, -1) : value;
}

function encryptAesEcb(plainBuffer, aesKey) {
  const cipher = createCipheriv("aes-128-ecb", aesKey, null);
  return Buffer.concat([cipher.update(plainBuffer), cipher.final()]);
}

function decryptAesEcb(encryptedBuffer, aesKey) {
  const decipher = createDecipheriv("aes-128-ecb", aesKey, null);
  return Buffer.concat([decipher.update(encryptedBuffer), decipher.final()]);
}

function decodeAesKey(aesKeyBase64) {
  const decoded = Buffer.from(aesKeyBase64, "base64");
  if (decoded.length === 16) {
    return decoded;
  }
  if (decoded.length === 32 && /^[0-9a-fA-F]{32}$/.test(decoded.toString("ascii"))) {
    return Buffer.from(decoded.toString("ascii"), "hex");
  }
  throw new Error(`invalid aes_key payload length: ${decoded.length}`);
}

async function safeReadText(response) {
  try {
    return await response.text();
  } catch {
    return "";
  }
}
