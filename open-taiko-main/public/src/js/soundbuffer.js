class SoundBuffer{
	constructor(){
		var AudioContext = window.AudioContext || window.webkitAudioContext
		try{
			this.context = new AudioContext({ latencyHint: "interactive" })
		}catch(e){
			try {
				this.context = new AudioContext()
			} catch(e2) {
				console.warn("AudioContext unavailable, using dummy context:", e2)
				this.context = {
					state: "suspended",
					currentTime: 0,
					destination: {},
					resume: function() { return Promise.resolve(); },
					createGain: function() { return { gain: { value: 1, linearRampToValueAtTime: function() {} }, connect: function() {} }; },
					createChannelMerger: function() { return { connect: function() {} }; },
					createBuffer: function() { return { duration: 0 }; },
					createBufferSource: function() { return { connect: function() {}, start: function() {}, stop: function() {} }; },
					decodeAudioData: function(buf, success, error) {
						var dummy = { duration: 0 };
						if (success) success(dummy);
						return Promise.resolve(dummy);
					}
				}
			}
		}
		this.audioDecoder = this.context.decodeAudioData.bind(this.context)
		this.oggDecoder = this.audioDecoder
		pageEvents.add(window, ["click", "touchend", "keypress"], this.pageClicked.bind(this))
		const resumeAudio = () => {
			if(this.context && this.context.state === "suspended"){
				this.context.resume()
			}
		}
		window.addEventListener("pointerdown", resumeAudio, { passive: true })
		window.addEventListener("touchstart", resumeAudio, { passive: true })
		this.gainList = []
	}
	load(file, gain){
		return file.arrayBuffer().then(response => {
			var decoder = file.name.endsWith(".ogg") ? this.oggDecoder : this.audioDecoder
			var tryDecode = (buf, dec) => {
				return new Promise((resolve, reject) => {
					try {
						var res = dec(buf, resolve, reject)
						if (res && typeof res.then === "function") {
							res.then(resolve, reject)
						}
					} catch(e) {
						reject(e)
					}
				})
			}
			var copy1 = response.slice(0)
			return tryDecode(copy1, decoder).catch(err => {
				var altDecoder = (decoder === this.oggDecoder) ? this.audioDecoder : this.oggDecoder
				if (altDecoder && altDecoder !== decoder) {
					var copy2 = response.slice(0)
					return tryDecode(copy2, altDecoder)
				}
				throw err
			}).catch(err2 => {
				if (this.fallbackDecoder && decoder !== this.fallbackDecoder) {
					var copy3 = response.slice(0)
					return tryDecode(copy3, this.fallbackDecoder)
				}
				throw err2
			}).catch(err3 => {
				if (file.url && file.url.endsWith(".ogg")) {
					var wavUrl = file.url.slice(0, -4) + ".wav"
					return fetch(wavUrl).then(r => {
						if (!r.ok) throw err3
						return r.arrayBuffer()
					}).then(wavBuf => tryDecode(wavBuf, this.audioDecoder))
				}
				throw err3
			}).catch(error => {
				console.warn("Audio decoding fallback to silent buffer for", file.url, error)
				var silentBuf = this.context.createBuffer(1, 4410, 44100)
				silentBuf._isSilentFallback = true
				return silentBuf
			})
		}).catch(fetchErr => {
			console.warn("Audio fetch fallback to silent buffer for", file.url, fetchErr)
			var silentBuf = this.context.createBuffer(1, 4410, 44100)
			silentBuf._isSilentFallback = true
			return silentBuf
		}).then(buffer => {
			var sndObj = new Sound(gain || {soundBuffer: this}, buffer)
			if(buffer && buffer._isSilentFallback){
				sndObj._isSilentFallback = true
			}
			return sndObj
		})
	}
	createGain(channel){
		var gain = new SoundGain(this, channel)
		this.gainList.push(gain)
		return gain
	}
	setCrossfade(gain1, gain2, median){
		if(!Array.isArray(gain1)){
			gain1 = [gain1]
		}
		if(!Array.isArray(gain2)){
			gain2 = [gain2]
		}
		gain1.forEach(gain => gain.setCrossfade(1 - median))
		gain2.forEach(gain => gain.setCrossfade(median))
	}
	getTime(){
		return this.context.currentTime
	}
	convertTime(time, absolute){
		time = (time || 0)
		if(time < 0){
			time = 0
		}
		return time + (absolute ? 0 : this.getTime())
	}
	createSource(sound){
		var source = this.context.createBufferSource()
		source.buffer = sound.buffer
		var dest = (sound.gain && sound.gain.gainNode) ? sound.gain.gainNode : this.context.destination
		var vol = (sound && sound.volume !== undefined) ? sound.volume : 1
		if(vol !== 1){
			var gainNode = this.context.createGain()
			gainNode.gain.value = vol * vol
			source.connect(gainNode)
			gainNode.connect(dest)
		}else{
			source.connect(dest)
		}
		if (sound.duration > 15.0 && window.taikoPlaybackRate) {
			source.playbackRate.value = window.taikoPlaybackRate;
		}
		return source
	}
	pageClicked(){
		if(this.context.state === "suspended"){
			this.context.resume()
		}
	}
	saveSettings(){
		for(var i = 0; i < this.gainList.length; i++){
			var gain = this.gainList[i]
			gain.defaultVol = gain.volume
		}
	}
	loadSettings(){
		for(var i = 0; i < this.gainList.length; i++){
			var gain = this.gainList[i]
			gain.setVolume(gain.defaultVol)
		}
	}
	fallbackDecoder(buffer, resolve, reject){
		if (typeof Oggmented === "undefined") {
			reject(new Error("Oggmented not loaded"))
			return
		}
		if (!this._oggmentedPromise) {
			this._oggmentedPromise = Oggmented({
				locateFile: function(path) {
					return "/taiko/src/js/lib/" + path
				}
			})
		}
		this._oggmentedPromise.then(oggmented => oggmented.decodeOggData(buffer, resolve, reject), reject)
	}
}
class SoundGain{
	constructor(soundBuffer, channel){
		this.soundBuffer = soundBuffer
		this.gainNode = soundBuffer.context.createGain()
		if(channel){
			var index = channel === "left" ? 0 : 1
			this.merger = soundBuffer.context.createChannelMerger(2)
			this.merger.connect(soundBuffer.context.destination)
			this.gainNode.connect(this.merger, 0, index)
		}else{
			this.gainNode.connect(soundBuffer.context.destination)
		}
		this.setVolume(1)
	}
	load(url){
		return this.soundBuffer.load(url, this)
	}
	convertTime(time, absolute){
		return this.soundBuffer.convertTime(time, absolute)
	}
	setVolume(amount){
		this.gainNode.gain.value = amount * amount
		this.volume = amount
	}
	setVolumeMul(amount){
		var def = (this.defaultVol !== undefined && this.defaultVol !== null) ? this.defaultVol : (this.volume !== undefined ? this.volume : 1);
		this.setVolume((amount !== undefined ? amount : 1) * def)
	}
	setCrossfade(amount){
		this.setVolume(Math.sqrt(Math.sin(Math.PI / 2 * amount)))
	}
	fadeIn(duration, time, absolute){
		this.fadeVolume(0, this.volume * this.volume, duration, time, absolute)
	}
	fadeOut(duration, time, absolute){
		this.fadeVolume(this.volume * this.volume, 0, duration, time, absolute)
	}
	fadeVolume(vol1, vol2, duration, time, absolute){
		time = this.convertTime(time, absolute)
		this.gainNode.gain.linearRampToValueAtTime(vol1, time)
		this.gainNode.gain.linearRampToValueAtTime(vol2, time + (duration || 0))
	}
	mute(){
		this.gainNode.gain.value = 0
	}
	unmute(){
		this.setVolume(this.volume)
	}
}
class Sound{
	constructor(gain, buffer){
		this.gain = gain
		this.buffer = buffer
		this.soundBuffer = gain.soundBuffer
		this.duration = buffer.duration
		this.timeouts = new Set()
		this.sources = new Set()
		this.volume = 1
	}
	copy(gain){
		var snd = new Sound(gain || this.gain, this.buffer)
		snd.name = this.name
		snd.isDrum = this.isDrum
		snd.volume = this.volume
		return snd
	}
	getTime(){
		return this.soundBuffer.getTime()
	}
	convertTime(time, absolute){
		return this.soundBuffer.convertTime(time, absolute)
	}
	setTimeouts(time){
		return new Promise(resolve => {
			var relTime = time - this.getTime()
			if(relTime > 0){
				var timeout = setTimeout(() => {
					this.timeouts.delete(timeout)
					resolve()
				}, relTime * 1000)
				this.timeouts.add(timeout)
			}else{
				resolve()
			}
		})
	}
	clearTimeouts(){
		this.timeouts.forEach(timeout => {
			clearTimeout(timeout)
			this.timeouts.delete(timeout)
		})
	}
	playLoop(time, absolute, seek1, seek2, until){
		time = this.convertTime(time, absolute)
		seek1 = seek1 || 0
		if(typeof seek2 === "undefined"){
			seek2 = seek1
		}
		until = until || this.duration
		if(seek1 >= until || seek2 >= until){
			return
		}
		this.loop = {
			started: time + until - seek1,
			seek: seek2,
			until: until
		}
		this.play(time, true, seek1, until)
		this.addLoop()
		this.loop.interval = setInterval(() => {
			this.addLoop()
		}, 100)
	}
	addLoop(){
		while(this.getTime() > this.loop.started - 1){
			this.play(this.loop.started, true, this.loop.seek, this.loop.until)
			this.loop.started += this.loop.until - this.loop.seek
		}
	}
	play(time, absolute, seek, until){
		if(this.soundBuffer.context.state === "suspended"){
			this.soundBuffer.context.resume()
		}
		var isImmediate = (!time && time !== 0) || time === 0 || (!absolute && time <= 0)
		time = this.convertTime(time, absolute)
		var source = this.soundBuffer.createSource(this)
		seek = seek || 0
		until = until || this.duration
		this.setTimeouts(time).then(() => {
			this.cfg = {
				started: time,
				seek: seek,
				until: until
			}
		})
		var startSchedule = isImmediate ? 0 : time
		source.start(startSchedule, Math.max(0, seek || 0), Math.max(0, until - seek))
		source.startTime = time
		this.sources.add(source)
		source.onended = () => {
			this.sources.delete(source)
		}
	}
	stop(time, absolute){
		time = this.convertTime(time, absolute)
		this.sources.forEach(source => {
			try{
				source.stop(Math.max(source.startTime, time))
			}catch(e){}
		})
		this.setTimeouts(time).then(() => {
			if(this.loop){
				clearInterval(this.loop.interval)
			}
			this.clearTimeouts()
		})
	}
	pause(time, absolute){
		if(this.cfg){
			time = this.convertTime(time, absolute)
			this.stop(time, true)
			this.cfg.pauseSeek = time - this.cfg.started + this.cfg.seek
		}
	}
	resume(time, absolute){
		if(this.cfg){
			if(this.loop){
				this.playLoop(time, absolute, this.cfg.pauseSeek, this.loop.seek, this.loop.until)
			}else{
				this.play(time, absolute, this.cfg.pauseSeek, this.cfg.until)
			}
		}
	}
	clean(){
		delete this.buffer
	}
}
