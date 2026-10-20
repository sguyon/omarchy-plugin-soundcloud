function displayLabel(player) {
  if (!player) return "SoundCloud"
  var title = String(player.trackTitle || "")
  var artist = String(player.trackArtist || "")
  if (title && artist) return title + "  ·  " + artist
  return title || artist || "SoundCloud"
}

function applyTrackResult(cachedTracks, result) {
  var cached = Array.isArray(cachedTracks) ? cachedTracks : []
  if (result && result.pending === true) {
    return { tracks: cached, loading: true }
  }
  var incoming = result && Array.isArray(result.tracks) ? result.tracks.slice(0, 100) : []
  return { tracks: incoming, loading: false }
}

function shouldLoadMoreTracks(state) {
  state = state || {}
  var count = Math.max(0, Math.floor(Number(state.count) || 0))
  var requestedCount = Math.max(0, Math.floor(Number(state.requestedCount) || 0))
  var yPosition = Number(state.yPosition)
  var heightRatio = Number(state.heightRatio)
  var nearEnd = state.atEnd === true
    || (Number.isFinite(yPosition) && yPosition >= 0
      && Number.isFinite(heightRatio) && heightRatio > 0
      && yPosition + heightRatio >= 0.85)
  return state.userInitiated === true
    && state.hasMore !== false
    && nearEnd
    && state.pending !== true
    && count > 0
    && count > requestedCount
}

function contentYAfterTrackUpdate(previousY, previousCount, nextCount) {
  var y = Number(previousY)
  var before = Math.max(0, Math.floor(Number(previousCount) || 0))
  var after = Math.max(0, Math.floor(Number(nextCount) || 0))
  return Number.isFinite(y) && after > before ? y : null
}

