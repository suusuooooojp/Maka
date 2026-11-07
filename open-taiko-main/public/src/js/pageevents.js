class PageEvents{
	constructor(){
		this.allEvents = new Map()
		this.keyListeners = new Map()
		this.mouseListeners = new Map()
		this.blurListeners = new Map()
		this.lastKeyEvent = -Infinity
		this.add(window, "keydown", this.keyEvent.bind(this), undefined, true)
		this.add(window, "keyup", this.keyEvent.bind(this), undefined, true)
		this.add(window, "mousemove", this.mouseEvent.bind(this))
		this.add(window, "blur", this.blurEvent.bind(this))
		if(typeof document !== "undefined"){
			this.add(document, "visibilitychange", event => {
				if(document.hidden){
					this.blurEvent(event)
				}
			})
		}
		this.kbd = []
	}
	add(target, type, callback, symbol, options){
		if(Array.isArray(type)){
			type.forEach(type => this.add(target, type, callback, symbol, options))
			return
		}
		if(!target || typeof target.addEventListener !== "function"){
			return
		}
		this.remove(target, type, symbol)
		var addedEvent = this.allEvents.get(symbol || target)
		if(!addedEvent){
			addedEvent = new Map()
			this.allEvents.set(symbol || target, addedEvent)
		}
		var listenerOptions = options
		if(listenerOptions === undefined && (type === "touchstart" || type === "touchend")){
			listenerOptions = {passive: false}
		}
		addedEvent.set(type, {callback: callback, options: listenerOptions})
		return listenerOptions !== undefined
			? target.addEventListener(type, callback, listenerOptions)
			: target.addEventListener(type, callback)
	}
	remove(target, type, symbol){
		if(Array.isArray(type)){
			type.forEach(type => this.remove(target, type, symbol))
			return
		}
		var addedEvent = this.allEvents.get(symbol || target)
		if(addedEvent){
			var entry = addedEvent.get(type)
			var callback = entry && typeof entry === "object" ? entry.callback : entry
			var options = entry && typeof entry === "object" ? entry.options : undefined
			if(callback){
				if(target && typeof target.removeEventListener === "function"){
					if(options !== undefined){
						target.removeEventListener(type, callback, options)
					}else{
						target.removeEventListener(type, callback)
					}
				}
				addedEvent.delete(type)
				if(addedEvent.size == 0){
					return this.allEvents.delete(symbol || target)
				}
			}
		}
	}
	once(target, type, symbol){
		return new Promise(resolve => {
			this.add(target, type, event => {
				this.remove(target, type)
				return resolve(event)
			}, symbol)
		})
	}
	race(){
		var symbols = []
		var target = arguments[0]
		return new Promise(resolve => {
			for(var i = 1;i < arguments.length; i++){
				symbols[i] = Symbol()
				let type = arguments[i]
				this.add(target, type, event => {
					resolve({
						type: type,
						event: event
					})
				}, symbols[i])
			}
		}).then(response => {
			for(var i = 1;i < arguments.length; i++){
				this.remove(target, arguments[i], symbols[i])
			}
			return response
		})
	}
	load(target){
		return new Promise((resolve, reject) => {
			this.race(target, "load", "error", "abort").then(response => {
				switch(response.type){
					case "load":
						return resolve(response.event)
					case "error":
						return reject(["Loading error", target])
					case "abort":
						return reject("Loading aborted")
				}
			})
		})
	}
	isEditableTarget(el){
		if(!el || typeof el !== "object"){
			return false
		}
		if(el.isContentEditable){
			return true
		}
		var tag = (el.tagName || "").toUpperCase()
		if(tag === "TEXTAREA" || tag === "SELECT"){
			return true
		}
		if(tag === "INPUT"){
			var t = (el.type || "text").toLowerCase()
			return t !== "button" && t !== "checkbox" && t !== "radio" && t !== "range" && t !== "submit" && t !== "reset" && t !== "color" && t !== "file"
		}
		return false
	}
	normalizeEventKey(event){
		var raw = (event && typeof event.key === "string") ? event.key : ""
		if(raw.length === 1){
			var cc = raw.charCodeAt(0)
			if(cc >= 0xFF01 && cc <= 0xFF5E){
				raw = String.fromCharCode(cc - 0xFEE0)
			}else if(cc === 0x3000){
				raw = " "
			}
		}
		var k = raw.toLowerCase()
		if(k === "spacebar"){
			k = " "
		}
		var code = (event && typeof event.code === "string") ? event.code : ""
		var codeKey = ""
		if(code.length === 4 && code.startsWith("Key")){
			codeKey = code.slice(3).toLowerCase()
		}else if(code.length === 6 && code.startsWith("Digit")){
			codeKey = code.slice(5)
		}else if(code === "Space"){
			codeKey = " "
		}else if(code === "Enter" || code === "NumpadEnter"){
			codeKey = "enter"
		}else if(code === "Escape"){
			codeKey = "escape"
		}else if(code === "Backspace"){
			codeKey = "backspace"
		}else if(code === "Tab"){
			codeKey = "tab"
		}else if(code === "ArrowUp" || code === "ArrowDown" || code === "ArrowLeft" || code === "ArrowRight"){
			codeKey = code.toLowerCase()
		}else if(code === "ShiftLeft" || code === "ShiftRight"){
			codeKey = "shift"
		}else if(code === "ControlLeft" || code === "ControlRight"){
			codeKey = "control"
		}else if(code === "AltLeft" || code === "AltRight"){
			codeKey = "alt"
		}
		if(!codeKey && event && typeof event.keyCode === "number"){
			var kc = event.keyCode
			if(kc >= 65 && kc <= 90){
				codeKey = String.fromCharCode(kc + 32)
			}else if(kc >= 48 && kc <= 57){
				codeKey = String.fromCharCode(kc)
			}else if(kc === 13){
				codeKey = "enter"
			}else if(kc === 32){
				codeKey = " "
			}else if(kc === 27){
				codeKey = "escape"
			}else if(kc === 8){
				codeKey = "backspace"
			}else if(kc === 37){
				codeKey = "arrowleft"
			}else if(kc === 38){
				codeKey = "arrowup"
			}else if(kc === 39){
				codeKey = "arrowright"
			}else if(kc === 40){
				codeKey = "arrowdown"
			}
		}
		if(!k || k === "process" || k === "unidentified" || (this.kbd.indexOf(k) === -1 && codeKey && this.kbd.indexOf(codeKey) !== -1)){
			return codeKey || k
		}
		return k
	}
	keyEvent(event){
		if(!event || (!("key" in event) && !("code" in event) && !("keyCode" in event))){
			return
		}
		var target = event.target || (typeof document !== "undefined" ? document.activeElement : null)
		if(this.isEditableTarget(target)){
			return
		}
		var normKey = this.normalizeEventKey(event)
		if(event.ctrlKey && (normKey === "c" || normKey === "x" || normKey === "v")){
			return
		}
		if(this.kbd.indexOf(normKey) !== -1){
			this.lastKeyEvent = Date.now()
			if(typeof event.preventDefault === "function"){
				event.preventDefault()
			}
		}else if((normKey === " " || normKey === "enter") && target && target.tagName && (target.tagName.toUpperCase() === "BUTTON" || target.tagName.toUpperCase() === "INPUT")){
			if(typeof event.preventDefault === "function"){
				event.preventDefault()
			}
		}
		this.keyListeners.forEach(addedKeyCode => {
			this.checkListener(addedKeyCode.get("all"), event)
			this.checkListener(addedKeyCode.get(event.keyCode), event)
		})
	}
	checkListener(keyObj, event){
		if(keyObj && (
			keyObj.type === "both"
			|| keyObj.type === "down" && event.type === "keydown"
			|| keyObj.type === "up" && (event.type === "keyup" || event.type === "up")
		)){
			keyObj.callback(event)
		}
	}
	keyAdd(target, keyCode, type, callback){
		// keyCode="all", type="both"
		var addedKeyCode = this.keyListeners.get(target)
		if(!addedKeyCode){
			addedKeyCode = new Map()
			this.keyListeners.set(target, addedKeyCode)
		}
		addedKeyCode.set(keyCode, {
			type: type,
			callback: callback
		})
	}
	keyRemove(target, keyCode){
		var addedKeyCode = this.keyListeners.get(target)
		if(addedKeyCode){
			var keyObj = addedKeyCode.get(keyCode)
			if(keyObj){
				addedKeyCode.delete(keyCode)
				if(addedKeyCode.size == 0){
					return this.keyListeners.delete(target)
				}
			}
		}
	}
	keyOnce(target, keyCode, type){
		return new Promise(resolve => {
			this.keyAdd(target, keyCode, type, event => {
				this.keyRemove(target, keyCode)
				return resolve(event)
			})
		})
	}
	mouseEvent(event){
		this.lastMouse = event
		this.mouseListeners.forEach(callback => callback(event))
	}
	mouseAdd(target, callback){
		this.mouseListeners.set(target, callback)
	}
	mouseRemove(target){
		this.mouseListeners.delete(target)
	}
	blurEvent(event){
		this.blurListeners.forEach(callback => callback(event))
	}
	blurAdd(target, callback){
		this.blurListeners.set(target, callback)
	}
	blurRemove(target){
		this.blurListeners.delete(target)
	}
	getMouse(){
		return this.lastMouse
	}
	send(name, detail){
		dispatchEvent(new CustomEvent(name, {detail: detail}))
	}
	setKbd(){
		this.kbd = []
		var kbdSettings = settings.getItem("keyboardSettings")
		for(var name in kbdSettings){
			var keys = kbdSettings[name]
			for(var i in keys){
				this.kbd.push(keys[i])
			}
		}
	}
}
