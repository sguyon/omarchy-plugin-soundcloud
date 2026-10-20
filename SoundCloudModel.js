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

// The filter field over the track list: it only searches the rows already
// loaded (no API call). A row matches when every word of the query is in its
// title or artist, ignoring case and accents. Each hit keeps its index in the
// full list. The query is cut at filterMaxChars; words are matched literally.
var filterMaxChars = 100

function filterNormalize(value) {
  return String(value === undefined || value === null ? "" : value)
    .normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase()
}

function filterWords(query) {
  var text = String(query === undefined || query === null ? "" : query).slice(0, filterMaxChars)
  return filterNormalize(text).split(/\s+/).filter(function(word) { return word !== "" })
}

function filterTracks(rows, query) {
  var list = Array.isArray(rows) ? rows : []
  var words = filterWords(query)
  var hits = []
  for (var i = 0; i < list.length; i++) {
    var row = list[i]
    var text = row ? filterNormalize(row.title) + "\n" + filterNormalize(row.artist) : ""
    var all = true
    for (var w = 0; w < words.length && all; w++) all = text.indexOf(words[w]) >= 0
    if (all && (row || words.length === 0)) hits.push({ track: row, index: i })
  }
  return hits
}

// "24" while the query is empty, "2 of 24" while filtering.
function filterCountText(query, matches, total) {
  return filterWords(query).length === 0 ? String(total) : matches + " of " + total
}