function shouldPreservePlaybackMetadata(state, currentTitle) {
  state = state || {}
  return state.playerPresent !== false
    && String(state.title || "") === ""
    && String(currentTitle || "") !== ""
    && /^https:\/\/soundcloud\.com\/(discover|feed)([/?#]|$)/.test(String(state.url || ""))
}

function selectionIsPending(playbackState) {
  return playbackState === "resolving" || playbackState === "buffering"
}

function pendingLabel(playbackState) {
  if (playbackState === "resolving") return "Loading…"
  if (playbackState === "buffering") return "Buffering…"
  return ""
}

// Starting a track from a list (play-from) or by id (play) replaces what is loading.
function isSelectionAction(action) {
  return action === "play" || action === "play-from"
}

function canStartAction(actionBusy, selectionBusy, action) {
  return !actionBusy || (selectionBusy === true && isSelectionAction(action))
}

// The command that plays a track from the list it was clicked in, or "" if the ids are not valid.
function playFromCommand(tab, playbackId) {
  var validTab = tab === "home" || tab === "feed" || tab === "likes" || tab === "history"
  var validId = /^soundcloud:tracks:[1-9][0-9]*$/.test(String(playbackId || ""))
  return validTab && validId ? "play-from:" + tab + ":" + playbackId : ""
}

// Keyboard selection in the track list: -1 means no row is selected.
function clampSelection(index, count) {
  var total = Math.max(0, Math.floor(Number(count) || 0))
  var value = Math.floor(Number(index))
  if (total === 0 || !Number.isFinite(value) || value < 0) return -1
  return Math.min(value, total - 1)
}

// The first move from "nothing selected" lands on the first row; moves stop at the ends.
function moveSelection(index, delta, count) {
  var total = Math.max(0, Math.floor(Number(count) || 0))
  if (total === 0) return -1
  var current = clampSelection(index, total)
  if (current < 0) return 0
  return clampSelection(Math.max(0, current + Math.sign(Number(delta) || 0)), total)
}

// A refresh keeps the row when the list only grew; a reset starts over.
function selectionAfterUpdate(index, count, reset) {
  return reset === true ? -1 : clampSelection(index, count)
}

// Tab and Shift+Tab walk the four tabs and then Queue (the fifth stop, at the
// right end of the tab row) in order and wrap around.
var tabStops = ["home", "feed", "likes", "history", "queue"]

function cycleTab(tab, direction, tabs) {
  var list = Array.isArray(tabs) && tabs.length > 0 ? tabs : tabStops
  var at = list.indexOf(tab)
  if (at < 0) return list[0]
  var step = Number(direction) < 0 ? -1 : 1
  return list[(at + step + list.length) % list.length]
}

// The stop that looks selected: Queue while its view is open, else the tab.
function currentStop(selectedTab, queueOpen) {
  return queueOpen === true ? "queue" : selectedTab
}

// The tracks still to come: the badge on the Queue button counts these (the same
// number as "N next" in the queue view), not the whole queue with the tracks
// that already played.
function upcomingCount(length, cursor) {
  var total = Math.max(0, Math.floor(Number(length) || 0))
  var at = Math.floor(Number(cursor))
  if (!Number.isFinite(at) || at < 0) return total
  return Math.max(0, total - at - 1)
}

// Count badge on the Queue button: empty (hidden) at 0, "99+" from 100.
function queueBadgeText(length) {
  var count = Math.max(0, Math.floor(Number(length) || 0))
  if (count === 0) return ""
  return count > 99 ? "99+" : String(count)
}

function isCurrentSelectionResponse(activeRequestId, responseId) {
  return Number(activeRequestId) > 0 && Number(activeRequestId) === Number(responseId)
}

function isCurrentTrack(trackPlaybackId, currentPlaybackId) {
  var current = String(currentPlaybackId || "")
  return current !== "" && String(trackPlaybackId || "") === current
}

function errorShownOnRow(tracks, playbackId, playbackState) {
  if (playbackState !== "error" || !Array.isArray(tracks)) return false
  for (var index = 0; index < tracks.length; index++) {
    if (tracks[index] && isCurrentTrack(tracks[index].playbackId, playbackId)) return true
  }
  return false
}

// Volume icon states follow Omarchy's own OSD (plugins/osd/OsdModel.js iconFor):
// muted at 0% or when muted, low up to 33%, medium up to 66%, high above.
function volumePercent(volume) {
  var value = Number(volume)
  return Number.isFinite(value) ? Math.round(Math.max(0, Math.min(1, value)) * 100) : 100
}

function volumeIconState(volume, muted) {
  var percent = volumePercent(volume)
  if (muted === true || percent <= 0) return "muted"
  if (percent <= 33) return "low"
  if (percent <= 66) return "medium"
  return "high"
}

function volumeIconGlyph(state) {
  if (state === "muted") return ""
  if (state === "low") return ""
  if (state === "medium") return ""
  return ""
}

// The volume icon's tooltip while the slider is not shown.
function volumeTooltip(volume, muted) {
  return muted === true ? "Muted (click to unmute)" : "Volume " + volumePercent(volume) + "% (click to mute)"
}

// The text next to the icon while the level changes.
function volumeText(volume, muted) {
  return muted === true ? "Muted" : volumePercent(volume) + "%"
}

// A wheel notch moves the volume by 5 points. A mouse wheel sends 120 units
// per notch and a smooth-scrolling swipe dozens of small events, so wheelSteps adds the
// events up and gives one step per full notch. volumeStepCommand and
// steppedVolume then only use the sign of one step.
var volumeStep = 0.05
var wheelNotch = 120

// Adds a wheel event to the running total; returns the whole notches reached
// (signed) and the remainder to carry. A change of direction drops the old
// remainder. The caller also clears the total after a short idle.
function wheelSteps(accumulator, delta) {
  var carried = Number(accumulator)
  var change = Number(delta)
  if (!Number.isFinite(carried)) carried = 0
  if (!Number.isFinite(change) || change === 0) return { steps: 0, accumulator: carried }
  if (carried !== 0 && Math.sign(carried) !== Math.sign(change)) carried = 0
  var total = carried + change
  var steps = Math.trunc(total / wheelNotch) || 0
  return { steps: steps, accumulator: total - steps * wheelNotch }
}

function volumeStepCommand(wheelDelta) {
  var delta = Number(wheelDelta)
  if (!Number.isFinite(delta) || delta === 0) return ""
  return "volume-step:" + (delta > 0 ? "+" : "-") + volumeStep.toFixed(2)
}

// The level shown right away, before the backend confirms it. Stays within 0..1.
function steppedVolume(volume, wheelDelta) {
  var current = Number(volume)
  if (!Number.isFinite(current)) current = 1
  var delta = Number(wheelDelta)
  var change = !Number.isFinite(delta) || delta === 0 ? 0 : (delta > 0 ? volumeStep : -volumeStep)
  return Math.max(0, Math.min(1, Math.round((current + change) * 100) / 100))
}

// The volume slider that opens beside the volume icon. Pure helpers for the
// pointer maths and the show/hide timing; VolumeSlider.qml and BarWidget use them.
var sliderThrottleMs = 50     // at most one `moved` per 50 ms while dragging
var sliderHideMs = 600        // the bar stays this long after the pointer leaves
var sliderFlashMs = 1000      // and this long after a wheel or key change
var sliderMinWidth = 48       // narrower than this the bar is not shown
var sliderMaxWidth = 96

// The level for a pointer x: the handle centre runs from `left` over `span`
// pixels. Clamped to 0..1 and rounded to whole percent (the command's precision).
function sliderValueFromX(x, left, span) {
  var position = Number(x)
  var origin = Number(left)
  var length = Number(span)
  if (!Number.isFinite(position) || !Number.isFinite(origin) || !Number.isFinite(length) || length <= 0) return 0
  return Math.round(Math.max(0, Math.min(1, (position - origin) / length)) * 100) / 100
}

// True when enough time passed since the last emit (or there was none, or the
// clock went backwards).
function sliderEmitDue(lastMs, nowMs, intervalMs) {
  var last = Number(lastMs)
  var now = Number(nowMs)
  if (!Number.isFinite(last) || !Number.isFinite(now) || last <= 0 || now < last) return true
  return now - last >= Number(intervalMs)
}

// The bar's width for the free space between the icon and the transport group,
// 0 when there is too little room (it must never overlap the buttons).
function sliderWidth(available) {
  var room = Number(available)
  if (!Number.isFinite(room) || room < sliderMinWidth) return 0
  return Math.min(sliderMaxWidth, Math.floor(room))
}

// How long the bar lingers after "leave" (pointer left) or "flash" (wheel or key).
function sliderHideDelay(reason) {
  return reason === "flash" ? sliderFlashMs : sliderHideMs
}

// Shown while the pointer is on the icon or the bar, while dragging, and until
// the linger timer runs out.
function sliderVisible(pointerOver, dragging, lingering) {
  return pointerOver === true || dragging === true || lingering === true
}

// The handle position: a muted player shows 0, the level is kept underneath.
function sliderDisplayValue(volume, muted) {
  if (muted === true) return 0
  var level = Number(volume)
  return Number.isFinite(level) ? Math.max(0, Math.min(1, level)) : 1
}

// The command for a dragged level.
function volumeSetCommand(value) {
  var level = Number(value)
  if (!Number.isFinite(level)) return ""
  return "volume:" + Math.max(0, Math.min(1, level)).toFixed(2)
}

function repeatMode(value) {
  return value === "one" || value === "all" ? value : "none"
}

var trackIdPattern = /^soundcloud:tracks:[1-9][0-9]*$/

// Commands for one track by id; "" if the id is not a track id.
function queuePlayCommand(playbackId) {
  return trackIdPattern.test(String(playbackId || "")) ? "queue-play:" + playbackId : ""
}

function queueRemoveCommand(playbackId) {
  return trackIdPattern.test(String(playbackId || "")) ? "queue-remove:" + playbackId : ""
}

// The small label under a queue row.
function queueRowLabel(item) {
  item = item || {}
  if (item.current === true) return "playing now"
  if (item.explicit === true) return "added by you"
  return ""
}

// What the queue view lists: the current track and everything after it.
function upcomingQueue(queue) {
  var items = queue && Array.isArray(queue.items) ? queue.items : []
  var start = 0
  for (var index = 0; index < items.length; index++) {
    if (items[index] && items[index].current === true) { start = index; break }
  }
  return items.slice(start)
}

function queueIndexOf(items, playbackId) {
  var list = Array.isArray(items) ? items : []
  for (var index = 0; index < list.length; index++) {
    if (list[index] && String(list[index].playbackId || "") === String(playbackId || "") && playbackId) return index
  }
  return -1
}

// Keyboard selection in the queue view is kept by track id, so it follows a row
// that moves. Up/Down walk the rows and stop at the ends; the first move from
// "nothing selected" lands on the first row after the playing one (or the playing
// one if it is alone).
function queueSelectionAfterMove(items, selectedId, delta) {
  var list = Array.isArray(items) ? items : []
  if (list.length === 0) return ""
  var at = queueIndexOf(list, selectedId)
  var next
  if (at < 0) {
    next = list.length > 1 ? 1 : 0
    if (Number(delta) < 0) next = list.length - 1
  } else {
    next = Math.max(0, Math.min(list.length - 1, at + Math.sign(Number(delta) || 0)))
  }
  return String(list[next].playbackId || "")
}

// The selection is dropped when its track left the queue.
function queueSelectionAfterUpdate(items, selectedId) {
  return queueIndexOf(items, selectedId) >= 0 ? String(selectedId) : ""
}

// The dim text on the queue toolbar: how many tracks follow the playing one.
function queueCountText(items) {
  var list = Array.isArray(items) ? items : []
  var following = list.length - (list.length > 0 && list[0] && list[0].current === true ? 1 : 0)
  return following > 0 ? following + " next" : "Empty"
}

// Next and Previous follow the queue; an older backend that does not report it keeps them on.
function queueButtonEnabled(actionBusy, hasTrack, allowed) {
  return actionBusy !== true && hasTrack === true && allowed !== false
}

function barIconColor(playing, accent, foreground) {
  return playing === true ? accent : foreground
}

// Keys the popup handles before the key catcher (which would turn h, l and
// Shift+arrows into moves). Returns an action name, "ignore" for a key that is
// swallowed on purpose (h, no longer a seek alias), or "" to leave it alone.
//   PageDown, Shift+Right: next      PageUp, Shift+Left: previous
//   Shift+Up/Down: volume +/- 5      l: like/unlike
//   Shift+L: repeat   Shift+S: shuffle
// j/k stay list navigation (Omarchy convention) although the website uses K/J
// for previous/next.
var keyShift = 0x02000000
var keyCtrl = 0x04000000
var keyAlt = 0x08000000
var keyMeta = 0x10000000

function keyAction(key, modifiers, text) {
  var mods = (Number(modifiers) || 0) & (keyShift | keyCtrl | keyAlt | keyMeta)
  var shift = mods === keyShift
  var none = mods === 0
  var letter = String(text || "").toLowerCase()
  if (none && key === 0x01000017) return "next"
  if (none && key === 0x01000016) return "previous"
  if (shift && key === 0x01000014) return "next"
  if (shift && key === 0x01000012) return "previous"
  if (shift && key === 0x01000013) return "volume-up"
  if (shift && key === 0x01000015) return "volume-down"
  if (key === 0x4c || (none && letter === "l")) {
    if (none) return "like"
    if (shift) return "repeat"
    return ""
  }
  if (shift && (key === 0x53)) return "shuffle"
  if (none && (key === 0x48 || letter === "h")) return "ignore"
  return ""
}

// Held keys repeat only for the ones that make sense repeated.
function actionRepeats(action) {
  return action === "volume-up" || action === "volume-down"
}

// Left and Right seek like the website: a step is max(5, n) seconds where n counts
// the key-repeat events since the key went down (a fresh press resets it, only the
// seek keys count), and at most one seek goes out every 200 ms. The backend command
// `seek-by:<signed seconds>` clamps to the track.
var seekMinSeconds = 5
var seekIntervalMs = 200

function seekRepeatCount(previous, isAutoRepeat) {
  var count = Math.max(0, Math.floor(Number(previous) || 0))
  return isAutoRepeat === true ? count + 1 : 0
}

function seekSeconds(direction, repeats) {
  var sign = Number(direction) < 0 ? -1 : (Number(direction) > 0 ? 1 : 0)
  var count = Math.max(0, Math.floor(Number(repeats) || 0))
  return sign * Math.max(seekMinSeconds, count)
}

// True when enough time has passed since the last seek that went out; a clock that
// moved backwards also counts as due.
function seekDue(now, lastAt) {
  var last = Number(lastAt)
  if (lastAt === undefined || lastAt === null || !Number.isFinite(last)) return true
  var elapsed = Number(now) - last
  return !Number.isFinite(elapsed) || elapsed < 0 || elapsed >= seekIntervalMs
}

function seekByCommand(seconds) {
  var value = Math.round(Number(seconds))
  if (!Number.isFinite(value) || value === 0) return ""
  return "seek-by:" + value
}

// Keys that seek: Left and Right only (h and l are no longer aliases: l likes).
function isSeekKey(key) {
  return key === 0x01000012 || key === 0x01000014
}

// A status position that arrives within `windowMs` of an optimistic position
// (set by a seek) is older than the seek and is ignored.
var optimisticPositionMs = 500

function positionFromStatus(current, incoming, now, optimisticAt) {
  var at = Number(optimisticAt)
  var elapsed = Number(now) - at
  if (Number.isFinite(at) && at > 0 && elapsed >= 0 && elapsed < optimisticPositionMs) return Number(current) || 0
  return Number(incoming) || 0
}

// The position after a seek of `seconds`, kept inside the track.
function seekedPosition(position, seconds, duration) {
  var total = Number(duration)
  var moved = (Number(position) || 0) + (Number(seconds) || 0)
  return Math.max(0, Number.isFinite(total) && total > 0 ? Math.min(total, moved) : moved)
}

function launchError(line) {
  try {
    var result = JSON.parse(line)
    return result && result.ok === false && result.error ? String(result.error) : ""
  } catch (error) {
    return ""
  }
}

if (typeof module !== "undefined") {
  module.exports = {
    displayLabel: displayLabel,
    applyTrackResult: applyTrackResult,
    shouldLoadMoreTracks: shouldLoadMoreTracks,
    contentYAfterTrackUpdate: contentYAfterTrackUpdate,
    shouldPreservePlaybackMetadata: shouldPreservePlaybackMetadata,
    selectionIsPending: selectionIsPending,
    pendingLabel: pendingLabel,
    isSelectionAction: isSelectionAction,
    canStartAction: canStartAction,
    playFromCommand: playFromCommand,
    clampSelection: clampSelection,
    moveSelection: moveSelection,
    selectionAfterUpdate: selectionAfterUpdate,
    cycleTab: cycleTab,
    currentStop: currentStop,
    queueBadgeText: queueBadgeText,
    upcomingCount: upcomingCount,
    isCurrentSelectionResponse: isCurrentSelectionResponse,
    isCurrentTrack: isCurrentTrack,
    errorShownOnRow: errorShownOnRow,
    volumePercent: volumePercent,
    volumeTooltip: volumeTooltip,
    volumeIconState: volumeIconState,
    volumeIconGlyph: volumeIconGlyph,
    volumeText: volumeText,
    wheelSteps: wheelSteps,
    volumeStepCommand: volumeStepCommand,
    steppedVolume: steppedVolume,
    sliderThrottleMs: sliderThrottleMs,
    sliderHideMs: sliderHideMs,
    sliderFlashMs: sliderFlashMs,
    sliderValueFromX: sliderValueFromX,
    sliderEmitDue: sliderEmitDue,
    sliderWidth: sliderWidth,
    sliderHideDelay: sliderHideDelay,
    sliderVisible: sliderVisible,
    sliderDisplayValue: sliderDisplayValue,
    volumeSetCommand: volumeSetCommand,
    repeatMode: repeatMode,
    queuePlayCommand: queuePlayCommand,
    queueRemoveCommand: queueRemoveCommand,
    queueRowLabel: queueRowLabel,
    upcomingQueue: upcomingQueue,
    queueIndexOf: queueIndexOf,
    queueCountText: queueCountText,
    queueSelectionAfterMove: queueSelectionAfterMove,
    queueSelectionAfterUpdate: queueSelectionAfterUpdate,
    queueButtonEnabled: queueButtonEnabled,
    barIconColor: barIconColor,
    seekRepeatCount: seekRepeatCount,
    seekSeconds: seekSeconds,
    seekDue: seekDue,
    seekByCommand: seekByCommand,
    isSeekKey: isSeekKey,
    keyAction: keyAction,
    actionRepeats: actionRepeats,
    positionFromStatus: positionFromStatus,
    seekedPosition: seekedPosition,
    launchError: launchError
  }
}
