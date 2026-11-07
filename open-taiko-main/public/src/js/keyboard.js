class Keyboard{
	constructor(bindings, callback){
		this.bindings = bindings
		this.callback = callback
		this.wildcard = false
		this.substitute = {
			"up": "arrowup",
			"right": "arrowright",
			"down": "arrowdown",
			"left": "arrowleft",
			"space": " ",
			"esc": "escape",
			"ctrl": "control",
			"altgr": "altgraph"
		}
		this.btn = {}
		this.TaikoForceLv5 = false;
		this.update()
		pageEvents.keyAdd(this, "all", "both", this.keyEvent.bind(this))
		pageEvents.blurAdd(this, this.blurEvent.bind(this))
	}
	isTaikoForceLv5(kbdSettings){
		// the key of Taiko Force Lv5's PC module looks like this
		return (kbdSettings.ka_l[0] === "f") && (kbdSettings.ka_r[0] === "e") && (kbdSettings.don_l[0] === "i") && (kbdSettings.don_r[0] === "j");
	}
	update(){
		var kbdSettings = settings.getItem("keyboardSettings")
		this.TaikoForceLv5 = this.isTaikoForceLv5(kbdSettings)
		var drumKeys = {}
		for(var name in kbdSettings){
			var keys = kbdSettings[name]
			for(var i in keys){
				drumKeys[keys[i]] = name
			}
		}
		this.kbd = {}
		for(var name in this.bindings){
			var keys = this.bindings[name]
			for(var i in keys){
				var key = keys[i]
				if(key in drumKeys){
					continue
				}
				if(key in kbdSettings){
					var keyArray = kbdSettings[key]
					for(var j in keyArray){
						key = keyArray[j]
						if(!(key in this.kbd)){
							this.kbd[key] = name
						}
					}
				}else{
					if(key in this.substitute){
						key = this.substitute[key]
					}
					if(!(key in this.kbd)){
						if(key === "wildcard"){
							this.wildcard = true
						}
						this.kbd[key] = name
					}
				}
			}
		}
	}
	resolveKey(event){
		var raw = (event && typeof event.key === "string") ? event.key : ""
		if(raw.length === 1){
			var cc = raw.charCodeAt(0)
			if(cc >= 0xFF01 && cc <= 0xFF5E){
				raw = String.fromCharCode(cc - 0xFEE0)
			}else if(cc === 0x3000){
				raw = " "
			}
		}
		var key = raw.toLowerCase()
		if(key === "spacebar"){
			key = " "
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
		if(key && key !== "process" && key !== "unidentified" && (key in this.kbd)){
			return key
		}
		if(codeKey && ((codeKey in this.kbd) || !key || key === "process" || key === "unidentified")){
			return codeKey
		}
		return key || codeKey
	}
	keyEvent(event){
		var key = this.resolveKey(event)
		if(!key){
			return
		}
		if(
			key === "escape" ||
			key === "backspace" ||
			key === "tab" ||
			key === "contextmenu" ||
			key === "alt"
		){
			if(typeof event.preventDefault === "function"){
				event.preventDefault()
			}
		}
		if(!event.repeat){
			var pressed = event.type === "keydown"
			if(pressed){
				this.btn[key] = true
			}else{
				delete this.btn[key]
				if(key in this.kbd){
					for(var i in this.btn){
						if(this.kbd[i] === this.kbd[key]){
							return
						}
					}
				}
			}
			if(key in this.kbd){
				this.callback(pressed, this.kbd[key], event)
			}else if(this.wildcard){
				this.callback(pressed, this.kbd["wildcard"], event)
			}
		}
	}
	blurEvent(){
		for(var key in this.btn){
			if(this.btn[key]){
				delete this.btn[key]
				var name = this.kbd[key] || (this.wildcard ? "wildcard" : false)
				if(name){
					this.callback(false, name)
				}
			}
		}
	}
	clean(){
		pageEvents.keyRemove(this, "all")
		pageEvents.blurRemove(this)
		delete this.bindings
		delete this.callback
		delete this.kbd
		delete this.btn
	}
}
