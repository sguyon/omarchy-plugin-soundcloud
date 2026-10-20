const assert = require("node:assert/strict");
const model = require("../SoundCloudModel.js");

assert.equal(model.displayLabel({ trackTitle: "Track", trackArtist: "Artist" }), "Track  ·  Artist");
assert.equal(model.displayLabel(null), "SoundCloud");

const cachedFeed = [{ title: "Cached feed track" }];
const pendingFeed = model.applyTrackResult(cachedFeed, { pending: true, tracks: [] });
assert.deepEqual(pendingFeed.tracks, cachedFeed);
assert.equal(pendingFeed.loading, true);

const refreshedFeed = [{ title: "Refreshed feed track" }];
const completedFeed = model.applyTrackResult(pendingFeed.tracks, {
  pending: false,
  tracks: refreshedFeed,
});
assert.deepEqual(completedFeed.tracks, refreshedFeed);
assert.equal(completedFeed.loading, false);

assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: true, count: 20, requestedCount: 0, pending: false,
}), true);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: true, count: 20, requestedCount: 20, pending: false,
}), false);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: true, count: 20, requestedCount: 0, pending: true,
}), false);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: false, count: 20, requestedCount: 0, pending: false,
}), false);

// Regression: Qt may not report atYEnd even when the user has visibly reached
// the bottom area. Loading must begin from the visible-area ratio instead.
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: false, yPosition: 0.76, heightRatio: 0.10,
  count: 20, requestedCount: 0, pending: false,
}), true);
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: true, atEnd: false, yPosition: 0.74, heightRatio: 0.10,
  count: 20, requestedCount: 0, pending: false,
}), false);

// Regression: appending a page changes the model and scroll geometry. That
// automatic update must not immediately request another page in a loop.
assert.equal(model.shouldLoadMoreTracks({
  userInitiated: false, atEnd: true, yPosition: 0.90, heightRatio: 0.10,
  count: 21, requestedCount: 11, pending: false,
}), false);

assert.equal(model.contentYAfterTrackUpdate(240, 11, 21), 240);
assert.equal(model.contentYAfterTrackUpdate(240, 21, 21), null);

// A badge restored by SoundCloud without a real audio player is stale. The UI
// must clear it instead of preserving it as an actionable current track.
assert.equal(model.shouldPreservePlaybackMetadata({
  playerPresent: false, title: "", url: "https://soundcloud.com/feed",
}, "Old track"), false);
assert.equal(model.shouldPreservePlaybackMetadata({
  playerPresent: true, title: "", url: "https://soundcloud.com/feed",
}, "Playing track"), true);

assert.equal(model.selectionIsPending("resolving"), true);
assert.equal(model.selectionIsPending("buffering"), true);
assert.equal(model.selectionIsPending("playing"), false);
assert.equal(model.selectionIsPending("paused"), false);
assert.equal(model.selectionIsPending("error"), false);
assert.equal(model.selectionIsPending("idle"), false);
// Every pending state needs visible feedback; settled states show none.
for (const state of ["resolving", "buffering", "playing", "paused", "error", "idle"]) {
  assert.equal(model.pendingLabel(state) !== "", model.selectionIsPending(state));
}

// A newer selection supersedes A while controls remain globally busy.  A's
// eventual response cannot complete or clear B.
assert.equal(model.canStartAction(true, true, "play"), true);
assert.equal(model.canStartAction(true, true, "next"), false);
assert.equal(model.isCurrentSelectionResponse(42, 41), false);
assert.equal(model.isCurrentSelectionResponse(42, 42), true);

// Keyboard selection: first move lands on row 0, stops at the ends, empty list has none.
assert.equal(model.moveSelection(-1, 1, 5), 0);
assert.equal(model.moveSelection(-1, -1, 5), 0);
assert.equal(model.moveSelection(0, 1, 5), 1);
assert.equal(model.moveSelection(4, 1, 5), 4);
assert.equal(model.moveSelection(0, -1, 5), 0);
assert.equal(model.moveSelection(2, -1, 5), 1);
assert.equal(model.moveSelection(9, 1, 5), 4);
assert.equal(model.moveSelection(0, 1, 0), -1);
assert.equal(model.clampSelection(7, 3), 2);
assert.equal(model.clampSelection(-3, 3), -1);
assert.equal(model.clampSelection(undefined, 3), -1);
assert.equal(model.clampSelection(0, 0), -1);
assert.equal(model.selectionAfterUpdate(3, 20, false), 3);
assert.equal(model.selectionAfterUpdate(3, 2, false), 1);
assert.equal(model.selectionAfterUpdate(3, 20, true), -1);

