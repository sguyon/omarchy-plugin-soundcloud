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


// The launcher prints one JSON line. A failed start carries the reason, such
// as missing packages, so the popup can show it instead of "Not connected".
assert.equal(
  model.launchError('{"ok": false, "error": "Missing gst-libav. Run: omarchy pkg add gst-libav"}'),
  "Missing gst-libav. Run: omarchy pkg add gst-libav",
);
assert.equal(model.launchError('{"ok": true, "starting": true, "running": true}'), "");
assert.equal(model.launchError("not json"), "");

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

// Volume icon: Omarchy OSD thresholds (0 or muted, up to 33, up to 66, above).
for (const [volume, muted, state] of [
  [0, false, "muted"], [0.004, false, "muted"], [0.5, true, "muted"],
  [0.01, false, "low"], [0.33, false, "low"], [0.34, false, "medium"],
  [0.66, false, "medium"], [0.67, false, "high"], [1, false, "high"],
  [undefined, false, "high"], [2, false, "high"], [-1, false, "muted"],
]) {
  assert.equal(model.volumeIconState(volume, muted), state, `${volume} ${muted}`);
}
assert.equal(model.volumeWaves("muted"), 0);
assert.equal(model.volumeWaves("low"), 1);
assert.equal(model.volumeWaves("medium"), 2);
assert.equal(model.volumeWaves("high"), 3);
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
assert.equal(model.volumeTooltip(0.45, false), "Volume 45% (click or m to mute)");
assert.equal(model.volumeTooltip(0.45, true), "Muted (click or m to unmute)");
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

// Repeat and switch commands.
assert.equal(model.repeatMode("one"), "one");
assert.equal(model.repeatMode("bogus"), "none");
assert.equal(model.repeatIconGlyph("one"), "\u{f0458}");
assert.equal(model.repeatIconGlyph("all"), "\u{f0456}");
assert.equal(model.repeatIconGlyph(undefined), "\u{f0457}");
assert.equal(model.toggleCommand("autoplay", true), "autoplay:off");
assert.equal(model.toggleCommand("notify", false), "notify:on");
assert.equal(model.toggleCommand("other", false), "");

// Queue commands take real track ids only.
assert.equal(model.queueNextCommand("soundcloud:tracks:7"), "queue-next:soundcloud:tracks:7");
assert.equal(model.queueRemoveCommand("soundcloud:tracks:7"), "queue-remove:soundcloud:tracks:7");
for (const bad of ["", "7", "soundcloud:tracks:0", undefined, "queue-clear"]) {
  assert.equal(model.queueNextCommand(bad), "");
  assert.equal(model.queueRemoveCommand(bad), "");
}

// Queue rows: label by origin; the view starts at the current track.
assert.equal(model.queueRowLabel({ current: true, explicit: true }), "playing now");
assert.equal(model.queueRowLabel({ explicit: true }), "added by you");
assert.equal(model.queueRowLabel({ auto: true }), "related");
assert.equal(model.queueRowLabel({}), "");
assert.equal(model.queueRowLabel(undefined), "");
const queue = { items: [{ id: "a" }, { id: "b", current: true }, { id: "c" }] };
assert.deepEqual(model.upcomingQueue(queue).map((item) => item.id), ["b", "c"]);
assert.deepEqual(model.upcomingQueue({ items: [{ id: "a" }] }).map((item) => item.id), ["a"]);
assert.deepEqual(model.upcomingQueue(undefined), []);

// Only SoundCloud track pages are opened; encrypted rows are flagged.
assert.equal(model.trackPageUrl("https://soundcloud.com/artist/track"), "https://soundcloud.com/artist/track");
for (const bad of ["", undefined, "http://soundcloud.com/a/b", "https://evil.com/soundcloud.com/a", "https://soundcloud.com.evil.com/a", "javascript:alert(1)", "https://soundcloud.com/a b"]) {
  assert.equal(model.trackPageUrl(bad), "");
}
assert.equal(model.isEncrypted({ encrypted: true }), true);
assert.equal(model.isEncrypted({ encrypted: "yes" }), false);
assert.equal(model.isEncrypted(undefined), false);

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

