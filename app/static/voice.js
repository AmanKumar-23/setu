/* Support Coach — language + voice, shared by /portal and /portal/chat.
 *
 * Browser APIs only: SpeechRecognition for dictation, speechSynthesis for
 * playback. Neither is universal, so every entry point here feature-detects
 * first and degrades to a disabled control with a tooltip. Voice is an input
 * and output method, never a second pipeline: dictation only fills a textarea
 * that the customer still has to send.
 */
(function (w) {
  "use strict";

  var SC = (w.SC = w.SC || {});

  /* ---------- storage: never let a locked-down browser break the page ---- */

  SC.get = function (key, fallback) {
    try {
      var v = w.localStorage.getItem(key);
      return v === null ? fallback : v;
    } catch (e) { return fallback; }
  };
  SC.set = function (key, value) {
    try { w.localStorage.setItem(key, value); } catch (e) { /* private mode */ }
  };

  /* ---------- language ---------------------------------------------------- */

  SC.languages = {};          // code -> {native, english, speech}
  SC.language = "en";         // the customer's chosen reply language

  SC.speechCode = function (code) {
    var row = SC.languages[code || SC.language];
    return (row && row.speech) || "en-IN";
  };
  SC.nativeName = function (code) {
    var row = SC.languages[code || SC.language];
    return (row && row.native) || "English";
  };

  /* Fills a <select> and wires it to POST /api/portal/language.
     `onChange` runs after the server has stored the choice. */
  SC.mountPicker = function (select, onChange) {
    if (!select) return Promise.resolve();
    return fetch("/api/portal/languages")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        SC.languages = {};
        select.innerHTML = d.languages.map(function (l) {
          SC.languages[l.code] = l;
          return '<option value="' + l.code + '">' + l.native + "</option>";
        }).join("");
        SC.language = d.selected || "en";
        select.value = SC.language;
        select.onchange = function () { SC.setLanguage(select.value, onChange); };
        if (onChange) onChange(SC.language);
      })
      .catch(function () {
        // The picker stays empty; the page still works, and anything waiting
        // on the language still gets told which one we are using.
        if (onChange) onChange(SC.language);
      });
  };

  SC.setLanguage = function (code, done) {
    SC.language = code;
    SC.stop();
    fetch("/api/portal/language", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ language: code }),
    }).catch(function () { /* the choice still holds for this page */ });
    if (done) done(code);
  };

  /* ---------- the global voice switch ------------------------------------ */

  SC.voiceOn = SC.get("sc.voice", "1") !== "0";
  var listeners = [];

  SC.onVoiceChange = function (fn) { listeners.push(fn); fn(SC.voiceOn); };

  SC.mountVoiceToggle = function (btn) {
    if (!btn) return;
    var paint = function () {
      btn.setAttribute("aria-pressed", SC.voiceOn ? "true" : "false");
      btn.title = SC.voiceOn ? "Turn voice controls off" : "Turn voice controls on";
    };
    btn.onclick = function () {
      SC.voiceOn = !SC.voiceOn;
      SC.set("sc.voice", SC.voiceOn ? "1" : "0");
      if (!SC.voiceOn) { SC.stop(); SC.stopDictation(); }
      paint();
      listeners.forEach(function (fn) { try { fn(SC.voiceOn); } catch (e) {} });
    };
    paint();
  };

  /* ---------- dictation --------------------------------------------------- */

  var Recog = w.SpeechRecognition || w.webkitSpeechRecognition;
  SC.canDictate = !!Recog;
  var active = null;           // the one running recogniser

  SC.stopDictation = function () { if (active) { try { active.stop(); } catch (e) {} } };

  /* Wires a mic button to a textarea.
   *   opts.ghost  element that shows the interim words, greyed
   *   opts.hint   element shown when the engine was unsure
   *   opts.onVoice() fires once the field has any dictated text
   * Returns a handle with .disable() so the voice toggle can switch it off. */
  SC.attachMic = function (btn, field, opts) {
    opts = opts || {};
    var off = false;

    var deny = function (why) {
      btn.disabled = true;
      btn.setAttribute("aria-pressed", "false");
      btn.title = why;
    };

    if (!SC.canDictate) {
      deny("Voice input needs Chrome, Edge or Safari. Type your message instead.");
      return { disable: function () {}, enable: function () {} };
    }

    var rec = null, base = "", said = "", shaky = false;

    var finish = function () {
      btn.setAttribute("aria-pressed", "false");
      if (opts.ghost) { opts.ghost.textContent = ""; opts.ghost.hidden = true; }
      active = null;
      rec = null;
    };

    var start = function () {
      rec = new Recog();
      rec.lang = SC.speechCode();
      rec.interimResults = true;
      rec.continuous = true;
      rec.maxAlternatives = 1;
      base = field.value ? field.value.replace(/\s+$/, "") + " " : "";
      said = "";
      shaky = false;
      if (opts.hint) opts.hint.hidden = true;

      rec.onstart = function () { btn.setAttribute("aria-pressed", "true"); };

      rec.onresult = function (ev) {
        var interim = "";
        for (var i = ev.resultIndex; i < ev.results.length; i++) {
          var r = ev.results[i], chunk = r[0].transcript;
          if (r.isFinal) {
            said += chunk;
            // Some engines report 0 confidence rather than a real score; only
            // a genuine low reading should raise the flag.
            if (r[0].confidence > 0 && r[0].confidence < 0.6) shaky = true;
          } else {
            interim += chunk;
          }
        }
        // The textarea holds what the engine has committed to. The interim
        // words sit greyed underneath until they firm up, because a plain
        // textarea cannot style half of its own text.
        field.value = base + said;
        if (opts.ghost) {
          opts.ghost.textContent = interim;
          opts.ghost.hidden = !interim;
        }
        if (said && opts.onVoice) opts.onVoice();
        field.dispatchEvent(new Event("input", { bubbles: true }));
      };

      rec.onerror = function (ev) {
        finish();
        if (ev.error === "not-allowed" || ev.error === "service-not-allowed") {
          deny("Microphone blocked. Allow it in your browser settings to dictate.");
        } else if (ev.error === "no-speech" && opts.hint) {
          opts.hint.textContent = "We did not hear anything. Try again, or type it.";
          opts.hint.hidden = false;
        }
      };

      rec.onend = function () {
        if (shaky && opts.hint) {
          opts.hint.textContent =
            "⚠ Check this — we were not sure we heard it right.";
          opts.hint.hidden = false;
        }
        finish();
        field.focus();     // the text stays editable; nothing is ever sent
      };

      try { rec.start(); active = rec; }
      catch (e) { finish(); }
    };

    btn.onclick = function () {
      if (off) return;
      if (rec) { try { rec.stop(); } catch (e) { finish(); } return; }
      SC.stopDictation();
      start();
    };

    return {
      disable: function () { off = true; btn.disabled = true; SC.stopDictation();
                             btn.title = "Voice is off"; },
      enable: function () { off = false; btn.disabled = false;
                            btn.title = "Speak instead of typing"; },
    };
  };

  /* ---------- playback ---------------------------------------------------- */

  SC.canSpeak = !!w.speechSynthesis && typeof w.SpeechSynthesisUtterance === "function";
  var speaking = null;         // {stop: fn} for whatever is playing

  function pickVoice(code) {
    var want = SC.speechCode(code);
    var voices = [];
    try { voices = w.speechSynthesis.getVoices() || []; } catch (e) { return null; }
    var exact = voices.filter(function (v) { return v.lang === want; })[0];
    if (exact) return exact;
    var stem = want.split("-")[0];
    return voices.filter(function (v) {
      return (v.lang || "").toLowerCase().indexOf(stem) === 0;
    })[0] || null;    // null means "let the browser choose"
  }

  SC.stop = function () {
    if (speaking) { var s = speaking; speaking = null; s.done(); }
    try { w.speechSynthesis.cancel(); } catch (e) {}
  };

  /* Speaks `text` in `code`. onStart/onEnd let a button flip to Stop.
     Only one utterance plays at a time. */
  SC.speak = function (text, code, onStart, onEnd) {
    if (!SC.canSpeak || !SC.voiceOn || !text) { if (onEnd) onEnd(); return; }
    SC.stop();
    var u = new w.SpeechSynthesisUtterance(text);
    var v = pickVoice(code);
    if (v) u.voice = v;
    u.lang = SC.speechCode(code);
    u.rate = 0.98;
    var settled = false;
    var done = function () {
      if (settled) return;
      settled = true;
      if (speaking && speaking.u === u) speaking = null;
      if (onEnd) onEnd();
    };
    u.onend = done;
    u.onerror = done;
    speaking = { u: u, done: done };
    if (onStart) onStart();
    try { w.speechSynthesis.speak(u); } catch (e) { done(); }
  };

  // Chrome loads voices asynchronously; nothing to do but let the list refresh.
  if (SC.canSpeak && typeof w.speechSynthesis.addEventListener === "function") {
    try { w.speechSynthesis.addEventListener("voiceschanged", function () {}); }
    catch (e) {}
  }
  // A page left mid-sentence should not keep talking.
  w.addEventListener("pagehide", function () { SC.stop(); });
})(window);
