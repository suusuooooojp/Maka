/**
 * linejs_bridge.mjs — Local @evex-dev/linejs v3.6.4 Engine Daemon for School Hub
 *
 * Runs on 127.0.0.1 and exposes async JSON / binary endpoints consumed by line_proxy.py.
 * Supports:
 * - Secure QR Login v3 (createQrCodeForSecure + qrCodeLoginV2ForSecure + E2EE keychain)
 * - Email/Password Login v2 (RSA + /LF1 E2EE PIN confirmation)
 * - Stateless Cloud Run Session Vault export/import (full MemoryStorage + E2EE historical keys)
 * - Talk list & history (getMessageBoxes + getRecentMessagesV2 + 64-bit BigInt getPreviousMessagesV2WithRequest + /SYNC4)
 * - Letter Sealing E2EE v1/v2 decryption & encryption (1:1 & Group, text & media)
 * - RelationService (/RE4) friend list, ID search & friend add
 */

import http from "node:http";
import crypto from "node:crypto";
import { Buffer } from "node:buffer";
import { BaseClient, MemoryStorage } from "./linejs_vendor.mjs";

let SHARED_UNDICI_DISPATCHER = undefined;
try {
  const { Agent, EnvHttpProxyAgent } = await import("undici");
  const hasEnvProxy = Boolean(
    process.env.HTTPS_PROXY ||
      process.env.HTTP_PROXY ||
      process.env.https_proxy ||
      process.env.http_proxy
  );
  SHARED_UNDICI_DISPATCHER = hasEnvProxy
    ? new EnvHttpProxyAgent({
        connectTimeout: 20_000,
        headersTimeout: 190_000,
        bodyTimeout: 190_000,
      })
    : new Agent({
        connectTimeout: 20_000,
        headersTimeout: 190_000,
        bodyTimeout: 190_000,
      });
} catch {}

const SUPPORTED_DEVICES = new Set([
  "ANDROIDSECONDARY",
  "DESKTOPMAC",
  "DESKTOPWIN",
  "IOSIPAD",
]);
const DEFAULT_DEVICE = "ANDROIDSECONDARY";

const REACTION_MAP = {
  2: "NICE",
  3: "LOVE",
  4: "FUN",
  5: "AMAZING",
  6: "SAD",
  7: "OMG",
};

function normalizeDevice(dev) {
  const upper = String(dev || "").trim().toUpperCase();
  if (SUPPORTED_DEVICES.has(upper)) return upper;
  return DEFAULT_DEVICE;
}

const SHARED_FETCH_DISPATCHER = SHARED_UNDICI_DISPATCHER;

function createProxySafeFetch(abortSignal) {
  return async function customFetch(input, init) {
    let url;
    let method = "GET";
    const headersObj = {};
    let body;
    let reqSignal = init?.signal;

    if (input instanceof Request) {
      url = input.url;
      method = input.method || "GET";
      reqSignal = input.signal || reqSignal;
      input.headers.forEach((v, k) => {
        const lk = k.toLowerCase();
        if (lk !== "host" && lk !== "connection" && lk !== "content-length") {
          headersObj[k] = v;
        }
      });
      if (method !== "GET" && method !== "HEAD") {
        const ab = await input.arrayBuffer();
        if (ab.byteLength > 0) body = Buffer.from(ab);
      }
    } else {
      url = String(input);
      method = init?.method || "GET";
      const h = new Headers(init?.headers || {});
      h.forEach((v, k) => {
        const lk = k.toLowerCase();
        if (lk !== "host" && lk !== "connection" && lk !== "content-length") {
          headersObj[k] = v;
        }
      });
      body = init?.body;
    }

    let finalSignal = reqSignal;
    if (abortSignal) {
      finalSignal = reqSignal ? AbortSignal.any([reqSignal, abortSignal]) : abortSignal;
    }

    const fetchOpts = {
      method,
      headers: headersObj,
      body,
      signal: finalSignal,
    };
    if (SHARED_FETCH_DISPATCHER) {
      fetchOpts.dispatcher = SHARED_FETCH_DISPATCHER;
    }
    return fetch(url, fetchOpts);
  };
}

function readInt32BEChunk(chunk) {
  if (!chunk) return 0;
  const b = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
  if (b.length >= 4) return b.readInt32BE(0);
  let val = 0;
  for (let i = 0; i < b.length; i++) val = (val << 8) | b[i];
  return val;
}

function installE2EEEnhancements(base, storage) {
  if (!base?.e2ee || base.e2ee.__shEnhanced) return base;
  const e2ee = base.e2ee;
  e2ee.__shEnhanced = true;

  const origGetSelfByKeyId = e2ee.getE2EESelfKeyDataByKeyId.bind(e2ee);
  e2ee.getE2EESelfKeyDataByKeyId = async function (keyId) {
    const direct = await origGetSelfByKeyId(keyId);
    if (direct && direct.privKey) return direct;
    const all = storage.getAll();
    for (const [k, v] of Object.entries(all)) {
      if (!k.startsWith("e2eeKeys:") || typeof v !== "string") continue;
      try {
        const parsed = JSON.parse(v);
        if (parsed && String(parsed.keyId) === String(keyId) && parsed.privKey) {
          await storage.set("e2eeKeys:" + keyId, v);
          return parsed;
        }
      } catch {}
    }
    return direct;
  };

  const origUnwrapGroup = e2ee.unwrapE2EEGroupSharedKey.bind(e2ee);
  e2ee.unwrapE2EEGroupSharedKey = async function (mid, e2eeGroupSharedKey) {
    if (!e2eeGroupSharedKey) throw new Error("Missing e2eeGroupSharedKey");
    const receiverKeyId = e2eeGroupSharedKey.receiverKeyId;
    const existing = await e2ee.getE2EESelfKeyDataByKeyId(receiverKeyId);
    if (!existing && base.profile?.mid) {
      const fallbackSelf = await e2ee.getE2EESelfKeyData(base.profile.mid);
      if (fallbackSelf?.privKey && receiverKeyId !== undefined) {
        await storage.set("e2eeKeys:" + receiverKeyId, JSON.stringify(fallbackSelf));
      }
    }
    return await origUnwrapGroup(mid, e2eeGroupSharedKey);
  };

  const origGetLocalPubKey = e2ee.getE2EELocalPublicKey.bind(e2ee);
  e2ee.getE2EELocalPublicKey = async function (mid, keyId) {
    try {
      return await origGetLocalPubKey(mid, keyId);
    } catch (err) {
      // 1:1 historical peer public key fallback via TalkService.getE2EEPublicKey
      if (typeof mid === "string" && mid.startsWith("u") && keyId !== undefined) {
        for (const kv of [1, 2]) {
          try {
            const res = await base.talk.getE2EEPublicKey({
              mid,
              keyVersion: kv,
              keyId: Number(keyId),
            });
            const rawKey = res?.keyData || res?.publicKey?.keyData;
            if (rawKey) {
              const keyBuf = Buffer.from(rawKey);
              if (keyBuf.length > 0) {
                await storage.set(`e2eePublicKeys:${mid}:${keyId}`, keyBuf.toString("base64"));
                return keyBuf;
              }
            }
          } catch {}
        }
      }
      // Group historical shared key fallback (keyVersion 1)
      if (typeof mid === "string" && (mid.startsWith("c") || mid.startsWith("r"))) {
        try {
          let gKey = null;
          if (keyId !== undefined && Number.isFinite(Number(keyId))) {
            try {
              gKey = await base.talk.getE2EEGroupSharedKey({
                keyVersion: 1,
                chatMid: mid,
                groupKeyId: Number(keyId),
              });
            } catch {}
          }
          if (!gKey) {
            gKey = await base.talk.getLastE2EEGroupSharedKey({
              keyVersion: 1,
              chatMid: mid,
            });
          }
          if (gKey) {
            return await e2ee.unwrapE2EEGroupSharedKey(mid, gKey);
          }
        } catch {}
      }
      throw err;
    }
  };

  async function resolveGroupSenderPubKey(myMid, senderKeyId) {
    const byId = await e2ee.getE2EESelfKeyDataByKeyId(senderKeyId);
    if (byId?.pubKey) return Buffer.from(byId.pubKey, "base64");
    if (myMid && senderKeyId !== undefined) {
      try {
        const fetched = await e2ee.getE2EELocalPublicKey(myMid, senderKeyId);
        if (Buffer.isBuffer(fetched) && fetched.length > 0) return fetched;
      } catch {}
    }
    const selfKey = await e2ee.getE2EESelfKeyData(myMid);
    return Buffer.from(selfKey.pubKey, "base64");
  }

  const origDecText = e2ee.decryptE2EETextMessage.bind(e2ee);
  e2ee.decryptE2EETextMessage = async function (messageObj, isSelf = false) {
    const myMid = base.profile?.mid || "";
    const _from = messageObj.from;
    const to = messageObj.to;
    const toType = messageObj.toType;
    const isGroupLike = toType !== 0 && toType !== "USER" && !(typeof to === "string" && to.startsWith("u"));
    if (isGroupLike && _from === myMid && Array.isArray(messageObj.chunks) && messageObj.chunks.length >= 5) {
      const chunks = messageObj.chunks.map((c) => (typeof c === "string" ? Buffer.from(c, "utf-8") : Buffer.from(c)));
      const senderKeyId = readInt32BEChunk(chunks[3]);
      const receiverKeyId = readInt32BEChunk(chunks[4]);
      const specVersion = String(messageObj.contentMetadata?.e2eeVersion || "2");
      const groupK = await e2ee.getE2EELocalPublicKey(to, receiverKeyId);
      const privK = Buffer.from(groupK.privKey, "base64");
      const pubK = await resolveGroupSenderPubKey(myMid, senderKeyId);
      const decrypted =
        specVersion === "2"
          ? e2ee.decryptE2EEMessageV2(to, _from, chunks, privK, pubK, parseInt(specVersion, 10), 0)
          : e2ee.decryptE2EEMessageV1(chunks, privK, pubK);
      const text = decrypted?.text || "";
      const meta = {};
      for (const [k, val] of Object.entries(decrypted || {})) {
        if (k === "text") continue;
        meta[k] = typeof val === "string" ? val : JSON.stringify(val);
      }
      return [text, meta];
    }
    return await origDecText({ ...messageObj, contentType: 0 }, isSelf);
  };

  const origDecData = e2ee.decryptE2EEDataMessage.bind(e2ee);
  e2ee.decryptE2EEDataMessage = async function (messageObj, isSelf = false) {
    const myMid = base.profile?.mid || "";
    const _from = messageObj.from;
    const to = messageObj.to;
    const toType = messageObj.toType;
    const isGroupLike = toType !== 0 && toType !== "USER" && !(typeof to === "string" && to.startsWith("u"));
    if (isGroupLike && _from === myMid && Array.isArray(messageObj.chunks) && messageObj.chunks.length >= 5) {
      const chunks = messageObj.chunks.map((c) => (typeof c === "string" ? Buffer.from(c, "utf-8") : Buffer.from(c)));
      const senderKeyId = readInt32BEChunk(chunks[3]);
      const receiverKeyId = readInt32BEChunk(chunks[4]);
      const specVersion = String(messageObj.contentMetadata?.e2eeVersion || "2");
      const ctCode = typeof messageObj.contentType === "number"
        ? messageObj.contentType
        : normalizeContentTypeCode(normalizeContentTypeName(messageObj.contentType));
      const groupK = await e2ee.getE2EELocalPublicKey(to, receiverKeyId);
      const privK = Buffer.from(groupK.privKey, "base64");
      const pubK = await resolveGroupSenderPubKey(myMid, senderKeyId);
      const decrypted =
        specVersion === "2"
          ? e2ee.decryptE2EEMessageV2(to, _from, chunks, privK, pubK, parseInt(specVersion, 10), ctCode)
          : e2ee.decryptE2EEMessageV1(chunks, privK, pubK);
      return decrypted || {};
    }
    return await origDecData(messageObj, isSelf);
  };

  return base;
}