// Tab cycling wraps both ways through the four tabs and Queue; an unknown tab starts at the first.
assert.equal(model.cycleTab("home", 1), "feed");
assert.equal(model.cycleTab("history", 1), "queue");
assert.equal(model.cycleTab("queue", 1), "home");
assert.equal(model.cycleTab("home", -1), "queue");
assert.equal(model.cycleTab("queue", -1), "history");
assert.equal(model.cycleTab("likes", -1), "feed");
assert.equal(model.cycleTab("nope", 1), "home");
assert.equal(model.currentStop("likes", false), "likes");
assert.equal(model.currentStop("likes", true), "queue");

// Queue count badge: hidden at 0 or junk, capped at 99+.
assert.equal(model.queueBadgeText(0), "");
assert.equal(model.queueBadgeText(undefined), "");
assert.equal(model.queueBadgeText(-3), "");
// the badge counts the tracks still to come, not the whole queue
for (const [length, cursor, want] of [[24, 21, 2], [24, 23, 0], [24, 0, 23], [5, -1, 5], [0, 0, 0], [3, 9, 0], [undefined, undefined, 0]]) {
  assert.equal(model.upcomingCount(length, cursor), want, `upcomingCount(${length}, ${cursor})`);
}
assert.equal(model.queueBadgeText(12), "12");
assert.equal(model.queueBadgeText("7"), "7");
assert.equal(model.queueBadgeText(99), "99");
assert.equal(model.queueBadgeText(100), "99+");

// Avatar: fixed Library address, tooltip with the name, only small png/jpeg data URLs.
assert.equal(model.libraryUrl, "https://soundcloud.com/you/library");
assert.equal(model.accountTooltip("Sacha"), "Signed in as Sacha (open Library)");
assert.equal(model.accountTooltip("  Sacha \n G "), "Signed in as Sacha G (open Library)");
assert.equal(model.accountTooltip(""), "Open Library");
assert.equal(model.accountTooltip(null), "Open Library");
assert.equal(model.accountTooltip(undefined), "Open Library");
assert.equal(model.accountAvatarSource("data:image/png;base64,AAAA"), "data:image/png;base64,AAAA");
assert.equal(model.accountAvatarSource("data:image/jpeg;base64,AAAA"), "data:image/jpeg;base64,AAAA");
assert.equal(model.accountAvatarSource("https://x/y.png"), "");
assert.equal(model.accountAvatarSource("data:image/svg+xml;base64,AAAA"), "");
assert.equal(model.accountAvatarSource("data:image/png;base64," + "A".repeat(180000)), "");
assert.equal(model.accountAvatarSource(null), "");

// Seeking like the website: max(5, n) seconds, n = key repeats since the press.
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
// List scrolling: 3x the touchpad pixels; a mouse notch is 60 px times the factor.
assert.equal(model.scrollChange(10, 0), -30);
assert.equal(model.scrollChange(-4, 480), 12);
assert.equal(model.scrollChange(0, 120), -180);
assert.equal(model.scrollChange(0, -240), 360);
assert.equal(model.scrollChange(0, 0), 0);
assert.equal(model.scrollChange(undefined, "x"), 0);
assert.equal(model.scrolledContentY(100, 50, 0, 1000, 300), 150);
assert.equal(model.scrolledContentY(100, -500, 0, 1000, 300), 0);
assert.equal(model.scrolledContentY(650, 500, 0, 1000, 300), 700);
assert.equal(model.scrolledContentY(0, 50, 0, 200, 300), 0);
assert.equal(model.scrolledContentY(-10, 5, -10, 1000, 300), -5);
assert.equal(model.scrolledContentY(NaN, 5, 0, 1000, 300), 5);
assert.equal(model.swipeVelocity(0, 30, 10), 3000);
assert.equal(model.swipeVelocity(3000, 10, 10), 2000);
assert.equal(model.swipeVelocity(500, 10, 0), 500);
assert.equal(model.flickStartVelocity(100), 0);
assert.equal(model.flickStartVelocity(-100), 0);
assert.equal(model.flickStartVelocity(1500), 1500);
assert.equal(model.flickStartVelocity(-99999), -6000);
assert.equal(model.flickStartVelocity(NaN), 0);
// Wheel events add up to whole notches: touchpad deltas are small, a mouse notch is 120.
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

