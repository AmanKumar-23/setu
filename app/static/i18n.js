/* सेतु · Setu — the customer-side string catalogue.
 *
 * Every word a customer can see on /portal and /portal/chat lives here, keyed.
 * English is the source text; other languages arrive from the server, which
 * translates this same catalogue with the model and caches the result per
 * language. Nothing is hardcoded per language except the Hindi seed below,
 * which exists so the two languages the author can proofread switch with no
 * round trip at all.
 *
 * What is NOT in here, on purpose: ticket ids, order and refund ids, amounts,
 * dates, and anything the customer themselves wrote. Those are never
 * translated, in any language, by anything.
 */
(function (w) {
  "use strict";

  var SC = (w.SC = w.SC || {});

  var EN = {
    /* ---- shell, on both pages ---- */
    "header.subtitle": "Customer portal",
    "language": "Language",
    "signOut": "Sign out",
    "voice.button": "Voice",
    "voice.turnOff": "Turn voice controls off",
    "voice.turnOn": "Turn voice controls on",
    "voice.isOff": "Voice is off",
    "voice.speakInstead": "Speak instead of typing",
    "voice.speakIn": "Speak in {lang}",
    "voice.unsupported":
      "Voice input needs Chrome, Edge or Safari. Type your message instead.",
    "voice.blocked":
      "Microphone blocked. Allow it in your browser settings to dictate.",
    "voice.nothingHeard": "We did not hear anything. Try again, or type it.",
    "voice.lowConfidence":
      "⚠ Check this — we were not sure we heard it right.",

    /* ---- common issues ---- */
    "faq.title": "Common issues — find your answer instantly",
    "faq.sub": "Tap one to start a conversation about it straight away.",

    /* ---- the ticket list ---- */
    "tickets.title": "Your tickets",
    "tickets.tagline": "AI where it’s safe. Human where it matters.",
    "tickets.welcome": "Welcome back, {name}",
    "tickets.one": "1 ticket",
    "tickets.many": "{n} tickets",
    "tickets.new": "New support request",
    "th.ticket": "Ticket",
    "th.subject": "Subject",
    "th.category": "Category",
    "th.status": "Status",
    "th.rating": "Rating",
    "th.updated": "Last updated",
    "action.open": "Open",
    "action.rateThis": "Rate this",
    "rating.youRated": "You rated this {n} of 5",
    "empty.title": "No tickets yet",
    "empty.body": "Raise your first one and we will pick it up straight away.",
    "notice.notYours": "That ticket is not yours. Here are your own.",

    /* ---- the three states a customer is ever shown ---- */
    "status.Handled by AI": "Handled by AI",
    "status.A human agent is reviewing": "A human agent is reviewing",
    "status.Resolved": "Resolved",

    /* ---- categories, keyed by their English value ---- */
    "cat.Recharge": "Recharge",
    "cat.Refund": "Refund",
    "cat.Order & Delivery": "Order & Delivery",
    "cat.Network": "Network",
    "cat.Account & Login": "Account & Login",
    "cat.Other": "Other",

    /* ---- relative time. The NUMBER stays a Latin numeral. ---- */
    "time.justNow": "just now",
    "time.minutes": "{n}m ago",
    "time.hours": "{n}h ago",
    "time.days": "{n}d ago",

    /* ---- the new-ticket form ---- */
    "modal.title": "New support request",
    "modal.sub": "Tell us what happened and we will look at it straight away.",
    "modal.subject": "Subject",
    "modal.subjectPlaceholder": "Recharge failed but money was deducted",
    "modal.category": "Category",
    "modal.what": "What happened",
    "modal.whatPlaceholder": "Describe the problem in your own words.",
    "modal.cancel": "Cancel",
    "modal.submit": "Raise ticket",
    "modal.raising": "Raising…",
    "modal.created": "Ticket created",
    "modal.lookingNow": "We’re looking at this now.",
    "err.unreachable": "Could not reach the server.",
    "err.couldNotRaise": "Could not raise that ticket.",

    /* ---- the conversation ---- */
    "chat.back": "← All tickets",
    "chat.placeholder": "Type your message…",
    "chat.send": "Send",
    "chat.sending": "Sending",
    "chat.thinking": "Looking at this…",
    "chat.humanJoining": "A human agent is joining this conversation",
    "chat.sentByVoice": "Sent by voice",
    "play.play": "Play",
    "play.stop": "Stop",
    "player.playLatest": "Play latest reply",
    "offer.question": "Reply in {lang} instead?",
    "offer.yes": "Yes, switch",
    "offer.no": "No, keep {lang}",

    /* ---- rating ---- */
    "rate.title": "Was your issue resolved?",
    "rate.titleDone": "Thanks — you rated this",
    "rate.sub": "Tell us how it went. One tap, and a line if you want to.",
    "rate.subDone": "You can change this for 24 hours after rating.",
    "rate.placeholder": "Anything you want to add? (optional)",
    "rate.send": "Send rating",
    "rate.update": "Update rating",
    "rate.pickStar": "Pick a star first.",
    "rate.sending": "Sending…",
    "rate.couldNotSave": "Could not save that.",
    "rate.thanks": "Thanks for rating this",
    "rate.ariaStars": "Rate from 1 to 5",
    "faq.opening": "Opening a ticket…",
    "faq.showAll": "Show all {n}",
    "faq.showFewer": "Show fewer",
    "faq.couldNotOpen": "Could not open a ticket. Try again, or use New support request.",
    "cat.Billing & Payments": "Billing & Payments",
    "cat.App & Technical": "App & Technical",
    "note.assistantBusy": "Thanks — we have your message. Our assistant is taking longer than usual, so a member of our team will reply here shortly.",
  };

  /* Hindi, written by hand rather than bought from the model, so the two
     languages the author reads switch instantly and can be proofread. Every
     other language comes from the server. */
  var HI = {
    "header.subtitle": "ग्राहक पोर्टल",
    "language": "भाषा",
    "signOut": "साइन आउट",
    "voice.button": "आवाज़",
    "voice.turnOff": "आवाज़ नियंत्रण बंद करें",
    "voice.turnOn": "आवाज़ नियंत्रण चालू करें",
    "voice.isOff": "आवाज़ बंद है",
    "voice.speakInstead": "टाइप करने के बजाय बोलें",
    "voice.speakIn": "{lang} में बोलें",
    "voice.unsupported":
      "आवाज़ से लिखने के लिए Chrome, Edge या Safari चाहिए। कृपया टाइप करें।",
    "voice.blocked":
      "माइक बंद है। बोलकर लिखने के लिए ब्राउज़र सेटिंग में इसे अनुमति दें।",
    "voice.nothingHeard": "हमें कुछ सुनाई नहीं दिया। फिर से कोशिश करें, या टाइप करें।",
    "voice.lowConfidence":
      "⚠ इसे जाँच लें — हमें ठीक से सुनाई नहीं दिया।",

    "faq.title": "आम समस्याएं — तुरंत जवाब पाएं",
    "faq.sub": "एक चुनें और उसी पर तुरंत बातचीत शुरू करें।",

    "tickets.title": "आपके टिकट",
    "tickets.tagline": "जहाँ सुरक्षित हो वहाँ AI। जहाँ ज़रूरी हो वहाँ इंसान।",
    "tickets.welcome": "वापस स्वागत है, {name}",
    "tickets.one": "1 टिकट",
    "tickets.many": "{n} टिकट",
    "tickets.new": "नया अनुरोध",
    "th.ticket": "टिकट",
    "th.subject": "विषय",
    "th.category": "श्रेणी",
    "th.status": "स्थिति",
    "th.rating": "रेटिंग",
    "th.updated": "अंतिम बदलाव",
    "action.open": "खोलें",
    "action.rateThis": "रेट करें",
    "rating.youRated": "आपने इसे 5 में से {n} दिया",
    "empty.title": "अभी कोई टिकट नहीं",
    "empty.body": "पहला टिकट बनाएँ, हम तुरंत देख लेंगे।",
    "notice.notYours": "यह टिकट आपका नहीं है। ये रहे आपके अपने।",

    "status.Handled by AI": "AI ने संभाल लिया",
    "status.A human agent is reviewing": "एक इंसान देख रहा है",
    "status.Resolved": "हल हो गया",

    "cat.Recharge": "रिचार्ज",
    "cat.Refund": "रिफंड",
    "cat.Order & Delivery": "ऑर्डर और डिलीवरी",
    "cat.Network": "नेटवर्क",
    "cat.Account & Login": "खाता और लॉगिन",
    "cat.Other": "अन्य",

    "time.justNow": "अभी-अभी",
    "time.minutes": "{n}म पहले",
    "time.hours": "{n}घं पहले",
    "time.days": "{n}दि पहले",

    "modal.title": "नया अनुरोध",
    "modal.sub": "बताइए क्या हुआ, हम तुरंत देखते हैं।",
    "modal.subject": "विषय",
    "modal.subjectPlaceholder": "रिचार्ज फेल हो गया पर पैसे कट गए",
    "modal.category": "श्रेणी",
    "modal.what": "क्या हुआ",
    "modal.whatPlaceholder": "अपने शब्दों में समस्या बताएँ।",
    "modal.cancel": "रद्द करें",
    "modal.submit": "टिकट बनाएँ",
    "modal.raising": "बना रहे हैं…",
    "modal.created": "टिकट बन गया",
    "modal.lookingNow": "हम इसे अभी देख रहे हैं।",
    "err.unreachable": "सर्वर से संपर्क नहीं हो पाया।",
    "err.couldNotRaise": "यह टिकट बन नहीं पाया।",

    "chat.back": "← सभी टिकट",
    "chat.placeholder": "अपना संदेश लिखें…",
    "chat.send": "भेजें",
    "chat.sending": "भेज रहे हैं",
    "chat.thinking": "देख रहे हैं…",
    "chat.humanJoining": "एक इंसान इस बातचीत में शामिल हो रहा है",
    "chat.sentByVoice": "बोलकर भेजा गया",
    "play.play": "सुनें",
    "play.stop": "रोकें",
    "player.playLatest": "नया जवाब सुनें",
    "offer.question": "क्या {lang} में जवाब दें?",
    "offer.yes": "हाँ, बदलें",
    "offer.no": "नहीं, {lang} ही रखें",

    "rate.title": "क्या आपकी समस्या हल हुई?",
    "rate.titleDone": "धन्यवाद — आपने रेट किया",
    "rate.sub": "बताइए कैसा रहा। एक टैप, और चाहें तो एक लाइन।",
    "rate.subDone": "रेट करने के बाद 24 घंटे तक बदल सकते हैं।",
    "rate.placeholder": "कुछ और कहना चाहेंगे? (ऐच्छिक)",
    "rate.send": "रेटिंग भेजें",
    "rate.update": "रेटिंग बदलें",
    "rate.pickStar": "पहले एक सितारा चुनें।",
    "rate.sending": "भेज रहे हैं…",
    "rate.couldNotSave": "यह सहेजा नहीं जा सका।",
    "rate.thanks": "रेट करने के लिए धन्यवाद",
    "rate.ariaStars": "1 से 5 तक रेट करें",
    "faq.opening": "टिकट खोल रहे हैं…",
    "faq.showAll": "सभी {n} दिखाएँ",
    "faq.showFewer": "कम दिखाएँ",
    "faq.couldNotOpen": "टिकट नहीं खुल पाया। फिर कोशिश करें, या नया अनुरोध चुनें।",
    "cat.Billing & Payments": "बिलिंग और भुगतान",
    "cat.App & Technical": "ऐप और तकनीकी",
    "note.assistantBusy": "धन्यवाद — आपका संदेश हमें मिल गया है। हमारा सहायक अभी सामान्य से ज़्यादा समय ले रहा है, इसलिए हमारी टीम का कोई सदस्य जल्द ही यहीं जवाब देगा।",
  };

  /* Bumped whenever the English changes, so a browser that cached a
     translation of the old words fetches the new ones. */
  SC.CATALOGUE = "2";

  SC.EN = EN;
  SC.strings = EN;
  var listeners = [];

  /* Look a key up, filling {placeholders}. An unknown key falls back to
     English rather than showing the raw key: a missing translation should
     read as untranslated, never as broken. */
  SC.t = function (key, vars) {
    var text = SC.strings[key];
    if (text == null) text = EN[key];
    if (text == null) return key;
    if (!vars) return text;
    return text.replace(/\{(\w+)\}/g, function (whole, name) {
      return vars[name] == null ? whole : vars[name];
    });
  };

  /* Swap the whole catalogue and tell every page that paints from it.
     Missing keys fall through to English, so a partial translation degrades
     one string at a time instead of emptying the page. */
  SC.useStrings = function (table) {
    SC.strings = Object.assign({}, EN, table || {});
    SC.paintStatic();
    listeners.forEach(function (fn) { try { fn(); } catch (e) {} });
  };

  SC.seedFor = function (code) { return code === "hi" ? HI : null; };

  SC.onStrings = function (fn) { listeners.push(fn); };

  /* Everything static in the markup carries its key, so a language change is
     a walk of the document rather than a list of getElementById calls that
     somebody has to remember to extend. */
  SC.paintStatic = function (root) {
    var scope = root || document;
    scope.querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = SC.t(el.getAttribute("data-i18n"));
    });
    scope.querySelectorAll("[data-i18n-ph]").forEach(function (el) {
      el.placeholder = SC.t(el.getAttribute("data-i18n-ph"));
    });
    scope.querySelectorAll("[data-i18n-title]").forEach(function (el) {
      el.title = SC.t(el.getAttribute("data-i18n-title"));
    });
    scope.querySelectorAll("[data-i18n-aria]").forEach(function (el) {
      el.setAttribute("aria-label", SC.t(el.getAttribute("data-i18n-aria")));
    });
  };

  /* ---------- the dev-only audit ----------
     Turn on with ?i18n=audit, or SC.audit() from the console. Walks every
     visible text node and reports any that is not a catalogue value, so a
     string somebody forgot to key shows up as a warning rather than as
     English sitting quietly in a Tamil page. */
  SC.audit = function () {
    var known = new Set();
    Object.keys(SC.strings).forEach(function (k) {
      var v = SC.strings[k];
      known.add(v);
      // A pattern like "{n} tickets" can never match literally.
      if (/\{\w+\}/.test(v)) {
        known.add(v.replace(/\{\w+\}/g, "").replace(/\s+/g, " ").trim());
      }
    });

    // Things that are DATA, not chrome, and must never be translated.
    var exempt = [
      /^SC-\d+$/,                       // ticket ids
      /^(OD|RF|ORD|INV)-[\w-]+$/,       // order and refund ids
      /^[\s\d.,:%₹$+\-/()★☆—–·→←▶■]*$/,
      /^\p{Extended_Pictographic}+$/u,  // icons
    ];

    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var missed = [], node;
    while ((node = walker.nextNode())) {
      var text = (node.textContent || "").replace(/\s+/g, " ").trim();
      if (!text || known.has(text)) continue;
      if (exempt.some(function (re) { return re.test(text); })) continue;

      var el = node.parentElement;
      if (!el || !el.offsetParent) continue;          // not visible
      if (el.closest("[data-i18n-exempt]")) continue; // the customer's own words
      missed.push({text: text.slice(0, 70), where: el.className || el.tagName});
    }

    if (missed.length) {
      console.warn("[i18n] " + missed.length
        + " visible string(s) not in the catalogue:");
      console.table(missed);
    } else {
      console.info("[i18n] every visible string is in the catalogue.");
    }
    return missed;
  };

  if (/[?&]i18n=audit/.test(w.location.search)) {
    w.addEventListener("load", function () { setTimeout(SC.audit, 1200); });
  }
})(window);
