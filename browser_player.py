from __future__ import annotations

import json
import os
from typing import Callable
from urllib.parse import parse_qs, urlparse

import wx
import wx.html2 as html2

from app_paths import webview2_profile_dir
from maoer_api import BASE_URL, PlaybackInfo
from windows_audio import set_current_app_volume


class PlayerUnavailable(RuntimeError):
    pass


WEBVIEW_AUDIO_PROCESS_NAMES = ("msedgewebview2.exe", "webview2", "missevan.com")
MIN_PLAYBACK_RATE = 0.5
MAX_PLAYBACK_RATE = 2.0
ScriptCallback = Callable[[dict[str, object] | None], None]


def debug_log(message: str) -> None:
    if os.environ.get("MAOER_DEBUG"):
        print(f"[browser] {message}", flush=True)


CONTROL_SCRIPT = r"""
(function(action, value) {
  try {
    function result(data) {
      data = data || {ok: true};
      data.action = action;
      return JSON.stringify(data);
    }

    function demo() {
      return window.index && index.mo && index.mo.soundDemo ? index.mo.soundDemo : null;
    }

    function jq(selector) {
      return window.$ ? window.$(selector) : null;
    }

    function click(selector) {
      var item = document.querySelector(selector);
      if (!item) {
        return false;
      }
      item.click();
      return true;
    }

    function mediaElements() {
      var found = [];
      function collect(root) {
        if (!root || !root.querySelectorAll) {
          return;
        }
        found = found.concat(Array.prototype.slice.call(root.querySelectorAll("video,audio")));
        Array.prototype.slice.call(root.querySelectorAll("*")).forEach(function(item) {
          if (item.shadowRoot) {
            collect(item.shadowRoot);
          }
        });
        Array.prototype.slice.call(root.querySelectorAll("iframe")).forEach(function(frame) {
          try {
            collect(frame.contentDocument);
          } catch (err) {}
        });
      }
      collect(document);
      return found.filter(function(item) {
        return item && (item.currentSrc || item.src || isFinite(item.duration));
      });
    }

    function currentMedia() {
      var list = mediaElements();
      return (
        list.find(function(item) { return !item.paused && !item.ended; }) ||
        list.find(function(item) { return isFinite(item.currentTime) && item.currentTime > 0 && !item.ended; }) ||
        list.find(function(item) { return !item.ended; }) ||
        list[0] ||
        null
      );
    }

    function installSingleSoundGuard(options) {
      options = options || {};
      var guard = window.__maoerSingleSoundGuard || {};
      var nextSoundId = options.soundId == null ? null : String(options.soundId);
      if (guard.soundId !== nextSoundId) {
        guard.handled = false;
        guard.blockNext = false;
        guard.originalSrc = "";
        guard.originalSoundSignature = "";
      }
      guard.soundId = nextSoundId;
      guard.expectedUrl = options.url || "";
      guard.generation = options.generation || 0;
      window.__maoerSingleSoundGuard = guard;

      function currentGuard() {
        return window.__maoerSingleSoundGuard || {};
      }

      function normalizedUrl(url) {
        if (!url) {
          return "";
        }
        try {
          return new URL(String(url), window.location.href).href.split("#")[0];
        } catch (err) {
          return String(url);
        }
      }

      function mediaSource(item) {
        if (!item) {
          return "";
        }
        return normalizedUrl(item.currentSrc || item.src || item.getAttribute && item.getAttribute("src") || "");
      }

      function soundSignature(sound) {
        if (!sound) {
          return "";
        }
        var values = [];
        ["id", "sID", "url", "_url", "src", "currentSrc", "soundId", "sound_id"].forEach(function(key) {
          try {
            if (sound[key] != null && sound[key] !== "") {
              values.push(key + "=" + normalizedUrl(sound[key]));
            }
          } catch (err) {}
        });
        return values.join("|");
      }

      function hasUrlSignature(signature) {
        return /(^|\|)(url|_url|src|currentSrc)=/.test(signature || "");
      }

      function captureOriginal(item, sound) {
        var state = currentGuard();
        if (!state.originalSrc) {
          state.originalSrc = mediaSource(item);
        }
        var signature = soundSignature(sound);
        if (!state.originalSoundSignature || (!hasUrlSignature(state.originalSoundSignature) && hasUrlSignature(signature))) {
          state.originalSoundSignature = signature;
        }
      }

      function isUnexpectedMedia(item) {
        var state = currentGuard();
        var src = mediaSource(item);
        return !!(state.originalSrc && src && src !== state.originalSrc);
      }

      function isUnexpectedSound(sound) {
        var state = currentGuard();
        var signature = soundSignature(sound);
        if (!state.originalSoundSignature || !signature || signature === state.originalSoundSignature) {
          return false;
        }
        if (!hasUrlSignature(state.originalSoundSignature) && hasUrlSignature(signature)) {
          state.originalSoundSignature = signature;
          return false;
        }
        return true;
      }

      function stopSound(sound) {
        if (!sound) {
          return;
        }
        ["pause", "stop"].forEach(function(name) {
          if (typeof sound[name] === "function") {
            try {
              sound[name]();
            } catch (err) {}
          }
        });
        try {
          if (typeof sound.setPosition === "function") {
            sound.setPosition(0);
          } else if ("position" in sound) {
            sound.position = 0;
          }
        } catch (err) {}
      }

      function markHandled(item, sound) {
        var state = currentGuard();
        if (state.handled) {
          return;
        }
        state.handled = true;
        state.blockNext = true;
        stopSound(sound || demo());
        try {
          if (item) {
            item.loop = false;
            item.pause();
          }
        } catch (err) {}
        try {
          if (item) {
            item.currentTime = 0;
          }
        } catch (err) {}
      }

      function blockUnexpected(item, sound) {
        var state = currentGuard();
        state.blockNext = true;
        stopSound(sound || demo());
        if (item) {
          try {
            item.loop = false;
          } catch (err) {}
          try {
            item.pause();
          } catch (err) {}
          try {
            item.currentTime = 0;
          } catch (err) {}
        }
      }

      function protectMedia(item) {
        if (!item) {
          return;
        }
        try {
          item.loop = false;
        } catch (err) {}
        if (item.__maoerSingleSoundGuardInstalled) {
          return;
        }
        item.__maoerSingleSoundGuardInstalled = true;
        item.addEventListener("ended", function(event) {
          event.preventDefault();
          event.stopImmediatePropagation();
          markHandled(item, demo());
        }, true);
      }

      function protectSound(sound) {
        if (!sound || sound.__maoerSingleSoundGuardInstalled) {
          return;
        }
        sound.__maoerSingleSoundGuardInstalled = true;
        ["play", "resume"].forEach(function(name) {
          if (typeof sound[name] !== "function") {
            return;
          }
          var original = sound[name];
          sound[name] = function() {
            var state = currentGuard();
            if ((state.blockNext && state.handled) || isUnexpectedSound(sound)) {
              blockUnexpected(currentMedia(), sound);
              return sound;
            }
            return original.apply(this, arguments);
          };
        });
      }

      function protectPlayPrototype() {
        if (!window.HTMLMediaElement || HTMLMediaElement.prototype.__maoerSingleSoundGuardPatched) {
          return;
        }
        var nativePlay = HTMLMediaElement.prototype.play;
        HTMLMediaElement.prototype.__maoerSingleSoundGuardPatched = true;
        HTMLMediaElement.prototype.play = function() {
          var state = currentGuard();
          if ((state.blockNext && state.handled) || isUnexpectedMedia(this)) {
            blockUnexpected(this, demo());
            if (window.Promise && Promise.resolve) {
              return Promise.resolve();
            }
            return undefined;
          }
          return nativePlay.apply(this, arguments);
        };
      }

      function tick() {
        var state = currentGuard();
        var sound = demo();
        protectPlayPrototype();
        protectSound(sound);
        mediaElements().forEach(protectMedia);

        var item = currentMedia();
        captureOriginal(item, sound);
        protectMedia(item);

        if ((item && isUnexpectedMedia(item)) || (sound && isUnexpectedSound(sound))) {
          blockUnexpected(item, sound);
          return;
        }

        if (!state.handled && item && isFinite(item.duration) && item.duration > 0 && isFinite(item.currentTime)) {
          if (item.currentTime >= Math.max(0, item.duration - 0.25)) {
            markHandled(item, sound);
            return;
          }
        }

        if (!state.handled && sound && isFinite(sound.duration) && sound.duration > 0 && isFinite(sound.position)) {
          if (sound.position >= Math.max(0, sound.duration - 300)) {
            markHandled(item, sound);
          }
        }
      }

      if (!guard.installed) {
        guard.installed = true;
        document.addEventListener("ended", function(event) {
          event.preventDefault();
          event.stopImmediatePropagation();
          markHandled(event.target || currentMedia(), demo());
        }, true);
        window.setInterval(tick, 120);
      }

      tick();
      return result({ok: true, soundId: guard.soundId});
    }

    function tryCall(callback) {
      try {
        callback();
        return true;
      } catch (err) {
        return false;
      }
    }

    function playMedia(item) {
      if (!item || !item.play) {
        return false;
      }
      var promise = item.play();
      if (promise && promise.catch) {
        promise.catch(function() {});
      }
      return true;
    }

    function currentPositionMs(item, sound) {
      if (item && isFinite(item.currentTime) && item.currentTime > 0) {
        return item.currentTime * 1000;
      }
      if (sound && isFinite(sound.position) && sound.position > 0) {
        return sound.position;
      }
      if (item && isFinite(item.currentTime)) {
        return item.currentTime * 1000;
      }
      if (sound && isFinite(sound.position)) {
        return sound.position;
      }
      return 0;
    }

    function currentDurationMs(item, sound) {
      if (item && isFinite(item.duration) && item.duration > 0) {
        return item.duration * 1000;
      }
      if (sound && isFinite(sound.duration) && sound.duration > 0) {
        return sound.duration;
      }
      return 0;
    }

    function clampPlaybackRate(rate) {
      rate = Number(rate);
      if (!isFinite(rate) || rate <= 0) {
        rate = 1;
      }
      return Math.max(0.5, Math.min(2, Math.round(rate * 10) / 10));
    }

    function applyMediaPlaybackRate(target, rate) {
      if (!target) {
        return 0;
      }
      var touched = 0;
      if ("defaultPlaybackRate" in target) {
        touched += tryCall(function() { target.defaultPlaybackRate = rate; }) &&
          Math.abs(Number(target.defaultPlaybackRate) - rate) < 0.05 ? 1 : 0;
      }
      if ("playbackRate" in target) {
        touched += tryCall(function() { target.playbackRate = rate; }) &&
          Math.abs(Number(target.playbackRate) - rate) < 0.05 ? 1 : 0;
      }
      return touched;
    }

    function applyPlayerPlaybackRate(target, rate) {
      if (!target) {
        return 0;
      }
      var touched = applyMediaPlaybackRate(target, rate);
      ["setPlaybackRate", "setSpeed", "setRate", "changePlaybackRate"].forEach(function(name) {
        if (typeof target[name] === "function") {
          touched += tryCall(function() { target[name](rate); }) ? 1 : 0;
        }
      });
      if ("speed" in target && typeof target.speed !== "function") {
        touched += tryCall(function() { target.speed = rate; }) &&
          Math.abs(Number(target.speed) - rate) < 0.05 ? 1 : 0;
      }
      if ("rate" in target && typeof target.rate !== "function") {
        touched += tryCall(function() { target.rate = rate; }) &&
          Math.abs(Number(target.rate) - rate) < 0.05 ? 1 : 0;
      }
      return touched;
    }

    function scanNestedMediaPlaybackRates(root, rate, depth) {
      var touched = 0;
      var seen = typeof WeakSet === "function" ? new WeakSet() : null;
      var keysPattern = /audio|video|media|sound|player|element|node|html5|rate|speed|_a/i;

      function visit(target, remaining) {
        if (!target || remaining < 0) {
          return;
        }
        var kind = typeof target;
        if (kind !== "object" && kind !== "function") {
          return;
        }
        try {
          if (seen) {
            if (seen.has(target)) {
              return;
            }
            seen.add(target);
          }
        } catch (err) {
          return;
        }

        touched += applyMediaPlaybackRate(target, rate);
        if (remaining <= 0) {
          return;
        }

        var keys = [];
        try {
          keys = Object.keys(target);
        } catch (err) {
          return;
        }
        keys.slice(0, 100).forEach(function(key) {
          if (!keysPattern.test(key)) {
            return;
          }
          try {
            visit(target[key], remaining - 1);
          } catch (err) {}
        });
      }

      visit(root, depth);
      return touched;
    }

    function objectPlaybackRate(target) {
      if (!target) {
        return 0;
      }
      try {
        if (isFinite(target.playbackRate) && Number(target.playbackRate) > 0) {
          return Number(target.playbackRate);
        }
      } catch (err) {}
      try {
        if (isFinite(target.defaultPlaybackRate) && Number(target.defaultPlaybackRate) > 0) {
          return Number(target.defaultPlaybackRate);
        }
      } catch (err) {}
      return 0;
    }

    function playerObjectPlaybackRate(target) {
      var rate = objectPlaybackRate(target);
      if (rate > 0) {
        return rate;
      }
      try {
        if (isFinite(target.speed) && Number(target.speed) > 0) {
          return Number(target.speed);
        }
      } catch (err) {}
      try {
        if (isFinite(target.rate) && Number(target.rate) > 0) {
          return Number(target.rate);
        }
      } catch (err) {}
      return 0;
    }

    function storedPlaybackRate() {
      try {
        if (isFinite(window.__maoerPlaybackRate) && Number(window.__maoerPlaybackRate) > 0) {
          return Number(window.__maoerPlaybackRate);
        }
      } catch (err) {}
      return 0;
    }

    function findNestedPlaybackRate(root, depth) {
      var seen = typeof WeakSet === "function" ? new WeakSet() : null;
      var keysPattern = /audio|video|media|sound|player|element|node|html5|rate|speed|_a/i;

      function visit(target, remaining) {
        if (!target || remaining < 0) {
          return 0;
        }
        var kind = typeof target;
        if (kind !== "object" && kind !== "function") {
          return 0;
        }
        try {
          if (seen) {
            if (seen.has(target)) {
              return 0;
            }
            seen.add(target);
          }
        } catch (err) {
          return 0;
        }

        var rate = objectPlaybackRate(target);
        if (rate > 0 || remaining <= 0) {
          return rate;
        }

        var keys = [];
        try {
          keys = Object.keys(target);
        } catch (err) {
          return 0;
        }
        for (var index = 0; index < Math.min(keys.length, 100); index += 1) {
          var key = keys[index];
          if (!keysPattern.test(key)) {
            continue;
          }
          try {
            rate = visit(target[key], remaining - 1);
          } catch (err) {
            rate = 0;
          }
          if (rate > 0) {
            return rate;
          }
        }
        return 0;
      }

      return visit(root, depth);
    }

    function currentPlaybackRate(item, sound) {
      return (
        objectPlaybackRate(item) ||
        playerObjectPlaybackRate(sound) ||
        playerObjectPlaybackRate(window.play && play.soundBox) ||
        findNestedPlaybackRate(sound, 3) ||
        findNestedPlaybackRate(window.soundManager, 2) ||
        findNestedPlaybackRate(window.play && play.soundBox, 2) ||
        storedPlaybackRate() ||
        1
      );
    }

    function setPlaybackRate(rate) {
      rate = clampPlaybackRate(rate);
      var touched = 0;
      var target = "none";

      function remember(count, name) {
        if (count > 0 && target === "none") {
          target = name;
        }
        touched += count;
      }

      var item = currentMedia();
      remember(applyMediaPlaybackRate(item, rate), "media");
      mediaElements().forEach(function(media) {
        remember(applyMediaPlaybackRate(media, rate), "media");
      });

      var sound = demo();
      remember(applyPlayerPlaybackRate(sound, rate), "soundDemo");
      remember(scanNestedMediaPlaybackRates(sound, rate, 3), "soundDemo");

      if (window.soundManager && soundManager.sounds) {
        Object.keys(soundManager.sounds).forEach(function(key) {
          try {
            var soundItem = soundManager.sounds[key];
            remember(applyPlayerPlaybackRate(soundItem, rate), "soundManager");
            remember(scanNestedMediaPlaybackRates(soundItem, rate, 2), "soundManager");
          } catch (err) {}
        });
      }

      if (window.play && play.soundBox) {
        remember(applyPlayerPlaybackRate(play.soundBox, rate), "play.soundBox");
        remember(scanNestedMediaPlaybackRates(play.soundBox, rate, 2), "play.soundBox");
      }

      var actualRate = currentPlaybackRate(currentMedia(), demo());
      if (touched > 0 && Math.abs(actualRate - rate) > 0.05 && target !== "media") {
        actualRate = rate;
      }
      if (touched > 0) {
        window.__maoerPlaybackRate = actualRate;
      }

      return result({
        ok: touched > 0,
        rate: actualRate,
        requestedRate: rate,
        touched: touched,
        target: target
      });
    }

    function setPosition(ms) {
      ms = Math.max(0, Number(ms) || 0);
      if (window.play && play.js && typeof play.js.changeSoundPosition === "function") {
        play.js.changeSoundPosition(ms);
        return result({ok: true, target: "play.js", position: ms / 1000});
      }

      var sound = demo();
      if (sound) {
        if (typeof sound.setPosition === "function") {
          sound.setPosition(ms);
        } else {
          sound.position = ms;
        }
        return result({ok: true, target: "soundDemo", position: ms / 1000});
      }

      var item = currentMedia();
      if (item && isFinite(item.currentTime)) {
        item.currentTime = ms / 1000;
        return result({ok: true, target: "media", position: item.currentTime});
      }
      return result({ok: false, error: "no-player"});
    }

    function setVolume(volume) {
      volume = Math.max(0, Math.min(100, Number(volume) || 0));
      var lowVolume = volume / 100;
      var touched = 0;

      ["localStorage", "sessionStorage"].forEach(function(storageName) {
        try {
          var storage = window[storageName];
          if (!storage) {
            return;
          }
          storage.setItem("volume", String(volume));
          storage.setItem("sound-volume", String(volume));
          storage.setItem("player-volume", String(volume));
        } catch (err) {}
      });

      if (window.store && typeof store.set === "function") {
        touched += tryCall(function() { store.set("volume", volume); }) ? 1 : 0;
        touched += tryCall(function() { store.set("sound-volume", volume); }) ? 1 : 0;
        touched += tryCall(function() { store.set("player-volume", volume); }) ? 1 : 0;
      }

      if (window.play && play.soundBox) {
        if (typeof play.soundBox.updateVolume === "function") {
          touched += tryCall(function() { play.soundBox.updateVolume(volume, true); }) ? 1 : 0;
        }
        if (typeof play.soundBox.setVolume === "function") {
          touched += tryCall(function() { play.soundBox.setVolume(volume, true); }) ? 1 : 0;
        }
      }

      var sound = demo();
      if (sound) {
        touched += setObjectVolume(sound, volume, false);
      }

      if (window.soundManager) {
        touched += setObjectVolume(soundManager, volume, false);
        touched += tryCall(function() { soundManager.setVolume(volume); }) ? 1 : 0;
        touched += tryCall(function() { soundManager.unmute(); }) ? 1 : 0;
        touched += tryCall(function() { soundManager.unmuteAll(); }) ? 1 : 0;
      }

      mediaElements().forEach(function(item) {
        touched += setObjectVolume(item, lowVolume, true);
      });

      touched += scanPlayerObjects(volume);
      return result({ok: true, volume: volume, touched: touched});
    }

    function setObjectVolume(target, volume, zeroToOne) {
      if (!target) {
        return 0;
      }

      var touched = 0;
      var methodValue = zeroToOne ? volume : Math.max(0, Math.min(100, volume));
      var propertyValue = methodValue;
      try {
        if (zeroToOne && "volume" in target && Number(target.volume) > 1) {
          propertyValue = Math.max(0, Math.min(100, volume * 100));
        } else if (!zeroToOne && "volume" in target && Number(target.volume) <= 1) {
          propertyValue = Math.max(0, Math.min(1, methodValue / 100));
        }
      } catch (err) {}

      if ("muted" in target) {
        touched += tryCall(function() { target.muted = false; }) ? 1 : 0;
      }
      if ("volume" in target && typeof target.volume !== "function") {
        touched += tryCall(function() { target.volume = propertyValue; }) ? 1 : 0;
      }
      ["setMuted", "mute"].forEach(function(name) {
        if (typeof target[name] === "function") {
          touched += tryCall(function() { target[name](false); }) ? 1 : 0;
        }
      });
      if (typeof target.unmute === "function") {
        touched += tryCall(function() { target.unmute(); }) ? 1 : 0;
      }

      ["setVolume", "changeVolume", "volume"].forEach(function(name) {
        if (typeof target[name] === "function") {
          touched += tryCall(function() { target[name](methodValue); }) ? 1 : 0;
        }
      });
      return touched;
    }

    function scanPlayerObjects(volume) {
      var touched = 0;
      var seen = typeof WeakSet === "function" ? new WeakSet() : null;
      var visits = 0;
      var maxVisits = 1800;
      var keysPattern = /player|audio|video|sound|volume|aegis|bili|mao|miss/i;

      function visit(target, depth) {
        if (!target || visits >= maxVisits) {
          return;
        }
        var kind = typeof target;
        if (kind !== "object" && kind !== "function") {
          return;
        }
        if (target === window || target === document || target.nodeType) {
          return;
        }
        try {
          if (seen) {
            if (seen.has(target)) {
              return;
            }
            seen.add(target);
          }
        } catch (err) {
          return;
        }

        visits += 1;
        touched += setObjectVolume(target, volume, false);
        if (depth <= 0) {
          return;
        }

        var keys = [];
        try {
          keys = Object.keys(target);
        } catch (err) {
          return;
        }
        keys.slice(0, 120).forEach(function(key) {
          if (depth < 3 || keysPattern.test(key)) {
            try {
              visit(target[key], depth - 1);
            } catch (err) {}
          }
        });
      }

      [
        window.index,
        window.play,
        window.soundManager,
        window.player,
        window.Player,
        window.aegis,
        window.Aegis,
        window.biliPlayer,
        window.BiliPlayer,
        window.__player,
        window.__PLAYER__
      ].forEach(function(root) {
        visit(root, 4);
      });

      Object.keys(window).forEach(function(key) {
        if (!keysPattern.test(key)) {
          return;
        }
        try {
          visit(window[key], 3);
        } catch (err) {}
      });
      return touched;
    }

    function togglePause() {
      var sound = demo();
      if (sound) {
        if (sound.paused) {
          if (typeof sound.resume === "function") {
            sound.resume();
          } else if (typeof sound.play === "function") {
            sound.play();
          }
          return result({ok: true, paused: false, target: "soundDemo"});
        }
        if (sound.playState === 0 && typeof sound.play === "function") {
          sound.play();
          return result({ok: true, paused: false, target: "soundDemo"});
        }
        if (typeof sound.pause === "function") {
          sound.pause();
          return result({ok: true, paused: true, target: "soundDemo"});
        }
      }

      if (click("#mpi")) {
        var button = jq("#mpi");
        var paused = !!(button && button.hasClass && !button.hasClass("mpip"));
        return result({ok: true, paused: paused, target: "button"});
      }

      var item = currentMedia();
      if (item) {
        if (item.paused) {
          playMedia(item);
        } else {
          item.pause();
        }
        return result({ok: true, paused: !!item.paused, target: "media"});
      }
      return result({ok: false, error: "no-player"});
    }

    function autoplay() {
      var sound = demo();
      var item = currentMedia();
      setVolume(value);

      if (sound && sound.playState === 0 && typeof sound.play === "function") {
        sound.play();
        return result({ok: true, target: "soundDemo"});
      }
      if (item && item.paused) {
        return result({ok: playMedia(item), target: "media"});
      }
      if (!window.__maoer_hidden_player_clicked_autoplay && click("#centerplaybtn")) {
        window.__maoer_hidden_player_clicked_autoplay = true;
        return result({ok: true, target: "centerplaybtn"});
      }
      if (!window.__maoer_hidden_player_clicked_autoplay && click("#mpi")) {
        window.__maoer_hidden_player_clicked_autoplay = true;
        return result({ok: true, target: "mpi"});
      }
      return result({ok: !!sound || !!item, target: "waiting"});
    }

    if (action === "autoplay" || action === "play") {
      return autoplay();
    }

    if (action === "guard") {
      return installSingleSoundGuard(value);
    }

    if (action === "status") {
      var statusSound = demo();
      var statusItem = currentMedia();
      var statusPositionMs = currentPositionMs(statusItem, statusSound);
      var statusDurationMs = currentDurationMs(statusItem, statusSound);
      var statusPaused = false;
      if (statusSound) {
        statusPaused = !!statusSound.paused || statusSound.playState === 0;
      } else if (statusItem) {
        statusPaused = !!statusItem.paused;
      }
      return result({
        ok: !!(statusSound || statusItem),
        position: statusPositionMs / 1000,
        duration: statusDurationMs / 1000,
        paused: statusPaused,
        rate: currentPlaybackRate(statusItem, statusSound),
        target: statusItem ? "media" : (statusSound ? "soundDemo" : "none")
      });
    }

    if (action === "seek") {
      var sound = demo();
      var item = currentMedia();
      var next = currentPositionMs(item, sound) + (Number(value) || 0) * 1000;
      return setPosition(next);
    }

    if (action === "pause") {
      return togglePause();
    }

    if (action === "volume") {
      return setVolume(value);
    }

    if (action === "rate") {
      return setPlaybackRate(value);
    }

    if (action === "stop") {
      var sound = demo();
      if (sound && typeof sound.stop === "function") {
        sound.stop();
      }
      mediaElements().forEach(function(item) {
        item.pause();
        try {
          item.currentTime = 0;
        } catch (err) {}
      });
      return result({ok: true});
    }

    return result({ok: false, error: "unknown-action"});
  } catch (err) {
    return JSON.stringify({ok: false, error: String(err && err.message ? err.message : err)});
  }
})
"""