// Keys handled before the key catcher.
const SH = 0x02000000, CT = 0x04000000, AL = 0x08000000;
assert.equal(model.keyAction(0x01000017, 0, ""), "next");
assert.equal(model.keyAction(0x01000016, 0, ""), "previous");
assert.equal(model.keyAction(0x01000014, SH, ""), "next");
assert.equal(model.keyAction(0x01000012, SH, ""), "previous");
assert.equal(model.keyAction(0x01000014, 0, ""), ""); // plain arrows are for the catcher
assert.equal(model.keyAction(0x01000013, SH, ""), "volume-up");
assert.equal(model.keyAction(0x01000015, SH, ""), "volume-down");
assert.equal(model.keyAction(0x01000013, AL, ""), "queue-up");
assert.equal(model.keyAction(0x01000015, AL, ""), "queue-down");
assert.equal(model.keyAction(0x01000013, 0, ""), "");
assert.equal(model.keyAction(0x4c, 0, "l"), "like");
assert.equal(model.keyAction(0x4c, SH, "L"), "repeat");
assert.equal(model.keyAction(0x4c, CT, ""), "");
assert.equal(model.keyAction(0x53, SH, "S"), "shuffle");
assert.equal(model.keyAction(0x53, 0, "s"), "");
assert.equal(model.keyAction(0x48, 0, "h"), "ignore");
assert.equal(model.keyAction(0x56, CT, "\x16"), "link-next");
assert.equal(model.keyAction(0x56, CT | SH, "\x16"), "link-play");
assert.equal(model.keyAction(0x56, 0, "v"), "");
assert.equal(model.keyAction(0x4a, 0, "j"), ""); // j and k stay list navigation
assert.equal(model.keyAction(0x4b, 0, "k"), "");
assert.equal(model.keyAction(0x20, 0x20000000, ""), ""); // keypad flag alone is no modifier
assert.equal(model.actionRepeats("volume-up"), true);
assert.equal(model.actionRepeats("next"), false);
assert.equal(model.actionRepeats("link-next"), false);

// Pasted links.
assert.equal(model.pastedLinkUrl("https://soundcloud.com/a/b"), "https://soundcloud.com/a/b");
assert.equal(model.pastedLinkUrl("  https://soundcloud.com/a/b\n"), "https://soundcloud.com/a/b");
assert.equal(model.pastedLinkUrl("http://soundcloud.com/a/b"), "");
assert.equal(model.pastedLinkUrl("ftp://x"), "");
assert.equal(model.pastedLinkUrl("see https://soundcloud.com/a/b"), "");
assert.equal(model.pastedLinkUrl("https://a.com/x y"), "");
assert.equal(model.pastedLinkUrl("https://" + "a".repeat(3000)), "");
assert.equal(model.pastedLinkUrl(""), "");
assert.equal(model.pastedLinkUrl(null), "");
assert.equal(model.linkCommand("https://soundcloud.com/a/b?x=1&y=2", false),
  "link-next:" + encodeURIComponent("https://soundcloud.com/a/b?x=1&y=2"));
assert.equal(model.linkCommand("https://soundcloud.com/a/b", true), "link-play:https%3A%2F%2Fsoundcloud.com%2Fa%2Fb");
assert.equal(model.linkCommand("javascript:alert(1)", true), "");
assert.equal(model.linkCommand("", false), "");

