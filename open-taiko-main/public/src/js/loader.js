class Loader {
  constructor(callback) {
    this.callback = callback;
    this.loadedAssets = 0;
    this.assetsDiv = document.getElementById("assets") || document.body;
    this.screen = document.getElementById("screen") || document.body;
    this.startTime = Date.now();
    this.errorMessages = [];

    var promises = [];

    promises.push(
      this.ajax("/taiko/src/views/loader.html").then((page) => {
        if (!this.screen) {
          this.screen = document.getElementById("screen") || document.body;
        }
        this.screen.innerHTML = page;
      }).catch((err) => {
        console.warn("Loader HTML fetch fallback:", err);
      })
    );

    promises.push(
      this.ajax("/taiko/api/config").then((conf) => {
        gameConfig = typeof conf === "string" ? JSON.parse(conf) : conf;
      }).catch((err) => {
        console.warn("Config fetch fallback:", err);
        gameConfig = { songs_baseurl: "/taiko/songs/", assets_baseurl: "/taiko/assets/" };
      })
    );

    Promise.all(promises).then(this.run.bind(this)).catch((err) => {
      console.error("Loader bootstrap failed:", err);
      this.run();
    });
  }
  run() {
    this.promises = [];
    this.loaderDiv = document.querySelector("#loader");
    this.loaderPercentage = document.querySelector("#loader .percentage");
    this.loaderProgress = document.querySelector("#loader .progress");

    this.queryString = gameConfig._version && gameConfig._version.commit_short
      ? "?" + gameConfig._version.commit_short
      : "?v=20261031v1";

    var songsPrefetch = this.ajax("/taiko/api/songs");

    if (gameConfig.custom_js) {
      this.addPromise(
        this.loadScript(gameConfig.custom_js),
        gameConfig.custom_js
      );
    }
    var oggSupport = new Audio().canPlayType("audio/ogg;codecs=vorbis") === "probably";
    if (!oggSupport && assets.js.indexOf("lib/oggmented-wasm.js") === -1) {
      assets.js.push("lib/oggmented-wasm.js");
    }
    assets.js.forEach((name) => {
      this.addPromise(this.loadScript("/taiko/src/js/" + name), "/taiko/src/js/" + name);
    });

    var pageVersion = (typeof versionLink !== "undefined" && versionLink && versionLink.href) ? versionLink.href : "https://github.com/sammwyy/open-taiko";
    var index = pageVersion.lastIndexOf("/");
    if (index !== -1) {
      pageVersion = pageVersion.slice(index + 1);
    }
    this.addPromise(
      new Promise((resolve) => {
        var allCssUrls = assets.css
          .map((name) => "/taiko/src/css/" + name + this.queryString)
          .concat(
            assets.assetsCss.map(
              (name) => gameConfig.assets_baseurl + name + this.queryString
            )
          );
        var remaining = allCssUrls.length;
        var settled = false;
        var finish = () => {
          if (!settled) {
            settled = true;
            resolve();
          }
        };
        if (remaining === 0) {
          finish();
          return;
        }
        var onOne = () => {
          remaining--;
          if (remaining <= 0) {
            finish();
          }
        };
        setTimeout(finish, 3000);
        allCssUrls.forEach((href) => {
          var stylesheet = document.createElement("link");
          stylesheet.rel = "stylesheet";
          stylesheet.onload = onOne;
          stylesheet.onerror = onOne;
          stylesheet.href = href;
          document.head.appendChild(stylesheet);
        });
      })
    );

    for (var name in assets.fonts) {
      let fontName = name;
      let url = gameConfig.assets_baseurl + "fonts/" + assets.fonts[name];
      this.addPromise(
        new FontFace(fontName, "url('" + url + "')")
          .load()
          .then((font) => {
            document.fonts.add(font);
          })
          .catch((err) => {
            console.warn("Font fallback for", fontName, url, err);
          }),
        url
      );
    }

    assets.img.forEach((name) => {
      var id = this.getFilename(name);
      var image = document.createElement("img");
      var url = gameConfig.assets_baseurl + "img/" + name;
      this.addPromise(this.loadImageWithRetry(image, url), url);
      image.id = name;
      image.src = url;
      this.assetsDiv.appendChild(image);
      assets.image[id] = image;
    });

    assets.views.forEach((name) => {
      var id = this.getFilename(name);
      var url = "/taiko/src/views/" + name + this.queryString;
      this.addPromise(
        this.ajax(url).then((page) => {
          assets.pages[id] = page;
        }),
        url
      );
    });

    this.addPromise(
      this.ajax("/taiko/api/categories").then((cats) => {
        try {
          assets.categories = typeof cats === "string" ? JSON.parse(cats) : (cats || []);
        } catch (_) {
          assets.categories = [];
        }
        if (!Array.isArray(assets.categories)) assets.categories = [];
        assets.categories.forEach((cat) => {
          if (cat && cat.song_skin) {
            cat.songSkin = cat.song_skin; //rename the song_skin property and add category title to categories array
            delete cat.song_skin;
            cat.songSkin.infoFill = cat.songSkin.info_fill;
            delete cat.songSkin.info_fill;
          }
        });

        assets.categories.push({
          title: "default",
          songSkin: {
            background: "#ececec",
            border: ["#fbfbfb", "#8b8b8b"],
            outline: "#656565",
            infoFill: "#656565",
          },
        });
      }).catch((err) => {
        console.warn("Categories fetch fallback:", err);
        assets.categories = [{
          title: "default",
          songSkin: {
            background: "#ececec",
            border: ["#fbfbfb", "#8b8b8b"],
            outline: "#656565",
            infoFill: "#656565",
          },
        }];
      }),
      "/taiko/api/categories"
    );

    var url = gameConfig.assets_baseurl + "img/vectors.json" + this.queryString;
    this.addPromise(
      this.ajax(url).then((response) => {
        try {
          vectors = typeof response === "string" ? JSON.parse(response) : (response || {});
        } catch (_) {
          vectors = {};
        }
      }).catch((err) => {
        console.warn("Vectors fetch fallback:", err);
        vectors = {};
      }),
      url
    );

    this.afterJSCount =
      ["/taiko/api/songs", "blurPerformance", "categories"].length +
      assets.audioSfx.length +
      assets.audioMusic.length +
      assets.audioSfxLR.length +
      assets.audioSfxLoud.length +
      (gameConfig.accounts ? 1 : 0);

    Promise.all(this.promises).then(() => {
      if (this.error) {
        return;
      }

      var parseSongsList = (raw) => {
        try {
          var parsed = typeof raw === "string" ? JSON.parse(raw) : (raw || []);
          return Array.isArray(parsed) ? parsed : [];
        } catch (_) {
          return [];
        }
      };

      this.addPromise(
        songsPrefetch
          .catch(() => "[]")
          .then((raw) => {
            var list = parseSongsList(raw);
            if (list.length > 0) return list;
            return this.ajax("/taiko/api/songs").then((raw2) => parseSongsList(raw2));
          })
          .then((songs) => {
            songs.forEach((song) => {
              if (!song) return;
              var directory = gameConfig.songs_baseurl + song.id + "/";
              var songExt = song.music_type ? String(song.music_type).toLowerCase() : "mp3";
              song.music = new RemoteFile(directory + "main." + songExt);
              if (song.type === "tja") {
                song.chart = new RemoteFile(directory + "main.tja");
              } else {
                song.chart = { separateDiff: true };
                for (var diff in song.courses) {
                  if (song.courses[diff]) {
                    song.chart[diff] = new RemoteFile(directory + diff + ".osu");
                  }
                }
              }
              if (song.lyrics) {
                song.lyricsFile = new RemoteFile(directory + "main.vtt");
              }
              if (song.preview > 0 && gameConfig.preview_type) {
                song.previewMusic = new RemoteFile(directory + "preview." + gameConfig.preview_type);
              }
            });
            assets.songsDefault = songs;
            assets.songs = assets.songsDefault;
          })
          .catch((err) => {
            console.warn("Songs prefetch fallback:", err);
            assets.songsDefault = [];
            assets.songs = [];
          }),
        "/taiko/api/songs"
      );

      var categoryPromises = [];
      if (assets.categories && Array.isArray(assets.categories)) {
        assets.categories //load category backgrounds to DOM
          .filter((cat) => cat && cat.songSkin && cat.songSkin.bg_img)
          .forEach((cat) => {
            let name = cat.songSkin.bg_img;
            var id = this.getFilename(name);
            var image = document.createElement("img");
            var url = gameConfig.assets_baseurl + "img/" + name;
            categoryPromises.push(this.loadImageWithRetry(image, url));
            image.id = name;
            image.src = url;
            this.assetsDiv.appendChild(image);
            assets.image[id] = image;
          });
      }
      this.addPromise(Promise.all(categoryPromises));

      try {
        snd.buffer = new SoundBuffer();
      } catch (err) {
        console.warn("SoundBuffer constructor fallback:", err);
        snd.buffer = {
          context: { state: "suspended", resume: () => Promise.resolve() },
          createGain: () => ({ gain: { value: 1, linearRampToValueAtTime: () => {} }, connect: () => {} }),
          setCrossfade: () => {},
          loadFolder: () => Promise.resolve(),
        };
      }
      if (!oggSupport && snd.buffer.fallbackDecoder) {
        snd.buffer.oggDecoder = snd.buffer.fallbackDecoder;
      }
      snd.musicGain = snd.buffer.createGain();
      snd.sfxGain = snd.buffer.createGain();
      snd.previewGain = snd.buffer.createGain();
      snd.sfxGainL = snd.buffer.createGain("left");
      snd.sfxGainR = snd.buffer.createGain("right");
      snd.sfxLoudGain = snd.buffer.createGain();
      snd.buffer.setCrossfade(
        [snd.musicGain, snd.previewGain],
        [snd.sfxGain, snd.sfxGainL, snd.sfxGainR],
        0.5
      );
      snd.sfxLoudGain.setVolume(1.0);
      snd.buffer.saveSettings();

      var sfxVol = (window.taikoDrumVolume !== undefined && window.taikoDrumVolume !== null) ? window.taikoDrumVolume : 0.425;
      snd.sfxGain.setVolumeMul(sfxVol);
      snd.sfxGainL.setVolumeMul(sfxVol);
      snd.sfxGainR.setVolumeMul(sfxVol);
      snd.sfxLoudGain.setVolume(1.0 * sfxVol);

      this.afterJSCount = 0;

      assets.audioSfx.forEach((name) => {
        this.addPromise(this.loadSound(name, snd.sfxGain), this.soundUrl(name));
      });
      assets.audioMusic.forEach((name) => {
        this.addPromise(
          this.loadSound(name, snd.musicGain),
          this.soundUrl(name)
        );
      });
      assets.audioSfxLR.forEach((name) => {
        this.addPromise(
          this.loadSound(name, snd.sfxGain).then((sound) => {
            var id = this.getFilename(name);
            var baseSound = assets.sounds[id] || sound;
            if (baseSound && typeof baseSound.copy === "function") {
              assets.sounds[id + "_p1"] = baseSound.copy(snd.sfxGainL);
              assets.sounds[id + "_p2"] = baseSound.copy(snd.sfxGainR);
            }
          }),
          this.soundUrl(name)
        );
      });
      assets.audioSfxLoud.forEach((name) => {
        this.addPromise(
          this.loadSound(name, snd.sfxLoudGain),
          this.soundUrl(name)
        );
      });

      this.canvasTest = new CanvasTest();
      this.addPromise(
        this.canvasTest.blurPerformance().then((result) => {
          perf.blur = result;
          if (result > 1000 / 50) {
            // Less than 50 fps with blur enabled
            disableBlur = true;
          }
        }),
        "blurPerformance"
      );

      settings = new Settings();
      pageEvents.setKbd();
      scoreStorage = new ScoreStorage();
      db = new IDB("taiko", "store");

      if (gameConfig.accounts) {
        this.addPromise(
          this.ajax("/taiko/api/scores").then((response) => {
            try {
              response = typeof response === "string" ? JSON.parse(response) : response;
              if (response && response.status === "ok") {
                account.loggedIn = true;
                account.username = response.username;
                account.displayName = response.display_name;
                account.don = response.don;
                if (scoreStorage && typeof scoreStorage.load === "function") {
                  scoreStorage.load(response.scores);
                }
                pageEvents.send("login", account.username);
              }
            } catch (_) {}
          }).catch(() => {}),
          "/taiko/api/scores"
        );
      }

      Promise.all(this.promises).then(() => {
        if (this.error) {
          return;
        }
        if (!account.loggedIn) {
          scoreStorage.load();
        }
        for (var i in assets.songsDefault) {
          var song = assets.songsDefault[i];
          if (!song.hash) {
            song.hash = (song.id !== undefined && song.id !== null && song.id !== "") ? String(song.id) : song.title;
          }
          scoreStorage.songTitles[song.title] = song.hash;
          var score = scoreStorage.get(song.hash, false, true) || scoreStorage.get(song.title, false, true);
          if (score) {
            score.title = song.title;
          }
        }
        var promises = [];

        var readyEvent = "normal";
        var songId;
        var hashLower = location.hash.toLowerCase();
        p2 = new P2Connection();
        if (hashLower.startsWith("#song=")) {
          var rawSongId = decodeURIComponent(location.hash.slice(6)).trim();
          if (rawSongId) {
            var number = parseInt(rawSongId, 10);
            songId = String(number) === rawSongId && number > 0 ? number : rawSongId;
            readyEvent = "song-id";
          }
        } else if (location.hash.length === 6) {
          p2.hashLock = true;
          promises.push(
            new Promise((resolve) => {
              p2.open();
              pageEvents.add(p2, "message", (response) => {
                if (response.type === "session") {
                  pageEvents.send("session-start", "invited");
                  readyEvent = "session-start";
                  resolve();
                } else if (response.type === "gameend") {
                  p2.hash("");
                  p2.hashLock = false;
                  readyEvent = "session-expired";
                  resolve();
                }
              });
              p2.send("invite", {
                id: location.hash.slice(1).toLowerCase(),
                name: account.loggedIn ? account.displayName : null,
                don: account.loggedIn ? account.don : null,
              });
              setTimeout(() => {
                if (p2.socket.readyState !== 1) {
                  p2.hash("");
                  p2.hashLock = false;
                  resolve();
                }
              }, 10000);
            }).then(() => {
              pageEvents.remove(p2, "message");
            })
          );
        } else {
          p2.hash("");
        }

        promises.push(this.canvasTest.drawAllImages());

        Promise.all(promises).then((result) => {
          perf.allImg = result;
          perf.load = Date.now() - this.startTime;
          this.canvasTest.clean();
          this.clean();
          this.callback(songId);
          pageEvents.send("ready", readyEvent);
        });
      }, this.errorMsg.bind(this));
    });
  }
  addPromise(promise, url) {
    this.promises.push(promise);
    promise.then(this.assetLoaded.bind(this), (response) => {
      this.errorMsg(response, url);
    });
  }
  soundUrl(name) {
    return gameConfig.assets_baseurl + "audio/" + name;
  }
  loadSound(name, gain) {
    var id = this.getFilename(name);
    return gain.load(new RemoteFile(this.soundUrl(name))).then((sound) => {
      sound.name = id;
      if (id.startsWith("se_don") || id.startsWith("se_ka") || id.startsWith("neiro_")) {
        sound.isDrum = true;
        sound.volume = 0.75;
      }
      if (id === "v_start") {
        sound.volume = 0.5;
      }
      assets.sounds[id] = sound;
      return sound;
    }).catch((err) => {
      console.warn("Sound load fallback for", name, err);
      var dummySound = {
        name: id,
        volume: 1,
        duration: 0.1,
        copy: function(g) { return this; },
        play: function() {},
        stop: function() {},
        pause: function() {},
        resume: function() {},
        clean: function() {},
      };
      assets.sounds[id] = dummySound;
      return dummySound;
    });
  }
  getFilename(name) {
    return name.slice(0, name.lastIndexOf("."));
  }
  errorMsg(error, url) {
    if (url || error) {
      if (url) {
        error =
          (Array.isArray(error) ? error[0] + ": " : error ? error + ": " : "") +
          url;
      }
      this.errorMessages.push(error);
      pageEvents.send("loader-error", url || error);
    }
    if (!this.error) {
      this.error = true;
      cancelTouch = false;
      this.loaderDiv.classList.add("loaderError");
      if (typeof allStrings === "object") {
        var lang = localStorage.lang;
        if (!lang) {
          var userLang = navigator.languages.slice();
          userLang.unshift(navigator.language);
          for (var i in userLang) {
            for (var j in allStrings) {
              if (allStrings[j].regex.test(userLang[i])) {
                lang = j;
              }
            }
          }
        }
        if (!lang) {
          lang = "en";
        }
        loader.screen.getElementsByClassName("view-content")[0].innerText =
          allStrings[lang].errorOccured;
      }
      var loaderError =
        loader.screen.getElementsByClassName("loader-error-div")[0];
      loaderError.style.display = "flex";
      var diagTxt = loader.screen.getElementsByClassName("diag-txt")[0];
      var debugLink = loader.screen.getElementsByClassName("debug-link")[0];
      if (navigator.userAgent.indexOf("Android") >= 0) {
        var iframe = document.createElement("iframe");
        diagTxt.appendChild(iframe);
        var body = iframe.contentWindow.document.body;
        body.setAttribute(
          "style",
          `
					font-family: monospace;
					margin: 2px 0 0 2px;
					white-space: pre-wrap;
					word-break: break-all;
					cursor: text;
				`
        );
        body.setAttribute(
          "onblur",
          `
					getSelection().removeAllRanges()
				`
        );
        this.errorTxt = {
          element: body,
          method: "innerText",
        };
      } else {
        var textarea = document.createElement("textarea");
        textarea.readOnly = true;
        diagTxt.appendChild(textarea);
        if (!this.touchEnabled) {
          textarea.addEventListener("focus", () => {
            textarea.select();
          });
          textarea.addEventListener("blur", () => {
            getSelection().removeAllRanges();
          });
        }
        this.errorTxt = {
          element: textarea,
          method: "value",
        };
      }
      var show = () => {
        diagTxt.style.display = "block";
        debugLink.style.display = "none";
      };
      debugLink.addEventListener("click", show);
      debugLink.addEventListener("touchstart", show);
      this.clean(true);
    }
    var percentage = Math.floor(
      (this.loadedAssets * 100) / (this.promises.length + this.afterJSCount)
    );
    this.errorTxt.element[this.errorTxt.method] =
      "```\n" +
      this.errorMessages.join("\n") +
      "\nPercentage: " +
      percentage +
      "%\n```";
  }
  assetLoaded() {
    if (!this.error) {
      this.loadedAssets++;
      var total = Math.max(1, this.promises.length + this.afterJSCount);
      var percentage = Math.min(100, Math.floor((this.loadedAssets * 100) / total));
      if (this.loaderProgress) {
        this.loaderProgress.style.width = percentage + "%";
      }
      if (this.loaderPercentage) {
        if (this.loaderPercentage.firstChild) {
          this.loaderPercentage.firstChild.data = percentage + "%";
        } else {
          this.loaderPercentage.textContent = percentage + "%";
        }
      }
    }
  }
  changePage(name, patternBg) {
    this.screen.innerHTML = assets.pages[name];
    this.screen.classList[patternBg ? "add" : "remove"]("pattern-bg");
  }
  loadImageWithRetry(image, url, maxRetries = 3) {
    var attempt = 0;
    var tryLoad = () => {
      return pageEvents.load(image).catch((err) => {
        attempt++;
        if (attempt < maxRetries) {
          return new Promise((r) => setTimeout(r, 250 * attempt)).then(() => {
            var sep = url.indexOf("?") === -1 ? "?" : "&";
            image.src = url + sep + "_r=" + attempt;
            return tryLoad();
          });
        }
        console.warn("Image fallback after retries:", url, err);
        image.src =
          "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7";
        return Promise.resolve();
      });
    };
    return tryLoad();
  }
  ajax(url, customRequest, customResponse, maxRetries = 3) {
    var attempt = 0;
    var doAjax = () => {
      var request = new XMLHttpRequest();
      request.open("GET", url);
      var promise = pageEvents.load(request);
      if (!customResponse) {
        promise = promise.then(() => {
          if (request.status === 200) {
            return request.response;
          } else {
            return Promise.reject(`${url} (${request.status})`);
          }
        });
      }
      if (customRequest) {
        customRequest(request);
      }
      request.send();
      return promise.catch((err) => {
        attempt++;
        if (attempt < maxRetries && !customResponse) {
          return new Promise((r) => setTimeout(r, 250 * attempt)).then(doAjax);
        }
        return Promise.reject(err);
      });
    };
    return doAjax();
  }
  loadScript(url, maxRetries = 3) {
    var fullUrl = url + this.queryString;
    var attempt = 0;
    var doScript = () => {
      var script = document.createElement("script");
      var promise = pageEvents.load(script);
      script.src =
        attempt === 0
          ? fullUrl
          : fullUrl + (fullUrl.indexOf("?") === -1 ? "?" : "&") + "_r=" + attempt;
      document.head.appendChild(script);
      return promise.catch((err) => {
        try {
          document.head.removeChild(script);
        } catch (_) {}
        attempt++;
        if (attempt < maxRetries) {
          return new Promise((r) => setTimeout(r, 250 * attempt)).then(doScript);
        }
        return Promise.reject(err);
      });
    };
    return doScript();
  }
  getCsrfToken() {
    return this.ajax("/taiko/api/csrftoken").then((response) => {
      var json = JSON.parse(response);
      if (json.status === "ok") {
        return Promise.resolve(json.token);
      } else {
        return Promise.reject();
      }
    });
  }
  clean(error) {
    delete this.loaderDiv;
    delete this.loaderPercentage;
    delete this.loaderProgress;
    if (!error) {
      delete this.promises;
      delete this.errorText;
    }
    pageEvents.remove(root, "touchstart");
  }
}