function asRecord(val) {
  return val && typeof val === "object" ? val : {};
}

function toSafeNumber(val) {
  if (typeof val === "bigint") return Number(val);
  const n = Number(val);
  return Number.isFinite(n) ? n : 0;
}

function toSafeString(val) {
  if (val === null || val === undefined) return "";
  return String(val);
}

function serializeContact(raw) {
  const rec = asRecord(raw);
  const contactInner = asRecord(rec.contact);
  const detail = asRecord(rec.targetProfileDetail || contactInner.targetProfileDetail);
  const statusObj = detail.statusMessage && typeof detail.statusMessage === "object"
    ? detail.statusMessage
    : null;

  const mid = toSafeString(
    rec.targetUserMid || rec.mid || contactInner.mid || detail.mid
  ).trim();
  const displayName = toSafeString(
    detail.displayNameOverride ||
      detail.profileName ||
      rec.displayNameOverridden ||
      rec.displayName ||
      contactInner.displayNameOverridden ||
      contactInner.displayName ||
      mid.slice(0, 10)
  );
  const statusMessage = toSafeString(
    (statusObj ? statusObj.value : detail.statusMessage) ??
      rec.statusMessage ??
      contactInner.statusMessage ??
      ""
  );
  let picturePath = toSafeString(
    detail.picturePath ||
      rec.picturePath ||
      contactInner.picturePath ||
      detail.pictureStatus ||
      rec.pictureStatus ||
      ""
  );
  if (picturePath && !picturePath.startsWith("/") && !picturePath.startsWith("http")) {
    picturePath = "/" + picturePath;
  }
  return {
    mid,
    displayName: displayName || "LINE User",
    statusMessage,
    picturePath,
  };
}

function serializeProfile(raw) {
  const rec = asRecord(raw);
  const mid = toSafeString(rec.mid).trim();
  let picturePath = toSafeString(rec.picturePath || rec.pictureStatus || "");
  if (picturePath && !picturePath.startsWith("/") && !picturePath.startsWith("http")) {
    picturePath = "/" + picturePath;
  }
  return {
    mid,
    displayName: toSafeString(rec.displayName || "LINE User"),
    statusMessage: toSafeString(rec.statusMessage || ""),
    picturePath,
  };
}

function countSelfE2EEKeys(storageObj, myMid) {
  let count = 0;
  for (const [k, v] of Object.entries(storageObj || {})) {
    if (!k.startsWith("e2eeKeys:")) continue;
    const suffix = k.slice("e2eeKeys:".length);
    if (myMid && suffix === myMid) continue;
    if (v) count++;
  }
  if (count === 0 && myMid && storageObj?.[`e2eeKeys:${myMid}`]) {
    count = 1;
  }
  return count;
}

async function ensureSelfKeyIndexedByMid(base, storage, myMid) {
  if (!myMid) return;
  const existing = await storage.get(`e2eeKeys:${myMid}`);
  if (existing) return;
  const all = storage.getAll();
  let bestKeyId = -1;
  let bestVal = null;
  for (const [k, v] of Object.entries(all)) {
    if (!k.startsWith("e2eeKeys:")) continue;
    const idNum = Number(k.slice("e2eeKeys:".length));
    if (Number.isInteger(idNum) && idNum > bestKeyId && typeof v === "string") {
      try {
        const parsed = JSON.parse(v);
        if (parsed?.privKey && parsed?.pubKey) {
          bestKeyId = idNum;
          bestVal = v;
        }
      } catch {}
    }
  }
  if (bestVal) {
    await storage.set(`e2eeKeys:${myMid}`, bestVal);
  }
}

function buildInitialStorageMap(sessionData, certificate) {
  const map = {};
  if (sessionData && typeof sessionData === "object") {
    if (sessionData.storage && typeof sessionData.storage === "object") {
      for (const [k, v] of Object.entries(sessionData.storage)) {
        if (v !== undefined && v !== null) map[k] = v;
      }
    }
    if (sessionData.refreshToken && !map.refreshToken) {
      map.refreshToken = sessionData.refreshToken;
    }
    if (sessionData.certificate && !map.qrCert) {
      map.qrCert = sessionData.certificate;
    }
    // Migrate legacy okline E2EE vault if present
    const legacyE2ee = sessionData.e2ee;
    const myMid = sessionData.profile?.mid || "";
    if (legacyE2ee && typeof legacyE2ee === "object") {
      if (legacyE2ee.myKeyId && legacyE2ee.myPrivB64 && legacyE2ee.myPubB64) {
        const selfEntry = JSON.stringify({
          keyId: Number(legacyE2ee.myKeyId),
          privKey: legacyE2ee.myPrivB64,
          pubKey: legacyE2ee.myPubB64,
          e2eeVersion: 1,
        });
        map[`e2eeKeys:${legacyE2ee.myKeyId}`] = selfEntry;
        if (myMid) map[`e2eeKeys:${myMid}`] = selfEntry;
      }
      if (legacyE2ee.keyRing && typeof legacyE2ee.keyRing === "object") {
        for (const [kid, kp] of Object.entries(legacyE2ee.keyRing)) {
          if (kp?.privB64 && kp?.pubB64) {
            map[`e2eeKeys:${kid}`] = JSON.stringify({
              keyId: Number(kid),
              privKey: kp.privB64,
              pubKey: kp.pubB64,
              e2eeVersion: 1,
            });
          }
        }
      }
      if (legacyE2ee.peerPubKeys && typeof legacyE2ee.peerPubKeys === "object") {
        for (const [ck, pubB64] of Object.entries(legacyE2ee.peerPubKeys)) {
          if (typeof pubB64 === "string" && ck.includes(":")) {
            map[`e2eePublicKeys:${ck}`] = pubB64;
          }
        }
      }
    }
  }
  if (certificate && !map.qrCert) {
    map.qrCert = certificate;
  }
  return map;
}

// Active sessions and login flows
const sessions = new Map();
const qrLogins = new Map();
const emailLogins = new Map();

function exportSessionData(entry) {
  const storageAll = entry.storage.getAll();
  const myMid = entry.profile?.mid || entry.base.profile?.mid || "";
  const keyCount = countSelfE2EEKeys(storageAll, myMid);
  return {
    engine: "linejs",
    device: entry.device,
    accessToken: entry.base.authToken || "",
    refreshToken: storageAll.refreshToken || "",
    certificate: storageAll.qrCert || entry.certificate || "",
    profile: entry.profile || serializeProfile(entry.base.profile || {}),
    storage: storageAll,
    syncState: {
      revision: String(entry.syncState.revision || 0n),
      globalRev: String(entry.syncState.globalRev || 0n),
      individualRev: String(entry.syncState.individualRev || 0n),
    },
    e2ee: {
      ready: keyCount > 0,
      keyCount,
    },
  };
}

function createSessionEntry({ sessionId, base, storage, device, profile, syncState }) {
  const sid = sessionId || crypto.randomBytes(16).toString("hex");
  const entry = {
    sessionId: sid,
    base,
    storage,
    device,
    profile,
    contactsMap: new Map(),
    groupsMap: new Map(),
    squareChatsMap: new Map(),
    squareMembersMap: new Map(),
    opMessagesByChat: new Map(),
    decryptedMsgCache: new Map(),
    syncState: {
      revision: BigInt(syncState?.revision || 0),
      globalRev: BigInt(syncState?.globalRev || 0),
      individualRev: BigInt(syncState?.individualRev || 0),
    },
    updatedAt: Date.now(),
    lock: Promise.resolve(),
  };
  base.on("update:authtoken", (nextToken) => {
    if (nextToken) base.authToken = nextToken;
  });
  sessions.set(sid, entry);
  return entry;
}