// Queue commands and keyboard selection.
const t1 = "soundcloud:tracks:1", t2 = "soundcloud:tracks:2", t3 = "soundcloud:tracks:3";
assert.equal(model.queueAddCommand(t1), "queue-add:" + t1);
assert.equal(model.queueAddCommand("x"), "");
assert.equal(model.queuePlayCommand(t2), "queue-play:" + t2);
assert.equal(model.queuePlayCommand(""), "");
assert.equal(model.queueMoveCommand(t2, -1), "queue-move:" + t2 + ":-1");
assert.equal(model.queueMoveCommand(t2, 3), "queue-move:" + t2 + ":3");
assert.equal(model.queueMoveCommand(t2, 0), "");
assert.equal(model.queueMoveCommand(t2, 1.5), "");
assert.equal(model.queueMoveCommand("bad", 1), "");
const rows = [{ playbackId: t1, current: true }, { playbackId: t2 }, { playbackId: t3 }];
assert.equal(model.queueCountText(rows), "2 next");
assert.equal(model.queueCountText([rows[0]]), "Empty");
assert.equal(model.queueCountText([rows[1], rows[2]]), "2 next");
assert.equal(model.queueCountText([]), "Empty");
assert.equal(model.queueIndexOf(rows, t3), 2);
assert.equal(model.queueIndexOf(rows, ""), -1);
assert.equal(model.queueSelectionAfterMove(rows, "", 1), t2);
assert.equal(model.queueSelectionAfterMove(rows, "", -1), t3);
assert.equal(model.queueSelectionAfterMove(rows, t2, 1), t3);
assert.equal(model.queueSelectionAfterMove(rows, t3, 1), t3);
assert.equal(model.queueSelectionAfterMove(rows, t2, -1), t1);
assert.equal(model.queueSelectionAfterMove(rows, t1, -1), t1);
assert.equal(model.queueSelectionAfterMove([rows[0]], "", 1), t1);
assert.equal(model.queueSelectionAfterMove([], "", 1), "");
assert.equal(model.queueSelectionAfterUpdate(rows, t2), t2);
assert.equal(model.queueSelectionAfterUpdate([rows[0]], t2), "");
assert.equal(model.queueKeyMoveOffset(rows, t2, -1), 0); // cannot go above the playing row
assert.equal(model.queueKeyMoveOffset(rows, t2, 1), 1);
assert.equal(model.queueKeyMoveOffset(rows, t3, 1), 0);
assert.equal(model.queueKeyMoveOffset(rows, t3, -1), -1);
assert.equal(model.queueKeyMoveOffset(rows, t1, 1), 0); // the playing row never moves
assert.equal(model.queueKeyMoveOffset(rows, "", 1), 0);
assert.equal(model.queueDragOffset(100, 51, 1, 3), 1); // 100 px is about two rows, clamped to the end
assert.equal(model.queueDragOffset(-200, 51, 2, 3), -1); // clamped below the playing row
assert.equal(model.queueDragOffset(60, 51, 1, 5), 1);
// Rows passed by a dragged row make room for it.
assert.equal(model.queueDragShift(2, 1, 2, 51), -51); // dragged down two: rows 2 and 3 move up
assert.equal(model.queueDragShift(3, 1, 2, 51), -51);
assert.equal(model.queueDragShift(4, 1, 2, 51), 0);
assert.equal(model.queueDragShift(1, 1, 2, 51), 0); // the dragged row itself
assert.equal(model.queueDragShift(3, 4, -2, 51), 51); // dragged up two: rows 2 and 3 move down
assert.equal(model.queueDragShift(1, 4, -2, 51), 0);
assert.equal(model.queueDragShift(2, 4, 0, 51), 0);
assert.equal(model.queueDragShift(2, -1, 3, 51), 0); // nothing is being dragged
assert.equal(model.queueDragOffset(10, 51, 1, 5), 0);
assert.equal(model.queueDragOffset(-60, 51, 0, 5), 0); // the playing row stays
assert.equal(model.queueDragOffset(60, 0, 1, 5), 0);