class HiddenBrowserPlayer:
    def __init__(
        self,
        parent: wx.Window,
        cookie: str = "",
        volume: int = 100,
    ) -> None:
        self.parent = parent
        self.cookie = cookie
        self._volume = volume
        self._webview: html2.WebView | None = None
        self._current: PlaybackInfo | None = None
        self._load_generation = 0
        self._page_loaded = False
        self._paused = False
        self._suppress_autoplay = False
        self._playback_rate = 1.0
        self._cookie_primer_target: str | None = None
        self._cookie_primer_generation = 0
        self._script_callbacks: dict[int, tuple[str, ScriptCallback]] = {}
        self._next_script_callback_id = 1

    def play(self, playback: PlaybackInfo) -> None:
        page_url = playback.page_url or f"https://www.missevan.com/sound/player?id={playback.sound_id}"
        debug_log(f"play sound_id={playback.sound_id} title={playback.title!r} url={page_url}")
        webview = self._ensure_webview()
        self._current = playback
        self._page_loaded = False
        self._paused = False
        self._suppress_autoplay = False
        self._playback_rate = 1.0
        self._load_generation += 1
        self._install_user_scripts(webview)
        if self.cookie:
            self._cookie_primer_target = page_url
            self._cookie_primer_generation = self._load_generation
            webview.LoadURL(BASE_URL + "/")
            wx.CallLater(2200, self._load_cookie_primer_target, self._load_generation)
            return

        self._load_playback_url(page_url, self._load_generation)

    def _load_playback_url(self, page_url: str, generation: int) -> None:
        if self._webview is None or generation != self._load_generation or self._current is None:
            return
        self._cookie_primer_target = None
        self._webview.LoadURL(page_url)
        wx.CallLater(1500, self._mark_loaded_if_current, self._load_generation)
        wx.CallLater(2500, self._schedule_autoplay, self._load_generation)

    def seek(self, seconds: int) -> None:
        self._run_control("seek", seconds)

    def set_playback_rate(self, rate: float, callback: ScriptCallback) -> None:
        rate = self._clamp_playback_rate(rate)

        def done(result: dict[str, object] | None) -> None:
            if result and result.get("ok"):
                actual_rate = self._float_value(result.get("rate")) or rate
                self._playback_rate = self._clamp_playback_rate(actual_rate)
                result["rate"] = self._playback_rate
            callback(result)

        self._run_control_callback("rate", rate, done)

    def volume_up(self, step: int = 10) -> int:
        return self._change_volume(step)

    def volume_down(self, step: int = 10) -> int:
        return self._change_volume(-step)

    def toggle_pause(self) -> bool:
        self._suppress_autoplay = True
        self._paused = not self._paused
        self._run_control("pause")
        return self._paused

    def is_paused(self) -> bool:
        return self._paused

    def status(self, callback: Callable[[dict[str, object] | None], None]) -> None:
        self._run_control_callback("status", None, callback)

    def _run_control_callback(self, action: str, value: object | None, callback: ScriptCallback) -> None:
        callback_id = self._next_script_callback_id
        self._next_script_callback_id += 1
        self._script_callbacks[callback_id] = (action, callback)
        if not self._run_control_async(action, value, callback_id):
            self._script_callbacks.pop(callback_id, None)
            wx.CallAfter(callback, None)
            return
        wx.CallLater(1200, self._expire_script_callback, callback_id)

    def stop(self) -> None:
        if self._webview is None:
            return
        self._suppress_autoplay = True
        self._paused = False
        self._run_control("stop")
        self._current = None
        self._page_loaded = False
        self._webview.LoadURL("about:blank")

    def shutdown(self) -> None:
        if self._webview is not None:
            self.stop()
            self._webview.Destroy()
            self._webview = None

    def _change_volume(self, delta: int) -> int:
        old_volume = self._volume
        self._volume = max(0, min(100, self._volume + delta))
        debug_log(f"change_volume delta={delta} old={old_volume} new={self._volume}")
        self._apply_volume(self._volume)
        return self._volume

    def _apply_volume(self, volume: int) -> None:
        changed = set_current_app_volume(volume, include_process_names=WEBVIEW_AUDIO_PROCESS_NAMES)
        debug_log(f"apply_volume immediate volume={volume} audio_session_changed={changed}")
        self._run_control("volume", volume)
        wx.CallLater(250, self._apply_volume_if_current, volume)
        wx.CallLater(900, self._apply_volume_if_current, volume)

    def _apply_volume_if_current(self, volume: int) -> None:
        if self._webview is None or volume != self._volume:
            debug_log(f"apply_volume delayed skip requested={volume} current={self._volume} webview={self._webview is not None}")
            return
        changed = set_current_app_volume(volume, include_process_names=WEBVIEW_AUDIO_PROCESS_NAMES)
        debug_log(f"apply_volume delayed volume={volume} audio_session_changed={changed}")
        self._run_control("volume", volume)

    def _ensure_webview(self) -> html2.WebView:
        if self._webview is not None:
            return self._webview

        self._prepare_environment()
        if not html2.WebView.IsBackendAvailable(html2.WebViewBackendEdge):
            raise PlayerUnavailable("当前系统没有可用的 WebView2 Runtime，无法播放 DRM 内容。请安装 Microsoft Edge WebView2 Runtime。")

        webview = html2.WebView.New(
            self.parent,
            url="about:blank",
            pos=(-32000, -32000),
            size=(1, 1),
            backend=html2.WebViewBackendEdge,
        )
        webview.Move(-32000, -32000)
        webview.SetSize((1, 1))
        webview.Bind(html2.EVT_WEBVIEW_LOADED, self._on_loaded)
        webview.Bind(html2.EVT_WEBVIEW_NAVIGATED, self._on_navigated)
        webview.Bind(html2.EVT_WEBVIEW_ERROR, self._on_error)
        webview.Bind(html2.EVT_WEBVIEW_SCRIPT_RESULT, self._on_script_result)

        self._install_user_scripts(webview)

        self._webview = webview
        debug_log("webview created")
        return webview

    def _install_user_scripts(self, webview: html2.WebView) -> None:
        try:
            webview.RemoveAllUserScripts()
        except Exception:
            pass
        webview.AddUserScript(self._single_sound_guard_script(), html2.WEBVIEW_INJECT_AT_DOCUMENT_START)
        webview.AddUserScript(self._volume_bootstrap_script(), html2.WEBVIEW_INJECT_AT_DOCUMENT_START)
        cookie_script = self._cookie_script()
        if cookie_script:
            webview.AddUserScript(cookie_script, html2.WEBVIEW_INJECT_AT_DOCUMENT_START)

    def _prepare_environment(self) -> None:
        os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--autoplay-policy=no-user-gesture-required")
        profile = webview2_profile_dir()
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(profile)

    def _cookie_script(self) -> str:
        pairs = []
        for part in self.cookie.split(";"):
            item = part.strip()
            if not item or "=" not in item:
                continue
            name, value = item.split("=", 1)
            name = name.strip()
            if not name:
                continue
            pairs.append(f"{name}={value.strip()}")
        if not pairs:
            return ""

        return (
            "(function(){"
            f"var cookies={json.dumps(pairs)};"
            "cookies.forEach(function(cookie){"
            "try{document.cookie=cookie+'; path=/; domain=.missevan.com; secure; SameSite=None';}catch(err){}"
            "try{document.cookie=cookie+'; path=/';}catch(err){}"
            "});"
            "})();"
        )

    def _volume_bootstrap_script(self) -> str:
        return (
            "(function(){"
            f"var volume={json.dumps(self._volume)};"
            "['localStorage','sessionStorage'].forEach(function(name){"
            "try{"
            "var storage=window[name];"
            "if(!storage){return;}"
            "storage.setItem('volume',String(volume));"
            "storage.setItem('sound-volume',String(volume));"
            "storage.setItem('player-volume',String(volume));"
            "}catch(err){}"
            "});"
            "})();"
        )

    def _on_loaded(self, _event: wx.Event) -> None:
        debug_log(f"loaded current={self._current is not None} generation={self._load_generation}")
        if self._cookie_primer_target:
            self._run_cookie_script_now()
            wx.CallLater(150, self._load_cookie_primer_target, self._cookie_primer_generation)
            return
        if self._current is None:
            return
        self._page_loaded = True
        self._schedule_autoplay(self._load_generation)

    def _on_navigated(self, _event: wx.Event) -> None:
        debug_log(f"navigated current={self._current is not None}")
        if self._cookie_primer_target:
            return
        if self._current is not None:
            navigated_id = self._event_sound_id(_event)
            if (
                navigated_id is not None
                and navigated_id != self._current.sound_id
            ):
                debug_log(f"blocked auto-next current={self._current.sound_id} navigated={navigated_id}")
                wx.CallAfter(self._block_auto_next, self._load_generation)
                return
            self._page_loaded = True

    def _run_cookie_script_now(self) -> None:
        script = self._cookie_script()
        if not script or self._webview is None:
            return
        try:
            if hasattr(self._webview, "RunScriptAsync"):
                self._webview.RunScriptAsync(script)
            else:
                self._webview.RunScript(script)
        except Exception:
            debug_log("cookie script failed")

    def _load_cookie_primer_target(self, generation: int) -> None:
        if generation != self._load_generation or self._webview is None or self._current is None:
            return
        target = self._cookie_primer_target
        if not target:
            return
        self._run_cookie_script_now()
        self._load_playback_url(target, generation)

    def _mark_loaded_if_current(self, generation: int) -> None:
        if self._current is not None and generation == self._load_generation:
            self._page_loaded = True

    def _on_error(self, event: wx.Event) -> None:
        self._current = None
        self._page_loaded = False
        event.Skip()

    def _schedule_autoplay(self, generation: int, attempts: int = 12) -> None:
        if attempts <= 0 or generation != self._load_generation or self._current is None or self._suppress_autoplay:
            debug_log(
                "autoplay skip "
                f"attempts={attempts} generation={generation} current_generation={self._load_generation} "
                f"has_current={self._current is not None} suppress={self._suppress_autoplay}"
            )
            return

        changed = set_current_app_volume(self._volume, include_process_names=WEBVIEW_AUDIO_PROCESS_NAMES)
        debug_log(f"autoplay attempt={13 - attempts} volume={self._volume} audio_session_changed={changed}")
        self._run_control("guard", self._single_sound_guard_options())
        self._run_control("autoplay", self._volume)
        wx.CallLater(800, self._schedule_autoplay, generation, attempts - 1)

    def _single_sound_guard_script(self) -> str:
        return f"{CONTROL_SCRIPT}({json.dumps('guard')}, {json.dumps(self._single_sound_guard_options())});"

    def _single_sound_guard_options(self) -> dict[str, object]:
        return {
            "soundId": self._current.sound_id if self._current is not None else None,
            "url": self._current.url if self._current is not None else "",
            "generation": self._load_generation,
        }

    def _event_sound_id(self, event: wx.Event) -> int | None:
        try:
            url = event.GetURL()
        except Exception:
            return None
        return self._sound_id_from_url(url)

    @staticmethod
    def _sound_id_from_url(url: str) -> int | None:
        try:
            query = parse_qs(urlparse(url).query)
        except Exception:
            return None
        for value in query.get("id") or query.get("soundid") or ():
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None

    def _block_auto_next(self, generation: int) -> None:
        if generation != self._load_generation or self._webview is None or self._current is None:
            return
        self.stop()

    def _on_script_result(self, event: wx.Event) -> None:
        try:
            result = event.GetString()
        except Exception:
            result = ""
        parsed = self._parse_control_result(result)
        if not parsed:
            debug_log(f"script_result ignored result={parsed}")
            return

        parsed_action = str(parsed.get("action") or "")
        callback_id = self._script_callback_id_from_event(event)
        callback_entry = self._script_callbacks.pop(callback_id, None) if callback_id is not None else None
        if callback_entry is None and parsed_action:
            for pending_id, (expected_action, _callback) in list(self._script_callbacks.items()):
                if expected_action == parsed_action:
                    callback_id = pending_id
                    callback_entry = self._script_callbacks.pop(pending_id)
                    break
        if callback_entry is None:
            debug_log(
                f"script_result unmatched action={parsed_action!r} "
                f"callback_id={callback_id} pending={len(self._script_callbacks)}"
            )
            return
        expected_action, callback = callback_entry
        if expected_action != parsed_action:
            debug_log(
                f"script_result action mismatch expected={expected_action!r} "
                f"actual={parsed_action!r} callback_id={callback_id}"
            )
            callback(None)
            return
        debug_log(f"script_result callback_id={callback_id} result={parsed}")
        callback(parsed)

    def _expire_script_callback(self, callback_id: int) -> None:
        callback_entry = self._script_callbacks.pop(callback_id, None)
        if callback_entry is None:
            return
        _action, callback = callback_entry
        debug_log(f"script callback timeout id={callback_id}")
        callback(None)

    def _script_callback_id_from_event(self, event: wx.Event) -> int | None:
        for getter_name in ("GetInt", "GetClientData"):
            getter = getattr(event, getter_name, None)
            if getter is None:
                continue
            try:
                value = getter()
            except Exception:
                continue
            try:
                callback_id = int(value)
            except (TypeError, ValueError):
                continue
            if callback_id in self._script_callbacks:
                return callback_id
        return None

    def _run_control_async(self, action: str, value: object | None, callback_id: int) -> bool:
        if self._webview is None or not hasattr(self._webview, "RunScriptAsync"):
            debug_log(f"run_control_async skip action={action} webview={self._webview is not None}")
            return False
        script = f"{CONTROL_SCRIPT}({json.dumps(action)}, {json.dumps(value)})"
        try:
            self._webview.RunScriptAsync(script, callback_id)
        except Exception as exc:
            debug_log(f"run_control_async exception action={action} id={callback_id}: {type(exc).__name__}: {exc}")
            return False
        return True

    def _run_control(self, action: str, value: object | None = None) -> str:
        if self._webview is None:
            debug_log(f"run_control skip action={action} webview=None")
            return ""
        script = f"{CONTROL_SCRIPT}({json.dumps(action)}, {json.dumps(value)})"
        try:
            if hasattr(self._webview, "RunScriptAsync"):
                debug_log(f"run_control async action={action} value={value}")
                self._webview.RunScriptAsync(script)
                return ""
            ok, result = self._webview.RunScript(script)
        except Exception:
            debug_log(f"run_control exception action={action} value={value}")
            return ""
        debug_log(f"run_control sync action={action} value={value} ok={ok} result={result}")
        return result if ok else ""

    @staticmethod
    def _clamp_playback_rate(rate: float) -> float:
        try:
            value = float(rate)
        except (TypeError, ValueError):
            value = 1.0
        value = round(value, 1)
        return max(MIN_PLAYBACK_RATE, min(MAX_PLAYBACK_RATE, value))

    @staticmethod
    def _float_value(value: object) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _parse_control_result(result: str) -> dict[str, object] | None:
        try:
            decoded = json.loads(result)
            if isinstance(decoded, str):
                decoded = json.loads(decoded)
        except Exception:
            return None
        return decoded if isinstance(decoded, dict) else None