async function withSessionLock(entry, fn) {
  const prev = entry.lock;
  let release;
  entry.lock = new Promise((r) => {
    release = r;
  });
  await prev.catch(() => {});
  try {
    entry.updatedAt = Date.now();
    return await fn();
  } finally {
    release();
  }
}

async function getOrRestoreSession({ sessionId, sessionData, accessToken, device }) {
  if (sessionId && sessions.has(sessionId)) {
    const existing = sessions.get(sessionId);
    if (existing.base.authToken) {
      return existing;
    }
  }

  const token = toSafeString(
    accessToken || sessionData?.accessToken || ""
  ).trim();
  if (!token) {
    const err = new Error("LINEのセッションが期限切れです。再ログインしてください。");
    err.statusCode = 401;
    throw err;
  }

  const candidateDevices = [];
  const requestedDevice = device || sessionData?.device;
  if (requestedDevice) {
    candidateDevices.push(normalizeDevice(requestedDevice));
  }
  for (const d of ["ANDROIDSECONDARY", "DESKTOPMAC", "DESKTOPWIN", "IOSIPAD"]) {
    if (!candidateDevices.includes(d)) candidateDevices.push(d);
  }

  let lastErr = null;
  for (const dev of candidateDevices) {
    try {
      const storage = new MemoryStorage(buildInitialStorageMap(sessionData));
      const base = installE2EEEnhancements(
        new BaseClient({
          device: dev,
          storage,
          fetch: createProxySafeFetch(),
        }),
        storage
      );
      await base.loginProcess.login({
        authToken: token,
        skipCheck: false,
      });
      const profile = serializeProfile(base.profile);
      await ensureSelfKeyIndexedByMid(base, storage, profile.mid);
      return createSessionEntry({
        sessionId,
        base,
        storage,
        device: dev,
        profile,
        syncState: sessionData?.syncState,
      });
    } catch (err) {
      lastErr = err;
      // If device was explicitly stored by linejs, only retry others on device mismatch
      if (sessionData?.engine === "linejs" && sessionData?.device === dev) {
        break;
      }
    }
  }

  const err = new Error(
    `セッション復元に失敗しました: ${lastErr?.message || String(lastErr)}`
  );
  err.statusCode = 401;
  throw err;
}

const CONTENT_TYPE_NAMES = {
  0: "NONE",
  1: "IMAGE",
  2: "VIDEO",
  3: "AUDIO",
  6: "CALL",
  7: "STICKER",
  13: "CONTACT",
  14: "FILE",
  15: "LOCATION",
  22: "FLEX",
};

function normalizeContentTypeName(ct) {
  if (typeof ct === "string") {
    const u = ct.toUpperCase();
    if (/^\d+$/.test(u)) return CONTENT_TYPE_NAMES[Number(u)] || "NONE";
    return u;
  }
  if (typeof ct === "number") {
    return CONTENT_TYPE_NAMES[ct] || "NONE";
  }
  return "NONE";
}

function normalizeToTypeName(tt, toMid) {
  if (typeof tt === "string") {
    if (tt === "0" || tt === "USER") return "USER";
    if (tt === "1" || tt === "ROOM") return "ROOM";
    if (tt === "2" || tt === "GROUP") return "GROUP";
    return tt;
  }
  if (tt === 1) return "ROOM";
  if (tt === 2) return "GROUP";
  if (toMid && (toMid.startsWith("c") || toMid.startsWith("r"))) return "GROUP";
  return "USER";
}

const CONTENT_TYPE_CODES = {
  NONE: 0,
  IMAGE: 1,
  VIDEO: 2,
  AUDIO: 3,
  CALL: 6,
  STICKER: 7,
  CONTACT: 13,
  FILE: 14,
  LOCATION: 15,
  FLEX: 22,
};

function normalizeContentTypeCode(ctName) {
  return CONTENT_TYPE_CODES[ctName] ?? 0;
}

function normalizeEpochMs(val) {
  const n = toSafeNumber(val);
  if (n > 0 && n < 10_000_000_000) return n * 1000;
  return n;
}

async function decryptAndSerializeMessage(entry, rawMsg) {
  if (!rawMsg) return null;
  const raw = asRecord(rawMsg);
  const msgId = toSafeString(raw.id);
  const myMid = entry.profile?.mid || "";

  if (msgId && entry.decryptedMsgCache.has(msgId)) {
    const cached = entry.decryptedMsgCache.get(msgId);
    const senderInfo = entry.contactsMap.get(cached.fromMid);
    if (senderInfo && !cached.isMe) {
      cached.senderName = senderInfo.displayName;
      cached.senderPicture = senderInfo.picturePath;
    }
    if (Array.isArray(raw.reactions)) {
      cached.reactions = raw.reactions.map((r) => ({
        actorMid: toSafeString(r.userMid || r.actorMid || ""),
        from: toSafeString(r.userMid || r.actorMid || ""),
        reactionType: toSafeString(r.reaction?.predefinedReactionType ?? r.reactionType ?? ""),
        type: toSafeString(r.reaction?.predefinedReactionType ?? r.reactionType ?? ""),
      }));
    }
    return cached;
  }

  const fromMid = toSafeString(raw.from || raw._from || "");
  const toMid = toSafeString(raw.to || "");
  const toType = normalizeToTypeName(raw.toType, toMid);
  const ctName = normalizeContentTypeName(raw.contentType);
  const ctCode = normalizeContentTypeCode(ctName);
  const meta = { ...(asRecord(raw.contentMetadata)) };
  const hasChunks = Array.isArray(raw.chunks) && raw.chunks.length > 0;
  const wasEncrypted = Boolean(hasChunks || meta.e2eeVersion);

  let text = typeof raw.text === "string" ? raw.text : "";
  let decryptedOk = !wasEncrypted;

  if (wasEncrypted && hasChunks) {
    const msgClone = {
      ...raw,
      from: fromMid,
      to: toMid,
      toType,
      contentType: ctName,
      contentMetadata: { ...meta },
      chunks: raw.chunks.map((c) =>
        Buffer.isBuffer(c)
          ? c
          : c instanceof Uint8Array
            ? Buffer.from(c)
            : typeof c === "string"
              ? Buffer.from(c, "base64")
              : Buffer.from(c)
      ),
    };

    try {
      if (ctName === "NONE" || ctName === "LOCATION") {
        const dec = await entry.base.e2ee.decryptE2EEMessage(msgClone);
        if (typeof dec.text === "string") text = dec.text;
        if (dec.contentMetadata) Object.assign(meta, dec.contentMetadata);
        if (dec.location && !text) {
          text = `📍 ${dec.location.title || dec.location.address || "位置情報"}`;
        }
        decryptedOk = true;
      } else if (["IMAGE", "VIDEO", "AUDIO", "FILE"].includes(ctName)) {
        const decData = await entry.base.e2ee.decryptE2EEDataMessage(msgClone);
        if (decData?.keyMaterial) {
          meta.ENC_KM = toSafeString(decData.keyMaterial);
        }
        if (decData?.fileName && !meta.FILE_NAME) {
          meta.FILE_NAME = toSafeString(decData.fileName);
        }
        decryptedOk = true;
      }
    } catch {
      decryptedOk = false;
      if (!text && ctName === "NONE") {
        text = "🔒 [暗号化メッセージ (この端末に過去の鍵がありません)]";
      }
    }
  }

  let stickerUrl = "";
  if (ctName === "STICKER") {
    const stkId = meta.STKID || "";
    const stkOpt = meta.STKOPT || "";
    if (stkId) {
      stickerUrl =
        stkOpt === "A"
          ? `https://stickershop.line-scdn.net/stickershop/v1/sticker/${stkId}/android/sticker_animation.png`
          : `https://stickershop.line-scdn.net/stickershop/v1/sticker/${stkId}/android/sticker.png`;
    }
    if (!text) text = "[スタンプ]";
  } else if (ctName === "IMAGE") {
    if (!text) text = "[画像]";
  } else if (ctName === "VIDEO") {
    if (!text) text = "[動画]";
  } else if (ctName === "AUDIO") {
    if (!text) text = "[音声メッセージ]";
  } else if (ctName === "FILE") {
    const fn = meta.FILE_NAME || "ファイル";
    if (!text) text = `📎 ${fn}`;
  } else if (ctName === "CALL") {
    if (!text) text = "📞 通話";
  } else if (ctName === "CONTACT") {
    if (!text) text = `👤 連絡先: ${meta.displayName || ""}`;
  } else if (ctName === "FLEX") {
    if (!text) text = meta.ALT_TEXT || "[リッチメッセージ]";
  }

  const isMe = Boolean(myMid && fromMid === myMid);
  const sender = isMe
    ? entry.profile
    : entry.contactsMap.get(fromMid) || { displayName: fromMid.slice(0, 8), picturePath: "" };

  const createdTime = normalizeEpochMs(raw.createdTime || raw.deliveredTime || 0);
  const deliveredTime = normalizeEpochMs(raw.deliveredTime || raw.createdTime || 0) || createdTime;
  const reactions = Array.isArray(raw.reactions)
    ? raw.reactions.map((r) => ({
        actorMid: toSafeString(r.userMid || r.actorMid || ""),
        from: toSafeString(r.userMid || r.actorMid || ""),
        reactionType: toSafeString(r.reaction?.predefinedReactionType ?? r.reactionType ?? ""),
        type: toSafeString(r.reaction?.predefinedReactionType ?? r.reactionType ?? ""),
      }))
    : [];

  const serialized = {
    id: msgId,
    from: fromMid,
    fromMid,
    to: toMid,
    toMid,
    isMe,
    senderName: sender?.displayName || (isMe ? "自分" : "LINE User"),
    senderPicture: sender?.picturePath || "",
    text,
    contentType: ctCode,
    contentTypeName: ctName,
    hasMedia: [1, 2, 3, 14].includes(ctCode),
    stickerUrl,
    contentMetadata: meta,
    createdTime,
    deliveredTime,
    relatedMessageId: toSafeString(raw.relatedMessageId || ""),
    wasEncrypted,
    decryptedOk,
    reactions,
    rawForMedia: [1, 2, 3, 14].includes(ctCode) ? raw : undefined,
  };

  if (msgId && (!wasEncrypted || decryptedOk)) {
    if (entry.decryptedMsgCache.size > 2000) {
      const firstKey = entry.decryptedMsgCache.keys().next().value;
      entry.decryptedMsgCache.delete(firstKey);
    }
    entry.decryptedMsgCache.set(msgId, serialized);
  }
  return serialized;
}

