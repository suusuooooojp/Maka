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
import {
  BaseClient,
  Client,
  MemoryStorage,
  UndiciAgent,
  UndiciEnvHttpProxyAgent,
  undiciFetch,
} from "./linejs_vendor.mjs";

let SHARED_UNDICI_DISPATCHER = undefined;
try {
  const hasEnvProxy = Boolean(
    process.env.HTTPS_PROXY ||
      process.env.HTTP_PROXY ||
      process.env.https_proxy ||
      process.env.http_proxy
  );
  SHARED_UNDICI_DISPATCHER = hasEnvProxy
    ? new UndiciEnvHttpProxyAgent({
        connectTimeout: 20_000,
        headersTimeout: 190_000,
        bodyTimeout: 190_000,
      })
    : new UndiciAgent({
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

function combineAbortSignals(sigA, sigB) {
  if (!sigA) return sigB;
  if (!sigB) return sigA;
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([sigA, sigB]);
  }
  const ctrl = new AbortController();
  const onAbort = () => {
    try {
      ctrl.abort();
    } catch {}
  };
  if (sigA.aborted || sigB.aborted) {
    onAbort();
    return ctrl.signal;
  }
  sigA.addEventListener("abort", onAbort, { once: true });
  sigB.addEventListener("abort", onAbort, { once: true });
  return ctrl.signal;
}

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

    const finalSignal = combineAbortSignals(reqSignal, abortSignal);
    const fetchOpts = {
      method,
      headers: headersObj,
      body,
      signal: finalSignal,
    };
    if (SHARED_FETCH_DISPATCHER && typeof undiciFetch === "function") {
      fetchOpts.dispatcher = SHARED_FETCH_DISPATCHER;
      return undiciFetch(url, fetchOpts);
    }
    return fetch(url, fetchOpts);
  };
}

/**
 * Convert a Thrift binary chunk from @evex-dev/linejs into a raw Node Buffer.
 * IMPORTANT: @evex-dev/linejs's Thrift reader decodes valid-UTF-8 binary fields
 * (including all 4-byte big-endian keyId chunks like [0,0,0,1]) into UTF-8 strings,
 * NOT base64! Using Buffer.from(str, "base64") corrupts keyIds to 0 bytes.
 */
function chunkToBuffer(chunk) {
  if (!chunk) return Buffer.alloc(0);
  if (Buffer.isBuffer(chunk)) return chunk;
  if (chunk instanceof Uint8Array) return Buffer.from(chunk);
  if (typeof chunk === "string") return Buffer.from(chunk, "utf-8");
  if (typeof chunk === "object") {
    if (typeof chunk.__b64 === "string") return Buffer.from(chunk.__b64, "base64");
    if (chunk.type === "Buffer" && Array.isArray(chunk.data)) return Buffer.from(chunk.data);
  }
  return Buffer.from(chunk);
}

function normalizeMessageChunks(chunks) {
  if (!Array.isArray(chunks)) return undefined;
  return chunks.map(chunkToBuffer);
}