// Omarchy OSD payload.
assert.deepEqual(JSON.parse(model.volumeOsdPayload(0.45, false)), { icon: "volume-medium", value: 45, progressText: "SoundCloud 45%" });
assert.deepEqual(JSON.parse(model.volumeOsdPayload(1, false)), { icon: "volume-high", value: 100, progressText: "SoundCloud 100%" });
assert.deepEqual(JSON.parse(model.volumeOsdPayload(0.2, false)), { icon: "volume-low", value: 20, progressText: "SoundCloud 20%" });
assert.deepEqual(JSON.parse(model.volumeOsdPayload(0.6, true)), { icon: "volume-muted", value: 0, progressText: "SoundCloud 0%" });
assert.deepEqual(JSON.parse(model.volumeOsdPayload(0, false)), { icon: "volume-muted", value: 0, progressText: "SoundCloud 0%" });

// Waveform, transient statuses and optimistic seek position.
assert.equal(model.sameLevels([1, 2, 3], [1, 2, 3]), true);
assert.equal(model.sameLevels([1, 2, 3], [1, 2, 4]), false);
assert.equal(model.sameLevels([1, 2], [1, 2, 3]), false);
assert.equal(model.sameLevels([], []), true);
assert.equal(model.sameLevels([], null), false);
assert.equal(model.sameLevels(null, null), true);
assert.equal(model.isTransientStatus({ playbackState: "buffering", playbackId: "" }, t1), true);
assert.equal(model.isTransientStatus({ playbackState: "resolving", playbackId: t1 }, t1), true);
assert.equal(model.isTransientStatus({ playbackState: "resolving", playbackId: t2 }, t1), false);
assert.equal(model.isTransientStatus({ playbackState: "playing", playbackId: t1 }, t1), false);
assert.equal(model.isTransientStatus({ playbackState: "idle", playbackId: "" }, t1), false);
assert.equal(model.isTransientStatus({ playbackState: "buffering", playerPresent: false }, t1), false);
assert.equal(model.isTransientStatus({ playbackState: "buffering" }, ""), false);
assert.equal(model.positionFromStatus(30, 12, 1100, 1000), 30); // within 500 ms of a seek
assert.equal(model.positionFromStatus(30, 12, 1600, 1000), 12);
assert.equal(model.positionFromStatus(30, 12, 1100, 0), 12);
assert.equal(model.positionFromStatus(30, 12, 900, 1000), 12); // clock went backwards
assert.equal(model.seekedPosition(10, 5, 100), 15);
assert.equal(model.seekedPosition(3, -5, 100), 0);
assert.equal(model.seekedPosition(98, 5, 100), 100);
assert.equal(model.seekedPosition(10, 5, 0), 15);


// Paste keys: Ctrl+V, Shift+Insert (Omarchy's SUPER+V in a terminal), Meta ignored.
{
  const ctrl = 0x04000000, shift = 0x02000000, alt = 0x08000000, meta = 0x10000000;
  assert.equal(model.keyAction(0x56, ctrl, "\u0016"), "link-next");
  assert.equal(model.keyAction(0x56, ctrl | meta, ""), "link-next");
  assert.equal(model.keyAction(0x56, ctrl | shift, ""), "link-play");
  assert.equal(model.keyAction(0x56, ctrl | shift | meta, ""), "link-play");
  assert.equal(model.keyAction(0x01000006, shift, ""), "link-next");
  assert.equal(model.keyAction(0x01000006, shift | meta, ""), "link-next");
  assert.equal(model.keyAction(0x01000006, 0, ""), "");
  assert.equal(model.keyAction(0x56, ctrl | alt, ""), "");
  assert.equal(model.keyAction(0x56, 0, "v"), "");
}