// Tab cycling wraps both ways through the four tabs; an unknown tab starts at the first.
assert.equal(model.cycleTab("home", 1), "feed");
assert.equal(model.cycleTab("history", 1), "home");
assert.equal(model.cycleTab("home", -1), "history");
assert.equal(model.cycleTab("likes", -1), "feed");
assert.equal(model.cycleTab("nope", 1), "home");

assert.equal(model.isCurrentTrack("soundcloud:tracks:9", "soundcloud:tracks:9"), true);
assert.equal(model.isCurrentTrack("soundcloud:tracks:8", "soundcloud:tracks:9"), false);
// Nothing playing must not mark rows that also lack an id.
assert.equal(model.isCurrentTrack("", ""), false);
assert.equal(model.isCurrentTrack(undefined, ""), false);

// A selection error moves onto its row only when that row is in the visible list;
// otherwise it must stay in the shared error line so it is never hidden.
const listed = [{ playbackId: "soundcloud:tracks:1" }, { playbackId: "soundcloud:tracks:2" }];
assert.equal(model.errorShownOnRow(listed, "soundcloud:tracks:2", "error"), true);
assert.equal(model.errorShownOnRow(listed, "soundcloud:tracks:3", "error"), false);
assert.equal(model.errorShownOnRow(listed, "soundcloud:tracks:2", "playing"), false);
assert.equal(model.errorShownOnRow(listed, "", "error"), false);

// The bar follows the theme palette: playback uses the theme accent, while
// every non-playing state matches the other bar icons.
assert.equal(model.barIconColor(true, "accent", "foreground"), "accent");
assert.equal(model.barIconColor(false, "accent", "foreground"), "foreground");

var requestEvents = [];
function observeListEvent(reason, count, hasMore) {
  if (model.shouldLoadMoreTracks({
    userInitiated: reason === "movement-ended",
    hasMore: hasMore,
    atEnd: false,
    yPosition: 0.86,
    heightRatio: 0.1,
    count: count,
    requestedCount: requestEvents.length ? 11 : 0,
    pending: false
  })) requestEvents.push({ reason: reason, count: count });
}

observeListEvent("movement-ended", 11, true);
observeListEvent("page-appended", 21, true);
assert.deepEqual(requestEvents, [{ reason: "movement-ended", count: 11 }]);
observeListEvent("movement-ended", 21, true);
assert.equal(requestEvents.length, 2);
observeListEvent("movement-ended", 31, false);
assert.equal(requestEvents.length, 2);

// Volume icon: Omarchy OSD thresholds (0 or muted, up to 33, up to 66, above).
for (const [volume, muted, state] of [
  [0, false, "muted"], [0.004, false, "muted"], [0.5, true, "muted"],
  [0.01, false, "low"], [0.33, false, "low"], [0.34, false, "medium"],
  [0.66, false, "medium"], [0.67, false, "high"], [1, false, "high"],
  [undefined, false, "high"], [2, false, "high"], [-1, false, "muted"],
]) {
  assert.equal(model.volumeIconState(volume, muted), state, `${volume} ${muted}`);
}
assert.equal(model.volumeIconGlyph("muted"), "");
assert.equal(model.volumeIconGlyph("low"), "");
assert.equal(model.volumeIconGlyph("medium"), "");
assert.equal(model.volumeIconGlyph("high"), "");
assert.equal(model.volumeText(0.354, false), "35%");
assert.equal(model.volumeText(0.354, true), "Muted");
assert.equal(model.volumeText(5, false), "100%");