function recordOpMessage(entry, rawMsg) {
  if (!rawMsg || !rawMsg.id) return;
  const myMid = entry.profile?.mid || "";
  const fromMid = toSafeString(rawMsg.from || rawMsg._from || "");
  const toMid = toSafeString(rawMsg.to || "");
  const toType = normalizeToTypeName(rawMsg.toType, toMid);
  const chatMid =
    toType === "GROUP" || toType === "ROOM"
      ? toMid
      : fromMid === myMid
        ? toMid
        : fromMid;
  if (!chatMid) return;
  let map = entry.opMessagesByChat.get(chatMid);
  if (!map) {
    map = new Map();
    entry.opMessagesByChat.set(chatMid, map);
  }
  map.set(toSafeString(rawMsg.id), rawMsg);
  if (map.size > 150) {
    const oldest = map.keys().next().value;
    map.delete(oldest);
  }
}

async function syncOperationsIncremental(entry) {
  try {
    const res = await entry.base.talk.sync({
      limit: 100,
      revision: entry.syncState.revision,
      globalRev: entry.syncState.globalRev,
      individualRev: entry.syncState.individualRev,
      timeout: 5000,
    });
    const ops = res?.operationResponse?.operations || [];
    for (const op of ops) {
      if (op.revision !== undefined) {
        const r = BigInt(op.revision);
        if (r > entry.syncState.revision) entry.syncState.revision = r;
      }
      const opType = op.type;
      if (
        (opType === "SEND_MESSAGE" ||
          opType === "RECEIVE_MESSAGE" ||
          opType === 25 ||
          opType === 26) &&
        op.message
      ) {
        recordOpMessage(entry, op.message);
      }
    }
    const ge = res?.operationResponse?.globalEvents;
    if (ge?.lastRevision !== undefined) {
      entry.syncState.globalRev = BigInt(ge.lastRevision);
    }
    const ie = res?.operationResponse?.individualEvents;
    if (ie?.lastRevision !== undefined) {
      entry.syncState.individualRev = BigInt(ie.lastRevision);
    }
  } catch {
    // Non-fatal: messageBox and getRecentMessagesV2 still work
  }
}

async function resolveContactsBatch(entry, mids) {
  const missing = [...new Set(mids)].filter(
    (m) => m && m.startsWith("u") && !entry.contactsMap.has(m)
  );
  if (!missing.length) return;

  const chunks = [];
  for (let i = 0; i < missing.length; i += 100) {
    chunks.push(missing.slice(i, i + 100));
  }

  for (const batch of chunks) {
    let resolved = false;
    try {
      const v3 = await entry.base.relation.getContactsV3({ mids: batch });
      for (const item of v3?.responses || []) {
        const c = serializeContact(item);
        if (c.mid) {
          entry.contactsMap.set(c.mid, c);
          resolved = true;
        }
      }
    } catch {}

    if (!resolved) {
      try {
        const v2 = await entry.base.talk.getContactsV2({ mids: batch });
        for (const [mid, item] of Object.entries(v2?.contacts || {})) {
          const c = serializeContact({ ...asRecord(item), targetUserMid: mid });
          if (c.mid) {
            entry.contactsMap.set(c.mid, c);
            resolved = true;
          }
        }
      } catch {}
    }

    if (!resolved) {
      try {
        const v1 = await entry.base.talk.getContacts({ mids: batch });
        for (const item of v1 || []) {
          const c = serializeContact(item);
          if (c.mid) entry.contactsMap.set(c.mid, c);
        }
      } catch {}
    }
  }
}

async function fetchFriendsList(entry) {
  let friendMids = [];
  try {
    const r = await entry.base.relation.getUserFriendIds({
      request: { blockStatus: "ALL" },
    });
    friendMids = r?.userFriendMids || [];
  } catch {
    try {
      friendMids = (await entry.base.talk.getAllContactIds({ syncReason: "AUTO_REPAIR" })) || [];
    } catch {}
  }
  await resolveContactsBatch(entry, friendMids);
  return friendMids
    .map((m) => entry.contactsMap.get(m))
    .filter(Boolean)
    .sort((a, b) => a.displayName.localeCompare(b.displayName, "ja"));
}

async function fetchGroupsList(entry) {
  let memberMids = [];
  let invitedMids = [];
  try {
    const res = await entry.base.talk.getAllChatMids({
      request: { withMemberChats: true, withInvitedChats: true },
      syncReason: "AUTO_REPAIR",
    });
    memberMids = res?.memberChatMids || [];
    invitedMids = res?.invitedChatMids || [];
  } catch {}

  const allChatMids = [...new Set([...memberMids, ...invitedMids])];
  const invitedSet = new Set(invitedMids);
  const groups = [];
  const invitedGroups = [];

  for (let i = 0; i < allChatMids.length; i += 50) {
    const batch = allChatMids.slice(i, i + 50);
    try {
      const res = await entry.base.talk.getChats({
        chatMids: batch,
        withMembers: true,
        withInvitees: true,
      });
      for (const ch of res?.chats || []) {
        const chatMid = toSafeString(ch.chatMid);
        if (!chatMid) continue;
        let picturePath = toSafeString(ch.picturePath || "");
        if (picturePath && !picturePath.startsWith("/") && !picturePath.startsWith("http")) {
          picturePath = "/" + picturePath;
        }
        const memberMap = ch.extra?.groupExtra?.memberMids || {};
        const memberCount = Object.keys(memberMap).length;
        const isInvited = invitedSet.has(chatMid);
        const gObj = {
          chatMid,
          chatName: toSafeString(ch.chatName || "グループ"),
          picturePath,
          memberCount,
          isInvited,
          createdTime: toSafeNumber(ch.createdTime || 0),
        };
        entry.groupsMap.set(chatMid, gObj);
        if (isInvited) invitedGroups.push(gObj);
        else groups.push(gObj);
      }
    } catch {}
  }

  return { groups, invitedGroups };
}

