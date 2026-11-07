function decodeTextBuffer(buf, fallbackEncoding){
	if(typeof TextDecoder !== "undefined"){
		try{
			var utf8Text = new TextDecoder("utf-8", {fatal: true}).decode(buf)
			if(utf8Text.charCodeAt(0) === 0xFEFF){
				utf8Text = utf8Text.slice(1)
			}
			return utf8Text
		}catch(e){
			try{
				var enc = fallbackEncoding || "sjis"
				if(enc === "sjis") enc = "shift-jis"
				return new TextDecoder(enc).decode(buf)
			}catch(e2){
				return new TextDecoder("utf-8").decode(buf)
			}
		}
	}
	return null
}
function readFile(file, arrayBuffer, encoding){
	var reader = new FileReader()
	if(!arrayBuffer && typeof TextDecoder !== "undefined"){
		var promise = pageEvents.load(reader).then(event => {
			var decoded = decodeTextBuffer(event.target.result, encoding)
			return decoded !== null ? decoded : ""
		})
		reader.readAsArrayBuffer(file)
		return promise
	}
	var promise = pageEvents.load(reader).then(event => event.target.result)
	reader[arrayBuffer ? "readAsArrayBuffer" : "readAsText"](file, encoding)
	return promise
}
function filePermission(file){
	return file.queryPermission().then(response => {
		if(response === "granted"){
			return file
		}else{
			return file.requestPermission().then(response => {
				if(response === "granted"){
					return file
				}else{
					return Promise.reject(file)
				}
			})
		}
	})
}
var _remoteBufferLRU = []
function _rememberRemoteBuffer(file){
	var idx = _remoteBufferLRU.indexOf(file)
	if(idx !== -1){
		_remoteBufferLRU.splice(idx, 1)
	}
	_remoteBufferLRU.push(file)
	while(_remoteBufferLRU.length > 6){
		var evicted = _remoteBufferLRU.shift()
		if(evicted && evicted !== file){
			delete evicted._cachedArrayBuffer
			delete evicted._arrayBufferPromise
		}
	}
}
class RemoteFile{
	constructor(url){
		this.url = url
		try{
			this.path = new URL(url).pathname
		}catch(e){
			this.path = url
		}
		if(this.path.startsWith("/")){
			this.path = this.path.slice(1)
		}
		this.name = this.path
		var index = this.name.lastIndexOf("/")
		if(index !== -1){
			this.name = this.name.slice(index + 1)
		}
	}
	arrayBuffer(){
		if(this._cachedArrayBuffer && this._cachedArrayBuffer.byteLength > 0){
			_rememberRemoteBuffer(this)
			return Promise.resolve(this._cachedArrayBuffer.slice(0))
		}
		if(this._arrayBufferPromise){
			return this._arrayBufferPromise.then(buf => buf.slice(0))
		}
		this._arrayBufferPromise = loader.ajax(this.url, request => {
			request.responseType = "arraybuffer"
		}).then(buf => {
			delete this._arrayBufferPromise
			if(buf && buf.byteLength > 0){
				this._cachedArrayBuffer = buf
				_rememberRemoteBuffer(this)
				return buf.slice(0)
			}
			return buf
		}).catch(err => {
			delete this._arrayBufferPromise
			return Promise.reject(err)
		})
		return this._arrayBufferPromise
	}
	read(encoding){
		var encKey = encoding || "__utf8__"
		if(this._cachedReads && this._cachedReads[encKey] !== undefined){
			return Promise.resolve(this._cachedReads[encKey])
		}
		if(!this._readPromises){
			this._readPromises = {}
		}
		if(this._readPromises[encKey]){
			return this._readPromises[encKey]
		}
		var p = encoding
			? this.arrayBuffer().then(buf => {
				var decoded = decodeTextBuffer(buf, encoding)
				if(decoded !== null) return decoded
				return readFile(new Blob([buf]), false, encoding)
			})
			: loader.ajax(this.url)
		this._readPromises[encKey] = p.then(text => {
			delete this._readPromises[encKey]
			if(!this._cachedReads){
				this._cachedReads = {}
			}
			this._cachedReads[encKey] = text
			return text
		}).catch(err => {
			delete this._readPromises[encKey]
			return Promise.reject(err)
		})
		return this._readPromises[encKey]
	}
	blob(){
		return this.arrayBuffer().then(response => new Blob([response]))
	}
}
class LocalFile{
	constructor(file, path){
		this.file = file
		this.path = path || file.webkitRelativePath
		this.url = this.path
		this.name = file.name
	}
	arrayBuffer(){
		return readFile(this.file, true)
	}
	read(encoding){
		return readFile(this.file, false, encoding)
	}
	blob(){
		return Promise.resolve(this.file)
	}
}
class FilesystemFile{
	constructor(file, path){
		this.file = file
		this.path = path
		this.url = this.path
		this.name = file.name
	}
	arrayBuffer(){
		return this.blob().then(blob => blob.arrayBuffer())
	}
	read(encoding){
		return this.blob().then(blob => readFile(blob, false, encoding))
	}
	blob(){
		return filePermission(this.file).then(file => file.getFile())
	}
}
class GdriveFile{
	constructor(fileObj){
		this.path = fileObj.path
		this.name = fileObj.name
		this.id = fileObj.id
		this.url = gpicker.filesUrl + this.id + "?alt=media"
	}
	arrayBuffer(){
		return gpicker.downloadFile(this.id, true)
	}
	read(encoding){
		if(encoding){
			return this.blob().then(blob => readFile(blob, false, encoding))
		}else{
			return gpicker.downloadFile(this.id)
		}
	}
	blob(){
		return this.arrayBuffer().then(response => new Blob([response]))
	}
}
class CachedFile{
	constructor(contents, oldFile){
		this.contents = contents
		this.oldFile = oldFile
		this.path = oldFile.path
		this.name = oldFile.name
		this.url = oldFile.url
	}
	arrayBuffer(){
		return Promise.resolve(this.contents)
	}
	read(encoding){
		return this.arrayBuffer()
	}
	blob(){
		return this.arrayBuffer().then(response => new Blob([response]))
	}
}
