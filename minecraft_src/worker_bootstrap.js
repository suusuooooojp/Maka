onmessage = function(o) {
	try {
		importScripts("https://cdn.jsdelivr.net/gh/U5KUN/Eaglercraft-Japanese@main/classes_server.js");
	} catch (e) {
		try {
			importScripts("/minecraft/classes_server.js");
		} catch (e2) {
			importScripts("classes_server.js");
		}
	}
	eaglercraftServerOpts = o.data;
	main();
};

