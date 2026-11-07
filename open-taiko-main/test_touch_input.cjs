const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const jsDir = fs.existsSync(path.join(__dirname, 'public/src/js')) ? path.join(__dirname, 'public/src/js') : path.join(__dirname, 'open-taiko-main/public/src/js');
const origJsDir = '/tmp/extracted_orig/open-taiko-main/public/src/js';

function createGameEnv(dirToLoad) {
  let nowMs = 1000;
  let audioPlayedCount = 0;
  const windowListeners = {};
  const docListeners = {};

  const sandbox = {
    window: null,
    document: {
      hidden: false,
      hasFocus: () => true,
      activeElement: { tagName: 'CANVAS' },
      addEventListener: (type, fn) => {
        docListeners[type] = docListeners[type] || [];
        docListeners[type].push(fn);
      },
      removeEventListener: (type, fn) => {
        if (docListeners[type]) {
          docListeners[type] = docListeners[type].filter(f => f !== fn);
        }
      },
      getElementById: () => ({
        classList: { add: () => {}, remove: () => {}, contains: () => false },
        style: { setProperty: () => {} },
        parentNode: { insertBefore: () => {}, appendChild: () => {}, removeChild: () => {} },
        appendChild: () => {},
        getBoundingClientRect: () => ({ left: 0, top: 0, width: 1280, height: 720 }),
        getContext: () => ({
          beginPath: () => {},
          ellipse: () => {},
          isPointInPath: () => true,
          save: () => {},
          restore: () => {},
          clearRect: () => {},
          fillRect: () => {},
          drawImage: () => {}
        })
      }),
      createElement: () => ({
        classList: { add: () => {}, remove: () => {} },
        style: { setProperty: () => {} },
        appendChild: () => {},
        getContext: () => ({})
      })
    },
    performance: {
      now: () => nowMs
    },
    Date: {
      now: () => 1700000000000 + nowMs
    },
    navigator: {
      userAgent: 'Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15',
      language: 'ja-JP',
      getGamepads: () => []
    },
    innerWidth: 1280,
    innerHeight: 720,
    lastHeight: 720,
    devicePixelRatio: 2,
    setInterval: () => 1,
    clearInterval: () => {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    requestAnimationFrame: () => 1,
    CustomEvent: function(name, opts) { this.type = name; this.detail = opts && opts.detail; },
    dispatchEvent: () => {},
    addEventListener: (type, fn) => {
      windowListeners[type] = windowListeners[type] || [];
      windowListeners[type].push(fn);
    },
    removeEventListener: (type, fn) => {
      if (windowListeners[type]) {
        windowListeners[type] = windowListeners[type].filter(f => f !== fn);
      }
    },
    settings: {
      getItem: (k) => {
        if (k === 'latency') return { audio: 0, video: 0, drumSounds: true };
        if (k === 'keyboardSettings') return { ka_l: ['d'], don_l: ['f'], don_r: ['j'], ka_r: ['k'] };
        if (k === 'gamepadLayout') return 'a';
        if (k === 'easierBigNotes') return false;
        if (k === 'touchAnimation') return true;
        return null;
      },
      setItem: () => {}
    },
    assets: {
      sounds: new Proxy({}, {
        get: () => ({
          play: () => { audioPlayedCount++; },
          stop: () => {},
          copy: () => ({ play: () => { audioPlayedCount++; }, stop: () => {} })
        })
      }),
      image: new Proxy({}, {
        get: () => ({ src: 'blob:mock', width: 100, height: 100 })
      }),
      songs: []
    },
    snd: {
      buffer: {
        getTime: () => nowMs / 1000,
        loadSettings: () => {},
        context: { state: 'running', resume: () => {} }
      },
      musicGain: { setVolumeMul: () => {} },
      sfxGain: {}, sfxGainL: {}, sfxGainR: {}
    },
    strings: {
      pauseOptions: ['演奏をつづける', 'はじめからやりなおす', '曲選択にもどる'],
      calibration: {},
      defaultName: '1P',
      default2PName: '2P',
      combo: 'コンボ'
    },
    loader: { changePage: () => {}, screen: { parentNode: { insertBefore: () => {}, appendChild: () => {} } } },
    versionDiv: { classList: { add: () => {}, remove: () => {} } },
    fullScreenSupported: false,
    toggleFullscreen: () => {},
    account: { loggedIn: false },
    defaultDon: { body_fill: '#000', face_fill: '#fff' },
    debugObj: {},
    p2: { session: false },
    Gamepad: class { play() {} clean() {} },
    CanvasDraw: class { clean() {} },
    ViewAssets: class {
      constructor() {
        this.don = { getAnimation: () => 'normal', setAnimation: () => {}, normal: () => {}, setUpdateSpeed: () => {} };
      }
      drawAssets() {}
      changeBeatInterval() {}
      clean() {}
    },
    CanvasCache: class { get() {} clean() {} },
     CanvasTest: class {}
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);

  for (const file of ['pageevents.js', 'circle.js', 'gamerules.js', 'keyboard.js', 'mekadon.js', 'gameinput.js', 'game.js', 'view.js']) {
    const code = fs.readFileSync(path.join(dirToLoad, file), 'utf8');
    vm.runInContext(code, sandbox, { filename: file });
  }
  vm.runInContext(`
    window.PageEvents = PageEvents;
    window.pageEvents = new PageEvents();
    window.Circle = Circle;
    window.GameRules = GameRules;
    window.Keyboard = Keyboard;
    window.Mekadon = Mekadon;
    window.GameInput = GameInput;
    window.Game = Game;
    window.View = View;
  `, sandbox);

  return {
    sandbox,
    setNow: (t) => { nowMs = t; },
    getNow: () => nowMs,
    getAudioPlayedCount: () => audioPlayedCount,
    resetAudioPlayedCount: () => { audioPlayedCount = 0; },
    dispatchWindowEvent: (type, ev) => {
      (windowListeners[type] || []).forEach(fn => fn(ev));
    },
    dispatchDocEvent: (type, ev) => {
      (docListeners[type] || []).forEach(fn => fn(ev));
    }
  };
}

function createSession(circlesConfig, difficulty = 'oni', dirToLoad = jsDir) {
  const env = createGameEnv(dirToLoad);
  const { sandbox, setNow } = env;

  const circles = circlesConfig.map((c, i) => new sandbox.Circle({
    id: i + 1,
    start: c.ms,
    type: c.type,
    txt: '',
    speed: 1,
    endTime: c.endTime !== undefined ? c.endTime : c.ms,
    requiredHits: c.requiredHits || 0
  }));

  const songData = {
    circles,
    measures: [],
    events: [],
    branches: null,
    beatInfo: { beatInterval: 500 },
    scoremode: 2,
    scoreinit: 1000,
    scorediff: 100
  };

  let soundPlayCalls = 0;
  const controller = {
    selectedSong: { title: 'Test Song', folder: 'test', difficulty },
    songData,
    parsedSongData: songData,
    autoPlayEnabled: false,
    saveScore: true,
    multiplayer: false,
    touchEnabled: true,
    audioLatency: 0,
    videoLatency: 0,
    offset: 0,
    easierBigNotes: false,
    drumSounds: true,
    snd: '',
    playedSounds: {},
    playSound: (id) => {
      if (typeof id === 'string' && id.indexOf('neiro_') === 0) {
        soundPlayCalls++;
      }
      env.sandbox.assets.sounds.neiro_1_don.play();
    },
    displayScore: () => {},
    togglePause: () => { controller.game.paused = !controller.game.paused; },
    getElapsedTime: () => controller.game.elapsedTime,
    getCircles: () => controller.game.getCircles(),
    getCurrentCircle: () => controller.game.getCurrentCircle(),
    getKeys: () => controller.keyboard.getKeys(),
    setKey: (pressed, name, ms, meta) => controller.keyboard.setKey(pressed, name, ms, meta),
    isWaiting: (key, type) => controller.keyboard.isWaiting(key, type),
    waitForKeyup: (key, type) => controller.keyboard.waitForKeyup(key, type),
    getKeyTime: () => controller.keyboard.getKeyTime(),
    getCombo: () => controller.game.getCombo(),
    getGlobalScore: () => controller.game.getGlobalScore()
  };

  controller.view = {
    pauseOptions: ['Continue', 'Restart', 'Quit'],
    pauseMove: () => {},
    pauseConfirm: () => {},
    gameDiv: { classList: { add: () => {}, remove: () => {} } },
    getMS: () => controller.game.elapsedTime,
    pointer: () => {},
    updateCombo: () => {},
    setDarkBg: () => {},
    drumroll: []
  };

  controller.game = new sandbox.Game(controller, controller.selectedSong, songData);
  controller.keyboard = new sandbox.GameInput(controller);
  controller.game.view = controller.view;
  controller.game.started = true;
  controller.game.startDate = 0;
  sandbox.debugObj = { controller };

  return {
    env,
    sandbox,
    controller,
    game: controller.game,
    input: controller.keyboard,
    circles,
    setNow,
    getSoundPlayCalls: () => soundPlayCalls
  };
}

// Helper to create a simulated Mobile Drum Pad using the exact logic from index.html
function createPadHarness(session) {
  const pads = {};
  for (const keyName of ['ka_l', 'don_l', 'don_r', 'ka_r']) {
    const activePointers = new Set();
    const activeTouches = new Set();
    const recentPointerTouches = [];
    const recordDuplicateBlocked = () => {
      if (session.input.inputStats) session.input.inputStats.duplicateBlocked++;
    };
    const firePadDown = (meta) => {
      const ms = session.game.getAccurateTime();
      session.input.setKey(false, keyName);
      session.input.setKey(true, keyName, ms, meta);
    };
    const firePadUpIfReleased = () => {
      if (activePointers.size === 0 && activeTouches.size === 0) {
        session.input.setKey(false, keyName);
      }
    };
    pads[keyName] = {
      pointerDown(pid = 1, x = 100, y = 500, pointerType = 'touch') {
        if (activePointers.has(pid)) {
          recordDuplicateBlocked();
          return false;
        }
        activePointers.add(pid);
        if (pointerType !== 'mouse') {
          const now = session.env.getNow();
          while (recentPointerTouches.length > 0 && now - recentPointerTouches[0].perfNow > 100) {
            recentPointerTouches.shift();
          }
          recentPointerTouches.push({ id: pid, x, y, perfNow: now });
        }
        firePadDown({ source: 'pointer', pointerId: pid });
        return true;
      },
      pointerUp(pid = 1) {
        activePointers.delete(pid);
        for (let i = recentPointerTouches.length - 1; i >= 0; i--) {
          if (recentPointerTouches[i].id === pid) recentPointerTouches.splice(i, 1);
        }
        firePadUpIfReleased();
      },
      pointerCancel(pid = 1) {
        this.pointerUp(pid);
      },
      touchStart(tid = 1, x = 100, y = 500) {
        const now = session.env.getNow();
        if (activeTouches.has(tid)) {
          recordDuplicateBlocked();
          return false;
        }
        let matchedIdx = -1;
        for (let j = 0; j < recentPointerTouches.length; j++) {
          const pt = recentPointerTouches[j];
          if (pt.id === tid || (now - pt.perfNow <= 35 && Math.abs(pt.x - x) <= 6 && Math.abs(pt.y - y) <= 6)) {
            matchedIdx = j;
            break;
          }
        }
        if (matchedIdx !== -1) {
          recentPointerTouches.splice(matchedIdx, 1);
          activeTouches.add(tid);
          recordDuplicateBlocked();
          return false;
        }
        activeTouches.add(tid);
        firePadDown({ source: 'touch', pointerId: tid });
        return true;
      },
      touchEnd(tid = 1) {
        activeTouches.delete(tid);
        activePointers.delete(tid);
        for (let j = recentPointerTouches.length - 1; j >= 0; j--) {
          if (recentPointerTouches[j].id === tid) recentPointerTouches.splice(j, 1);
        }
        firePadUpIfReleased();
      },
      touchCancel(tid = 1) {
        this.touchEnd(tid);
      },
      blur() {
        activePointers.clear();
        activeTouches.clear();
        recentPointerTouches.length = 0;
        firePadUpIfReleased();
      }
    };
  }
  return pads;
}

const results = [];

function recordTest(id, name, metrics) {
  results.push({ id, name, ...metrics });
}

// ============================================================================
// Test 1: 単発タップ100回 (100 single taps with sub-frame touchDown+touchUp)
// ============================================================================
{
  const notes = Array.from({ length: 100 }, (_, i) => ({ ms: 1000 + i * 200, type: 'don' }));
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let generated = 0;
  for (let i = 0; i < 100; i++) {
    const t = 1000 + i * 200;
    s.setNow(t);
    generated++;
    pads.don_l.pointerDown(1, 120, 500);
    s.setNow(t + 4); // Finger lifts 4ms later within same 16.67ms frame
    pads.don_l.pointerUp(1);
    s.setNow(t + 16); // rAF frame runs at t+16ms
    s.game.update();
  }
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 100, 'All 100 single taps must be judged GOOD');
  recordTest(1, '単発タップ100回 (同一フレーム内tap+release含む)', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 100 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 2: 3連打を連続100セット (100 sets of 3-note streams = 300 notes)
// ============================================================================
{
  const notes = [];
  for (let setIdx = 0; setIdx < 100; setIdx++) {
    const base = 1000 + setIdx * 500;
    const pattern = setIdx % 2 === 0 ? ['don', 'don', 'don'] : ['don', 'ka', 'don'];
    notes.push({ ms: base, type: pattern[0] });
    notes.push({ ms: base + 55, type: pattern[1] });
    notes.push({ ms: base + 110, type: pattern[2] });
  }
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let generated = 0;
  for (let setIdx = 0; setIdx < 100; setIdx++) {
    const base = 1000 + setIdx * 500;
    const keys = setIdx % 2 === 0 ? ['don_l', 'don_r', 'don_l'] : ['don_l', 'ka_l', 'don_r'];
    // Simulate frame hitch where first 2 taps of the 3連打 land in the same rAF frame!
    s.setNow(base);
    generated++;
    pads[keys[0]].pointerDown(1);
    pads[keys[0]].pointerUp(1);

    s.setNow(base + 55);
    generated++;
    pads[keys[1]].pointerDown(2);
    pads[keys[1]].pointerUp(2);

    // Delayed frame processes both taps together at base + 60ms
    s.setNow(base + 60);
    s.game.update();

    // 3rd tap of 3連打 at base + 110ms
    s.setNow(base + 110);
    generated++;
    pads[keys[2]].pointerDown(1);
    pads[keys[2]].pointerUp(1);
    s.setNow(base + 116);
    s.game.update();
  }
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 300, 'All 300 notes in 100x 3連打 sets must be judged GOOD');
  recordTest(2, '3連打×100セット (計300ノーツ・同一フレーム複数入力含む)', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 300 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 3: 交互の高速タップ1000入力 (1000 alternating high-speed taps at 20Hz)
// ============================================================================
{
  const notes = Array.from({ length: 1000 }, (_, i) => ({ ms: 1000 + i * 50, type: 'don' }));
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let generated = 0;
  let nextFrame = 1016;
  for (let i = 0; i < 1000; i++) {
    const t = 1000 + i * 50;
    const key = i % 2 === 0 ? 'don_l' : 'don_r';
    s.setNow(t);
    generated++;
    pads[key].pointerDown((i % 2) + 1);
    s.setNow(t + 5);
    pads[key].pointerUp((i % 2) + 1);
    while (nextFrame <= t + 50) {
      s.setNow(nextFrame);
      s.game.update();
      nextFrame += 16.67;
    }
  }
  s.setNow(nextFrame + 20);
  s.game.update();
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 1000, 'All 1000 alternating taps must be judged GOOD');
  recordTest(3, '交互高速タップ1000入力 (20Hz / 50ms間隔)', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 1000 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 4: ロール高速入力 (8Hz, 12Hz, 16Hz, 20Hz)
// ============================================================================
for (const hz of [8, 12, 16, 20]) {
  const durationSec = 5;
  const totalTaps = hz * durationSec;
  const intervalMs = 1000 / hz;
  const s = createSession([{ ms: 1000, endTime: 1000 + durationSec * 1000 + 100, type: 'drumroll' }]);
  const pads = createPadHarness(s);
  let generated = 0;
  let frameTime = 1000;
  for (let i = 0; i < totalTaps; i++) {
    const t = 1000 + i * intervalMs;
    const key = ['don_l', 'don_r', 'ka_l', 'ka_r'][i % 4];
    s.setNow(t);
    generated++;
    pads[key].pointerDown((i % 4) + 1);
    pads[key].pointerUp((i % 4) + 1);
    // Run frame every ~16.67ms
    while (frameTime <= t) {
      s.setNow(frameTime);
      s.game.update();
      frameTime += 16.67;
    }
  }
  s.setNow(frameTime + 20);
  s.game.update();
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.drumroll, totalTaps, `Drumroll at ${hz}Hz must register all ${totalTaps} hits`);
  recordTest(`4-${hz}Hz`, `ロール高速連打試験 (${hz}回/秒 × ${durationSec}秒 = ${totalTaps}打)`, {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    rollHits: s.game.globalScore.drumroll,
    errors: 0,
    passed: s.game.globalScore.drumroll === totalTaps && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 5: 左カッ・左ドン・右ドン・右カッ 4レーン連続交互入力 (400 inputs)
// ============================================================================
{
  const order = [
    { key: 'ka_l', type: 'ka' },
    { key: 'don_l', type: 'don' },
    { key: 'don_r', type: 'don' },
    { key: 'ka_r', type: 'ka' }
  ];
  const notes = Array.from({ length: 400 }, (_, i) => ({ ms: 1000 + i * 60, type: order[i % 4].type }));
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let generated = 0;
  for (let i = 0; i < 400; i++) {
    const t = 1000 + i * 60;
    const item = order[i % 4];
    s.setNow(t);
    generated++;
    pads[item.key].pointerDown((i % 4) + 1);
    pads[item.key].pointerUp((i % 4) + 1);
    s.setNow(t + 10);
    s.game.update();
  }
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 400, 'All 400 4-lane alternating inputs must be GOOD');
  recordTest(5, '左カッ・左ドン・右ドン・右カッ 4レーン連続入力 (400打)', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 400 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 6: 異なるレーンへの同時入力 (Simultaneous multi-lane: daiDon & daiKa)
// ============================================================================
{
  const notes = [];
  for (let i = 0; i < 50; i++) {
    notes.push({ ms: 1000 + i * 200, type: i % 2 === 0 ? 'daiDon' : 'daiKa' });
  }
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let generated = 0;
  for (let i = 0; i < 50; i++) {
    const t = 1000 + i * 200;
    s.setNow(t);
    if (i % 2 === 0) {
      // Simultaneous 2-finger hit on don_l (at t) and don_r (at t+5ms, after finger 1 already lifted at t+2ms!)
      generated += 2;
      pads.don_l.pointerDown(1);
      s.setNow(t + 2);
      pads.don_l.pointerUp(1);
      s.setNow(t + 5);
      pads.don_r.pointerDown(2);
      pads.don_r.pointerUp(2);
    } else {
      generated += 2;
      pads.ka_l.pointerDown(1);
      pads.ka_r.pointerDown(2);
      pads.ka_l.pointerUp(1);
      pads.ka_r.pointerUp(2);
    }
    s.setNow(t + 16);
    s.game.update();
  }
  const st = s.input.getInputStats();
  const allBigHit = s.circles.every(c => c.isPlayed === 2);
  assert.strictEqual(allBigHit, true, 'All 50 daiDon/daiKa notes must be judged as Big GOOD (isPlayed === 2)');
  recordTest(6, '異なるレーンへの同時入力 (大音符daiDon/daiKa 50個・100入力)', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    bigNotesPerfect: s.circles.filter(c => c.isPlayed === 2).length,
    errors: 0,
    passed: allBigHit && s.game.globalScore.good === 50 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 7: 同じPointer Events / 互換Touch Eventsの二重処理防止
// ============================================================================
{
  const notes = Array.from({ length: 50 }, (_, i) => ({ ms: 1000 + i * 200, type: 'don' }));
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let rawDispatched = 0;
  for (let i = 0; i < 50; i++) {
    const t = 1000 + i * 200;
    s.setNow(t);
    // 1. Primary pointerdown (pointerId = 1)
    rawDispatched++;
    pads.don_l.pointerDown(1, 120, 500, 'touch');
    // 2. Duplicate pointerdown with same active pointerId = 1 (should be blocked)
    rawDispatched++;
    pads.don_l.pointerDown(1, 120, 500, 'touch');
    // 3. Browser compatibility touchstart for the same touch at (120, 500) (should be blocked)
    rawDispatched++;
    pads.don_l.touchStart(999 + i, 120, 500);

    s.setNow(t + 5);
    pads.don_l.pointerUp(1);
    pads.don_l.touchEnd(999 + i);
    s.setNow(t + 16);
    s.game.update();
  }
  const st = s.input.getInputStats();
  assert.strictEqual(st.enqueuedEvents, 50, 'Only 50 genuine taps should be enqueued out of 150 raw events');
  assert.strictEqual(st.duplicateBlocked, 100, '100 duplicate pointer/touch events must be blocked');
  assert.strictEqual(s.game.globalScore.good, 50, 'All 50 notes judged GOOD without double consumption');
  recordTest(7, '同一Pointer/互換Touchイベントの二重処理防止検証', {
    generatedEvents: rawDispatched,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: st.enqueuedEvents === 50 && st.duplicateBlocked === 100 && s.game.globalScore.good === 50
  });
}

// ============================================================================
// Test 8: pointerup / pointercancel / touchcancel 後の即時復帰検証
// ============================================================================
{
  const notes = Array.from({ length: 60 }, (_, i) => ({ ms: 1000 + i * 100, type: 'don' }));
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let generated = 0;
  for (let i = 0; i < 60; i++) {
    const t = 1000 + i * 100;
    s.setNow(t);
    generated++;
    if (i % 3 === 0) {
      pads.don_l.pointerDown(1, 100, 500);
      pads.don_l.pointerUp(1);
    } else if (i % 3 === 1) {
      pads.don_l.pointerDown(1, 100, 500);
      pads.don_l.pointerCancel(1); // Abrupt pointercancel (e.g. palm rejection / notification)
    } else {
      pads.don_l.touchStart(1, 100, 500);
      pads.don_l.touchCancel(1); // Abrupt touchcancel
    }
    s.setNow(t + 10);
    s.game.update();
  }
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 60, 'All 60 taps across pointerup/pointercancel/touchcancel must succeed');
  recordTest(8, 'pointerup / pointercancel / touchcancel 後の入力復帰検証', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 60 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 9: タブ切替 (visibilitychange) / フォーカス喪失 (blur) 後の固着防止検証
// ============================================================================
{
  const notes = Array.from({ length: 40 }, (_, i) => ({ ms: 1000 + i * 100, type: 'don' }));
  const s = createSession(notes);
  const pads = createPadHarness(s);
  let generated = 0;
  for (let i = 0; i < 40; i++) {
    const t = 1000 + i * 100;
    s.setNow(t);
    generated++;
    pads.don_l.pointerDown(1);
    pads.don_r.pointerDown(2);
    // Simulate sudden blur or visibilitychange WITHOUT pointerup/keyup!
    if (i % 2 === 0) {
      s.env.dispatchWindowEvent('blur', {});
      pads.don_l.blur();
      pads.don_r.blur();
    } else {
      s.sandbox.document.hidden = true;
      s.env.dispatchDocEvent('visibilitychange', {});
      s.sandbox.document.hidden = false;
      pads.don_l.blur();
      pads.don_r.blur();
    }
    // Verify keys are released after blur/visibilitychange
    assert.strictEqual(Boolean(s.input.keys['don_l']), false, 'don_l must not stick after blur');
    assert.strictEqual(Boolean(s.input.keys['don_r']), false, 'don_r must not stick after blur');
    s.setNow(t + 10);
    s.game.update();
  }
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 40, 'All 40 notes across blur/visibilitychange cycles must be judged GOOD');
  recordTest(9, 'タブ切替 (visibilitychange) / フォーカス喪失 (blur) 後の復帰検証', {
    generatedEvents: generated * 2,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 40 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 10: 音源再生中の高速連打と効果音重複防止検証
// ============================================================================
{
  const notes = Array.from({ length: 200 }, (_, i) => ({ ms: 1000 + i * 50, type: i % 2 === 0 ? 'don' : 'ka' }));
  const s = createSession(notes);
  s.game.mainMusicPlaying = true;
  const pads = createPadHarness(s);
  s.env.resetAudioPlayedCount();
  let generated = 0;
  for (let i = 0; i < 200; i++) {
    const t = 1000 + i * 50;
    const key = i % 2 === 0 ? 'don_l' : 'ka_l';
    s.setNow(t);
    generated++;
    pads[key].pointerDown(1);
    pads[key].pointerUp(1);
    s.setNow(t + 10);
    s.game.update();
  }
  const st = s.input.getInputStats();
  const sfxCount = s.getSoundPlayCalls();
  assert.strictEqual(s.game.globalScore.good, 200, 'All 200 notes during audio playback must be judged GOOD');
  assert.strictEqual(sfxCount, 200, 'Exactly 1 sound effect play call per tap (no double SFX)');
  recordTest(10, '音源再生中の高速連打 (200打) と効果音単一再生検証', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    sfxPlayCount: sfxCount,
    errors: 0,
    passed: s.game.globalScore.good === 200 && sfxCount === 200 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 11: 複合実戦譜面 (3連打 + 大音符 + ロール + 風船連打) E2E判定経路検証
// ============================================================================
{
  const chart = [
    // 3連打 #1 (Don-Don-Don)
    { ms: 1000, type: 'don' },
    { ms: 1060, type: 'don' },
    { ms: 1120, type: 'don' },
    // 3連打 #2 (Ka-Don-Ka)
    { ms: 1300, type: 'ka' },
    { ms: 1360, type: 'don' },
    { ms: 1420, type: 'ka' },
    // Big Note daiDon + daiKa
    { ms: 1600, type: 'daiDon' },
    { ms: 1800, type: 'daiKa' },
    // Drumroll (2000ms - 2500ms) -> 10 hits at 20Hz
    { ms: 2000, endTime: 2500, type: 'drumroll' },
    // Post-roll 3連打 immediately after drumroll at 2600, 2660, 2720
    { ms: 2600, type: 'don' },
    { ms: 2660, type: 'ka' },
    { ms: 2720, type: 'don' },
    // Balloon (3000ms - 3500ms, requiredHits: 5)
    { ms: 3000, endTime: 3500, type: 'balloon', requiredHits: 5 },
    // Final note at 3600ms
    { ms: 3600, type: 'don' }
  ];
  const s = createSession(chart);
  const pads = createPadHarness(s);
  let generated = 0;

  const tap = (t, key, pid = 1) => {
    s.setNow(t);
    generated++;
    pads[key].pointerDown(pid);
    pads[key].pointerUp(pid);
  };

  // Hit 3連打 #1 (simulate frame hitch across all 3 notes!)
  tap(1000, 'don_l', 1);
  tap(1060, 'don_r', 2);
  tap(1120, 'don_l', 1);
  s.setNow(1125);
  s.game.update();

  // Hit 3連打 #2
  tap(1300, 'ka_l', 1);
  tap(1360, 'don_l', 2);
  tap(1420, 'ka_r', 1);
  s.setNow(1425);
  s.game.update();

  // Hit daiDon (1600ms) and daiKa (1800ms)
  tap(1600, 'don_l', 1);
  tap(1605, 'don_r', 2);
  s.setNow(1610);
  s.game.update();

  tap(1800, 'ka_l', 1);
  tap(1804, 'ka_r', 2);
  s.setNow(1810);
  s.game.update();

  // Drumroll 10 hits (2020ms .. 2470ms)
  for (let i = 0; i < 10; i++) {
    tap(2020 + i * 45, i % 2 === 0 ? 'don_l' : 'don_r', (i % 2) + 1);
  }
  s.setNow(2480);
  s.game.update();

  // Post-roll 3連打 (2600, 2660, 2720) — verifies roll auto-closes via advanceExpiredCircles!
  tap(2600, 'don_l', 1);
  tap(2660, 'ka_l', 2);
  tap(2720, 'don_r', 1);
  s.setNow(2730);
  s.game.update();

  // Balloon 5 hits (3050 .. 3250)
  for (let i = 0; i < 5; i++) {
    tap(3050 + i * 40, i % 2 === 0 ? 'don_l' : 'don_r', 1);
  }
  s.setNow(3300);
  s.game.update();

  // Final note at 3600ms
  tap(3600, 'don_l', 1);
  s.setNow(3610);
  s.game.update();

  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 12, 'All 12 normal/big notes in composite chart must be GOOD');
  assert.strictEqual(s.game.globalScore.drumroll, 15, 'All 10 drumroll + 5 balloon hits must be counted');
  recordTest(11, '実戦複合譜面 (3連打+大音符+ロール+ロール直後3連打+風船) 検証', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    rollHits: s.game.globalScore.drumroll,
    errors: 0,
    passed: s.game.globalScore.good === 12 && s.game.globalScore.drumroll === 15 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 12: マウス・キーボード・Canvasタッチ操作の回帰テスト
// ============================================================================
{
  const notes = [
    { ms: 1000, type: 'ka' },
    { ms: 1100, type: 'don' },
    { ms: 1200, type: 'don' },
    { ms: 1300, type: 'ka' },
    { ms: 1400, type: 'don' },
    { ms: 1500, type: 'ka' }
  ];
  const s = createSession(notes);
  s.controller.touchEnabled = false; // Start with touchEnabled = false to verify dynamic canvas touch activation!
  const view = new s.sandbox.View(s.controller);
  s.controller.view = view;
  s.game.view = view;
  view.winW = 1280;
  view.winH = 720;
  view.pixelRatio = 2;
  view.ratio = 1;
  view.touchCircle = { x: 640, y: 720, rx: 320, ry: 300 };

  let generated = 0;
  // 1-4: Keyboard presses (D, F, J, K)
  for (const [t, keyChar, code] of [[1000, 'd', 68], [1100, 'f', 70], [1200, 'j', 74], [1300, 'k', 75]]) {
    s.setNow(t);
    generated++;
    s.sandbox.pageEvents.keyEvent({ type: 'keydown', key: keyChar, keyCode: code, preventDefault: () => {} });
    s.setNow(t + 5);
    s.sandbox.pageEvents.keyEvent({ type: 'keyup', key: keyChar, keyCode: code, preventDefault: () => {} });
    s.setNow(t + 10);
    s.game.update();
  }

  // 5-6: Canvas touch when touchEnabled was initially false (inside left half of drum ellipse -> don_l, top right corner -> ka_r)
  s.setNow(1400);
  generated++;
  view.ontouch({
    cancelable: true,
    preventDefault: () => {},
    changedTouches: [{ identifier: 10, clientX: 500 / view.pixelRatio, clientY: 600 / view.pixelRatio }]
  });
  s.setNow(1410);
  s.game.update();

  s.setNow(1500);
  generated++;
  view.ontouch({
    cancelable: true,
    preventDefault: () => {},
    changedTouches: [{ identifier: 11, clientX: (view.winW - 10) / view.pixelRatio, clientY: 10 }]
  });
  s.setNow(1510);
  s.game.update();

  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 6, 'All 4 keyboard inputs + 2 canvas touch inputs must be judged GOOD');
  assert.strictEqual(view.touchEnabled, true, 'Canvas touch must dynamically enable touchEnabled');
  recordTest(12, 'キーボード(D/F/J/K)・マウス・Canvasタッチ回帰検証', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 6 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 13: モバイル高DPI (390x844 @3x 縦画面/横画面) ドン・カッ太鼓位置判定 & pointerdown重複排除検証
// ============================================================================
{
  const notes = [
    { ms: 1000, type: 'don' }, // don_l (center-left of portrait drum)
    { ms: 1020, type: 'don' }, // don_l (fast 20ms follow-up tap at the exact same spot!)
    { ms: 1100, type: 'don' }, // don_r (center-right of portrait drum)
    { ms: 1200, type: 'ka' },  // ka_l  (left rim/edge of portrait drum)
    { ms: 1300, type: 'ka' },  // ka_r  (right rim/edge of portrait drum)
    { ms: 1400, type: 'ka' },  // ka_l  (upper-left above drum)
    { ms: 1500, type: 'ka' }   // ka_r  (upper-right above drum)
  ];
  const s = createSession(notes);
  s.sandbox.innerWidth = 390;
  s.sandbox.innerHeight = 844;
  s.sandbox.lastHeight = 844;
  s.sandbox.devicePixelRatio = 3;
  const view = new s.sandbox.View(s.controller);
  s.controller.view = view;
  s.game.view = view;

  // Simulate View.refresh() geometry calculation for 390x844 @3x portrait
  const winWPhys = 390 * 3; // 1170
  const winHPhys = 844 * 3; // 2532
  const ratio = winWPhys / 720; // 1.625
  view.portrait = true;
  view.pixelRatio = 3;
  view.winW = winWPhys;
  view.winH = winHPhys;
  view.ratio = ratio;
  const winW = winWPhys / ratio; // 720
  const winH = winHPhys / ratio; // 1558.15
  const frameTop = winH / 2 - 1280 / 2;
  const yStart = frameTop + 477;
  const maxH = winH - yStart;
  const drumSize = Math.min(winW * 0.94, maxH * 0.86);
  const bottomMargin = Math.max(18, (maxH - drumSize) * 0.32);
  const drumY = Math.max(yStart + 10, winH - drumSize - bottomMargin);
  view.touchDrum = { x: (winW - drumSize) / 2, y: drumY, w: drumSize, h: drumSize };
  view.touchCircle = {
    x: winW / 2,
    y: view.touchDrum.y + view.touchDrum.h / 2,
    rx: drumSize * 0.38,
    ry: drumSize * 0.38
  };

  let generated = 0;
  // On 390x844 CSS px, drum center is at (195, 645), Don radius is ~139px, Ka rim is outside ~139px
  const tapPoints = [
    [1000, 140, 645, 201], // Center-left of round drum -> don_l
    [1020, 140, 645, 202], // Fast 20ms double-tap at exact same spot -> don_l (must NOT be swallowed!)
    [1100, 250, 645, 203], // Center-right of round drum -> don_r
    [1200, 30,  645, 204], // Left outer rim -> ka_l
    [1300, 360, 645, 205], // Right outer rim -> ka_r
    [1400, 120, 480, 206], // Upper-left rim above Don face -> ka_l
    [1500, 270, 480, 207]  // Upper-right rim above Don face -> ka_r
  ];

  for (const [t, clientX, clientY, pid] of tapPoints) {
    s.setNow(t);
    generated++;
    // Fire both pointerdown AND compatibility touchstart to verify low-latency pointerdown + zero duplicate firing
    view.onpointerdown({
      type: 'pointerdown',
      pointerType: 'touch',
      pointerId: pid,
      clientX,
      clientY,
      cancelable: true,
      preventDefault: () => {}
    });
    view.ontouch({
      type: 'touchstart',
      cancelable: true,
      preventDefault: () => {},
      changedTouches: [{ identifier: pid, clientX, clientY }]
    });
    s.setNow(t + 5);
    view.onpointerup({
      type: 'pointerup',
      pointerType: 'touch',
      pointerId: pid
    });
    s.setNow(t + 8);
    s.game.update();
  }

  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 7, 'All 7 mobile portrait Don/Ka taps (including 20ms double-tap) must hit the exact intended note');
  assert.strictEqual(st.enqueuedEvents, 7, 'pointerdown + compatibility touchstart must enqueue exactly 7 events (no duplicates, no drops)');
  recordTest(13, 'モバイル高DPI(390x844 @3x) ドン・カッ太鼓位置判定 & pointerdown検証', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: 7,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 7 && st.enqueuedEvents === 7 && st.droppedEvents === 0
  });
}

// ============================================================================
// Test 14: PC日本語IME (key='Process' + code='KeyD/F/J/K') & 全角キー入力検証
// ============================================================================
{
  const notes = [
    { ms: 1000, type: 'ka' },  // Process + KeyD
    { ms: 1100, type: 'don' }, // Process + KeyF
    { ms: 1200, type: 'don' }, // Process + KeyJ
    { ms: 1300, type: 'ka' },  // Process + KeyK
    { ms: 1400, type: 'don' }, // 全角 'ｆ'
    { ms: 1500, type: 'ka' }   // 全角 'ｋ'
  ];
  const s = createSession(notes);
  let generated = 0;
  const keySeq = [
    [1000, 'Process', 'KeyD', 229],
    [1100, 'Process', 'KeyF', 229],
    [1200, 'Process', 'KeyJ', 229],
    [1300, 'Process', 'KeyK', 229],
    [1400, 'ｆ', 'KeyF', 70],
    [1500, 'ｋ', 'KeyK', 75]
  ];
  for (const [t, k, code, kc] of keySeq) {
    s.setNow(t);
    generated++;
    s.env.dispatchWindowEvent('keydown', { type: 'keydown', key: k, code: code, keyCode: kc, repeat: false, target: { tagName: 'CANVAS' }, preventDefault: () => {} });
    s.setNow(t + 5);
    s.env.dispatchWindowEvent('keyup', { type: 'keyup', key: k, code: code, keyCode: kc, repeat: false, target: { tagName: 'CANVAS' }, preventDefault: () => {} });
    s.setNow(t + 10);
    s.game.update();
  }
  const st = s.input.getInputStats();
  assert.strictEqual(s.game.globalScore.good, 6, 'All 6 IME Process / full-width keyboard inputs must be judged GOOD');
  recordTest(14, 'PC日本語IME (Process + KeyD/F/J/K) & 全角キー入力検証', {
    generatedEvents: generated,
    receivedEvents: st.receivedEvents,
    enqueuedEvents: st.enqueuedEvents,
    judgedEvents: st.judgedEvents,
    duplicateBlocked: st.duplicateBlocked,
    droppedEvents: st.droppedEvents,
    avgLatencyMs: Number((st.totalLatencyMs / st.judgedEvents).toFixed(2)),
    maxLatencyMs: st.maxLatencyMs,
    notesGood: s.game.globalScore.good,
    errors: 0,
    passed: s.game.globalScore.good === 6 && st.droppedEvents === 0
  });
}

console.table(results.map(r => ({
  ID: r.id,
  Test: r.name,
  Gen: r.generatedEvents,
  Recv: r.receivedEvents,
  Queue: r.enqueuedEvents,
  Judged: r.judgedEvents,
  DupBlocked: r.duplicateBlocked,
  Dropped: r.droppedEvents,
  MaxLatMs: r.maxLatencyMs,
  Pass: r.passed ? 'PASS' : 'FAIL'
})));

const allPassed = results.every(r => r.passed);
if (!allPassed) {
  console.error('Some tests failed!');
  process.exit(1);
}
console.log('ALL 17 AUTOMATED TEST SUITES PASSED!');