async function buildOverview(entry) {
  if (!entry.profile?.mid) {
    entry.profile = serializeProfile(await entry.base.talk.getProfile());
    await ensureSelfKeyIndexedByMid(entry.base, entry.storage, entry.profile.mid);
  }

  const [, contacts, { groups, invitedGroups }, boxResult, squareResult] = await Promise.all([
    syncOperationsIncremental(entry),
    fetchFriendsList(entry),
    fetchGroupsList(entry),
    entry.base.talk
      .getMessageBoxes({
        messageBoxListRequest: {
          activeOnly: false,
          unreadOnly: false,
          messageBoxCountLimit: 200,
          withUnreadCount: true,
          lastMessagesPerMessageBoxCount: 2,
        },
      })
      .catch(async () => {
        try {
          return await entry.base.talk.getMessageBoxes({ messageBoxListRequest: {} });
        } catch {
          return { messageBoxes: [] };
        }
      }),
    entry.base.square
      .getJoinedSquareChats({ request: { limit: 100 } })
      .catch(() => null),
  ]);

  const boxes = boxResult?.messageBoxes || [];
  const unknownUserMids = [];
  const unknownGroupMids = [];

  for (const b of boxes) {
    const mid = toSafeString(b.id);
    if (mid.startsWith("u") && mid !== entry.profile.mid && !entry.contactsMap.has(mid)) {
      unknownUserMids.push(mid);
    } else if ((mid.startsWith("c") || mid.startsWith("r")) && !entry.groupsMap.has(mid)) {
      unknownGroupMids.push(mid);
    }
  }
  for (const mid of entry.opMessagesByChat.keys()) {
    if (mid.startsWith("u") && mid !== entry.profile.mid && !entry.contactsMap.has(mid)) {
      unknownUserMids.push(mid);
    } else if ((mid.startsWith("c") || mid.startsWith("r")) && !entry.groupsMap.has(mid)) {
      unknownGroupMids.push(mid);
    }
  }

  if (unknownUserMids.length) {
    await resolveContactsBatch(entry, unknownUserMids);
  }
  if (unknownGroupMids.length) {
    try {
      const gRes = await entry.base.talk.getChats({
        chatMids: [...new Set(unknownGroupMids)].slice(0, 50),
        withMembers: true,
        withInvitees: false,
      });
      for (const ch of gRes?.chats || []) {
        const cmid = toSafeString(ch.chatMid);
        if (!cmid) continue;
        let pic = toSafeString(ch.picturePath || "");
        if (pic && !pic.startsWith("/") && !pic.startsWith("http")) pic = "/" + pic;
        entry.groupsMap.set(cmid, {
          chatMid: cmid,
          chatName: toSafeString(ch.chatName || "グループ"),
          picturePath: pic,
          memberCount: Object.keys(ch.extra?.groupExtra?.memberMids || {}).length,
          isInvited: false,
        });
      }
    } catch {}
  }

  const chatRoomsByMid = new Map();

  // 1. Populate from getMessageBoxes
  for (const b of boxes) {
    const chatMid = toSafeString(b.id);
    if (!chatMid) continue;
    const isGroup = chatMid.startsWith("c") || chatMid.startsWith("r");
    const gInfo = isGroup ? entry.groupsMap.get(chatMid) : null;
    const uInfo = !isGroup
      ? chatMid === entry.profile.mid
        ? { displayName: "Keepメモ (自分)", picturePath: entry.profile.picturePath }
        : entry.contactsMap.get(chatMid)
      : null;

    let rawLast = Array.isArray(b.lastMessages) && b.lastMessages.length > 0
      ? b.lastMessages[b.lastMessages.length - 1]
      : null;
    const opMap = entry.opMessagesByChat.get(chatMid);
    if (opMap && opMap.size > 0) {
      for (const m of opMap.values()) {
        if (!rawLast || toSafeNumber(m.createdTime) >= toSafeNumber(rawLast.createdTime)) {
          rawLast = m;
        }
      }
    }

    let lastMessage = null;
    if (rawLast) {
      const dec = await decryptAndSerializeMessage(entry, rawLast);
      if (dec) {
        lastMessage = {
          id: dec.id,
          text: dec.text,
          createdTime: dec.createdTime,
          from: dec.from,
        };
      }
    }

    const lastDeliveredTime = toSafeNumber(
      b.lastDeliveredMessageId?.deliveredTime || lastMessage?.createdTime || 0
    );

    chatRoomsByMid.set(chatMid, {
      chatMid,
      title: (isGroup ? gInfo?.chatName : uInfo?.displayName) || chatMid.slice(0, 10),
      picturePath: (isGroup ? gInfo?.picturePath : uInfo?.picturePath) || "",
      isGroup,
      memberCount: isGroup ? gInfo?.memberCount || 0 : 0,
      unreadCount: toSafeNumber(b.unreadCount || 0),
      lastMessage,
      lastTime: Math.max(lastDeliveredTime, lastMessage?.createdTime || 0),
      lastDeliveredMessageId: b.lastDeliveredMessageId
        ? {
            messageId: String(b.lastDeliveredMessageId.messageId),
            deliveredTime: String(b.lastDeliveredMessageId.deliveredTime || 0),
          }
        : null,
    });
  }

  // 2. Merge any active chats from opMessagesByChat or joined groups not in boxes
  for (const [chatMid, opMap] of entry.opMessagesByChat.entries()) {
    if (chatRoomsByMid.has(chatMid) || opMap.size === 0) continue;
    const isGroup = chatMid.startsWith("c") || chatMid.startsWith("r");
    const gInfo = isGroup ? entry.groupsMap.get(chatMid) : null;
    const uInfo = !isGroup ? entry.contactsMap.get(chatMid) : null;
    const latestRaw = [...opMap.values()].sort(
      (a, b) => toSafeNumber(a.createdTime) - toSafeNumber(b.createdTime)
    ).at(-1);
    const dec = latestRaw ? await decryptAndSerializeMessage(entry, latestRaw) : null;
    chatRoomsByMid.set(chatMid, {
      chatMid,
      title: (isGroup ? gInfo?.chatName : uInfo?.displayName) || chatMid.slice(0, 10),
      picturePath: (isGroup ? gInfo?.picturePath : uInfo?.picturePath) || "",
      isGroup,
      memberCount: isGroup ? gInfo?.memberCount || 0 : 0,
      unreadCount: 0,
      lastMessage: dec
        ? { id: dec.id, text: dec.text, createdTime: dec.createdTime, from: dec.from }
        : null,
      lastTime: dec?.createdTime || Date.now(),
    });
  }

  for (const g of groups) {
    if (!chatRoomsByMid.has(g.chatMid)) {
      chatRoomsByMid.set(g.chatMid, {
        chatMid: g.chatMid,
        title: g.chatName,
        picturePath: g.picturePath,
        isGroup: true,
        memberCount: g.memberCount,
        unreadCount: 0,
        lastMessage: null,
        lastTime: g.createdTime || 0,
      });
    }
  }

  // 3. Merge joined OpenChat (Square) rooms if any
  if (squareResult && Array.isArray(squareResult.chats)) {
    const statuses = asRecord(squareResult.chatStatuses);
    const sqMembers = asRecord(squareResult.squareMembers);
    for (const [smid, smem] of Object.entries(sqMembers)) {
      if (smem && typeof smem === "object") {
        entry.squareMembersMap.set(smid, smem);
      }
    }
    for (const sq of squareResult.chats) {
      const sqMid = toSafeString(sq?.squareChatMid);
      if (!sqMid) continue;
      entry.squareChatsMap.set(sqMid, sq);
      if (chatRoomsByMid.has(sqMid)) continue;
      const st = asRecord(statuses[sqMid]);
      const lastSqMsg = st.lastMessage?.message || null;
      const otherStatus = asRecord(st.otherStatus);
      let pic = toSafeString(sq.chatImageObsHash || "");
      if (pic && !pic.startsWith("/") && !pic.startsWith("http")) pic = "/" + pic;
      const lastCreated = normalizeEpochMs(lastSqMsg?.createdTime || lastSqMsg?.deliveredTime || 0);
      chatRoomsByMid.set(sqMid, {
        chatMid: sqMid,
        title: `💬 ${toSafeString(sq.name || "オープンチャット")}`,
        picturePath: pic,
        isGroup: true,
        isSquare: true,
        memberCount: toSafeNumber(sq.memberCount || otherStatus.memberCount || 0),
        unreadCount: toSafeNumber(otherStatus.unreadMessageCount || 0),
        lastMessage: lastSqMsg
          ? {
              id: toSafeString(lastSqMsg.id),
              text: toSafeString(lastSqMsg.text || "[メッセージ]"),
              createdTime: lastCreated,
              from: toSafeString(lastSqMsg.from || lastSqMsg._from || ""),
            }
          : null,
        lastTime: lastCreated,
      });
    }
  }

  const chats = [...chatRoomsByMid.values()].sort((a, b) => (b.lastTime || 0) - (a.lastTime || 0));
  const sdata = exportSessionData(entry);

  return {
    ok: true,
    sessionId: entry.sessionId,
    sessionData: sdata,
    profile: entry.profile,
    e2ee: sdata.e2ee,
    chats,
    contacts,
    groups,
    invitedGroups,
  };
}

async function fetchSquareChatMessages(entry, { chatMid, count = 50 }) {
  const messages = [];
  try {
    let evRes = await entry.base.square.fetchSquareChatEvents({
      squareChatMid: chatMid,
      limit: Math.min(Math.max(count, 20), 100),
      direction: "BACKWARD",
    }).catch(() => null);

    if (!evRes || !Array.isArray(evRes.events) || evRes.events.length === 0) {
      const head = await entry.base.square.fetchSquareChatEvents({
        squareChatMid: chatMid,
        limit: 50,
      }).catch(() => null);
      if (head?.events?.length) {
        evRes = head;
      } else if (head?.syncToken) {
        evRes = await entry.base.square.fetchSquareChatEvents({
          squareChatMid: chatMid,
          syncToken: head.syncToken,
          limit: Math.min(Math.max(count, 20), 100),
          direction: "BACKWARD",
        }).catch(() => head);
      }
    }

    const mySqMember = entry.squareMembersMap.get(chatMid);
    const mySqMid = toSafeString(mySqMember?.squareMemberMid || "");

    for (const ev of evRes?.events || []) {
      const sqMsg =
        ev?.payload?.receiveMessage?.squareMessage ||
        ev?.payload?.sendMessage?.squareMessage ||
        ev?.payload?.notifiedUpdateMessageStatus?.squareMessage ||
        null;
      const raw = sqMsg?.message;
      if (!raw || !raw.id) continue;
      const senderMid = toSafeString(raw.from || raw._from || "");
      const senderName = toSafeString(
        ev?.payload?.receiveMessage?.senderDisplayName ||
          sqMsg?.senderDisplayName ||
          (mySqMid && senderMid === mySqMid ? entry.profile?.displayName : "") ||
          senderMid.slice(0, 8)
      );
      const isMe = Boolean(
        ev?.type === "SEND_MESSAGE" || (mySqMid && senderMid === mySqMid)
      );
      const serialized = await decryptAndSerializeMessage(entry, raw);
      if (serialized) {
        serialized.isMe = isMe;
        if (senderName) serialized.senderName = senderName;
        const { rawForMedia: _omit, ...cleanMsg } = serialized;
        messages.push(cleanMsg);
      }
    }
  } catch {}

  messages.sort((a, b) => (a.createdTime || 0) - (b.createdTime || 0));
  return {
    ok: true,
    messages,
    hasMore: false,
    oldestMessageId: messages[0]?.id || "",
    oldestDeliveredTime: messages[0]?.deliveredTime || messages[0]?.createdTime || 0,
    sessionData: exportSessionData(entry),
  };
}