// The wheel steps by 5 points and never leaves 0..1.
// Volume slider: pointer maths, throttle, width and show/hide timing.
assert.equal(model.sliderValueFromX(6, 6, 88), 0);
assert.equal(model.sliderValueFromX(50, 6, 88), 0.5);
assert.equal(model.sliderValueFromX(94, 6, 88), 1);
assert.equal(model.sliderValueFromX(-20, 6, 88), 0);
assert.equal(model.sliderValueFromX(500, 6, 88), 1);
assert.equal(model.sliderValueFromX(30, 6, 0), 0);
assert.equal(model.sliderValueFromX("x", 6, 88), 0);
assert.equal(model.sliderValueFromX(6 + 88 * 0.337, 6, 88), 0.34);
assert.equal(model.sliderEmitDue(0, 1000, 50), true);        // nothing emitted yet
assert.equal(model.sliderEmitDue(1000, 1049, 50), false);
assert.equal(model.sliderEmitDue(1000, 1050, 50), true);
assert.equal(model.sliderEmitDue(1000, 900, 50), true);      // clock went backwards
assert.equal(model.sliderEmitDue(1000, 1000, 0), true);
assert.equal(model.sliderThrottleMs, 50);
assert.equal(model.volumeTooltip(0.45, false), "Volume 45% (click to mute)");
assert.equal(model.volumeTooltip(0.45, true), "Muted (click to unmute)");
assert.equal(model.sliderWidth(200), 96);
assert.equal(model.sliderWidth(96.7), 96);
assert.equal(model.sliderWidth(80), 80);
assert.equal(model.sliderWidth(48), 48);
assert.equal(model.sliderWidth(47), 0);                      // too narrow: hidden, never overlaps
assert.equal(model.sliderWidth(-5), 0);
assert.equal(model.sliderWidth(undefined), 0);
assert.equal(model.sliderHideDelay("leave"), 600);
assert.equal(model.sliderHideDelay("flash"), 1000);
assert.equal(model.sliderVisible(false, false, false), false);
assert.equal(model.sliderVisible(true, false, false), true);
assert.equal(model.sliderVisible(false, true, false), true);
assert.equal(model.sliderVisible(false, false, true), true);
assert.equal(model.sliderDisplayValue(0.6, false), 0.6);
assert.equal(model.sliderDisplayValue(0.6, true), 0);
assert.equal(model.sliderDisplayValue(7, false), 1);
assert.equal(model.sliderDisplayValue(undefined, false), 1);
assert.equal(model.volumeSetCommand(0.35), "volume:0.35");
assert.equal(model.volumeSetCommand(0), "volume:0.00");
assert.equal(model.volumeSetCommand(3), "volume:1.00");
assert.equal(model.volumeSetCommand(-1), "volume:0.00");
assert.equal(model.volumeSetCommand("x"), "");

assert.equal(model.volumeStepCommand(120), "volume-step:+0.05");
assert.equal(model.volumeStepCommand(-15), "volume-step:-0.05");
assert.equal(model.volumeStepCommand(0), "");
assert.equal(model.volumeStepCommand("x"), "");
assert.equal(model.steppedVolume(0.5, 120), 0.55);
assert.equal(model.steppedVolume(0.98, 120), 1);
assert.equal(model.steppedVolume(0.02, -120), 0);
assert.equal(model.steppedVolume(0.3, 0), 0.3);
assert.equal(model.steppedVolume(undefined, -120), 0.95);

// Repeat mode: anything but one or all means off.
assert.equal(model.repeatMode("one"), "one");
assert.equal(model.repeatMode("bogus"), "none");

// A track clicked in a list plays from that list; a new click replaces a pending one.
assert.equal(
  model.playFromCommand("likes", "soundcloud:tracks:42"),
  "play-from:likes:soundcloud:tracks:42",
);
for (const [tab, id] of [["library", "soundcloud:tracks:42"], ["home", "42"], ["home", ""], ["home", "soundcloud:tracks:0"], [undefined, undefined]]) {
  assert.equal(model.playFromCommand(tab, id), "");
}
assert.equal(model.isSelectionAction("play-from"), true);
assert.equal(model.isSelectionAction("play"), true);
assert.equal(model.isSelectionAction("next"), false);
assert.equal(model.canStartAction(true, true, "play-from"), true);
assert.equal(model.canStartAction(true, true, "next"), false);
assert.equal(model.canStartAction(true, false, "play-from"), false);