// Paste result: what to send, or what to tell.
assert.deepEqual(model.pasteRequest("https://soundcloud.com/a/b\n", false),
  { command: "link-next:https%3A%2F%2Fsoundcloud.com%2Fa%2Fb", error: "" });
assert.deepEqual(model.pasteRequest("https://soundcloud.com/a/b", true).command.indexOf("link-play:"), 0);
assert.equal(model.pasteRequest("", false).command, "");
assert.equal(model.pasteRequest("   ", false).error, "The clipboard has no text to paste");
assert.equal(model.pasteRequest(null, false).error, "The clipboard has no text to paste");
assert.equal(model.pasteRequest("hello world", false).error, "The clipboard text is not a link");
assert.equal(model.pasteNotice({ ok: true, queued: true, title: "Little  Snowflake" }), "Queued: Little Snowflake");
assert.equal(model.pasteNotice({ ok: true, queued: true }), "Queued");
assert.equal(model.pasteNotice({ ok: true, title: "X" }), "Playing: X");
assert.equal(model.pasteNotice({ ok: true }), "Playing the link");
assert.equal(model.pasteNotice({ ok: false, error: "no" }), "");
assert.equal(model.pasteNotice(null), "");

// Clear queue confirmation: stays only while the same queue is shown.
{
  const rows = [{ playbackId: "a" }, { playbackId: "b" }];
  assert.equal(model.clearConfirmDelay(), 12000);
  assert.equal(model.queueSignature(rows), "a|b");
  assert.equal(model.queueSignature(null), "");
  assert.equal(model.clearConfirmStays("a|b", rows, true), true);
  assert.equal(model.clearConfirmStays("a|b", rows, false), false);
  assert.equal(model.clearConfirmStays("a|b", [{ playbackId: "a" }], true), false);
  assert.equal(model.clearConfirmStays("a|b", [rows[1], rows[0]], true), false);
  assert.equal(model.clearConfirmStays(null, rows, true), false);
  assert.equal(model.clearConfirmStays("", [], true), false);
  assert.equal(model.clearConfirmCountdownRuns("a|b", false), true);
  assert.equal(model.clearConfirmCountdownRuns("a|b", true), false);
  assert.equal(model.clearConfirmCountdownRuns("", false), true);
  assert.equal(model.clearConfirmCountdownRuns(null, false), false);
}

// OSD command and rate limit.
assert.deepEqual(model.osdCommand(0.45, false),
  ["omarchy-shell", "osd", "show", JSON.stringify({ icon: "volume-medium", value: 45, progressText: "SoundCloud 45%" })]);
assert.equal(model.osdDue(1000, 0), true);
assert.equal(model.osdDue(1050, 1000), false);
assert.equal(model.osdDue(1080, 1000), true);
assert.equal(model.osdDue(900, 1000), true);

// Message line policy.
assert.equal(model.toastDelay("info"), 4000);
assert.equal(model.toastDelay("error"), 8000);
assert.equal(model.toastDelay("other"), 4000);
assert.equal(model.toastLeaveDelay(), 2000);
assert.equal(model.toastRestarts("error", "a", "error", "a", true), false);
assert.equal(model.toastRestarts("error", "a", "error", "a", false), true);
assert.equal(model.toastRestarts("error", "a", "error", "b", true), true);
assert.equal(model.toastRestarts("info", "a", "error", "a", true), true);
assert.equal(model.toastRestarts("", "", "error", "b", true), true);
assert.equal(model.statusErrorShows("", "boom"), true);
assert.equal(model.statusErrorShows("boom", "boom"), false);
assert.equal(model.statusErrorShows("boom", "other"), true);
assert.equal(model.statusErrorShows("boom", ""), false);
for (const kind of ["like", "ui", "queue-edit", "paste", "action", "selection"]) assert.equal(model.errorClearedByCommand(kind), true);
for (const kind of ["status", "subscribe", "queue", "tracks:home", "artwork:1", undefined]) assert.equal(model.errorClearedByCommand(kind), false);