async function fetchChatMessages(entry, { chatMid, count = 50, beforeMessageId = "", beforeTime = 0 }) {
  if (chatMid && chatMid.startsWith("m")) {
    return await fetchSquareChatMessages(entry, { chatMid, count });
  }

  await syncOperationsIncremental(entry);

  const rawById = new Map();
  let fetchedOlderBatch = [];

  if (beforeMessageId && /^\d+$/.test(String(beforeMessageId))) {
    // Pagination cursor (older messages)
    try {
      const prev = await entry.base.talk.getPreviousMessagesV2WithRequest({
        request: {
          messageBoxId: chatMid,
          endMessageId: {
            messageId: BigInt(beforeMessageId),
            deliveredTime: BigInt(beforeTime || Date.now()),
          },
          messagesCount: count,
          withReadCount: true,
          receivedOnly: false,
        },
      });
      if (Array.isArray(prev)) {
        fetchedOlderBatch = prev;
        for (const m of prev) {
          if (m?.id && String(m.id) !== String(beforeMessageId)) {
            rawById.set(String(m.id), m);
          }
        }
      }
    } catch {}
  } else {
    // Initial / recent load: combine getRecentMessagesV2 + getMessageBoxes exact anchor + getPreviousMessagesV2WithRequest + sync ops
    let recentArr = [];
    try {
      const res = await entry.base.talk.getRecentMessagesV2({
        messageBoxId: chatMid,
        messagesCount: count,
      });
      if (Array.isArray(res)) recentArr = res;
    } catch {}

    for (const m of recentArr) {
      if (m?.id) rawById.set(String(m.id), m);
    }

    if (rawById.size < Math.min(count, 20)) {
      try {
        let boxes = await entry.base.talk
          .getMessageBoxes({
            messageBoxListRequest: {
              activeOnly: false,
              messageBoxCountLimit: 200,
              lastMessagesPerMessageBoxCount: 2,
            },
          })
          .catch(() => null);
        let box = (boxes?.messageBoxes || []).find((b) => b.id === chatMid);
        if (!box) {
          boxes = await entry.base.talk
            .getMessageBoxes({ messageBoxListRequest: {} })
            .catch(() => null);
          box = (boxes?.messageBoxes || []).find((b) => b.id === chatMid);
        }
        if (box) {
          for (const lm of box.lastMessages || []) {
            if (lm?.id) rawById.set(String(lm.id), lm);
          }
          const anchor = box.lastDeliveredMessageId;
          if (anchor?.messageId !== undefined) {
            const prev = await entry.base.talk.getPreviousMessagesV2WithRequest({
              request: {
                messageBoxId: chatMid,
                endMessageId: {
                  messageId: BigInt(anchor.messageId),
                  deliveredTime: BigInt(anchor.deliveredTime || Date.now()),
                },
                messagesCount: count,
                withReadCount: true,
                receivedOnly: false,
              },
            });
            if (Array.isArray(prev)) {
              for (const m of prev) {
                if (m?.id) rawById.set(String(m.id), m);
              }
            }
          }
        }
      } catch {}
    }

    const opMap = entry.opMessagesByChat.get(chatMid);
    if (opMap) {
      for (const [id, m] of opMap.entries()) {
        if (!rawById.has(id)) rawById.set(id, m);
      }
    }
  }

  const rawList = [...rawById.values()];

  // Ensure all senders in group chats have resolved names/avatars
  const senderMids = rawList
    .map((m) => toSafeString(m.from || m._from || ""))
    .filter((m) => m.startsWith("u") && m !== entry.profile?.mid && !entry.contactsMap.has(m));
  if (senderMids.length) {
    await resolveContactsBatch(entry, senderMids);
  }

  const decryptedList = [];
  for (const m of rawList) {
    const serialized = await decryptAndSerializeMessage(entry, m);
    if (serialized) {
      const { rawForMedia: _omit, ...cleanMsg } = serialized;
      decryptedList.push(cleanMsg);
    }
  }

  decryptedList.sort((a, b) => {
    const ta = a.createdTime || a.deliveredTime || 0;
    const tb = b.createdTime || b.deliveredTime || 0;
    if (ta !== tb) return ta - tb;
    try {
      const ba = BigInt(a.id);
      const bb = BigInt(b.id);
      return ba < bb ? -1 : ba > bb ? 1 : 0;
    } catch {
      return String(a.id).localeCompare(String(b.id));
    }
  });

  const oldest = decryptedList[0] || null;
  const hasMore = beforeMessageId
    ? fetchedOlderBatch.length >= Math.max(5, Math.floor(count / 2))
    : decryptedList.length >= Math.min(count, 20);

  return {
    ok: true,
    messages: decryptedList,
    hasMore: Boolean(oldest && hasMore),
    oldestMessageId: oldest ? oldest.id : "",
    oldestDeliveredTime: oldest ? oldest.deliveredTime || oldest.createdTime || 0 : 0,
    sessionData: exportSessionData(entry),
  };
}

function detectMediaMime(buf, fallback = "application/octet-stream") {
  if (!buf || buf.length < 4) return fallback;
  if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return "image/jpeg";
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return "image/png";
  if (buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x38) return "image/gif";
  if (
    buf.length >= 12 &&
    buf.subarray(0, 4).toString() === "RIFF" &&
    buf.subarray(8, 12).toString() === "WEBP"
  ) {
    return "image/webp";
  }
  if (buf.length >= 12 && buf.subarray(4, 8).toString() === "ftyp") return "video/mp4";
  if (buf.subarray(0, 4).toString() === "OggS") return "audio/ogg";
  if (buf.subarray(0, 3).toString() === "ID3" || (buf[0] === 0xff && (buf[1] & 0xe0) === 0xe0)) {
    return "audio/mpeg";
  }
  return fallback;
}

async function downloadMediaBuffer(entry, { chatMid, messageId, contentMetadata }) {
  const hint = asRecord(contentMetadata);
  const cached = entry.decryptedMsgCache.get(String(messageId));
  const mergedMeta = { ...(cached?.contentMetadata || {}), ...hint };

  // 1. Direct E2EE keyMaterial decryption if ENC_KM + OID/SID are known
  if (mergedMeta.ENC_KM && mergedMeta.OID) {
    const sidNs = mergedMeta.SID || "emi";
    const idBytes = Buffer.from(String(messageId), "utf-8");
    const idLen = Buffer.alloc(4);
    idLen.writeUInt32BE(idBytes.length, 0);
    const thriftStruct = Buffer.concat([
      Buffer.from([0x0b, 0x00, 0x04]),
      idLen,
      idBytes,
      Buffer.from([0x0f, 0x00, 0x1b, 0x0c, 0x00, 0x00, 0x00, 0x00, 0x00]),
    ]);
    const talkMeta = Buffer.from(
      JSON.stringify({ message: thriftStruct.toString("base64") })
    ).toString("base64");
    const blob = await entry.base.obs.downloadObjectForService({
      oid: String(mergedMeta.OID),
      obsPath: `talk/${sidNs}`,
      addHeaders: { "X-Talk-Meta": talkMeta },
    });
    const encBuf = Buffer.from(await blob.arrayBuffer());
    const decBytes = await entry.base.e2ee.decryptByKeyMaterial(encBuf, String(mergedMeta.ENC_KM));
    const out = Buffer.from(decBytes);
    return { buffer: out, contentType: detectMediaMime(out) };
  }

  // 2. If rawForMedia is cached with chunks, use linejs downloadMediaByE2EE
  if (cached?.rawForMedia?.chunks?.length) {
    const file = await entry.base.obs.downloadMediaByE2EE(cached.rawForMedia);
    if (file) {
      const out = Buffer.from(await file.arrayBuffer());
      return { buffer: out, contentType: detectMediaMime(out, file.type) };
    }
  }

  // 3. Look up message in chatMid if needed
  if (chatMid) {
    try {
      const recent = await entry.base.talk.getRecentMessagesV2({
        messageBoxId: chatMid,
        messagesCount: 80,
      });
      const found = (recent || []).find((m) => String(m?.id) === String(messageId));
      if (found?.chunks?.length) {
        const file = await entry.base.obs.downloadMediaByE2EE(found);
        if (file) {
          const out = Buffer.from(await file.arrayBuffer());
          return { buffer: out, contentType: detectMediaMime(out, file.type) };
        }
      }
    } catch {}
  }

  // 4. Standard (non-E2EE) OBS download
  const resp = await entry.base.fetch(
    entry.base.obs.getMessageDataUrl(String(messageId), false, false),
    {
      headers: {
        accept: "*/*",
        "x-line-application": entry.base.request.systemType,
        "x-line-access": entry.base.authToken,
      },
    }
  );
  if (!resp.ok) {
    throw new Error(`OBS HTTP ${resp.status}`);
  }
  const out = Buffer.from(await resp.arrayBuffer());
  return { buffer: out, contentType: detectMediaMime(out, resp.headers.get("content-type") || undefined) };
}

function readJsonBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on("data", (chunk) => {
      total += chunk.length;
      if (total > 35 * 1024 * 1024) {
        reject(new Error("Payload too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (!chunks.length) return resolve({});
      try {
        resolve(JSON.parse(Buffer.concat(chunks).toString("utf-8")));
      } catch (e) {
        reject(e);
      }
    });
    req.on("error", reject);
  });
}

function sendJson(res, statusCode, payload) {
  const body = JSON.stringify(payload, (_k, v) => (typeof v === "bigint" ? v.toString() : v));
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(body);
}