function readInt32BEChunk(chunk) {
  const b = chunkToBuffer(chunk);
  if (!b.length) return 0;
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
    // Self / Keep Memo fast path when key is already in local self keychain
    if (mid && mid === base.profile?.mid) {
      if (keyId !== undefined) {
        const selfById = await e2ee.getE2EESelfKeyDataByKeyId(keyId);
        if (selfById?.pubKey) return Buffer.from(selfById.pubKey, "base64");
      } else {
        const selfLatest = await e2ee.getE2EESelfKeyData(mid);
        if (selfLatest?.pubKey) return Buffer.from(selfLatest.pubKey, "base64");
      }
    }
    try {
      return await origGetLocalPubKey(mid, keyId);
    } catch (err) {
      // 1:1 historical peer public key fallback via TalkService.getE2EEPublicKey
      if (typeof mid === "string" && mid.startsWith("u") && keyId !== undefined) {
        for (const kv of [2, 1]) {
          try {
            const res = await base.talk.getE2EEPublicKey({
              mid,
              keyVersion: kv,
              keyId: Number(keyId),
            });
            const rawKey = res?.keyData || res?.publicKey?.keyData;
            if (rawKey) {
              const keyBuf = chunkToBuffer(rawKey);
              if (keyBuf.length > 0) {
                await storage.set(`e2eePublicKeys:${mid}:${keyId}`, keyBuf.toString("base64"));
                return keyBuf;
              }
            }
          } catch {}
        }
      }
      // Group historical shared key fallback (keyVersion 2 then 1)
      if (typeof mid === "string" && (mid.startsWith("c") || mid.startsWith("r"))) {
        for (const kv of [2, 1]) {
          try {
            let gKey = null;
            if (keyId !== undefined && Number.isFinite(Number(keyId))) {
              try {
                gKey = await base.talk.getE2EEGroupSharedKey({
                  keyVersion: kv,
                  chatMid: mid,
                  groupKeyId: Number(keyId),
                });
              } catch {}
            }
            if (!gKey) {
              gKey = await base.talk.getLastE2EEGroupSharedKey({
                keyVersion: kv,
                chatMid: mid,
              });
            }
            if (gKey) {
              return await e2ee.unwrapE2EEGroupSharedKey(mid, gKey);
            }
          } catch {}
        }
      }
      throw err;
    }
  };

  const origEncrypt = e2ee.encryptE2EEMessage.bind(e2ee);
  e2ee.encryptE2EEMessage = async function (to, data, contentType = 0, specVersion = 2) {
    try {
      return await origEncrypt(to, data, contentType, specVersion);
    } catch (err) {
      // Keep Memo (self-chat) fallback using own E2EE keypair directly
      const myMid = base.profile?.mid;
      if (myMid && to === myMid) {
        const selfKeyData = await e2ee.getE2EESelfKeyData(myMid);
        if (selfKeyData?.privKey && selfKeyData?.pubKey && selfKeyData?.keyId !== undefined) {
          const privBuf = Buffer.from(selfKeyData.privKey, "base64");
          const pubBuf = Buffer.from(selfKeyData.pubKey, "base64");
          const shared = Buffer.from(e2ee.generateSharedSecret(privBuf, pubBuf));
          const kid = Number(selfKeyData.keyId);
          if (typeof data === "string" || Buffer.isBuffer(data)) {
            return e2ee.encryptE2EETextMessage(kid, kid, shared, 2, data, to, myMid);
          }
          const ctNum = typeof contentType === "number" ? contentType : normalizeContentTypeCode(normalizeContentTypeName(contentType));
          return e2ee.encryptE2EEMessageByData(kid, kid, shared, 2, data, to, myMid, ctNum);
        }
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
    const normChunks = normalizeMessageChunks(messageObj.chunks);
    const isGroupLike = toType !== 0 && toType !== "USER" && !(typeof to === "string" && to.startsWith("u"));
    if (isGroupLike && _from === myMid && Array.isArray(normChunks) && normChunks.length >= 5) {
      const senderKeyId = readInt32BEChunk(normChunks[3]);
      const receiverKeyId = readInt32BEChunk(normChunks[4]);
      const specVersion = String(messageObj.contentMetadata?.e2eeVersion || "2");
      const groupK = await e2ee.getE2EELocalPublicKey(to, receiverKeyId);
      const privK = Buffer.from(groupK.privKey, "base64");
      const pubK = await resolveGroupSenderPubKey(myMid, senderKeyId);
      const decrypted =
        specVersion === "2"
          ? e2ee.decryptE2EEMessageV2(to, _from, normChunks, privK, pubK, parseInt(specVersion, 10), 0)
          : e2ee.decryptE2EEMessageV1(normChunks, privK, pubK);
      const text = decrypted?.text || "";
      const meta = {};
      for (const [k, val] of Object.entries(decrypted || {})) {
        if (k === "text") continue;
        meta[k] = typeof val === "string" ? val : JSON.stringify(val);
      }
      return [text, meta];
    }
    return await origDecText({ ...messageObj, chunks: normChunks, contentType: 0 }, isSelf);
  };

  const origDecData = e2ee.decryptE2EEDataMessage.bind(e2ee);
  e2ee.decryptE2EEDataMessage = async function (messageObj, isSelf = false) {
    const myMid = base.profile?.mid || "";
    const _from = messageObj.from;
    const to = messageObj.to;
    const toType = messageObj.toType;
    const normChunks = normalizeMessageChunks(messageObj.chunks);
    const isGroupLike = toType !== 0 && toType !== "USER" && !(typeof to === "string" && to.startsWith("u"));
    if (isGroupLike && _from === myMid && Array.isArray(normChunks) && normChunks.length >= 5) {
      const senderKeyId = readInt32BEChunk(normChunks[3]);
      const receiverKeyId = readInt32BEChunk(normChunks[4]);
      const specVersion = String(messageObj.contentMetadata?.e2eeVersion || "2");
      const ctCode = typeof messageObj.contentType === "number"
        ? messageObj.contentType
        : normalizeContentTypeCode(normalizeContentTypeName(messageObj.contentType));
      const groupK = await e2ee.getE2EELocalPublicKey(to, receiverKeyId);
      const privK = Buffer.from(groupK.privKey, "base64");
      const pubK = await resolveGroupSenderPubKey(myMid, senderKeyId);
      const decrypted =
        specVersion === "2"
          ? e2ee.decryptE2EEMessageV2(to, _from, normChunks, privK, pubK, parseInt(specVersion, 10), ctCode)
          : e2ee.decryptE2EEMessageV1(normChunks, privK, pubK);
      return decrypted || {};
    }
    return await origDecData({ ...messageObj, chunks: normChunks }, isSelf);
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
  const client = new Client(base);
  const entry = {
    sessionId: sid,
    client,
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
    unsentMsgIds: new Set(),
    boxesCache: { ts: 0, boxes: [] },
    sseClients: new Set(),
    eventSeq: 0,
    recentEvents: [],
    syncLoopRunning: false,
    syncAbort: new AbortController(),
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
  ensureBackgroundSyncLoop(entry);
  return entry;
}

function stopSessionEntry(sid) {
  const existing = sessions.get(sid);
  if (!existing) return;
  sessions.delete(sid);
  try {
    existing.syncAbort?.abort();
  } catch {}
  for (const res of existing.sseClients) {
    try {
      res.end();
    } catch {}
  }
  existing.sseClients.clear();
}

function emitSessionRealtimeEvent(entry, payload) {
  const seq = ++entry.eventSeq;
  const fullEvent = {
    seq,
    ts: Date.now(),
    ...payload,
  };
  entry.recentEvents.push(fullEvent);
  if (entry.recentEvents.length > 80) {
    entry.recentEvents.shift();
  }
  if (entry.sseClients.size > 0) {
    const serialized = `event: talk_event\ndata: ${JSON.stringify(fullEvent, (_k, v) =>
      typeof v === "bigint" ? v.toString() : v
    )}\n\n`;
    for (const res of [...entry.sseClients]) {
      try {
        res.write(serialized);
      } catch {
        entry.sseClients.delete(res);
      }
    }
  }
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
      existing.updatedAt = Date.now();
      ensureBackgroundSyncLoop(existing);
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
  if (toMid && toMid.startsWith("r")) return "ROOM";
  if (toMid && toMid.startsWith("c")) return "GROUP";
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

function resolveChatMidForMessage(entry, rawMsg) {
  const myMid = entry.profile?.mid || "";
  const fromMid = toSafeString(rawMsg.from || rawMsg._from || "");
  const toMid = toSafeString(rawMsg.to || "");
  const toType = normalizeToTypeName(rawMsg.toType, toMid);
  if (toType === "GROUP" || toType === "ROOM") return toMid;
  if (fromMid === myMid) return toMid || fromMid;
  return fromMid || toMid;
}

async function decryptAndSerializeMessage(entry, rawMsg) {
  if (!rawMsg) return null;
  const raw = asRecord(rawMsg);
  const msgId = toSafeString(raw.id);
  if (msgId && entry.unsentMsgIds.has(msgId)) return null;
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
  const normChunks = normalizeMessageChunks(raw.chunks);
  const hasChunks = Array.isArray(normChunks) && normChunks.length > 0;
  const wasEncrypted = Boolean(hasChunks || meta.e2eeVersion);

  let text = typeof raw.text === "string" ? raw.text : "";
  let decryptedOk = !wasEncrypted || Boolean(text && !hasChunks);

  const rawWithBufferChunks = hasChunks ? { ...raw, chunks: normChunks } : raw;

  if (wasEncrypted && hasChunks) {
    const msgClone = {
      ...rawWithBufferChunks,
      from: fromMid,
      to: toMid,
      toType,
      contentType: ctCode,
      contentMetadata: { ...meta },
      chunks: normChunks,
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
  const deliveredTime = normalizeEpochMs(raw.deliveredTime || 0);
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
    rawForMedia: [1, 2, 3, 14].includes(ctCode) ? rawWithBufferChunks : undefined,
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
  if (!rawMsg || !rawMsg.id) return "";
  const msgId = toSafeString(rawMsg.id);
  entry.unsentMsgIds.delete(msgId);
  const chatMid = resolveChatMidForMessage(entry, rawMsg);
  if (!chatMid) return "";
  let map = entry.opMessagesByChat.get(chatMid);
  if (!map) {
    map = new Map();
    entry.opMessagesByChat.set(chatMid, map);
  }
  const normalizedRaw = Array.isArray(rawMsg.chunks)
    ? { ...rawMsg, chunks: normalizeMessageChunks(rawMsg.chunks) }
    : rawMsg;
  map.set(msgId, normalizedRaw);
  if (map.size > 200) {
    const oldest = map.keys().next().value;
    map.delete(oldest);
  }
  entry.boxesCache.ts = 0;
  return chatMid;
}

function removeUnsentMessageFromState(entry, chatMidHint, messageId) {
  const msgId = toSafeString(messageId);
  if (!msgId) return "";
  entry.unsentMsgIds.add(msgId);
  if (entry.unsentMsgIds.size > 500) {
    const first = entry.unsentMsgIds.values().next().value;
    entry.unsentMsgIds.delete(first);
  }
  entry.decryptedMsgCache.delete(msgId);
  entry.boxesCache.ts = 0;
  let foundChat = chatMidHint || "";
  for (const [cmid, map] of entry.opMessagesByChat.entries()) {
    if (map.has(msgId)) {
      map.delete(msgId);
      if (!foundChat) foundChat = cmid;
    }
  }
  return foundChat;
}

async function processSyncResponse(entry, res) {
  // Initial sync (revision === 0) or full-sync reset returns fullSyncResponse.nextRevision!
  const fullNextRev = res?.fullSyncResponse?.nextRevision;
  if (fullNextRev !== undefined && fullNextRev !== null) {
    const nr = BigInt(fullNextRev);
    if (nr > entry.syncState.revision) {
      entry.syncState.revision = nr;
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
      const chatMid = recordOpMessage(entry, op.message);
      const fromMid = toSafeString(op.message.from || op.message._from || "");
      if (fromMid.startsWith("u") && !entry.contactsMap.has(fromMid)) {
        await resolveContactsBatch(entry, [fromMid]).catch(() => {});
      }
      const serialized = await decryptAndSerializeMessage(entry, op.message).catch(() => null);
      if (serialized && chatMid) {
        const { rawForMedia: _omit, ...cleanMsg } = serialized;
        emitSessionRealtimeEvent(entry, {
          type: "message",
          chatMid,
          message: cleanMsg,
        });
      }
    } else if (
      (opType === "NOTIFIED_EDIT_MESSAGE" || opType === "EDIT_MESSAGE") &&
      op.message
    ) {
      const msgId = toSafeString(op.message.id);
      if (msgId) entry.decryptedMsgCache.delete(msgId);
      const chatMid = recordOpMessage(entry, op.message);
      const serialized = await decryptAndSerializeMessage(entry, op.message).catch(() => null);
      if (serialized && chatMid) {
        const { rawForMedia: _omit, ...cleanMsg } = serialized;
        emitSessionRealtimeEvent(entry, {
          type: "message_edit",
          chatMid,
          message: cleanMsg,
        });
      }
    } else if (
      opType === "NOTIFIED_DESTROY_MESSAGE" ||
      opType === "DESTROY_MESSAGE" ||
      opType === 65
    ) {
      const hintChat = toSafeString(op.param1 || "");
      const msgId = toSafeString(op.param2 || op.message?.id || "");
      const chatMid = removeUnsentMessageFromState(entry, hintChat, msgId);
      if (msgId) {
        emitSessionRealtimeEvent(entry, {
          type: "message_unsend",
          chatMid,
          messageId: msgId,
        });
      }
    } else if (
      opType === "SEND_REACTION" ||
      opType === "NOTIFIED_SEND_REACTION"
    ) {
      const msgId = toSafeString(op.param1 || "");
      const chatMid = toSafeString(op.param2 || "");
      if (msgId) entry.decryptedMsgCache.delete(msgId);
      emitSessionRealtimeEvent(entry, {
        type: "reaction",
        chatMid,
        messageId: msgId,
      });
    } else if (
      opType === "SEND_CHAT_CHECKED" ||
      opType === "NOTIFIED_READ_MESSAGE" ||
      opType === 40 ||
      opType === 55
    ) {
      const chatMid = toSafeString(op.param1 || "");
      if (chatMid) {
        emitSessionRealtimeEvent(entry, {
          type: "read",
          chatMid,
        });
      }
    }
  }
}

async function seedInitialSyncRevision(entry) {
  if (entry.syncState.revision > 0n) return;
  try {
    const res = await entry.base.talk.sync({
      limit: 100,
      revision: 0n,
      globalRev: entry.syncState.globalRev,
      individualRev: entry.syncState.individualRev,
      timeout: 0,
    });
    await processSyncResponse(entry, res);
  } catch {}
  if (entry.syncState.revision === 0n) {
    try {
      const lastRev = await entry.base.talk.getLastOpRevision();
      if (lastRev !== undefined && lastRev !== null) {
        entry.syncState.revision = BigInt(lastRev);
      }
    } catch {}
  }
}

function ensureBackgroundSyncLoop(entry) {
  if (entry.syncLoopRunning || entry.syncAbort.signal.aborted) return;
  entry.syncLoopRunning = true;

  (async () => {
    let consecutiveErrors = 0;
    try {
      while (
        sessions.get(entry.sessionId) === entry &&
        !entry.syncAbort.signal.aborted &&
        entry.base.authToken
      ) {
        // Pause background polling if session has been completely inactive > 15m with no SSE clients
        if (
          entry.sseClients.size === 0 &&
          Date.now() - entry.updatedAt > 15 * 60 * 1000
        ) {
          break;
        }
        try {
          if (entry.syncState.revision === 0n) {
            await seedInitialSyncRevision(entry);
          }
          const res = await entry.base.talk.sync({
            limit: 100,
            revision: entry.syncState.revision,
            globalRev: entry.syncState.globalRev,
            individualRev: entry.syncState.individualRev,
            timeout: 15_000,
          });
          consecutiveErrors = 0;
          await processSyncResponse(entry, res);
        } catch (err) {
          consecutiveErrors++;
          const msg = err?.message || String(err);
          if (/AUTHENTICATION_FAILED|INVALID_MID|NOT_AUTHORIZED_DEVICE/i.test(msg)) {
            break;
          }
          const backoffMs = Math.min(1000 * Math.pow(1.6, consecutiveErrors), 12_000);
          await new Promise((r) => setTimeout(r, backoffMs));
        }
        await new Promise((r) => setTimeout(r, 80));
      }
    } finally {
      entry.syncLoopRunning = false;
    }
  })();
}

async function getMessageBoxesCached(entry, maxAgeMs = 3500) {
  const now = Date.now();
  if (entry.boxesCache.boxes.length > 0 && now - entry.boxesCache.ts < maxAgeMs) {
    return entry.boxesCache.boxes;
  }
  let res = await entry.base.talk
    .getMessageBoxes({
      messageBoxListRequest: {
        activeOnly: false,
        unreadOnly: false,
        messageBoxCountLimit: 300,
        withUnreadCount: true,
        lastMessagesPerMessageBoxCount: 5,
      },
    })
    .catch(() => null);

  if (!res || !Array.isArray(res.messageBoxes)) {
    res = await entry.base.talk
      .getMessageBoxes({ messageBoxListRequest: {} })
      .catch(() => ({ messageBoxes: [] }));
  }

  const boxes = Array.isArray(res?.messageBoxes) ? res.messageBoxes : [];
  if (boxes.length > 0) {
    entry.boxesCache = { ts: Date.now(), boxes };
  }
  return boxes;
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
  ensureBackgroundSyncLoop(entry);

  const [, contacts, { groups, invitedGroups }, boxes, squareResult] = await Promise.all([
    seedInitialSyncRevision(entry),
    fetchFriendsList(entry),
    fetchGroupsList(entry),
    getMessageBoxesCached(entry, 1500),
    entry.base.square
      .getJoinedSquareChats({ request: { limit: 100 } })
      .catch(() => null),
  ]);

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

    const lastDeliveredTime = normalizeEpochMs(
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
            deliveredTime: String(b.lastDeliveredMessageId.deliveredTime ?? 0),
          }
        : null,
    });
  }

  // 2. Merge any active chats from opMessagesByChat or joined groups not in boxes
  for (const [chatMid, opMap] of entry.opMessagesByChat.entries()) {
    if (chatRoomsByMid.has(chatMid) || opMap.size === 0) continue;
    const isGroup = chatMid.startsWith("c") || chatMid.startsWith("r");
    const gInfo = isGroup ? entry.groupsMap.get(chatMid) : null;
    const uInfo = !isGroup
      ? chatMid === entry.profile.mid
        ? { displayName: "Keepメモ (自分)", picturePath: entry.profile.picturePath }
        : entry.contactsMap.get(chatMid)
      : null;
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

  ensureBackgroundSyncLoop(entry);
  if (entry.syncState.revision === 0n) {
    await seedInitialSyncRevision(entry);
  }

  const rawById = new Map();
  let fetchedOlderBatch = [];

  if (beforeMessageId && /^\d+$/.test(String(beforeMessageId))) {
    // Pagination cursor (older messages)
    let resolvedBeforeTime = Number(beforeTime) || 0;
    if (!resolvedBeforeTime) {
      const cachedMsg =
        entry.messagesCacheByChat?.get(chatMid)?.find?.((x) => String(x?.id) === String(beforeMessageId)) ||
        entry.opMessagesByChat?.get(chatMid)?.get?.(String(beforeMessageId));
      resolvedBeforeTime = Number(cachedMsg?.deliveredTime || cachedMsg?.createdTime || 0);
    }
    try {
      const prev = await entry.base.talk.getPreviousMessagesV2WithRequest({
        request: {
          messageBoxId: chatMid,
          endMessageId: {
            messageId: BigInt(beforeMessageId),
            deliveredTime: BigInt(resolvedBeforeTime || 0),
          },
          messagesCount: count,
          withReadCount: true,
          receivedOnly: false,
        },
      });
      if (Array.isArray(prev)) {
        fetchedOlderBatch = prev;
        for (const m of prev) {
          const midStr = toSafeString(m?.id);
          if (midStr && midStr !== String(beforeMessageId) && !entry.unsentMsgIds.has(midStr)) {
            rawById.set(midStr, m);
          }
        }
      }
    } catch {}
  } else {
    // Primary @evex-dev/linejs history path: getMessageBoxes -> getPreviousMessagesV2WithRequest
    // run in parallel with getRecentMessagesV2 + getMessageBoxesByIds + opMessagesByChat merge for 100% coverage!
    const [boxesResult, recentResult] = await Promise.all([
      getMessageBoxesCached(entry, 3500).catch(() => []),
      entry.base.talk
        .getRecentMessagesV2({
          messageBoxId: chatMid,
          messagesCount: count,
        })
        .catch(() => []),
    ]);

    if (Array.isArray(recentResult)) {
      for (const m of recentResult) {
        const midStr = toSafeString(m?.id);
        if (midStr && !entry.unsentMsgIds.has(midStr)) rawById.set(midStr, m);
      }
    }

    let box = (boxesResult || []).find((b) => toSafeString(b?.id) === chatMid) || null;
    if (!box && typeof entry.base.talk.getMessageBoxesByIds === "function") {
      const byIds = await entry.base.talk
        .getMessageBoxesByIds({
          messageBoxIds: [chatMid],
        })
        .catch(() => null);
      const byIdBoxes = Array.isArray(byIds) ? byIds : byIds?.messageBoxes || [];
      box = byIdBoxes.find((b) => toSafeString(b?.id) === chatMid) || byIdBoxes[0] || null;
    }
    if (!box) {
      // Refresh boxes uncached once if chatMid was newly active
      const freshBoxes = await getMessageBoxesCached(entry, 0).catch(() => []);
      box = (freshBoxes || []).find((b) => toSafeString(b?.id) === chatMid) || null;
    }

    if (box) {
      for (const lm of box.lastMessages || []) {
        const midStr = toSafeString(lm?.id);
        if (midStr && !entry.unsentMsgIds.has(midStr)) rawById.set(midStr, lm);
      }
      const anchor = box.lastDeliveredMessageId;
      if (anchor?.messageId !== undefined && anchor?.messageId !== null) {
        try {
          const prev = await entry.base.talk.getPreviousMessagesV2WithRequest({
            request: {
              messageBoxId: toSafeString(box.id) || chatMid,
              endMessageId: {
                messageId: BigInt(anchor.messageId),
                deliveredTime: BigInt(anchor.deliveredTime ?? 0),
              },
              messagesCount: count,
              withReadCount: true,
              receivedOnly: false,
            },
          });
          if (Array.isArray(prev)) {
            for (const m of prev) {
              const midStr = toSafeString(m?.id);
              if (midStr && !entry.unsentMsgIds.has(midStr)) rawById.set(midStr, m);
            }
          }
        } catch {}
      }
    }

    const opMap = entry.opMessagesByChat.get(chatMid);
    if (opMap) {
      for (const [id, m] of opMap.entries()) {
        if (!entry.unsentMsgIds.has(id)) {
          rawById.set(id, m);
        }
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
        const normalizedFound = { ...found, chunks: normalizeMessageChunks(found.chunks) };
        const file = await entry.base.obs.downloadMediaByE2EE(normalizedFound);
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

function encodeQrState(st) {
  try {
    return Buffer.from(
      JSON.stringify({
        l: st.loginId,
        s: st.sqr,
        n: st.nonce,
        k: st.secretB64,
        d: st.device,
        c: st.certificate || "",
        p: st.pinCode || "",
        u: st.qrUrl || "",
        t: st.status,
        a: st.createdAt,
      }),
      "utf-8"
    ).toString("base64url");
  } catch {
    return "";
  }
}

function decodeQrState(raw) {
  if (!raw || typeof raw !== "string") return null;
  try {
    const parsed = JSON.parse(Buffer.from(raw, "base64url").toString("utf-8"));
    if (!parsed?.s || !parsed?.k) return null;
    return parsed;
  } catch {
    return null;
  }
}

function isTransientQrPollError(err) {
  const msg = `${err?.code || ""} ${err?.cause?.code || ""} ${err?.message || String(err)}`;
  return /Timeout|timed out|status=408|fetch failed|socket|ECONNRESET|EPIPE|UND_ERR|ETIMEDOUT|EAI_AGAIN|network|terminated/i.test(
    msg
  );
}

function runResilientQrBackground(state) {
  if (state.bgRunning) return;
  state.bgRunning = true;

  (async () => {
    try {
      const lp = state.base.loginProcess;
      const pollSec = 18;

      if (state.status === "waiting_scan" || state.status === "creating") {
        state.status = "waiting_scan";
        let verified = false;
        while (!state.abortController.signal.aborted && Date.now() - state.createdAt < 210_000) {
          try {
            const ok = await lp.checkQrCodeVerified(state.sqr, 1, pollSec);
            if (ok) {
              verified = true;
              break;
            }
          } catch (err) {
            if (state.abortController.signal.aborted) break;
            if (isTransientQrPollError(err)) {
              await new Promise((r) => setTimeout(r, 250));
              continue;
            }
            const msg = err?.message || String(err);
            if (/expired|status=410|410|INVALID_STATE|NOT_FOUND/i.test(msg)) {
              state.status = "expired";
              state.error = "QRコードの有効期限が切れました。更新ボタンを押してください。";
              return;
            }
            throw err;
          }
        }
        if (!verified) {
          if (!state.abortController.signal.aborted) {
            state.status = "expired";
            state.error = "QRコードの有効期限が切れました。更新ボタンを押してください。";
          }
          return;
        }

        let certOk = false;
        try {
          const savedCert = await lp.getQrCert();
          if (savedCert) {
            await lp.verifyCertificate(state.sqr, savedCert);
            certOk = true;
          }
        } catch {
          certOk = false;
        }

        if (!certOk) {
          const pinRes = await lp.createPinCode(state.sqr);
          const pincode = String(pinRes?.[1] ?? pinRes?.pincode ?? "");
          state.pinCode = pincode;
          state.status = "waiting_pin";
        } else {
          state.status = "completing";
        }
      }

      if (state.status === "waiting_pin") {
        let pinVerified = false;
        while (!state.abortController.signal.aborted && Date.now() - state.createdAt < 240_000) {
          try {
            const ok = await lp.checkPinCodeVerified(state.sqr, 1, pollSec);
            if (ok) {
              pinVerified = true;
              break;
            }
          } catch (err) {
            if (state.abortController.signal.aborted) break;
            if (isTransientQrPollError(err)) {
              await new Promise((r) => setTimeout(r, 250));
              continue;
            }
            const msg = err?.message || String(err);
            if (/expired|status=410|410|INVALID_STATE|NOT_FOUND/i.test(msg)) {
              state.status = "expired";
              state.error = "本人確認コードの有効期限が切れました。再度QRコードを読み取ってください。";
              return;
            }
            throw err;
          }
        }
        if (!pinVerified) {
          if (!state.abortController.signal.aborted) {
            state.status = "expired";
            state.error = "本人確認コードの入力期限が切れました。更新ボタンを押してください。";
          }
          return;
        }
        state.status = "completing";
      }

      const response = await lp.qrCodeLoginV2ForSecure(state.sqr, state.nonce || "");
      const pem = response?.[1] ?? response?.certificate;
      const tokenInfo = response?.[3] ?? response?.tokenV3IssueResult;
      const metaData = response?.[6] ?? response?.metaData;
      if (pem) {
        state.certificate = String(pem);
        state.base.emit("update:qrcert", pem);
        await lp.registerQrCert(String(pem));
      }

      const e2eeInfo = response?.[10] ?? metaData?.["e2eeInfo"] ?? response?.e2eeKeyBackupInfo;
      const secretBuf = Buffer.from(state.secretB64, "base64");
      let e2eeKeyResult;
      if (e2eeInfo) {
        try {
          e2eeKeyResult = await state.base.e2ee.decodeE2EEKeyV1(e2eeInfo, secretBuf);
        } catch {}
      }

      const authToken = String(tokenInfo?.[1] ?? tokenInfo?.accessToken ?? response?.[2] ?? "");
      const refreshToken = String(tokenInfo?.[2] ?? tokenInfo?.refreshToken ?? "");
      const dur = Number(tokenInfo?.[3] ?? tokenInfo?.durationUntilRefreshInSec ?? 0);
      const issuedAt = Number(
        tokenInfo?.[6] ?? tokenInfo?.issueTimeEpochSec ?? Math.floor(Date.now() / 1000)
      );
      if (!authToken) {
        throw new Error("LINEサーバーからアクセストークンを取得できませんでした");
      }
      if (refreshToken) await state.storage.set("refreshToken", refreshToken);
      if (dur || issuedAt) await state.storage.set("expire", dur + issuedAt);

      state.base.authToken = authToken;
      state.base.emit("update:authtoken", authToken);

      if (!e2eeKeyResult) {
        try {
          await state.base.e2ee.registerE2EEKeyPair();
        } catch {}
      }
      try {
        await state.base.e2ee.verifyLoginKey();
      } catch {}

      await lp.ready();
      const profile = serializeProfile(state.base.profile);
      await ensureSelfKeyIndexedByMid(state.base, state.storage, profile.mid);
      const entry = createSessionEntry({
        base: state.base,
        storage: state.storage,
        device: state.device,
        profile,
      });
      state.sessionId = entry.sessionId;
      state.profile = profile;
      state.sessionData = exportSessionData(entry);
      state.status = "authenticated";
    } catch (err) {
      if (state.abortController.signal.aborted) {
        state.status = "expired";
        state.error = "新しいQRコードが発行されたため終了しました";
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
    } finally {
      state.bgRunning = false;
    }
  })();
}

function adoptQrStateIfNeeded(loginId, qrStateToken) {
  if (loginId && qrLogins.has(loginId)) {
    return qrLogins.get(loginId);
  }
  const decoded = decodeQrState(qrStateToken);
  if (!decoded) return null;
  const effectiveId = loginId || decoded.l || crypto.randomBytes(12).toString("hex");
  if (qrLogins.has(effectiveId)) {
    return qrLogins.get(effectiveId);
  }
  const device = normalizeDevice(decoded.d);
  const certificate = decoded.c || undefined;
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
  const adopted = {
    loginId: effectiveId,
    device,
    certificate: certificate || "",
    sqr: String(decoded.s),
    nonce: String(decoded.n || ""),
    secretB64: String(decoded.k),
    status: decoded.t === "waiting_pin" ? "waiting_pin" : "waiting_scan",
    qrUrl: String(decoded.u || ""),
    pinCode: String(decoded.p || ""),
    error: "",
    sessionId: "",
    sessionData: null,
    profile: null,
    createdAt: Number(decoded.a) || Date.now(),
    abortController,
    storage,
    base,
    bgRunning: false,
  };
  qrLogins.set(effectiveId, adopted);
  runResilientQrBackground(adopted);
  return adopted;
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

      const sessionRes = await Promise.race([
        base.loginProcess.createSession(),
        new Promise((_, rej) =>
          setTimeout(() => rej(new Error("LINEサーバーへの接続がタイムアウトしました")), 15000)
        ),
      ]);
      const sqr = String(sessionRes?.[1] ?? sessionRes?.authSessionId ?? "");
      if (!sqr) {
        throw new Error("LINE QRセッションの作成に失敗しました");
      }
      const forSecure = await Promise.race([
        base.loginProcess.createQrCodeForSecure(sqr),
        new Promise((_, rej) =>
          setTimeout(() => rej(new Error("LINEサーバーからのQRコード取得がタイムアウトしました")), 15000)
        ),
      ]);
      const baseQrUrl = String(forSecure?.[1] ?? forSecure?.callbackUrl ?? "");
      const nonce = String(forSecure?.[4] ?? forSecure?.nonce ?? "");
      const [secretBytes, secretUrl] = base.e2ee.createSqrSecret();
      const qrUrl = baseQrUrl + secretUrl;
      const secretB64 = Buffer.from(secretBytes).toString("base64");

      const state = {
        loginId,
        device,
        certificate: certificate || "",
        sqr,
        nonce,
        secretB64,
        status: "waiting_scan",
        qrUrl,
        pinCode: "",
        error: "",
        sessionId: "",
        sessionData: null,
        profile: null,
        createdAt: Date.now(),
        abortController,
        storage,
        base,
        bgRunning: false,
      };
      qrLogins.set(loginId, state);
      runResilientQrBackground(state);

      return sendJson(res, 200, {
        ok: true,
        loginId,
        qrUrl,
        qrState: encodeQrState(state),
        device,
        status: state.status,
      });
    }

    // --- QR Login Status (supports GET and POST with stateless qrState fallback) ---
    if ((req.method === "GET" || req.method === "POST") && path === "/auth/qr/status") {
      const body = req.method === "POST" ? await readJsonBody(req) : {};
      const loginId = toSafeString(body.loginId || url.searchParams.get("loginId") || "").trim();
      const qrStateToken = toSafeString(body.qrState || url.searchParams.get("qrState") || "").trim();
      const st = adoptQrStateIfNeeded(loginId, qrStateToken);
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
        qrState: st.status === "authenticated" ? "" : encodeQrState(st),
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
      if (sid) stopSessionEntry(sid);
      return sendJson(res, 200, { ok: true });
    }

    // --- Real-Time SSE Talk Stream (/SYNC4 push) ---
    if (req.method === "GET" && path === "/events") {
      const sid = toSafeString(url.searchParams.get("sessionId") || "").trim();
      const sinceSeq = Number(url.searchParams.get("since") || 0);
      const entry = sid ? sessions.get(sid) : null;
      if (!entry || !entry.base.authToken) {
        return sendJson(res, 404, { ok: false, error: "Session not active on bridge" });
      }
      entry.updatedAt = Date.now();
      ensureBackgroundSyncLoop(entry);

      res.writeHead(200, {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no",
      });
      res.write(`event: ready\ndata: ${JSON.stringify({ ok: true, seq: entry.eventSeq })}\n\n`);

      if (sinceSeq > 0) {
        for (const ev of entry.recentEvents) {
          if (ev.seq > sinceSeq) {
            res.write(`event: talk_event\ndata: ${JSON.stringify(ev, (_k, v) => (typeof v === "bigint" ? v.toString() : v))}\n\n`);
          }
        }
      }

      entry.sseClients.add(res);
      const hb = setInterval(() => {
        try {
          res.write(": keepalive\n\n");
        } catch {
          clearInterval(hb);
          entry.sseClients.delete(res);
        }
      }, 15_000);
      hb.unref?.();

      req.on("close", () => {
        clearInterval(hb);
        entry.sseClients.delete(res);
      });
      return;
    }

    // --- Incremental Event Poll Fallback ---
    if (req.method === "POST" && path === "/events/poll") {
      const body = await readJsonBody(req);
      const sinceSeq = Number(body.since || 0);
      const entry = await getOrRestoreSession({
        sessionId: body.sessionId,
        sessionData: body.sessionData,
      });
      entry.updatedAt = Date.now();
      ensureBackgroundSyncLoop(entry);
      const events = entry.recentEvents.filter((ev) => ev.seq > sinceSeq);
      return sendJson(res, 200, {
        ok: true,
        seq: entry.eventSeq,
        events,
        sessionData: exportSessionData(entry),
      });
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
        const myMid = entry.profile?.mid || entry.base.profile?.mid || "";
        const hasSelfKeys = countSelfE2EEKeys(entry.storage.getAll(), myMid) > 0;

        if (to.startsWith("m")) {
          const sqRes = await entry.base.square.sendMessage({
            squareChatMid: to,
            text,
            relatedMessageId: replyTo,
          });
          sent = sqRes?.createdSquareMessage?.message || {
            id: String(Date.now()),
            from: myMid,
            to,
            text,
            createdTime: Date.now(),
          };
        } else {
          let lastSendErr = null;
          // 1. Prefer Letter Sealing E2EE when self keys exist (1:1, Keep Memo, Group)
          if (hasSelfKeys && (to.startsWith("u") || to.startsWith("c"))) {
            try {
              sent = await entry.base.talk.sendMessage({
                to,
                text,
                relatedMessageId: replyTo,
                e2ee: true,
              });
              usedE2ee = true;
            } catch (e2eeErr) {
              lastSendErr = e2eeErr;
            }
          }
          // 2. Fallback to @evex-dev/linejs auto-negotiated sendMessage (plain + server E2EE retry)
          if (!sent) {
            try {
              sent = await entry.base.talk.sendMessage({
                to,
                text,
                relatedMessageId: replyTo,
              });
              usedE2ee = Boolean(sent?.chunks?.length || sent?.contentMetadata?.e2eeVersion);
            } catch (plainErr) {
              lastSendErr = plainErr;
            }
          }
          // 3. Fallback to explicit plain sendMessage (Official Accounts / legacy rooms)
          if (!sent) {
            try {
              sent = await entry.base.talk.sendMessage({
                to,
                text,
                relatedMessageId: replyTo,
                e2ee: false,
              });
              usedE2ee = false;
            } catch (explicitPlainErr) {
              lastSendErr = explicitPlainErr;
            }
          }
          // 4. Final fallback to @evex-dev/linejs Client.sendCompactMessage (/CA5 | /ECA5)
          if (!sent && !replyTo) {
            try {
              sent = await entry.client.sendCompactMessage(to, text);
              usedE2ee = Boolean(sent?.chunks?.length || sent?.contentMetadata?.e2eeVersion);
            } catch (compactErr) {
              lastSendErr = compactErr;
            }
          }
          if (!sent && lastSendErr) {
            throw lastSendErr;
          }
        }

        if (sent) {
          const msgObj = {
            ...sent,
            id: toSafeString(sent.id || Date.now()),
            text,
            from: myMid,
            to,
            createdTime: sent.createdTime || Date.now(),
          };
          recordOpMessage(entry, msgObj);
          const serialized = await decryptAndSerializeMessage(entry, msgObj);
          let cleanMsg = null;
          if (serialized) {
            serialized.text = text;
            serialized.isMe = true;
            serialized.wasEncrypted = usedE2ee;
            serialized.decryptedOk = true;
            if (!serialized.createdTime) serialized.createdTime = Date.now();
            const { rawForMedia: _omit, ...rest } = serialized;
            cleanMsg = rest;
            emitSessionRealtimeEvent(entry, {
              type: "message",
              chatMid: to,
              message: cleanMsg,
            });
          }
          return {
            ok: true,
            encrypted: usedE2ee,
            message: cleanMsg,
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
        let sent;
        if (to.startsWith("m")) {
          const sqRes = await entry.base.square.sendMessage({
            squareChatMid: to,
            contentType: "STICKER",
            contentMetadata: { STKID: stkId, STKPKGID: pkgId, STKVER: ver },
          });
          sent = sqRes?.createdSquareMessage?.message;
        } else {
          sent = await entry.base.talk.sendMessage({
            to,
            contentType: "STICKER",
            contentMetadata: {
              STKID: stkId,
              STKPKGID: pkgId,
              STKVER: ver,
            },
          });
        }
        const fallbackRaw = sent || {
          id: String(Date.now()),
          from: entry.profile?.mid,
          to,
          contentType: "STICKER",
          contentMetadata: { STKID: stkId, STKPKGID: pkgId, STKVER: ver },
          createdTime: Date.now(),
        };
        recordOpMessage(entry, fallbackRaw);
        const serialized = await decryptAndSerializeMessage(entry, fallbackRaw);
        let cleanMsg = null;
        if (serialized) {
          serialized.isMe = true;
          if (!serialized.createdTime) serialized.createdTime = Date.now();
          const { rawForMedia: _omit, ...rest } = serialized;
          cleanMsg = rest;
          emitSessionRealtimeEvent(entry, {
            type: "message",
            chatMid: to,
            message: cleanMsg,
          });
        }
        return { ok: true, message: cleanMsg, sessionData: exportSessionData(entry) };
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
        const myMid = entry.profile?.mid || entry.base.profile?.mid || "";
        const hasSelfKeys = countSelfE2EEKeys(entry.storage.getAll(), myMid) > 0;
        if (hasSelfKeys && (to.startsWith("u") || to.startsWith("c"))) {
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
        entry.boxesCache.ts = 0;
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
          const chatMidHint = toSafeString(body.chatMid || "").trim();
          await entry.base.talk.unsendMessage({
            seq: await entry.base.getReqseq(),
            messageId: msgId,
          });
          const chatMid = removeUnsentMessageFromState(entry, chatMidHint, msgId);
          emitSessionRealtimeEvent(entry, {
            type: "message_unsend",
            chatMid,
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