// Text filter over the loaded rows.
{
  const rows = [
    { title: "Café del Mar", artist: "Jose Padilla", playbackId: "soundcloud:tracks:1" },
    { title: "Blue Monday", artist: "New Order", playbackId: "soundcloud:tracks:2" },
    { title: "Untitled", artist: "DJ Padilla Mix", playbackId: "soundcloud:tracks:3" },
    { playbackId: "soundcloud:tracks:4" },
    { title: "Price (a+b) [live].*", artist: "X", playbackId: "soundcloud:tracks:5" },
  ];
  const idx = (q) => model.filterTracks(rows, q).map((hit) => hit.index);
  assert.deepEqual(idx(""), [0, 1, 2, 3, 4]); // empty: everything, in order
  assert.deepEqual(idx("   "), [0, 1, 2, 3, 4]);
  assert.equal(model.filterTracks(rows, "blue")[0].track, rows[1]); // the row itself, with its real index
  assert.deepEqual(idx("padilla"), [0, 2]); // title or artist
  assert.deepEqual(idx("jose padilla"), [0]); // every word must match
  assert.deepEqual(idx("padilla jose"), [0]); // in any order
  assert.deepEqual(idx("cafe"), [0]); // accent-insensitive
  assert.deepEqual(idx("CAFÉ"), [0]); // and the query may have accents
  assert.deepEqual(idx("MONDAY blue"), [1]); // case-insensitive
  assert.deepEqual(idx("new order"), [1]); // artist-only match
  assert.deepEqual(idx("zzz"), []); // no match
  assert.deepEqual(idx("untitled"), [2]); // a row with no title or artist never matches a word
  assert.deepEqual(idx("(a+b)"), [4]); // regex characters are literal
  assert.deepEqual(idx(".*"), [4]);
  assert.deepEqual(idx("["), [4]);
  assert.deepEqual(idx("\\"), []);
  assert.deepEqual(idx("mar" + " ".repeat(100) + "zzz"), [0]); // cut at 100 characters, so the tail is not searched
  assert.deepEqual(idx("mar" + " ".repeat(90) + "zzz"), []); // inside the cap it is
  assert.deepEqual(idx("x".repeat(5000)), []);
  assert.deepEqual(model.filterTracks(null, "a"), []);
  assert.deepEqual(model.filterTracks([null, { title: "a" }], "a").map((hit) => hit.index), [1]);
  assert.equal(model.filterCountText("", 24, 24), "24");
  assert.equal(model.filterCountText("ab", 2, 24), "2 of 24");
  assert.equal(model.filterCountText("ab", 0, 24), "0 of 24");
  // The selection follows its track by id when the visible rows change.
  const visible = [rows[0], rows[1], rows[2]];
  assert.equal(model.selectionAfterFilter(visible, 2, [rows[2]]), 0);
  assert.equal(model.selectionAfterFilter(visible, 0, [rows[2]]), -1);
  assert.equal(model.selectionAfterFilter(visible, -1, visible), -1);
  assert.equal(model.selectionAfterFilter([], 0, visible), -1);
  assert.equal(model.selectionAfterFilter(visible, 1, []), -1);
  // / and Ctrl+F open the filter (Shift allowed with /, other modifiers not).
  assert.equal(model.keyAction(0x2f, 0, "/"), "filter");
  assert.equal(model.keyAction(0x2f, 0x02000000, "/"), "filter");
  assert.equal(model.keyAction(0x37, 0x02000000, "/"), "filter");
  assert.equal(model.keyAction(0x46, 0x04000000, "\u0006"), "filter");
  assert.equal(model.keyAction(0x2f, 0x04000000, ""), "");
  assert.equal(model.keyAction(0x46, 0, "f"), "");
  assert.equal(model.keyAction(0x46, 0x04000000 | 0x02000000, ""), "");
}

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