// Cleanup stale QR/email flows every 60s
setInterval(() => {
  const now = Date.now();
  for (const [id, st] of qrLogins.entries()) {
    if (now - st.createdAt > 7 * 60 * 1000) {
      try { st.abortController?.abort(); } catch {}
      qrLogins.delete(id);
    }
  }
  for (const [id, st] of emailLogins.entries()) {
    if (now - st.createdAt > 7 * 60 * 1000) {
      try { st.abortController?.abort(); } catch {}
      emailLogins.delete(id);
    }
  }
}, 60_000).unref();

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", "http://127.0.0.1");
  const path = url.pathname;

  try {
    if (req.method === "GET" && path === "/health") {
      return sendJson(res, 200, {
        ok: true,
        engine: "@evex-dev/linejs",
        version: "3.6.4",
        defaultDevice: DEFAULT_DEVICE,
        activeSessions: sessions.size,
      });
    }

    // --- QR Login Start ---
    if (req.method === "POST" && path === "/auth/qr/start") {
      const body = await readJsonBody(req);
      const device = normalizeDevice(body.device);
      const certificate = toSafeString(body.certificate || "").trim() || undefined;
      const prevLoginId = toSafeString(body.prevLoginId || "").trim();

      if (prevLoginId && qrLogins.has(prevLoginId)) {
        const oldSt = qrLogins.get(prevLoginId);
        if (oldSt && oldSt.status !== "authenticated") {
          try { oldSt.abortController?.abort(); } catch {}
          qrLogins.delete(prevLoginId);
        }
      }

      const loginId = crypto.randomBytes(12).toString("hex");
      const abortController = new AbortController();
      const storage = new MemoryStorage(certificate ? { qrCert: certificate } : {});
      const base = installE2EEEnhancements(
        new BaseClient({
          device,
          storage,
          fetch: createProxySafeFetch(abortController.signal),
        }),
        storage
      );

      const state = {
        loginId,
        device,
        status: "creating",
        qrUrl: "",
        pinCode: "",
        error: "",
        sessionId: "",
        sessionData: null,
        profile: null,
        createdAt: Date.now(),
        abortController,
      };
      qrLogins.set(loginId, state);

      let resolveQrReady;
      let rejectQrReady;
      const qrReadyPromise = new Promise((resolve, reject) => {
        resolveQrReady = resolve;
        rejectQrReady = reject;
      });

      base.on("qrcall", (qrUrl) => {
        state.qrUrl = qrUrl;
        state.status = "waiting_scan";
        resolveQrReady(qrUrl);
      });

      base.on("pincall", (pin) => {
        state.pinCode = String(pin || "");
        state.status = "waiting_pin";
      });

      // Kick off full SQR2 login flow in background
      (async () => {
        try {
          await base.loginProcess.withQrCode({});
          await base.loginProcess.ready();
          const profile = serializeProfile(base.profile);
          await ensureSelfKeyIndexedByMid(base, storage, profile.mid);
          const entry = createSessionEntry({
            base,
            storage,
            device,
            profile,
          });
          state.sessionId = entry.sessionId;
          state.profile = profile;
          state.sessionData = exportSessionData(entry);
          state.status = "authenticated";
        } catch (err) {
          if (abortController.signal.aborted) {
            state.status = "expired";
            state.error = "新しいQRコードが発行されたため終了しました";
            rejectQrReady(err);
            return;
          }
          const msg = err?.message || String(err);
          if (/Timeout|timed out|expired|410/i.test(msg)) {
            state.status = "expired";
            state.error = "QRコードの有効期限が切れました。更新ボタンを押してください。";
          } else {
            state.status = "error";
            state.error = `QRログインエラー: ${msg}`;
          }
          rejectQrReady(err);
        }
      })();

      const qrUrl = await Promise.race([
        qrReadyPromise,
        new Promise((_, rej) =>
          setTimeout(() => rej(new Error("LINEサーバーからのQRコード取得がタイムアウトしました")), 15000)
        ),
      ]);

      return sendJson(res, 200, {
        ok: true,
        loginId,
        qrUrl,
        device,
        status: state.status,
      });
    }

    // --- QR Login Status ---
    if (req.method === "GET" && path === "/auth/qr/status") {
      const loginId = url.searchParams.get("loginId") || "";
      const st = qrLogins.get(loginId);
      if (!st) {
        return sendJson(res, 200, {
          ok: true,
          status: "expired",
          error: "QRコードセッションが見つかりません。再読み込みしてください。",
        });
      }
      return sendJson(res, 200, {
        ok: true,
        status: st.status,
        pinCode: st.pinCode,
        error: st.error,
        sessionId: st.sessionId,
        sessionData: st.sessionData,
        profile: st.profile,
      });
    }

    // --- Email Login Start ---
    if (req.method === "POST" && path === "/auth/email/start") {
      const body = await readJsonBody(req);
      const email = toSafeString(body.email).trim();
      const password = toSafeString(body.password);
      const device = normalizeDevice(body.device);
      const certificate = toSafeString(body.certificate || "").trim();

      if (!email || !password) {
        return sendJson(res, 400, { ok: false, error: "メールアドレスとパスワードを入力してください。" });
      }

      const loginId = crypto.randomBytes(12).toString("hex");
      const abortController = new AbortController();
      const initMap = {};
      if (certificate) initMap[`cert:${email}`] = certificate;
      const storage = new MemoryStorage(initMap);
      const base = installE2EEEnhancements(
        new BaseClient({
          device,
          storage,
          fetch: createProxySafeFetch(abortController.signal),
        }),
        storage
      );

      const randomPin = String(Math.floor(100000 + Math.random() * 900000));
      const state = {
        loginId,
        status: "authenticating",
        pinCode: "",
        error: "",
        sessionId: "",
        sessionData: null,
        profile: null,
        createdAt: Date.now(),
        abortController,
      };
      emailLogins.set(loginId, state);

      base.on("pincall", (pin) => {
        state.pinCode = String(pin || randomPin);
        state.status = "waiting_pin";
      });

      (async () => {
        try {
          await base.loginProcess.withPassword({
            email,
            password,
            pincode: randomPin,
          });
          await base.loginProcess.ready();
          const profile = serializeProfile(base.profile);
          await ensureSelfKeyIndexedByMid(base, storage, profile.mid);
          const entry = createSessionEntry({
            base,
            storage,
            device,
            profile,
          });
          state.sessionId = entry.sessionId;
          state.profile = profile;
          state.sessionData = exportSessionData(entry);
          state.status = "authenticated";
        } catch (err) {
          state.status = "error";
          state.error = `ログイン失敗: ${err?.message || String(err)}`;
        }
      })();

      // Wait briefly (up to 1.5s) to catch immediate password errors or immediate PIN generation
      await new Promise((r) => setTimeout(r, 900));
      if (state.status === "error") {
        return sendJson(res, 400, { ok: false, error: state.error });
      }

      return sendJson(res, 200, {
        ok: true,
        loginId,
        status: state.status,
        pinCode: state.pinCode,
      });
    }

    // --- Email Login Status ---
    if (req.method === "GET" && path === "/auth/email/status") {
      const loginId = url.searchParams.get("loginId") || "";
      const st = emailLogins.get(loginId);
      if (!st) {
        return sendJson(res, 200, { ok: true, status: "expired", error: "認証セッションが終了しました。" });
      }
      return sendJson(res, 200, {
        ok: true,
        status: st.status,
        pinCode: st.pinCode,
        error: st.error,
        sessionId: st.sessionId,
        sessionData: st.sessionData,
        profile: st.profile,
      });
    }

    // --- Restore Session ---
    if (req.method === "POST" && path === "/auth/restore") {
      const body = await readJsonBody(req);
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
        accessToken: body.accessToken,
        device: body.device,
      });
      return sendJson(res, 200, {
        ok: true,
        sessionId: entry.sessionId,
        sessionData: exportSessionData(entry),
        profile: entry.profile,
      });
    }

    // --- Logout Session ---
    if (req.method === "POST" && path === "/auth/logout") {
      const body = await readJsonBody(req);
      const sid = toSafeString(body.sessionId || "").trim();
      if (sid) sessions.delete(sid);
      return sendJson(res, 200, { ok: true });
    }

    // --- Overview (Chats, Friends, Groups) ---
    if (req.method === "POST" && path === "/overview") {
      const body = await readJsonBody(req);
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });
      const result = await withSessionLock(entry, () => buildOverview(entry));
      return sendJson(res, 200, result);
    }

    // --- Messages History ---
    if (req.method === "POST" && path === "/messages") {
      const body = await readJsonBody(req);
      const chatMid = toSafeString(body.chatMid).trim();
      if (!chatMid) {
        return sendJson(res, 400, { ok: false, error: "chatMidが指定されていません。" });
      }
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });
      const result = await withSessionLock(entry, async () => {
        const resMsgs = await fetchChatMessages(entry, {
          chatMid,
          count: Math.min(Math.max(Number(body.count) || 50, 10), 100),
          beforeMessageId: toSafeString(body.beforeMessageId || "").trim(),
          beforeTime: Number(body.beforeDeliveredTime || body.beforeTime || 0),
        });
        if (body.markRead && Array.isArray(resMsgs.messages) && resMsgs.messages.length > 0) {
          const lastMsg = resMsgs.messages[resMsgs.messages.length - 1];
          if (lastMsg?.id && !lastMsg.isMe && !chatMid.startsWith("m")) {
            entry.base
              .getReqseq()
              .then((seq) =>
                entry.base.talk.sendChatChecked({
                  seq,
                  chatMid,
                  lastMessageId: String(lastMsg.id),
                })
              )
              .catch(() => {});
          }
        }
        return resMsgs;
      });
      return sendJson(res, 200, result);
    }

    // --- Send Text / Reply Message ---
    if (req.method === "POST" && (path === "/send_message" || path === "/send")) {
      const body = await readJsonBody(req);
      const to = toSafeString(body.chatMid || body.to).trim();
      const text = toSafeString(body.text);
      const replyTo = toSafeString(body.replyToId || body.replyTo || "").trim() || undefined;
      if (!to || !text.trim()) {
        return sendJson(res, 400, { ok: false, error: "送信先とメッセージを入力してください。" });
      }
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });
      const result = await withSessionLock(entry, async () => {
        let sent;
        let usedE2ee = false;
        if (to.startsWith("m")) {
          const sqRes = await entry.base.square.sendMessage({
            squareChatMid: to,
            text,
            relatedMessageId: replyTo,
          });
          sent = sqRes?.createdSquareMessage?.message || {
            id: String(Date.now()),
            from: entry.profile?.mid,
            to,
            text,
            createdTime: Date.now(),
          };
        } else if ((to.startsWith("u") || to.startsWith("c")) && to !== entry.profile?.mid) {
          try {
            sent = await entry.base.talk.sendMessage({
              to,
              text,
              relatedMessageId: replyTo,
              e2ee: true,
            });
            usedE2ee = true;
          } catch (e2eeErr) {
            const msg = e2eeErr?.message || String(e2eeErr);
            if (/Not support E2EE|E2EE_NOT_SUPPORTED|NoE2EEKey/i.test(msg)) {
              sent = await entry.base.talk.sendMessage({
                to,
                text,
                relatedMessageId: replyTo,
                e2ee: false,
              });
            } else {
              throw e2eeErr;
            }
          }
        } else {
          sent = await entry.base.talk.sendMessage({
            to,
            text,
            relatedMessageId: replyTo,
            e2ee: false,
          });
        }
        if (sent) {
          const msgObj = { ...sent, text, from: entry.profile?.mid, to };
          recordOpMessage(entry, msgObj);
          const serialized = await decryptAndSerializeMessage(entry, msgObj);
          if (serialized) {
            serialized.text = text;
            serialized.isMe = true;
            serialized.wasEncrypted = usedE2ee;
            if (!serialized.createdTime) serialized.createdTime = Date.now();
            delete serialized.rawForMedia;
          }
          return {
            ok: true,
            encrypted: usedE2ee,
            message: serialized,
            sessionData: exportSessionData(entry),
          };
        }
        return { ok: true, encrypted: usedE2ee, sessionData: exportSessionData(entry) };
      });
      return sendJson(res, 200, result);
    }

    // --- Send Sticker ---
    if (req.method === "POST" && path === "/send_sticker") {
      const body = await readJsonBody(req);
      const to = toSafeString(body.chatMid || body.to).trim();
      const pkgId = toSafeString(body.packageId || "11537");
      const stkId = toSafeString(body.stickerId || "52002734");
      const ver = toSafeString(body.version || "1");
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });
      const result = await withSessionLock(entry, async () => {
        const sent = await entry.base.talk.sendMessage({
          to,
          contentType: "STICKER",
          contentMetadata: {
            STKID: stkId,
            STKPKGID: pkgId,
            STKVER: ver,
          },
        });
        if (sent) recordOpMessage(entry, sent);
        const serialized = await decryptAndSerializeMessage(entry, sent || {
          id: String(Date.now()),
          from: entry.profile?.mid,
          to,
          contentType: "STICKER",
          contentMetadata: { STKID: stkId, STKPKGID: pkgId, STKVER: ver },
          createdTime: Date.now(),
        });
        if (serialized) {
          serialized.isMe = true;
          if (!serialized.createdTime) serialized.createdTime = Date.now();
          delete serialized.rawForMedia;
        }
        return { ok: true, message: serialized, sessionData: exportSessionData(entry) };
      });
      return sendJson(res, 200, result);
    }

    // --- Send Media ---
    if (req.method === "POST" && path === "/send_media") {
      const body = await readJsonBody(req);
      const to = toSafeString(body.chatMid || body.to).trim();
      const filename = toSafeString(body.filename || "upload.bin");
      const mimeType = toSafeString(body.mimeType || "application/octet-stream");
      let b64 = toSafeString(body.dataBase64 || "");
      if (b64.startsWith("data:") && b64.includes(",")) {
        b64 = b64.slice(b64.indexOf(",") + 1);
      }
      if (!to || !b64) {
        return sendJson(res, 400, { ok: false, error: "送信データが空です。" });
      }
      const rawBuf = Buffer.from(b64, "base64");
      const blob = new Blob([rawBuf], { type: mimeType });
      let oType = "file";
      if (mimeType === "image/gif") oType = "gif";
      else if (mimeType.startsWith("image/")) oType = "image";
      else if (mimeType.startsWith("video/")) oType = "video";
      else if (mimeType.startsWith("audio/")) oType = "audio";

      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });
      const result = await withSessionLock(entry, async () => {
        let sent;
        if (to.startsWith("u") || to.startsWith("c")) {
          try {
            sent = await entry.base.obs.uploadMediaByE2EE({
              data: blob,
              oType,
              to,
              filename,
            });
          } catch {
            await entry.base.obs.uploadObjTalk(to, oType, blob, undefined, filename);
          }
        } else {
          await entry.base.obs.uploadObjTalk(to, oType, blob, undefined, filename);
        }
        if (sent) recordOpMessage(entry, sent);
        return { ok: true, sessionData: exportSessionData(entry) };
      });
      return sendJson(res, 200, result);
    }

    // --- Download / Decrypt Media ---
    if (req.method === "POST" && path === "/media") {
      const body = await readJsonBody(req);
      const messageId = toSafeString(body.messageId).trim();
      const chatMid = toSafeString(body.chatMid).trim();
      if (!messageId) {
        return sendJson(res, 400, { ok: false, error: "messageId missing" });
      }
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });
      const { buffer, contentType } = await withSessionLock(entry, () =>
        downloadMediaBuffer(entry, {
          chatMid,
          messageId,
          contentMetadata: body.contentMetadata,
        })
      );
      res.writeHead(200, {
        "Content-Type": contentType || "application/octet-stream",
        "Content-Length": String(buffer.length),
        "Cache-Control": "private, max-age=3600",
      });
      return res.end(buffer);
    }

    // --- Social & Message Actions ---
    if (req.method === "POST" && path === "/action") {
      const body = await readJsonBody(req);
      const action = toSafeString(body.action).trim();
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });

      const result = await withSessionLock(entry, async () => {
        if (action === "react") {
          const msgId = toSafeString(body.messageId).trim();
          const rType = REACTION_MAP[Number(body.reactionType) || 2] || "NICE";
          await entry.base.talk.react({
            id: BigInt(msgId),
            reaction: rType,
          });
          return { ok: true };
        }

        if (action === "unsend") {
          const msgId = toSafeString(body.messageId).trim();
          await entry.base.talk.unsendMessage({
            seq: await entry.base.getReqseq(),
            messageId: msgId,
          });
          return { ok: true };
        }

        if (action === "mark_read") {
          const chatMid = toSafeString(body.chatMid).trim();
          const msgId = toSafeString(body.messageId).trim();
          if (chatMid && msgId) {
            await entry.base.talk.sendChatChecked({
              seq: await entry.base.getReqseq(),
              chatMid,
              lastMessageId: msgId,
            });
          }
          return { ok: true };
        }

        if (action === "find_contact") {
          const rawId = toSafeString(body.userId).trim();
          if (!rawId) throw new Error("検索するLINE IDを入力してください。");
          const searchId = rawId.startsWith("@") ? rawId : rawId.replace(/^@+/, "");
          let contact;
          try {
            contact = await entry.base.relation.findContactBySearchIdOrTicketV3({ searchId });
          } catch {
            contact = await entry.base.talk.findContactByUserid({ searchId: searchId.replace(/^@/, "") });
          }
          return { ok: true, contact: serializeContact(contact) };
        }

        if (action === "add_friend") {
          const mid = toSafeString(body.mid).trim();
          if (!mid) throw new Error("友だちのMIDが指定されていません。");
          await entry.base.relation.addFriendByMid({ mid });
          return { ok: true };
        }

        if (action === "create_group") {
          const name = toSafeString(body.name).trim();
          const memberMids = Array.isArray(body.memberMids)
            ? body.memberMids.map((m) => toSafeString(m).trim()).filter(Boolean)
            : [];
          if (!name) throw new Error("グループ名を入力してください。");
          const created = await entry.base.talk.createChat({
            request: {
              reqSeq: await entry.base.getReqseq(),
              type: "GROUP",
              name,
              targetUserMids: memberMids,
            },
          });
          const ch = created?.chat || {};
          return {
            ok: true,
            group: {
              chatMid: toSafeString(ch.chatMid || ""),
              chatName: toSafeString(ch.chatName || name),
            },
          };
        }

        if (action === "accept_group") {
          const chatMid = toSafeString(body.chatMid).trim();
          await entry.base.talk.acceptChatInvitation({
            request: {
              reqSeq: await entry.base.getReqseq(),
              chatMid,
            },
          });
          return { ok: true };
        }

        if (action === "update_profile") {
          const attrs = {};
          if (body.displayName !== undefined && String(body.displayName).trim()) {
            attrs.DISPLAY_NAME = { value: String(body.displayName).trim() };
          }
          if (body.statusMessage !== undefined) {
            attrs.STATUS_MESSAGE = { value: String(body.statusMessage) };
          }
          if (Object.keys(attrs).length > 0) {
            await entry.base.talk.updateProfileAttributes({
              reqSeq: await entry.base.getReqseq(),
              request: { profileAttributes: attrs },
            });
          }
          entry.profile = serializeProfile(await entry.base.talk.getProfile());
          return {
            ok: true,
            profile: entry.profile,
            sessionData: exportSessionData(entry),
          };
        }

        throw new Error(`未対応のアクションです: ${action}`);
      });

      return sendJson(res, 200, result);
    }

    return sendJson(res, 404, { ok: false, error: `Unknown bridge path: ${path}` });
  } catch (err) {
    const code = Number(err?.statusCode) || 500;
    return sendJson(res, code, {
      ok: false,
      error: err?.message || String(err),
    });
  }
});

const requestedPort = Number(process.env.LINEJS_BRIDGE_PORT || 0);
server.listen(requestedPort, "127.0.0.1", () => {
  const addr = server.address();
  const actualPort = typeof addr === "object" && addr ? addr.port : requestedPort;
  process.stdout.write(`LINEJS_BRIDGE_READY port=${actualPort}\n`);
});