// The selected row follows its track when the visible rows change: its new
// position, or -1 when it is gone (or nothing was selected).
function selectionAfterFilter(before, index, after) {
  var old = Array.isArray(before) ? before[clampSelection(index, before.length)] : null
  var id = old ? String(old.playbackId || "") : ""
  if (id === "" || !Array.isArray(after)) return -1
  for (var i = 0; i < after.length; i++) {
    if (after[i] && String(after[i].playbackId || "") === id) return i
  }
  return -1
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

// The stops Tab walks. With the lists folded away on demand (startFolded) the walk has a
// stop before Home where no tab is open: Tab from there opens Home, and Tab from
// Queue folds everything again.
function tabStopsFor(foldable) {
  return foldable === true ? ["none"].concat(tabStops) : tabStops
}

// The stop that looks selected: Queue while its view is open, else the tab, or "none"
// while the lists are folded away.
function currentStop(selectedTab, queueOpen, folded) {
  if (folded === true) return "none"
  return queueOpen === true ? "queue" : selectedTab
}

// Count badge on the Queue button: empty (hidden) at 0, "99+" from 100.
// The tracks still to come: the badge counts these (the same number as "N next" in the
// queue view), not the whole queue with the tracks that already played.
function upcomingCount(length, cursor) {
  var total = Math.max(0, Math.floor(Number(length) || 0))
  var at = Math.floor(Number(cursor))
  if (!Number.isFinite(at) || at < 0) return total
  return Math.max(0, total - at - 1)
}

function queueBadgeText(length) {
  var count = Math.max(0, Math.floor(Number(length) || 0))
  if (count === 0) return ""
  return count > 99 ? "99+" : String(count)
}

// The header avatar opens the Library, a fixed address that never comes from
// the status; the tooltip names the signed-in account when it is known.
var libraryUrl = "https://soundcloud.com/you/library"

function accountTooltip(name) {
  var text = String(name === undefined || name === null ? "" : name).replace(/\s+/g, " ").trim()
  return text === "" ? "Open Library" : "Signed in as " + text + " (open Library)"
}

// A profile picture is only shown from a small png/jpeg data URL the backend made.
function accountAvatarSource(value) {
  var url = String(value === undefined || value === null ? "" : value)
  return /^data:image\/(png|jpeg);base64,/.test(url) && url.length <= 180000 ? url : ""
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

// List scrolling. A two-finger swipe moves the list listScrollFactor times as far as
// the toolkit's own 1:1 handling (touchpad pixels), and a mouse notch (120 units) moves
// a few rows. The change is the new contentY minus the old one, so it is positive
// when the content moves up (a swipe towards the top of the screen).
var listScrollFactor = 3
var wheelNotchPixels = 60
var minFlickVelocity = 200
var maxFlickVelocity = 6000

function scrollChange(pixelDeltaY, angleDeltaY) {
  var pixels = Number(pixelDeltaY)
  var angle = Number(angleDeltaY)
  var base = 0
  if (Number.isFinite(pixels) && pixels !== 0) base = pixels
  else if (Number.isFinite(angle) && angle !== 0) base = angle / wheelNotch * wheelNotchPixels
  return -base * listScrollFactor || 0
}

// The new contentY, kept inside the list (no overscroll).
function scrolledContentY(contentY, change, originY, contentHeight, viewHeight) {
  var top = Number(originY) || 0
  var bottom = Math.max(top, top + (Number(contentHeight) || 0) - (Number(viewHeight) || 0))
  var y = Number(contentY)
  if (!Number.isFinite(y)) y = top
  return Math.max(top, Math.min(bottom, y + (Number(change) || 0)))
}

// Running speed of the swipe in px/s: each event pulls the estimate towards the
// speed it implies, so one slow or odd event does not decide the release speed.
function swipeVelocity(previous, change, elapsedMs) {
  var before = Number(previous) || 0
  var dt = Number(elapsedMs)
  var moved = Number(change)
  if (!Number.isFinite(dt) || dt <= 0 || !Number.isFinite(moved)) return before
  var speed = moved / dt * 1000
  return before === 0 ? speed : before * 0.5 + speed * 0.5
}

// The speed to coast at after the fingers lift: 0 (no coasting) for a slow swipe.
function flickStartVelocity(velocity) {
  var speed = Number(velocity)
  if (!Number.isFinite(speed) || Math.abs(speed) < minFlickVelocity) return 0
  return Math.sign(speed) * Math.min(Math.abs(speed), maxFlickVelocity)
}

// Next and Previous follow the queue; an older backend that does not report it keeps them on.
function queueButtonEnabled(actionBusy, hasTrack, allowed) {
  return actionBusy !== true && hasTrack === true && allowed !== false
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

// How many sound waves the volume icon draws (VolumeGlyph): one, two or three.
function volumeWaves(state) {
  if (state === "low") return 1
  if (state === "medium") return 2
  if (state === "high") return 3
  return 0
}

// The volume icon's tooltip while the slider is not shown.
function volumeTooltip(volume, muted) {
  return muted === true ? "Muted (click or m to unmute)" : "Volume " + volumePercent(volume) + "% (click or m to mute)"
}

// The text next to the icon while the level changes.
function volumeText(volume, muted) {
  return muted === true ? "Muted" : volumePercent(volume) + "%"
}

// A wheel notch moves the volume by 5 points. A mouse wheel sends 120 units
// per notch and a touchpad swipe dozens of small events, so wheelSteps adds the
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

// Material Design glyphs: repeat-once, repeat, repeat-off.
function repeatIconGlyph(mode) {
  mode = repeatMode(mode)
  return mode === "one" ? "󰑘" : (mode === "all" ? "󰑖" : "󰑗")
}

var trackIdPattern = /^soundcloud:tracks:[1-9][0-9]*$/

// Commands for one track by id; "" if the id is not a track id.
function queueNextCommand(playbackId) {
  return trackIdPattern.test(String(playbackId || "")) ? "queue-next:" + playbackId : ""
}

function queueRemoveCommand(playbackId) {
  return trackIdPattern.test(String(playbackId || "")) ? "queue-remove:" + playbackId : ""
}

function queueAddCommand(playbackId) {
  return trackIdPattern.test(String(playbackId || "")) ? "queue-add:" + playbackId : ""
}

function queuePlayCommand(playbackId) {
  return trackIdPattern.test(String(playbackId || "")) ? "queue-play:" + playbackId : ""
}

// Moves a queue entry by a signed number of places (negative = earlier).
function queueMoveCommand(playbackId, offset) {
  var places = Number(offset)
  if (!trackIdPattern.test(String(playbackId || "")) || !Number.isInteger(places) || places === 0) return ""
  return "queue-move:" + playbackId + ":" + places
}

function toggleCommand(name, currentlyOn) {
  return (name === "autoplay" || name === "notify") ? name + ":" + (currentlyOn === true ? "off" : "on") : ""
}

// The small label under a queue row.
function queueRowLabel(item) {
  item = item || {}
  if (item.current === true) return "playing now"
  if (item.explicit === true) return "added by you"
  if (item.auto === true) return "related"
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

// How many places a queue row can move by one key press: 0 for the playing row,
// when nothing is selected, or at the ends (the playing row stays first).
function queueKeyMoveOffset(items, selectedId, direction) {
  var list = Array.isArray(items) ? items : []
  var at = queueIndexOf(list, selectedId)
  if (at < 1 || !(list[at] && list[at].current !== true)) return 0
  var step = Number(direction) < 0 ? -1 : (Number(direction) > 0 ? 1 : 0)
  var target = at + step
  return target >= 1 && target <= list.length - 1 ? step : 0
}

// Drag handle: the signed places a row dragged by `pixels` moves, clamped so it
// stays below the playing row (index 0) and inside the list.
function queueDragOffset(pixels, rowStep, index, count) {
  var step = Number(rowStep)
  var total = Math.max(0, Math.floor(Number(count) || 0))
  var at = Math.floor(Number(index))
  if (!(step > 0) || !Number.isFinite(Number(pixels)) || at < 1 || at > total - 1) return 0
  var places = Math.round(Number(pixels) / step)
  return Math.max(1 - at, Math.min(total - 1 - at, places)) || 0
}

// While a row is dragged from index `from` by `places` rows, the rows it passes
// make room: those it moved past shift one row (`step` pixels) the other way.
function queueDragShift(index, from, places, step) {
  var at = Math.floor(Number(index))
  var start = Math.floor(Number(from))
  var moved = Math.floor(Number(places))
  var size = Number(step)
  if (!(size > 0) || !(start >= 0) || !Number.isFinite(at) || !Number.isFinite(moved) || at === start) return 0
  if (moved > 0 && at > start && at <= start + moved) return -size
  if (moved < 0 && at < start && at >= start + moved) return size
  return 0
}

// The dim text on the queue toolbar: how many tracks follow the playing one.
function queueCountText(items) {
  var list = Array.isArray(items) ? items : []
  var following = list.length - (list.length > 0 && list[0] && list[0].current === true ? 1 : 0)
  return following > 0 ? following + " next" : "Empty"
}

// Only SoundCloud's own track pages are opened in the browser.
function trackPageUrl(url) {
  var text = String(url || "")
  return /^https:\/\/(www\.|m\.)?soundcloud\.com\/[^\s]+$/.test(text) && text.length <= 512 ? text : ""
}

function isEncrypted(track) {
  return !!track && track.encrypted === true
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

function barIconColor(playing, accent, foreground) {
  return playing === true ? accent : foreground
}

// Keys the popup handles before the key catcher (which would turn h, l and
// Shift+arrows into moves). Returns an action name, "ignore" for a key that is
// swallowed on purpose (h, no longer a seek alias), or "" to leave it alone.
//   PageDown, Shift+Right: next      PageUp, Shift+Left: previous
//   Shift+Up/Down: volume +/- 5      Alt+Up/Down: move the queue row
//   l: like/unlike   Shift+L: repeat   Shift+S: shuffle
//   Ctrl+V, Shift+Insert: link-next   Ctrl+Shift+V: link-play
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
  if (mods === keyAlt && key === 0x01000013) return "queue-up"
  if (mods === keyAlt && key === 0x01000015) return "queue-down"
  if (key === 0x4c || (none && letter === "l")) {
    if (none) return "like"
    if (shift) return "repeat"
    return ""
  }
  if (shift && (key === 0x53)) return "shuffle"
  // "/" is Shift+7 or Shift+: on some layouts, so Shift is allowed with it.
  if ((mods & ~keyShift) === 0 && (key === 0x2f || letter === "/")) return "filter"
  if (mods === keyCtrl && key === 0x46) return "filter"
  if (none && (key === 0x48 || letter === "h")) return "ignore"
  // Super may still be held when Omarchy's SUPER+V sends its Ctrl+V, so Meta is
  // ignored for the paste keys; in a terminal that shortcut sends Shift+Insert.
  var pasteMods = mods & ~keyMeta
  if (key === 0x56 && pasteMods === keyCtrl) return "link-next"
  if (key === 0x56 && pasteMods === (keyCtrl | keyShift)) return "link-play"
  if (key === 0x01000006 && pasteMods === keyShift) return "link-next"
  return ""
}

// Held keys repeat only for the ones that make sense repeated.
function actionRepeats(action) {
  return action === "volume-up" || action === "volume-down"
    || action === "queue-up" || action === "queue-down"
}

// A pasted link: only text that starts with https:// and has no whitespace.
function pastedLinkUrl(text) {
  var value = String(text === undefined || text === null ? "" : text).trim()
  return /^https:\/\/[^\s]+$/.test(value) && value.length <= 2048 ? value : ""
}

function linkCommand(url, playNow) {
  var clean = pastedLinkUrl(url)
  return clean === "" ? "" : (playNow === true ? "link-play:" : "link-next:") + encodeURIComponent(clean)
}

// The message line at the bottom of the popup (ToastLine): one policy for
// every message. An info message (a success, "Queued: ...") stays 4 s, an error
// 8 s; a new message replaces the old one and restarts the time; while the
// pointer is over it the time is paused, and 2 s are left once it leaves.
var toastInfoMs = 4000
var toastErrorMs = 8000
var toastLeaveMs = 2000
function toastLeaveDelay() { return toastLeaveMs }

function toastDelay(kind) {
  return kind === "error" ? toastErrorMs : toastInfoMs
}

// The same text pushed again by a status update (the backend repeats it) does
// not restart the time; anything else does.
function toastRestarts(currentKind, currentText, nextKind, nextText, fromStatus) {
  return !(fromStatus === true && currentKind === nextKind && String(currentText) === String(nextText))
}

// A status error the user dismissed (click, timeout or a new action) stays
// hidden while the backend keeps reporting the same text; an empty or a
// different status error shows again.
function statusErrorShows(dismissedText, nextText) {
  return String(nextText) !== "" && String(nextText) !== String(dismissedText)
}

// Commands the user starts (not the background reads) remove the old error.
function errorClearedByCommand(kind) {
  return kind === "like" || kind === "ui" || kind === "queue-edit" || kind === "paste"
    || kind === "action" || kind === "selection"
}

// What a paste does with the clipboard text: the backend command, or the
// message to show when there is nothing to send.
function pasteRequest(text, playNow) {
  var value = String(text === undefined || text === null ? "" : text).trim()
  if (value === "") return { command: "", error: "The clipboard has no text to paste" }
  var command = linkCommand(value, playNow)
  if (command === "") return { command: "", error: "The clipboard text is not a link" }
  return { command: command, error: "" }
}

// The short confirmation after a pasted link was accepted.
function pasteNotice(message) {
  if (!message || message.ok !== true) return ""
  var title = String(message.title || "").replace(/\s+/g, " ").trim()
  if (message.queued === true) return title === "" ? "Queued" : "Queued: " + title
  return title === "" ? "Playing the link" : "Playing: " + title
}

// Clear queue asks first: the toolbar switches to a confirm state that is
// dropped after clearConfirmMs of the pointer being away from the toolbar, when
// the queue view closes or when the queue changes (the signature is what was in
// the queue when it was armed).
var clearConfirmMs = 12000
function clearConfirmDelay() { return clearConfirmMs }

// The countdown runs only while a confirmation is pending and the pointer is
// away from the toolbar; coming back stops it, leaving starts it afresh.
function clearConfirmCountdownRuns(armedSignature, pointerOverToolbar) {
  return typeof armedSignature === "string" && pointerOverToolbar !== true
}

function queueSignature(rows) {
  var ids = []
  var list = Array.isArray(rows) ? rows : []
  for (var index = 0; index < list.length; index++) ids.push(String(list[index].playbackId))
  return ids.join("|")
}

function clearConfirmStays(armedSignature, rows, queueOpen) {
  return typeof armedSignature === "string" && queueOpen === true
    && queueSignature(rows) === armedSignature && Array.isArray(rows) && rows.length > 0
}

// Omarchy's on-screen display (omarchy.osd): the same icon names and value it
// uses for its own volume keys. progressText replaces the percent beside the
// bar (a message would drop the bar), so it is not mistaken for the system
// volume: this slider is the player's own volume.
function volumeOsdPayload(volume, muted) {
  var state = volumeIconState(volume, muted)
  var percent = state === "muted" ? 0 : volumePercent(volume)
  return JSON.stringify({ icon: "volume-" + state, value: percent, progressText: "SoundCloud " + percent + "%" })
}

// The bar-icon swipe shows Omarchy's OSD through the shell's own IPC target
// (omarchy-shell osd show <json>); the plugin cannot summon it itself.
function osdCommand(volume, muted) {
  return ["omarchy-shell", "osd", "show", volumeOsdPayload(volume, muted)]
}

// At most one OSD process per 80 ms while swiping; the latest value is sent
// by a trailing timer.
var osdIntervalMs = 80
function osdDue(now, lastAt) {
  return !(lastAt > 0) || now - lastAt >= osdIntervalMs || now < lastAt
}

// Waveform levels are only replaced when their content changed, so the canvas
// does not repaint on every status push.
function sameLevels(a, b) {
  if (a === b) return true
  if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
  for (var index = 0; index < a.length; index++) {
    if (a[index] !== b[index]) return false
  }
  return true
}

// A seek can make the backend report resolving/buffering for a moment, with the
// title, duration and waveform empty. When the track is the same one (or the
// status names none), that status must not wipe what is shown.
function isTransientStatus(state, currentPlaybackId) {
  state = state || {}
  var current = String(currentPlaybackId || "")
  var incoming = String(state.playbackId || "")
  return current !== ""
    && state.playerPresent !== false
    && selectionIsPending(String(state.playbackState || ""))
    && (incoming === "" || incoming === current)
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
    tabStopsFor: tabStopsFor,
    queueBadgeText: queueBadgeText,
    upcomingCount: upcomingCount,
    accountTooltip: accountTooltip,
    accountAvatarSource: accountAvatarSource,
    libraryUrl: libraryUrl,
    scrollChange: scrollChange,
    scrolledContentY: scrolledContentY,
    swipeVelocity: swipeVelocity,
    flickStartVelocity: flickStartVelocity,
    seekRepeatCount: seekRepeatCount,
    seekSeconds: seekSeconds,
    seekDue: seekDue,
    seekByCommand: seekByCommand,
    isSeekKey: isSeekKey,
    queueButtonEnabled: queueButtonEnabled,
    volumePercent: volumePercent,
    volumeTooltip: volumeTooltip,
    volumeIconState: volumeIconState,
    volumeWaves: volumeWaves,
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
    repeatIconGlyph: repeatIconGlyph,
    queueNextCommand: queueNextCommand,
    queueRemoveCommand: queueRemoveCommand,
    queueAddCommand: queueAddCommand,
    queuePlayCommand: queuePlayCommand,
    queueMoveCommand: queueMoveCommand,
    queueIndexOf: queueIndexOf,
    queueCountText: queueCountText,
    queueSelectionAfterMove: queueSelectionAfterMove,
    queueSelectionAfterUpdate: queueSelectionAfterUpdate,
    queueKeyMoveOffset: queueKeyMoveOffset,
    queueDragOffset: queueDragOffset,
    queueDragShift: queueDragShift,
    keyAction: keyAction,
    filterTracks: filterTracks,
    filterCountText: filterCountText,
    selectionAfterFilter: selectionAfterFilter,
    actionRepeats: actionRepeats,
    pastedLinkUrl: pastedLinkUrl,
    linkCommand: linkCommand,
    volumeOsdPayload: volumeOsdPayload,
    toastDelay: toastDelay,
    toastLeaveDelay: toastLeaveDelay,
    toastRestarts: toastRestarts,
    statusErrorShows: statusErrorShows,
    errorClearedByCommand: errorClearedByCommand,
    pasteRequest: pasteRequest,
    pasteNotice: pasteNotice,
    clearConfirmDelay: clearConfirmDelay,
    queueSignature: queueSignature,
    clearConfirmStays: clearConfirmStays,
    clearConfirmCountdownRuns: clearConfirmCountdownRuns,
    osdCommand: osdCommand,
    osdDue: osdDue,
    sameLevels: sameLevels,
    isTransientStatus: isTransientStatus,
    positionFromStatus: positionFromStatus,
    seekedPosition: seekedPosition,
    toggleCommand: toggleCommand,
    queueRowLabel: queueRowLabel,
    upcomingQueue: upcomingQueue,
    trackPageUrl: trackPageUrl,
    isEncrypted: isEncrypted,
    isCurrentSelectionResponse: isCurrentSelectionResponse,
    isCurrentTrack: isCurrentTrack,
    errorShownOnRow: errorShownOnRow,
    barIconColor: barIconColor,
    launchError: launchError
  }
}
