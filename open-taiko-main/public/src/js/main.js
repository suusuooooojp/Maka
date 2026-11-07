addEventListener("error", function(err){
	var stack
	if("error" in err && err.error){
		stack = err.error.stack
	}else{
		stack = err.message + "\n    at " + err.filename + ":" + err.lineno + ":" + err.colno
	}
	errorMessage(stack)
})

function errorMessage(stack){
	localStorage["lastError"] = JSON.stringify({
		timestamp: Date.now(),
		stack: stack
	})
}

function toggleFullscreen(){
	if("requestFullscreen" in root){
		if(document.fullscreenElement){
			document.exitFullscreen()
		}else{
			root.requestFullscreen()
		}
	}else if("webkitRequestFullscreen" in root){
		if(document.webkitFullscreenElement){
			document.webkitExitFullscreen()
		}else{
			root.webkitRequestFullscreen()
		}
	}else if("mozRequestFullScreen" in root){
		if(document.mozFullScreenElement){
			document.mozCancelFullScreen()
		}else{
			root.mozRequestFullScreen()
		}
	}
}

function resizeRoot(){
	if(lastHeight !== innerHeight){
		lastHeight = innerHeight
		root.style.height = innerHeight + "px"
	}
}

function debug(){
	if(debugObj.state === "open"){
		debugObj.debug.clean()
		return "Debug closed"
	}else if(debugObj.state === "minimised"){
		debugObj.debug.restore()
		return "Debug restored"
	}else{
		debugObj.debug = new Debug()
		return "Debug opened"
	}
}

var root = document.documentElement

if(/iPhone|iPad/.test(navigator.userAgent)){
	var fullScreenSupported = false
}else{
	var fullScreenSupported = "requestFullscreen" in root || "webkitRequestFullscreen" in root || "mozRequestFullScreen" in root
}

var pageEvents = new PageEvents()
var snd = {}
var p2
var disableBlur = false
var cancelTouch = true
var lastHeight
var debugObj = {
	state: "closed",
	debug: null
}
var perf = {
	blur: 0,
	allImg: 0,
	load: 0
}
var defaultDon = {
	body_fill: "#5fb7c1",
	face_fill: "#ff5724"
}
var strings
var vectors
var settings
var scoreStorage
var account = {}
var gpicker
var db

let audioUnlocked = false;
let cachedCanvasEl = null;

const activateApp = () => {
	const sndBuffer = (window.snd && window.snd.buffer) ? window.snd.buffer : (window.assets && window.assets.sounds && window.assets.sounds.se_don && window.assets.sounds.se_don.soundBuffer) ? window.assets.sounds.se_don.soundBuffer : null;
	const isAudioSuspended = sndBuffer && sndBuffer.context && sndBuffer.context.state === "suspended";
	const isUnfocused = !document.hasFocus();

	if (!isAudioSuspended && audioUnlocked && !isUnfocused) {
		return;
	}

	try {
		if (isUnfocused) {
			window.focus();
		}
		if (!cachedCanvasEl) cachedCanvasEl = document.getElementById("canvas");
		if (cachedCanvasEl && document.activeElement !== cachedCanvasEl && document.activeElement && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA" && document.activeElement.tagName !== "SELECT") {
			cachedCanvasEl.focus();
		}
	} catch(e) {}

	try {
		if (sndBuffer && sndBuffer.context) {
			if (sndBuffer.context.state === "suspended") {
				sndBuffer.context.resume();
			}
			if (!audioUnlocked && sndBuffer.context.state === "running") {
				const buffer = sndBuffer.context.createBuffer(1, 1, 22050);
				const source = sndBuffer.context.createBufferSource();
				source.buffer = buffer;
				source.connect(sndBuffer.context.destination);
				source.start(0);
				audioUnlocked = true;
			}
		}
	} catch(e) {}
};

// Activate once on load and on discrete user interactions
activateApp();
["pointerdown", "touchstart", "keydown", "focusin", "click"].forEach(evtName => {
	window.addEventListener(evtName, activateApp, { passive: true });
});
pageEvents.add(root, ["touchstart", "touchmove", "touchend"], event => {
	if(event.cancelable && cancelTouch){
		if(event.target.tagName === "SELECT" || event.target.closest('#companion-toggle-btn') || event.target.closest('#companion-sidebar') || event.target.closest('#focus-live-badge') || event.target.closest('#game-fps-counter') || event.target.closest('.btn-fps-target') || event.target.closest('a') || event.target.closest('button')){
			return
		}
		event.preventDefault()
	}
})
var versionDiv = document.getElementById("version")
var versionLink = document.getElementById("version-link")
if (versionLink) {
	versionLink.tabIndex = -1
}
if (versionDiv && versionLink) {
	pageEvents.add(versionDiv, ["click", "touchend"], event => {
		if(event.target === versionDiv){
			versionLink.click()
			pageEvents.send("version-link")
		}
	})
}
resizeRoot()
setInterval(resizeRoot, 100)
pageEvents.keyAdd(debugObj, "all", "down", event => {
	if((event.keyCode === 186 || event.keyCode === 59) && event.ctrlKey && (event.shiftKey || event.altKey)){
		// Semicolon
		if(debugObj.state === "open"){
			debugObj.debug.minimise()
		}else if(debugObj.state === "minimised"){
			debugObj.debug.restore()
		}else{
			try{
				debugObj.debug = new Debug()
			}catch(e){}
		}
	}
	if(event.keyCode === 82 && debugObj.debug && debugObj.controller){
		// R
		debugObj.controller.restartSong()
	}
})

var loader = new Loader(songId => {
	new Titlescreen(songId)
})