// Next and Previous are off when the backend says the queue cannot go that way.
// An older backend sends no flag, so the buttons stay on.
assert.equal(model.queueButtonEnabled(false, true, true), true);
assert.equal(model.queueButtonEnabled(false, true, undefined), true);
assert.equal(model.queueButtonEnabled(false, true, false), false);
assert.equal(model.queueButtonEnabled(true, true, true), false);
assert.equal(model.queueButtonEnabled(false, false, true), false);

// Seek keys: max(5, n) seconds, at most one seek per 200 ms.
assert.equal(model.seekRepeatCount(0, false), 0);
assert.equal(model.seekRepeatCount(7, false), 0);
assert.equal(model.seekRepeatCount(0, true), 1);
assert.equal(model.seekRepeatCount(4, true), 5);
assert.equal(model.seekRepeatCount(undefined, true), 1);
assert.equal(model.seekSeconds(1, 0), 5);
assert.equal(model.seekSeconds(-1, 3), -5);
assert.equal(model.seekSeconds(1, 5), 5);
assert.equal(model.seekSeconds(1, 6), 6);
assert.equal(model.seekSeconds(-1, 12), -12);
assert.equal(model.seekSeconds(0, 12), 0);
assert.equal(model.seekSeconds(1, undefined), 5);
// One seek per 200 ms; the first one and a clock that went back are always due.
assert.equal(model.seekDue(1000, undefined), true);
assert.equal(model.seekDue(1000, null), true);
assert.equal(model.seekDue(1000, 0), true);
assert.equal(model.seekDue(1199, 1000), false);
assert.equal(model.seekDue(1200, 1000), true);
assert.equal(model.seekDue(900, 1000), true);
assert.equal(model.seekDue(NaN, 1000), true);
assert.equal(model.seekByCommand(5), "seek-by:5");
assert.equal(model.seekByCommand(-12), "seek-by:-12");
assert.equal(model.seekByCommand(0), "");
assert.equal(model.seekByCommand("x"), "");
assert.equal(model.isSeekKey(0x01000012), true);
assert.equal(model.isSeekKey(0x01000014), true);
assert.equal(model.isSeekKey(0x4c), false); // l likes now
assert.equal(model.isSeekKey(0x48), false);
assert.equal(model.isSeekKey(0x01000013), false);
assert.equal(model.isSeekKey(0x4a), false);
// A held key: presses at 50 ms (autorepeat) go out every 200 ms with a growing step.
{
  let count = 0, last, sent = [];
  for (let at = 0; at <= 1000; at += 50) {
    count = model.seekRepeatCount(count, at > 0);
    if (model.seekDue(at, last)) { sent.push(model.seekSeconds(1, count)); last = at; }
  }
  assert.deepEqual(sent, [5, 5, 8, 12, 16, 20]);
}
// Keys handled before the key catcher.
const SH = 0x02000000, CT = 0x04000000, AL = 0x08000000;
assert.equal(model.keyAction(0x01000017, 0, ""), "next");
assert.equal(model.keyAction(0x01000016, 0, ""), "previous");
assert.equal(model.keyAction(0x01000014, SH, ""), "next");
assert.equal(model.keyAction(0x01000012, SH, ""), "previous");
assert.equal(model.keyAction(0x01000013, SH, ""), "volume-up");
assert.equal(model.keyAction(0x01000015, SH, ""), "volume-down");
assert.equal(model.keyAction(0x01000013, 0, ""), "");
assert.equal(model.keyAction(0x01000014, 0, ""), ""); // plain arrows are for the catcher
assert.equal(model.keyAction(0x4c, 0, "l"), "like");
assert.equal(model.keyAction(0x4c, SH, "L"), "repeat");
assert.equal(model.keyAction(0x4c, CT, ""), "");
assert.equal(model.keyAction(0x53, SH, "S"), "shuffle");
assert.equal(model.keyAction(0x53, 0, "s"), "");
assert.equal(model.keyAction(0x48, 0, "h"), "ignore");
assert.equal(model.keyAction(0x4a, 0, "j"), ""); // j and k stay list navigation
assert.equal(model.keyAction(0x4b, 0, "k"), "");
assert.equal(model.keyAction(0x20, 0x20000000, ""), ""); // keypad flag alone is no modifier
assert.equal(model.actionRepeats("volume-up"), true);
assert.equal(model.actionRepeats("volume-down"), true);
assert.equal(model.actionRepeats("like"), false);
assert.equal(model.actionRepeats("next"), false);

// Optimistic seek position: a status right after a seek is older than the seek.
assert.equal(model.positionFromStatus(30, 12, 1100, 1000), 30); // within 500 ms of a seek
assert.equal(model.positionFromStatus(30, 12, 1600, 1000), 12);
assert.equal(model.positionFromStatus(30, 12, 1100, 0), 12);
assert.equal(model.positionFromStatus(30, 12, 900, 1000), 12); // clock went backwards
assert.equal(model.seekedPosition(10, 5, 100), 15);
assert.equal(model.seekedPosition(3, -5, 100), 0);
assert.equal(model.seekedPosition(98, 5, 100), 100);
assert.equal(model.seekedPosition(10, 5, 0), 15);

// Wheel events add up to whole notches: smooth-scroll deltas are small, a mouse notch is 120.
assert.deepEqual(model.wheelSteps(0, 120), { steps: 1, accumulator: 0 });
assert.deepEqual(model.wheelSteps(0, -120), { steps: -1, accumulator: 0 });
assert.deepEqual(model.wheelSteps(0, 15), { steps: 0, accumulator: 15 });
assert.deepEqual(model.wheelSteps(105, 15), { steps: 1, accumulator: 0 });
assert.deepEqual(model.wheelSteps(100, 50), { steps: 1, accumulator: 30 });
assert.deepEqual(model.wheelSteps(-100, -50), { steps: -1, accumulator: -30 });
assert.deepEqual(model.wheelSteps(100, -20), { steps: 0, accumulator: -20 });
assert.deepEqual(model.wheelSteps(0, 360), { steps: 3, accumulator: 0 });
assert.deepEqual(model.wheelSteps(0, -250), { steps: -2, accumulator: -10 });
assert.deepEqual(model.wheelSteps(undefined, 30), { steps: 0, accumulator: 30 });
assert.deepEqual(model.wheelSteps(40, 0), { steps: 0, accumulator: 40 });
assert.deepEqual(model.wheelSteps(40, "x"), { steps: 0, accumulator: 40 });
let swipe = { steps: 0, accumulator: 0 };
let swipeSteps = 0;
for (let i = 0; i < 40; i++) {
  swipe = model.wheelSteps(swipe.accumulator, 6);
  swipeSteps += swipe.steps;
}
assert.equal(swipeSteps, 2); // 40 events of 6 units = 240 units = two notches, not forty steps

// QML only sees TOP-LEVEL function and var declarations of a JS module, not
// the properties of module.exports. Every name a .qml file uses as
// SoundCloudModel.<name> must be declared at the top level of the source.
{
  const fs = require("node:fs");
  const path = require("node:path");
  const root = path.join(__dirname, "..");
  const source = fs.readFileSync(path.join(root, "SoundCloudModel.js"), "utf8");
  const declared = new Set();
  for (const m of source.matchAll(/^(?:function\s+|var\s+)([A-Za-z_$][\w$]*)/gm)) declared.add(m[1]);
  const used = new Map();
  const files = [root, path.join(root, "tests", "qml")]
    .flatMap(dir => fs.readdirSync(dir).filter(f => f.endsWith(".qml")).map(f => path.join(dir, f)));
  for (const file of files) {
    const text = fs.readFileSync(file, "utf8");
    for (const m of text.matchAll(/\bSoundCloudModel\.([A-Za-z_$][\w$]*)/g)) {
      if (m[1] === "js") continue;
      if (!used.has(m[1])) used.set(m[1], path.basename(file));
    }
  }
  assert.ok(used.size > 20, "the scan should find the names the QML files use");
  for (const [name, file] of used) {
    assert.ok(declared.has(name), `${file} uses SoundCloudModel.${name}, which is not a top-level declaration in SoundCloudModel.js`);
  }
}

console.log("SoundCloudModel tests passed");

// The launcher prints one JSON line. A failed start carries the reason, such
// as missing packages, so the popup can show it instead of "Not connected".
assert.equal(
  model.launchError('{"ok": false, "error": "Missing gst-libav. Run: omarchy pkg add gst-libav"}'),
  "Missing gst-libav. Run: omarchy pkg add gst-libav",
);
assert.equal(model.launchError('{"ok": true, "starting": true, "running": true}'), "");
assert.equal(model.launchError("not json"), "");
