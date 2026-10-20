import QtQuick
import Quickshell
import Quickshell.Io
import qs.Ui
import qs.Commons
import "SoundCloudModel.js" as SoundCloudModel

BarWidget {
  id: root
  moduleName: "brunosilveira.soundcloud"

  property bool popupOpen: false
  property bool running: false
  property bool loggedIn: false
  property bool playing: false
  property string playbackState: "idle"
  property string playbackId: ""
  property bool liked: false
  property bool canNext: true
  property bool canPrevious: true
  property bool likeBusy: false
  property real volume: 1
  property bool muted: false
  property bool shuffle: false
  property string repeat: "none"
  property int queueCursor: -1
  property int queueLength: 0
  property string title: ""
  property string artist: ""
  property string artDataUrl: ""
  property var waveformLevels: []
  property real duration: 0
  property real position: 0
  // When the position was last set by a seek here; status positions right after
  // it are older than the seek and are ignored.
  property double optimisticPositionAt: 0
  property string lastError: ""
  property bool actionBusy: false
  property bool selectionBusy: false
  // Keyboard selection in the track list (-1 = none). It resets when the tab
  // changes or the list is replaced, and Enter plays that row.
  property int selectedRow: -1
  // Return emits returnRequested and then activateRequested; Space only the
  // latter. This flag tells the two apart.
  property bool returnHeld: false
  // When the last seek went out (Left/Right follow the website: max(5, n)
  // seconds, one per 200 ms).
  property double lastSeekAt: 0
  // The queue view replaces the track list below the tabs; queueData is the
  // last `queue` response ({cursor, items, explicit_count}).
  property bool queueOpen: false
  property var queueData: ({})
  property bool queueRequestPending: false
  property bool queueStale: false
  // Keyboard selection in the queue view, by track id so it follows a row that
  // moves ("" = none).
  property string queueSelectedId: ""
  readonly property var queueRows: SoundCloudModel.upcomingQueue(queueData)
  // The queue as it was when Clear was pressed (null = no confirmation pending).
  property var clearArmedSignature: null
  // The list row whose action menu is open ("tab:playbackId"), if any.
  property string menuRowKey: ""
  property int activeSelectionRequestId: 0
  property string selectedTab: "home"
  property var tracks: []
  property var homeTracks: []
  property var feedTracks: []
  property var likesTracks: []
  property var historyTracks: []
  property var trackArtworkData: ({})
  property var trackArtworkPending: ({})
  property bool tracksLoading: false
  property bool homeLoadMorePending: false
  property bool feedLoadMorePending: false
  property bool likesLoadMorePending: false
  property bool historyLoadMorePending: false
  readonly property bool loadMorePending: loadMorePendingFor(selectedTab)
  property bool homeHasMore: true
  property bool feedHasMore: true
  property bool likesHasMore: true
  property bool historyHasMore: true
  property int homeLoadMoreAtCount: 0
  property int feedLoadMoreAtCount: 0
  property int likesLoadMoreAtCount: 0
  property int historyLoadMoreAtCount: 0
  property int trackLoadAttempts: 0
  property bool tracksRequestPending: false
  property int nextRequestId: 1
  property var pendingRequests: ({})
  // The volume slider beside the volume icon: shown while the pointer is on the
  // icon or the bar, while dragging, and for a moment after a wheel change or
  // after the pointer leaves (volumeLinger, ended by volumeHideTimer).
  property bool volumeLinger: false
  readonly property bool volumePointer: volumeIconHover.hovered || volumeSliderHover.hovered
  readonly property bool volumeDragging: volumeSlider.dragging
  readonly property bool volumeShown: SoundCloudModel.sliderVisible(volumePointer, volumeDragging, volumeLinger)
  // Status updates leave the level alone while it is dragged and for a moment after.
  property bool volumeSettling: false
  onVolumePointerChanged: {
    if (volumePointer) return
    volumeLinger = true
    volumeHideTimer.interval = SoundCloudModel.sliderHideDelay("leave")
    volumeHideTimer.restart()
  }
  property bool connectionInitialized: false
  // Probe an existing daemon so shell restarts preserve playback, but never
  // launch one until the user interacts with the plugin.
  property bool backendWanted: true
  property bool socketProbePending: true
  property bool launchingBackend: false
  property string socketBuffer: ""
  readonly property int maxSocketFrameChars: 524288
  property bool hasTrack: title !== ""
  readonly property var activeSocket: socketLoader.item
  readonly property bool backendConnected: !!(activeSocket && activeSocket.connected)

  readonly property string helperPath: {
    var value = Qt.resolvedUrl("soundcloud_app.py").toString()
    return value.indexOf("file://") === 0 ? decodeURIComponent(value.substring(7)) : value
  }
  readonly property string socketPath: {
    var runtime = Quickshell.env("XDG_RUNTIME_DIR")
    return runtime ? String(runtime) + "/omarchy-soundcloud/control.sock" : ""
  }
  readonly property string label: SoundCloudModel.displayLabel({ trackTitle: title, trackArtist: artist })
  readonly property string safeTooltipLabel: plainForHost(label)
  readonly property string playIcon: playing ? "󰏤" : "󰐊"
  readonly property color dim: Qt.darker(bar.foreground, 1.5)

  // Bar.qml routes `omarchy-shell shell summon|hide|toggle` through these.
  readonly property bool opened: popupOpen

  function open() {
    popupOpen = true
    if (!backendConnected) startBackend(false)
    else if (tracks.length === 0) selectTab(selectedTab)
  }

  function close() {
    popupOpen = false
    queueOpen = false
    menuRowKey = ""
    selectedRow = -1
    queueSelectedId = ""
  }

  // Keyboard actions (see PanelKeyCatcher below).
  function moveSelectedRow(delta) {
    if (queueOpen) {
      queueSelectedId = SoundCloudModel.queueSelectionAfterMove(queueRows, queueSelectedId, delta)
      var at = SoundCloudModel.queueIndexOf(queueRows, queueSelectedId)
      if (at >= 0) queueList.positionViewAtIndex(at, ListView.Contain)
      return
    }
    selectedRow = SoundCloudModel.moveSelection(selectedRow, delta, tracks.length)
    menuRowKey = ""
    if (selectedRow >= 0) trackList.positionViewAtIndex(selectedRow, ListView.Contain)
  }

  // Enter plays the selected row from its list or from the queue, like a click on it.
  function playSelectedRow() {
    if (queueOpen) {
      var queued = queueRows[SoundCloudModel.queueIndexOf(queueRows, queueSelectedId)]
      if (queued && queued.current !== true) sendUi(SoundCloudModel.queuePlayCommand(queued.playbackId))
      return
    }
    var track = tracks[selectedRow]
    if (!track || SoundCloudModel.isEncrypted(track)) return
    var command = SoundCloudModel.playFromCommand(selectedTab, track.playbackId)
    if (command !== "" && (!actionBusy || selectionBusy)) runAction("play-from", command)
  }

  // direction is -1 or +1; seekKeys (below) counts the key repeats.
  function seekKey(direction) {
    if (!running || !loggedIn || !hasTrack) return
    var now = Date.now()
    if (!SoundCloudModel.seekDue(now, lastSeekAt)) return
    lastSeekAt = now
    var seconds = SoundCloudModel.seekSeconds(direction, seekKeys.repeats)
    position = SoundCloudModel.seekedPosition(position, seconds, duration)
    optimisticPositionAt = now
    sendUi(SoundCloudModel.seekByCommand(seconds))
  }

  // Shortcuts from AppKeys (next, previous, volume, like, repeat, shuffle); the
  // mapping is SoundCloudModel.keyAction.
  function appKey(action) {
    if (!running || !loggedIn) return
    if (action === "next" || action === "previous") {
      if (SoundCloudModel.queueButtonEnabled(actionBusy, hasTrack, action === "next" ? canNext : canPrevious))
        runAction(action)
    } else if (action === "volume-up" || action === "volume-down") {
      stepVolumeOnce(action === "volume-up" ? 1 : -1)
    } else if (action === "like") {
      toggleLike()
    } else if (action === "repeat") {
      sendUi("repeat")
    } else if (action === "shuffle") {
      sendUi("shuffle")
    } else if (action === "queue-up" || action === "queue-down") {
      if (queueOpen) moveQueueRow(queueSelectedId, SoundCloudModel.queueKeyMoveOffset(
        queueRows, queueSelectedId, action === "queue-up" ? -1 : 1))
    }
  }

  // Clear queue asks first (see SoundCloudModel.clearConfirmStays).
  function armClearQueue() {
    if (queueLength <= 0) return
    clearArmedSignature = SoundCloudModel.queueSignature(queueRows)
  }

  function cancelClearQueue() {
    clearArmedSignature = null
  }

  function confirmClearQueue() {
    cancelClearQueue()
    sendUi("queue-clear")
  }

  onQueueRowsChanged: {
    if (clearArmedSignature !== null
        && !SoundCloudModel.clearConfirmStays(clearArmedSignature, queueRows, queueOpen)) cancelClearQueue()
  }
  onQueueOpenChanged: {
    if (!queueOpen && clearArmedSignature !== null) cancelClearQueue()
  }

  function moveQueueRow(playbackId, offset) {
    var command = SoundCloudModel.queueMoveCommand(playbackId, offset)
    if (command !== "") sendCommand(command, "queue-edit")
  }

  function toggle() {
    if (popupOpen) close()
    else open()
  }

  function plainForHost(value) {
    return String(value || "")
      .replace(/[<>&\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, "")
      .slice(0, 256)
  }

  function startBackend(showWindow) {
    if (launcherProcess.running) return
    backendWanted = true
    launchingBackend = true
    socketProbePending = true
    launcherProcess.command = ["/usr/bin/python3", "-I", helperPath, showWindow ? "launch" : "ensure"]
    launcherProcess.running = true
  }

  function refreshTracks() {
    if (!popupOpen || tracksRequestPending || !backendConnected || !running || !loggedIn) return
    tracksLoading = true
    trackLoadAttempts += 1
    tracksRequestPending = sendCommand("tracks:" + selectedTab, "tracks:" + selectedTab) > 0
  }

  // Likes and History are read from the SoundCloud account API by the backend,
  // not from the hidden page, so selecting them never navigates it.
  function isAccountTab(tab) { return tab === "likes" || tab === "history" }

  function tabLabel(tab) {
    return tab === "home" ? "Home" : (tab === "likes" ? "Likes" : (tab === "history" ? "History" : "Feed"))
  }

  function tracksFor(tab) {
    return tab === "home" ? homeTracks : (tab === "likes" ? likesTracks
      : (tab === "history" ? historyTracks : feedTracks))
  }

  function loadMorePendingFor(tab) {
    return tab === "home" ? homeLoadMorePending : (tab === "likes" ? likesLoadMorePending
      : (tab === "history" ? historyLoadMorePending : feedLoadMorePending))
  }

  function loadMoreAtCountFor(tab) {
    return tab === "home" ? homeLoadMoreAtCount : (tab === "likes" ? likesLoadMoreAtCount
      : (tab === "history" ? historyLoadMoreAtCount : feedLoadMoreAtCount))
  }

  function hasMoreFor(tab) {
    return tab === "home" ? homeHasMore : (tab === "likes" ? likesHasMore
      : (tab === "history" ? historyHasMore : feedHasMore))
  }

  function setLoadMoreAtCount(tab, count) {
    if (tab === "home") homeLoadMoreAtCount = count
    else if (tab === "feed") feedLoadMoreAtCount = count
    else if (tab === "likes") likesLoadMoreAtCount = count
    else if (tab === "history") historyLoadMoreAtCount = count
  }

  function setHasMore(tab, hasMore) {
    if (tab === "home") homeHasMore = hasMore
    else if (tab === "feed") feedHasMore = hasMore
    else if (tab === "likes") likesHasMore = hasMore
    else if (tab === "history") historyHasMore = hasMore
  }

  function selectTab(tab) {
    selectedTab = tab
    queueOpen = false
    menuRowKey = ""
    selectedRow = -1
    tracks = tracksFor(tab)
    tracksLoading = true
    trackLoadAttempts = 0
    if (!isAccountTab(tab)) runAction(tab)
    refreshTracks()
  }

  function loadMoreTracks(yPosition, heightRatio, atEnd, userInitiated) {
    var requestedCount = loadMoreAtCountFor(selectedTab)
    var hasMore = hasMoreFor(selectedTab)
    if (!backendConnected || !running || !loggedIn
        || !SoundCloudModel.shouldLoadMoreTracks({
          userInitiated: userInitiated,
          hasMore: hasMore,
          atEnd: atEnd,
          yPosition: yPosition,
          heightRatio: heightRatio,
          count: tracks.length,
          requestedCount: requestedCount,
          pending: loadMorePending
        })) return
    setLoadMorePending(selectedTab, true)
    setLoadMoreAtCount(selectedTab, tracks.length)
    if (sendCommand("load-more:" + selectedTab, "load-more:" + selectedTab) === 0) {
      setLoadMorePending(selectedTab, false)
    }
  }

  function setLoadMorePending(sourceTab, pending) {
    if (sourceTab === "home") homeLoadMorePending = pending
    else if (sourceTab === "feed") feedLoadMorePending = pending
    else if (sourceTab === "likes") likesLoadMorePending = pending
    else if (sourceTab === "history") historyLoadMorePending = pending
  }

  function applyTracks(result, sourceTab) {
    var cached = tracksFor(sourceTab)
    var applied = SoundCloudModel.applyTrackResult(cached, result)
    var previousContentY = selectedTab === sourceTab && result && result.reset !== true
      ? SoundCloudModel.contentYAfterTrackUpdate(
          trackList.contentY, cached.length, applied.tracks.length)
      : null
    if (!result || result.pending !== true) {
      if (sourceTab === "home") homeTracks = applied.tracks
      else if (sourceTab === "feed") feedTracks = applied.tracks
      else if (sourceTab === "likes") likesTracks = applied.tracks
      else if (sourceTab === "history") historyTracks = applied.tracks
    }
    if (selectedTab === sourceTab) {
      tracks = applied.tracks
      var resetRows = !!(result && result.reset === true)
      selectedRow = SoundCloudModel.selectionAfterUpdate(selectedRow, applied.tracks.length, resetRows)
    }
    if (previousContentY !== null) {
      Qt.callLater(function() {
        var maximum = Math.max(trackList.originY,
          trackList.originY + trackList.contentHeight - trackList.height)
        trackList.contentY = Math.max(trackList.originY,
          Math.min(previousContentY, maximum))
      })
    }
    if (result && result.hasMore === false) {
      setHasMore(sourceTab, false)
    } else if (result && result.reset === true) {
      setHasMore(sourceTab, true)
      setLoadMoreAtCount(sourceTab, 0)
    }
    if (result && result.error) lastError = String(result.error)
    if (selectedTab === sourceTab) tracksLoading = applied.loading
  }

  function requestTrackArtwork(artworkId) {
    artworkId = String(artworkId || "")
    if (!/^[0-9a-f]{24}$/.test(artworkId)
        || trackArtworkData[artworkId]
        || trackArtworkPending[artworkId]) return
    var pending = Object.assign({}, trackArtworkPending)
    pending[artworkId] = true
    trackArtworkPending = pending
    if (sendCommand("artwork:" + artworkId, "artwork:" + artworkId) === 0) {
      delete pending[artworkId]
      trackArtworkPending = Object.assign({}, pending)
    }
  }

  function toggleLike() {
    if (likeBusy || playbackId === "") return
    likeBusy = sendCommand((liked ? "unlike:" : "like:") + playbackId, "like") > 0
  }

  // Fire-and-forget commands (volume, queue). The answer only matters when it fails;
  // state comes back through the status.
  function sendUi(command) {
    return command !== "" && sendCommand(command, "ui") > 0
  }

  // Tab and Shift+Tab: the four tabs, then Queue as the fifth stop.
  function selectStop(stop) {
    if (stop === "queue") {
      if (!queueOpen) toggleQueue()
    } else {
      selectTab(stop)
    }
  }

  function toggleQueue() {
    queueOpen = !queueOpen
    menuRowKey = ""
    if (queueOpen) refreshQueue()
  }

  function refreshQueue() {
    if (!queueOpen || !backendConnected) return
    if (queueRequestPending) {
      queueStale = true
      return
    }
    queueStale = false
    queueRequestPending = sendCommand("queue", "queue") > 0
  }

  // One call per volume step (VolumeControl adds the wheel events up into
  // whole notches, so a swipe does not run it from 0 to 100).
  function stepVolumeOnce(direction) {
    var command = SoundCloudModel.volumeStepCommand(direction)
    if (command === "") return
    volume = SoundCloudModel.steppedVolume(volume, direction)
    if (muted && direction > 0) muted = false
    flashVolumeSlider()
    sendUi(command)
  }

  // Shows the slider for a second (a wheel change moved the level).
  function flashVolumeSlider() {
    volumeLinger = true
    volumeHideTimer.interval = SoundCloudModel.sliderHideDelay("flash")
    volumeHideTimer.restart()
  }

  // The slider is being dragged: show the level and the icon at once, and send
  // it. The backend unmutes for any level above 0 and mutes at 0.
  function setVolumeFromSlider(level) {
    var command = SoundCloudModel.volumeSetCommand(level)
    if (command === "") return
    volume = level
    muted = level <= 0
    sendUi(command)
  }

  function commandValue(action, value) {
    if (action === "launch") return "show"
    if (action === "seek") return "seek:" + String(value)
    if (action === "play") return "play:" + String(value)
    if (action === "play-from") return String(value)
    return action
  }

  function sendCommand(command, kind) {
    var socket = activeSocket
    if (!socket || !socket.connected) return 0
    var id = nextRequestId++
    pendingRequests[String(id)] = String(kind || "action")
    var payload = { id: id, command: command }
    socket.write(JSON.stringify(payload) + "\n")
    socket.flush()
    return id
  }

  function initializeConnection() {
    if (!backendConnected) {
      resetConnectionState()
      return
    }
    if (connectionInitialized) return
    connectionInitialized = true
    running = true
    launchingBackend = false
    socketProbePending = false
    actionBusy = false
    reconnectAttempt = 0
    sendCommand("subscribe", "subscribe")
    sendCommand("status", "status")
    if (popupOpen && running && loggedIn) {
      if (!isAccountTab(selectedTab)) sendCommand(selectedTab, "action")
      trackReloadTimer.restart()
    }
  }

  function resetConnectionState() {
    connectionInitialized = false
    socketBuffer = ""
    pendingRequests = ({})
    tracksRequestPending = false
    queueRequestPending = false
    homeLoadMorePending = false
    feedLoadMorePending = false
    likesLoadMorePending = false
    historyLoadMorePending = false
    trackArtworkPending = ({})
    tracksLoading = false
    trackLoadAttempts = 0
    actionBusy = false
    selectionBusy = false
    activeSelectionRequestId = 0
    likeBusy = false
  }

  function runAction(action, value) {
    if (!SoundCloudModel.canStartAction(actionBusy, selectionBusy, action)) return false
    if (!backendConnected) {
      actionBusy = true
      startBackend(action === "launch" || action === "show")
      return true
    }
    actionBusy = true
    selectionBusy = SoundCloudModel.isSelectionAction(action)
    lastError = ""
    var sentId = sendCommand(commandValue(action, value), selectionBusy ? "selection" : "action")
    if (sentId > 0) {
      if (selectionBusy) activeSelectionRequestId = sentId
      return true
    }
    actionBusy = false
    selectionBusy = false
    activeSelectionRequestId = 0
    return false
  }

  function formatTime(seconds) {
    var value = Math.max(0, Math.floor(Number(seconds) || 0))
    var hours = Math.floor(value / 3600)
    var minutes = Math.floor((value % 3600) / 60)
    var remainder = value % 60
    if (hours > 0) {
      return hours + ":" + (minutes < 10 ? "0" : "") + minutes
        + ":" + (remainder < 10 ? "0" : "") + remainder
    }
    return minutes + ":" + (remainder < 10 ? "0" : "") + remainder
  }

  function formatCompactCount(value) {
    value = Math.max(0, Math.floor(Number(value) || 0))
    if (value < 1000) return String(value)
    var units = [[1000000000, "B"], [1000000, "M"], [1000, "K"]]
    for (var index = 0; index < units.length; index++) {
      if (value >= units[index][0]) {
        var compact = (value / units[index][0]).toFixed(value >= units[index][0] * 10 ? 0 : 1)
        return compact.replace(/\.0$/, "") + units[index][1]
      }
    }
    return String(value)
  }

  function trackDetails(track) {
    var details = [String(track.artist || "SoundCloud")]
    var playCount = Math.max(0, Math.floor(Number(track.playCount) || 0))
    if (playCount > 0) details.push("▶ " + formatCompactCount(playCount))
    return details.join("  •  ")
  }

  function trackDuration(track) {
    var durationMs = Math.max(0, Math.floor(Number(track.durationMs) || 0))
    return durationMs > 0 ? formatTime(durationMs / 1000) : "—:—"
  }

  function applyStatus(state) {
    state = state || ({})
    running = state.running === true
    var wasLoggedIn = loggedIn
    var reportsLoggedOut = /\/(signin|register)([/?#]|$)/.test(String(state.url || ""))
    loggedIn = state.loggedIn === true || (root.loggedIn && !reportsLoggedOut)
    if (state.error) lastError = String(state.error)
    playbackState = String(state.playbackState || (state.playing === true ? "playing" : "idle"))
    if (selectionBusy && !SoundCloudModel.selectionIsPending(playbackState)) {
      selectionBusy = false
      activeSelectionRequestId = 0
      actionBusy = false
    }
    var preserveMetadata = SoundCloudModel.shouldPreservePlaybackMetadata(state, root.title)
    playing = state.playing === true
    canNext = state.canNext !== false
    canPrevious = state.canPrevious !== false
    if (!volumeDragging && !volumeSettling) {
      if (state.volume !== undefined) volume = Math.max(0, Math.min(1, Number(state.volume) || 0))
      muted = state.muted === true
    }
    shuffle = state.shuffle === true
    repeat = SoundCloudModel.repeatMode(state.repeat)
    queueLength = Math.max(0, Math.floor(Number(state.length) || 0))
    queueCursor = state.cursor === undefined ? -1 : Math.floor(Number(state.cursor))
    duration = Number(state.duration || 0)
    position = SoundCloudModel.positionFromStatus(position, state.position, Date.now(), optimisticPositionAt)
    if (!preserveMetadata) {
      var incomingTitle = String(state.title || "").slice(0, 512)
      var artwork = String(state.artDataUrl || "")
      if (state.playerPresent === false) {
        artDataUrl = ""
      } else if (/^data:image\/(png|jpeg);base64,/.test(artwork) && artwork.length <= 180000) {
        artDataUrl = artwork
      } else if (incomingTitle !== title) {
        artDataUrl = ""
      }
      title = incomingTitle
      playbackId = String(state.playbackId || "")
      liked = state.liked === true
      artist = String(state.artist || "").slice(0, 256)
      waveformLevels = Array.isArray(state.waveform) ? state.waveform : []
    }
    if (popupOpen && loggedIn && !wasLoggedIn && tracks.length === 0) {
      if (!isAccountTab(selectedTab)) sendCommand(selectedTab, "action")
      trackReloadTimer.restart()
    }
  }

  function handleLine(line) {
    if (String(line || "").length > maxSocketFrameChars) {
      lastError = "SoundCloud response exceeded the security limit"
      return
    }
    var message
    try {
      message = JSON.parse(String(line || ""))
    } catch (error) {
      lastError = "Could not read SoundCloud response"
      return
    }
    if (message.type === "status") {
      applyStatus(message.state)
      return
    }
    if (message.type === "tracks") {
      var messageSource = String(message.source || "")
      setLoadMorePending(messageSource, false)
      if (message.error && Number(message.addedCount || 0) === 0) {
        setLoadMoreAtCount(messageSource, 0)
      }
      applyTracks(message, messageSource)
      if (message.cached !== true) trackLoadAttempts = 50
      return
    }
    if (message.type !== "response") return
    var key = String(message.id || "")
    var kind = pendingRequests[key]
    delete pendingRequests[key]
    if (kind === "status") applyStatus(message)
    else if (kind && kind.indexOf("tracks:") === 0) {
      tracksRequestPending = false
      var sourceTab = kind.substring(7)
      applyTracks(message, sourceTab)
      if (popupOpen && selectedTab === sourceTab
          && message.pending === true && trackLoadAttempts < 50) {
        trackReloadTimer.restart()
      } else if (popupOpen && selectedTab !== sourceTab) {
        trackLoadAttempts = 0
        trackReloadTimer.restart()
      }
    } else if (kind && kind.indexOf("load-more:") === 0) {
      var requestedSource = kind.substring(10)
      if (message.started !== true) {
        setLoadMorePending(requestedSource, false)
        setLoadMoreAtCount(requestedSource, 0)
      }
      if (message.hasMore === false) setHasMore(requestedSource, false)
      if (message.ok !== true) setLoadMoreAtCount(requestedSource, 0)
    } else if (kind && kind.indexOf("artwork:") === 0) {
      var artworkId = kind.substring(8)
      var pendingArtwork = Object.assign({}, trackArtworkPending)
      delete pendingArtwork[artworkId]
      trackArtworkPending = pendingArtwork
      var trackArtDataUrl = String(message.artDataUrl || "")
      if (message.ok === true
          && /^data:image\/(png|jpeg);base64,/.test(trackArtDataUrl)
          && trackArtDataUrl.length <= 45000) {
        var artworkData = Object.assign({}, trackArtworkData)
        if (Object.keys(artworkData).length >= 64) artworkData = ({})
        artworkData[artworkId] = trackArtDataUrl
        trackArtworkData = artworkData
      }
    } else if (kind === "like") {
      likeBusy = false
      if (message.ok !== true) lastError = String(message.error || "SoundCloud could not update this like")
      else if (SoundCloudModel.isCurrentTrack(message.playbackId, root.playbackId)) liked = message.liked === true
    } else if (kind === "queue") {
      queueRequestPending = false
      if (message.ok !== false && message.queue) queueData = message.queue
      else if (message.ok === false) lastError = String(message.error || "SoundCloud could not read the queue")
      queueSelectedId = SoundCloudModel.queueSelectionAfterUpdate(queueRows, queueSelectedId)
      if (queueStale) refreshQueue()
    } else if (kind === "queue-edit") {
      // queue-move answers with the new queue view.
      if (message.ok !== false && message.queue) queueData = message.queue
      if (message.ok === false) lastError = String(message.error || "SoundCloud could not move this track")
      queueSelectedId = SoundCloudModel.queueSelectionAfterUpdate(queueRows, queueSelectedId)
      uiRefreshTimer.restart()
    } else if (kind === "ui") {
      if (message.ok !== true || message.error) lastError = String(message.error || "SoundCloud action failed")
      else uiRefreshTimer.restart()
    } else if (kind === "action") {
      actionBusy = false
      if (message.ok !== true) lastError = String(message.error || "SoundCloud action failed")
    } else if (kind === "selection") {
      if (!SoundCloudModel.isCurrentSelectionResponse(activeSelectionRequestId, message.id)) return
      if (message.ok !== true) {
        selectionBusy = false
        activeSelectionRequestId = 0
        actionBusy = false
        lastError = String(message.error || "SoundCloud could not select this track")
      }
    }
  }

  function handleSocketChunk(chunk) {
    socketBuffer += String(chunk || "")
    var newline
    while ((newline = socketBuffer.indexOf("\n")) >= 0) {
      var line = socketBuffer.substring(0, newline)
      socketBuffer = socketBuffer.substring(newline + 1)
      if (line.length > maxSocketFrameChars) {
        socketBuffer = ""
        lastError = "SoundCloud response exceeded the security limit"
        if (activeSocket) activeSocket.connected = false
        return
      }
      if (line !== "") handleLine(line)
    }
    if (socketBuffer.length > maxSocketFrameChars) {
      socketBuffer = ""
      lastError = "SoundCloud response exceeded the security limit"
      if (activeSocket) activeSocket.connected = false
    }
  }

  onBackendConnectedChanged: initializeConnection()
  onQueueCursorChanged: refreshQueue()
  onQueueLengthChanged: refreshQueue()

  visible: true
  implicitWidth: row.implicitWidth + Style.space(14)
  implicitHeight: barSize

  Row {
    id: row
    anchors.centerIn: parent
    spacing: Style.space(6)

    Text {
      anchors.verticalCenter: parent.verticalCenter
      textFormat: Text.PlainText
      text: ""
      color: SoundCloudModel.barIconColor(root.playing, Color.accent, root.bar.barForeground)
      font.family: root.bar.fontFamily
      font.pixelSize: Style.font.body
    }

  }

  MouseArea {
    anchors.fill: parent
    hoverEnabled: true
    cursorShape: Qt.PointingHandCursor
    acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton

    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) root.runAction(root.running ? "play-pause" : "launch")
      else if (mouse.button === Qt.RightButton && root.running && root.canNext) root.runAction("next")
      else root.toggle()
    }
    onWheel: function(wheel) {
      if (!root.running) return
      root.runAction(wheel.angleDelta.y > 0 ? "previous" : "next")
    }
    onEntered: root.bar.showTooltip(root, root.safeTooltipLabel)
    onExited: root.bar.hideTooltip(root)
  }

  KeyboardPanel {
    id: popup
    anchorItem: root
    bar: root.bar
    owner: root
    open: root.popupOpen
    focusTarget: keyCatcher
    contentWidth: popup.fittedContentWidth(Style.space(456))
    contentHeight: popup.fittedContentHeight(Style.space(480))

    // KeyboardPanel catches clicks on the whole screen and hands a bar click on
    // to the bar's click targets, but a third-party widget only sees its own
    // targets, so a click on another bar icon just closed this popup. Leaving
    // the bar strip out of the input region lets that click reach the icon
    // itself, whose panel then takes over (Bar.requestPopout closes this one).
    mask: Region {
      width: popup.screenW
      height: popup.screenH

      Region {
        intersection: Intersection.Subtract
        x: popup.barPos === "right" ? popup.screenW - popup.barW : 0
        y: popup.barPos === "bottom" ? popup.screenH - popup.barH : 0
        width: popup.barPos === "top" || popup.barPos === "bottom" ? popup.screenW : popup.barW
        height: popup.barPos === "top" || popup.barPos === "bottom" ? popup.barH : popup.screenH
      }
    }

    // Keys: Esc closes (a pending Clear or an open row menu first), Up/Down (or j/k)
    // move the selection (in the list or the queue), Enter plays it, Tab/Shift+Tab
    // switch tabs and Queue, Space plays or pauses, Left/Right seek (5 s, longer
    // while held), q the queue, m mutes, Alt+Up/Down move the selected queue row.
    // AppKeys adds PageDown/Shift+Right next,
    // PageUp/Shift+Left previous, Shift+Up/Down volume, l like, Shift+L repeat
    // and Shift+S shuffle. j/k stay list keys (Omarchy), so unlike the website
    // they do not skip tracks. The content sits inside the key
    // catcher, so the catcher sees the keys first.
    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Forwarded keys reach seekKeys first, which counts the repeats.
      Keys.forwardTo: [appKeys, seekKeys]
      onCloseRequested: {
        if (root.clearArmedSignature !== null) root.cancelClearQueue()
        else if (root.menuRowKey !== "") root.menuRowKey = ""
        else root.close()
      }
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveSelectedRow(dy)
        else if (dx !== 0) root.seekKey(dx)
      }
      onReturnRequested: root.returnHeld = true
      onActivateRequested: {
        var fromReturn = root.returnHeld
        root.returnHeld = false
        if (fromReturn) root.playSelectedRow()
        else if (root.running && root.loggedIn && root.hasTrack) root.runAction("play-pause")
      }
      onTabRequested: function(direction) {
        if (root.running && root.loggedIn) root.selectStop(SoundCloudModel.cycleTab(
          SoundCloudModel.currentStop(root.selectedTab, root.queueOpen), direction))
      }
      onTextKey: function(text) {
        if (text === "q" || text === "Q") {
          if (root.running && root.loggedIn) root.toggleQueue()
        } else if (text === "m" || text === "M") {
          root.sendUi("mute")
        }
      }

    AppKeys {
      id: appKeys
      onTriggered: function(action) { root.appKey(action) }
    }

    SeekKeys { id: seekKeys }

    Column {
      id: content
      anchors.fill: parent
      spacing: Style.space(12)

      Row {
        width: parent.width
        spacing: Style.space(10)

        BorderSurface {
          width: Style.space(68)
          height: Style.space(68)
          radius: Style.spacing.labelGap
          color: Style.normalFillFor(root.bar.foreground, Color.accent)
          borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

          Image {
            anchors.fill: parent
            anchors.margins: Style.space(2)
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            sourceSize.width: 256
            sourceSize.height: 256
            source: root.artDataUrl
            visible: source !== ""
          }

          Text {
            anchors.centerIn: parent
            visible: root.artDataUrl === ""
            textFormat: Text.PlainText
            text: ""
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.displayLarge
          }
        }

        Column {
          width: parent.width - Style.space(78)
            - (likeButton.visible ? likeButton.width + parent.spacing : 0)
          anchors.verticalCenter: parent.verticalCenter
          spacing: Style.space(3)

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: root.title || (root.running && root.loggedIn ? "Nothing playing" : "SoundCloud")
            color: root.bar.foreground
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }

          Text {
            width: parent.width
            textFormat: Text.PlainText
            text: SoundCloudModel.pendingLabel(root.playbackState) || root.artist || (!root.running ? (root.launchingBackend ? "Connecting…" : "Not connected") : (!root.loggedIn ? "Sign in once to continue" : "Nothing playing"))
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
            elide: Text.ElideRight
          }
        }

        Text {
          id: likeButton
          anchors.verticalCenter: parent.verticalCenter
          visible: root.running && root.loggedIn && root.playbackId !== ""
          textFormat: Text.PlainText
          text: "󰋑"
          color: root.liked ? Color.accent : root.bar.foreground
          opacity: root.likeBusy ? 0.5 : 1
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.icon

          MouseArea {
            anchors.fill: parent
            anchors.margins: -Style.space(4)
            cursorShape: Qt.PointingHandCursor
            enabled: !root.likeBusy
            onClicked: root.toggleLike()
          }
        }
      }

      // Transport stays centred; the volume icon sits on the left edge.
      Item {
        width: parent.width
        height: transportRow.implicitHeight
        visible: root.running && root.loggedIn
        opacity: root.hasTrack ? 1 : 0.45

        Row {
          id: transportRow
          anchors.horizontalCenter: parent.horizontalCenter
          spacing: Style.space(7)

          Button {
            iconText: "󰒮"
            foreground: root.bar.foreground
            enabled: SoundCloudModel.queueButtonEnabled(root.actionBusy, root.hasTrack, root.canPrevious)
            opacity: enabled ? 1 : 0.4
            onClicked: root.runAction("previous")
          }
          Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: Style.space(40)
            height: width
            radius: width / 2
            color: playPauseMouse.containsMouse ? Qt.lighter(Color.accent, 1.15) : Color.accent
            enabled: !root.actionBusy && root.hasTrack
            opacity: enabled ? 1 : 0.4

            Text {
              anchors.centerIn: parent
              textFormat: Text.PlainText
              text: root.playIcon
              color: root.bar.background
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.iconLarge
            }

            MouseArea {
              id: playPauseMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.runAction("play-pause")
            }
          }
          Button {
            iconText: "󰒭"
            foreground: root.bar.foreground
            enabled: SoundCloudModel.queueButtonEnabled(root.actionBusy, root.hasTrack, root.canNext)
            opacity: enabled ? 1 : 0.4
            onClicked: root.runAction("next")
          }
        }

        // Click mutes; the wheel changes the level by 5 points; hovering opens the slider.
        Button {
          id: volumeButton
          anchors.left: parent.left
          anchors.leftMargin: Style.space(6)
          anchors.verticalCenter: parent.verticalCenter
          iconText: SoundCloudModel.volumeIconGlyph(SoundCloudModel.volumeIconState(root.volume, root.muted))
          foreground: root.bar.foreground
          // The tooltip would cover the slider.
          tooltipText: root.volumeShown ? "" : SoundCloudModel.volumeTooltip(root.volume, root.muted)
          onClicked: root.sendUi("mute")

          HoverHandler { id: volumeIconHover }

          VolumeControl {
            anchors.fill: parent
            onStepped: function(direction) { root.stepVolumeOnce(direction) }
          }
        }

        // The slider sits in the free space between the icon and the transport
        // group. The wrapper also covers the gap next to the icon, so moving
        // from the icon to the bar never loses the hover.
        Item {
          id: volumeSliderWrap
          readonly property real gap: Style.space(4)
          readonly property real barWidth: SoundCloudModel.sliderWidth(
            transportRow.x - (volumeButton.x + volumeButton.width) - gap - Style.space(12))
          x: volumeButton.x + volumeButton.width
          width: gap + barWidth
          height: parent.height
          visible: barWidth > 0 && opacity > 0
          opacity: root.volumeShown ? 1 : 0

          Behavior on opacity {
            NumberAnimation { duration: 110; easing.type: Easing.OutCubic }
          }

          HoverHandler { id: volumeSliderHover; enabled: volumeSliderWrap.visible }

          VolumeSlider {
            id: volumeSlider
            x: volumeSliderWrap.gap
            width: volumeSliderWrap.barWidth
            anchors.verticalCenter: parent.verticalCenter
            value: SoundCloudModel.sliderDisplayValue(root.volume, root.muted)
            trackColor: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.28)
            fillColor: Color.accent
            knobColor: root.bar.foreground
            onMoved: function(level) { root.setVolumeFromSlider(level) }
            onCommitted: {
              root.volumeSettling = true
              volumeSettleTimer.restart()
              root.volumeLinger = true
              volumeHideTimer.interval = SoundCloudModel.sliderHideDelay("leave")
              volumeHideTimer.restart()
            }
          }
        }
      }

      PanelSeparator {
        visible: root.running && root.loggedIn
        foreground: root.bar.foreground
      }

      Column {
        width: parent.width
        spacing: Style.space(3)
        visible: root.running && root.loggedIn
        opacity: root.hasTrack ? 1 : 0.45

        Canvas {
          id: waveform
          width: parent.width
          height: Style.space(42)

          property real playedRatio: root.duration > 0
            ? Math.max(0, Math.min(1, root.position / root.duration))
            : 0

          property var levels: root.waveformLevels
          property color playedColor: Color.accent

          onPlayedRatioChanged: requestPaint()
          onLevelsChanged: requestPaint()
          onPlayedColorChanged: requestPaint()
          onWidthChanged: requestPaint()
          onHeightChanged: requestPaint()

          Connections {
            target: root.bar
            function onForegroundChanged() { waveform.requestPaint() }
          }

          onPaint: {
            var context = getContext("2d")
            context.reset()
            var dimColor = Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.28)
            if (!levels || levels.length === 0) {
              // No real waveform for this track: plain progress bar.
              var lineHeight = 4
              var lineY = (height - lineHeight) / 2
              context.fillStyle = dimColor
              context.fillRect(0, lineY, width, lineHeight)
              context.fillStyle = playedColor
              context.fillRect(0, lineY, width * playedRatio, lineHeight)
              return
            }
            var count = levels.length
            var gap = 2
            var barWidth = Math.max(1, (width - (count - 1) * gap) / count)
            for (var index = 0; index < count; index++) {
              var barHeight = Math.max(3, height * Number(levels[index]) / 100)
              var x = index * (barWidth + gap)
              var y = (height - barHeight) / 2
              context.fillStyle = index / count <= playedRatio
                ? playedColor
                : dimColor
              context.fillRect(x, y, barWidth, barHeight)
            }
          }

          MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            enabled: root.hasTrack && root.duration > 0 && !root.actionBusy
            onClicked: function(mouse) {
              var ratio = Math.max(0, Math.min(1, mouse.x / width))
              root.position = root.duration * ratio
              root.optimisticPositionAt = Date.now()
              waveform.requestPaint()
              root.runAction("seek", ratio)
            }
          }
        }

        Row {
          width: parent.width

          Text {
            textFormat: Text.PlainText
            text: root.formatTime(root.position)
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }

          Item { width: parent.width - parent.children[0].implicitWidth - parent.children[2].implicitWidth; height: 1 }

          Text {
            textFormat: Text.PlainText
            text: root.formatTime(root.duration)
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }

      Item {
        width: parent.width
        height: tabRow.implicitHeight
        visible: root.running && root.loggedIn

        Row {
          id: tabRow
          spacing: Style.space(8)

          Button {
            id: homeTab
            text: "Home"
            foreground: root.bar.foreground
            enabled: !root.actionBusy
            selected: !root.queueOpen && root.selectedTab === "home"
            onClicked: root.selectTab("home")
          }

          Button {
            text: "Feed"
            foreground: root.bar.foreground
            enabled: !root.actionBusy
            selected: !root.queueOpen && root.selectedTab === "feed"
            onClicked: root.selectTab("feed")
          }

          Button {
            text: "Likes"
            foreground: root.bar.foreground
            enabled: !root.actionBusy
            selected: !root.queueOpen && root.selectedTab === "likes"
            onClicked: root.selectTab("likes")
          }

          Button {
            text: "History"
            foreground: root.bar.foreground
            enabled: !root.actionBusy
            selected: !root.queueOpen && root.selectedTab === "history"
            onClicked: root.selectTab("history")
          }
        }

        // Queue is a tab-like stop behind a divider: while its view is open it is the
        // only selected item. The badge shows how many tracks are still to come. The
        // divider has the same gap on both sides (the tab spacing) and is shorter
        // than a tab.
        Row {
          anchors.left: tabRow.right
          anchors.leftMargin: tabRow.spacing
          anchors.verticalCenter: parent.verticalCenter
          spacing: tabRow.spacing

          Rectangle {
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(1, Style.space(1))
            height: Math.round(homeTab.implicitHeight * 0.5)
            color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.28)
          }

          Button {
            id: queueButton
            // Glyphs are drawn centred on the whole line box, a pixel above the label's
            // x-height centre: nudge them down to line up with the text.
            readonly property real iconNudge: Math.max(1, Math.round(Style.space(1)))
            foreground: root.bar.foreground
            selected: root.queueOpen
            // Same height and padding as a text tab (its own row is empty, the
            // content below is drawn over it); the width only fits the content.
            implicitHeight: homeTab.implicitHeight
            implicitWidth: queueContent.implicitWidth + horizontalPadding * 2
              + _reservedBorderLeft + _reservedBorderRight
            onClicked: root.toggleQueue()

            Row {
              id: queueContent
              anchors.centerIn: parent
              spacing: Style.spacing.controlGap

              Text {
                anchors.verticalCenter: parent.verticalCenter
                anchors.verticalCenterOffset: queueButton.iconNudge
                textFormat: Text.PlainText
                text: "󰐑"
                color: queueButton.selected ? queueButton._selectedColor : root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.icon
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: "Queue"
                color: queueButton.selected ? queueButton._selectedColor : root.bar.foreground
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                font.bold: queueButton.selected
              }

              Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                anchors.verticalCenterOffset: queueButton.iconNudge
                visible: queueBadge.text !== ""
                width: Math.max(height, queueBadge.implicitWidth + Style.space(8))
                height: queueBadge.implicitHeight + Style.space(2)
                radius: height / 2
                color: Color.accent

                Text {
                  id: queueBadge
                  anchors.centerIn: parent
                  textFormat: Text.PlainText
                  text: SoundCloudModel.queueBadgeText(SoundCloudModel.upcomingCount(root.queueLength, root.queueCursor))
                  color: root.bar.background
                  font.family: root.bar.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }
        }

        // Opens SoundCloud's Library in the browser, so it sits apart from the in-popup
        // tabs. Only the arrow, to leave room for the Queue button.
        Button {
          anchors.right: parent.right
          anchors.rightMargin: Style.space(6)
          anchors.verticalCenter: parent.verticalCenter
          text: "↗"
          foreground: root.bar.foreground
          tooltipText: "Open your Library on SoundCloud"
          onClicked: {
            Quickshell.execDetached(["omarchy", "launch", "browser", "https://soundcloud.com/you/library"])
            root.close()
          }
        }
      }

      Item {
        width: parent.width
        height: Math.max(0, parent.height - y - (errorText.visible
          ? errorText.implicitHeight + parent.spacing : 0))
        visible: root.running && root.loggedIn && !root.queueOpen

        ListView {
          id: trackList
          anchors.fill: parent
          anchors.rightMargin: Style.space(6)
          clip: true
          spacing: Style.space(3)
          boundsBehavior: Flickable.StopAtBounds
          model: root.tracks
          onMovementEnded: root.loadMoreTracks(
            visibleArea.yPosition, visibleArea.heightRatio, atYEnd, true)

          footer: Item {
            width: trackList.width
            height: root.loadMorePending ? Style.space(28) : 0

            Text {
              anchors.centerIn: parent
              visible: root.loadMorePending
              textFormat: Text.PlainText
              text: "Loading more…"
              color: root.dim
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          delegate: Item {
            required property var modelData
            required property int index
            width: trackList.width
            height: Style.space(56)
            opacity: encrypted ? 0.5 : 1
            property var track: modelData
            property string artworkId: String(track.artworkId || "")
            property string trackArtDataUrl: String(root.trackArtworkData[artworkId] || "")
            property bool isCurrent: SoundCloudModel.isCurrentTrack(track.playbackId, root.playbackId)
            property string pendingLabel: isCurrent ? SoundCloudModel.pendingLabel(root.playbackState) : ""
            property bool failed: isCurrent && root.playbackState === "error"
            property bool encrypted: SoundCloudModel.isEncrypted(track)
            property string rowKey: root.selectedTab + ":" + String(track.playbackId || "")
            property bool menuOpen: root.menuRowKey === rowKey
            readonly property bool rowHovered: rowHover.hovered

            Component.onCompleted: root.requestTrackArtwork(artworkId)
            onArtworkIdChanged: root.requestTrackArtwork(artworkId)
            onRowHoveredChanged: if (!rowHovered && menuOpen) root.menuRowKey = ""

            HoverHandler { id: rowHover }

            Rectangle {
              anchors.fill: parent
              radius: Style.spacing.labelGap
              color: Style.hoverFillFor(root.bar.foreground, Color.accent)
              visible: trackMouse.enabled && trackMouse.containsMouse
            }

            // Keyboard selection.
            Rectangle {
              anchors.fill: parent
              radius: Style.spacing.labelGap
              color: Style.hoverFillFor(root.bar.foreground, Color.accent)
              border.width: 1
              border.color: Color.accent
              visible: index === root.selectedRow
            }

            Rectangle {
              anchors.right: trackArtwork.left
              anchors.rightMargin: Style.space(3)
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(3)
              height: Style.space(40)
              radius: width / 2
              color: Color.accent
              visible: isCurrent
            }

            BorderSurface {
              id: trackArtwork
              width: Style.space(48)
              height: Style.space(48)
              anchors.left: parent.left
              anchors.leftMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              radius: Style.spacing.labelGap
              color: Style.normalFillFor(root.bar.foreground, Color.accent)
              borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

              Image {
                anchors.fill: parent
                anchors.margins: Style.space(2)
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                sourceSize.width: 96
                sourceSize.height: 96
                source: trackArtDataUrl
                visible: trackArtDataUrl !== ""
              }

              Text {
                anchors.centerIn: parent
                visible: trackArtDataUrl === ""
                textFormat: Text.PlainText
                text: ""
                color: root.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.icon
              }
            }

            Column {
              anchors.left: trackArtwork.right
              anchors.leftMargin: Style.space(9)
              anchors.right: trackDurationText.left
              anchors.rightMargin: Style.space(9)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: track.title || "Untitled"
                textFormat: Text.PlainText
                color: isCurrent ? Color.accent : root.bar.foreground
                font.bold: isCurrent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: pendingLabel || (failed ? root.lastError || "Could not play this track" : root.trackDetails(track))
                textFormat: Text.PlainText
                color: failed ? root.bar.urgent : (pendingLabel ? Color.accent : root.dim)
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            Text {
              id: trackDurationText
              anchors.right: parent.right
              anchors.rightMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              text: encrypted ? "󰌾" : root.trackDuration(track)
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.bar.fontFamily
              font.pixelSize: Style.font.caption
              visible: !(rowHovered && !encrypted)
            }

            MouseArea {
              id: trackMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              enabled: !encrypted && (!root.actionBusy || root.selectionBusy)
                && /^soundcloud:tracks:[1-9][0-9]*$/.test(String(track.playbackId || ""))
              onClicked: {
                root.runAction("play-from", SoundCloudModel.playFromCommand(root.selectedTab, modelData.playbackId))
              }
            }

            // "..." appears on hover and opens the row's action menu.
            Button {
              id: rowMenuButton
              visible: rowHovered && !encrypted && !menuOpen
              anchors.right: parent.right
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              iconText: "󰇘"
              foreground: root.bar.foreground
              tooltipText: "More actions"
              onClicked: root.menuRowKey = rowKey
            }

            Rectangle {
              visible: menuOpen
              anchors.right: parent.right
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              width: menuButtons.implicitWidth + Style.space(8)
              height: menuButtons.implicitHeight + Style.space(6)
              radius: Style.spacing.labelGap
              color: root.bar.background
              border.width: 1
              border.color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.28)

              // Keeps clicks between the buttons from playing the row.
              MouseArea { anchors.fill: parent }

              Row {
                id: menuButtons
                anchors.centerIn: parent
                spacing: Style.space(4)

                Button {
                  text: "Play"
                  foreground: root.bar.foreground
                  enabled: trackMouse.enabled
                  onClicked: {
                    root.menuRowKey = ""
                    root.runAction("play-from", SoundCloudModel.playFromCommand(root.selectedTab, track.playbackId))
                  }
                }
                Button {
                  text: "Play next"
                  foreground: root.bar.foreground
                  onClicked: {
                    root.menuRowKey = ""
                    root.sendUi(SoundCloudModel.queueNextCommand(track.playbackId))
                  }
                }
                Button {
                  text: "Add to queue"
                  foreground: root.bar.foreground
                  onClicked: {
                    root.menuRowKey = ""
                    root.sendUi(SoundCloudModel.queueAddCommand(track.playbackId))
                  }
                }
              }
            }
          }
        }

        Rectangle {
          anchors.right: parent.right
          width: Style.space(2)
          y: trackList.visibleArea.yPosition * parent.height
          height: Math.max(Style.space(18), trackList.visibleArea.heightRatio * parent.height)
          radius: width / 2
          color: Qt.rgba(root.bar.foreground.r, root.bar.foreground.g, root.bar.foreground.b, 0.35)
          visible: trackList.visibleArea.heightRatio < 1
        }

        Text {
          anchors.centerIn: parent
          visible: root.tracksLoading && root.tracks.length === 0
          textFormat: Text.PlainText
          text: "Loading " + root.tabLabel(root.selectedTab) + "…"
          color: root.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }

        Column {
          anchors.centerIn: parent
          visible: !root.tracksLoading && root.tracks.length === 0
          spacing: Style.space(8)

          Text {
            anchors.horizontalCenter: parent.horizontalCenter
            textFormat: Text.PlainText
            text: "No tracks found"
            color: root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.body
          }

          Button {
            anchors.horizontalCenter: parent.horizontalCenter
            text: "Retry"
            foreground: root.bar.foreground
            enabled: !root.actionBusy
            onClicked: root.selectTab(root.selectedTab)
          }
        }
      }

      // The queue replaces the track list; the tabs above stay in place.
      Item {
        id: queueView
        width: parent.width
        height: Math.max(0, parent.height - y - (errorText.visible
          ? errorText.implicitHeight + parent.spacing : 0))
        visible: root.running && root.loggedIn && root.queueOpen

        // One slim row: how many tracks follow, then the queue switches as icon buttons.
        Item {
          id: queueHeader
          width: parent.width
          height: queueHeaderButtons.implicitHeight

          Text {
            anchors.left: parent.left
            anchors.leftMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            textFormat: Text.PlainText
            text: root.clearArmedSignature !== null ? "Clear queue?" : SoundCloudModel.queueCountText(root.queueRows)
            color: root.clearArmedSignature !== null ? root.bar.foreground : root.dim
            font.family: root.bar.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Row {
            id: queueHeaderButtons
            visible: root.clearArmedSignature === null
            anchors.right: parent.right
            anchors.rightMargin: Style.space(6)
            spacing: Style.space(2)

            Button {
              iconText: "󰒟"
              foreground: root.bar.foreground
              selected: root.shuffle
              tooltipText: root.shuffle ? "Shuffle: on" : "Shuffle: off"
              onClicked: root.sendUi("shuffle")
            }
            Button {
              iconText: SoundCloudModel.repeatIconGlyph(root.repeat)
              foreground: root.bar.foreground
              selected: root.repeat !== "none"
              tooltipText: root.repeat === "one" ? "Repeat: this track"
                : (root.repeat === "all" ? "Repeat: all" : "Repeat: off")
              onClicked: root.sendUi("repeat")
            }
            Button {
              iconText: "󰆴"
              foreground: root.bar.foreground
              enabled: root.queueLength > 0
              opacity: enabled ? 1 : 0.4
              tooltipText: "Clear the queue"
              onClicked: root.armClearQueue()
            }
          }

          // After Clear: confirm or cancel (Esc, a queue change or leaving the view also cancel).
          Row {
            visible: root.clearArmedSignature !== null
            anchors.right: parent.right
            anchors.rightMargin: Style.space(6)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Button {
              iconText: "󰄬"
              foreground: Color.accent
              tooltipText: "Yes, clear the queue"
              onClicked: root.confirmClearQueue()
            }
            Button {
              iconText: "󰅖"
              foreground: root.bar.foreground
              tooltipText: "Keep the queue"
              onClicked: root.cancelClearQueue()
            }
          }
        }

        ListView {
          id: queueList
          anchors.top: queueHeader.bottom
          anchors.topMargin: Style.space(4)
          anchors.bottom: parent.bottom
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.rightMargin: Style.space(6)
          clip: true
          spacing: Style.space(3)
          boundsBehavior: Flickable.StopAtBounds
          model: root.queueRows

          delegate: Item {
            required property var modelData
            width: queueList.width
            height: Style.space(40)
            readonly property real rowStep: height + queueList.spacing
            property var item: modelData
            property bool isCurrent: item.current === true
            property string artworkId: String(item.artworkId || "")
            property string itemArtDataUrl: String(root.trackArtworkData[artworkId] || "")
            // Drag handle: how far the row has been dragged (pixels), 0 at rest.
            property real dragDy: 0
            property bool dragging: false
            z: dragging ? 5 : 0
            transform: Translate { y: dragDy }

            Component.onCompleted: root.requestTrackArtwork(artworkId)
            onArtworkIdChanged: root.requestTrackArtwork(artworkId)

            Rectangle {
              anchors.fill: parent
              radius: Style.spacing.labelGap
              color: Style.hoverFillFor(root.bar.foreground, Color.accent)
              visible: queueRowMouse.enabled && queueRowMouse.containsMouse
            }

            Rectangle {
              anchors.right: queueArtwork.left
              anchors.rightMargin: Style.space(3)
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(3)
              height: Style.space(30)
              radius: width / 2
              color: Color.accent
              visible: isCurrent
            }

            // Keyboard selection.
            Rectangle {
              anchors.fill: parent
              radius: Style.spacing.labelGap
              color: Style.hoverFillFor(root.bar.foreground, Color.accent)
              border.width: 1
              border.color: Color.accent
              visible: String(item.playbackId || "") === root.queueSelectedId && root.queueSelectedId !== ""
            }

            // Clicking a row plays it (the playing row has nothing to play).
            MouseArea {
              id: queueRowMouse
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              enabled: !isCurrent
              onClicked: root.sendUi(SoundCloudModel.queuePlayCommand(item.playbackId))
            }

            BorderSurface {
              id: queueArtwork
              width: Style.space(36)
              height: Style.space(36)
              anchors.left: parent.left
              anchors.leftMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              radius: Style.spacing.labelGap
              color: Style.normalFillFor(root.bar.foreground, Color.accent)
              borderSpec: Border.controlSpec("normal", root.bar.foreground, Color.accent)

              Image {
                anchors.fill: parent
                anchors.margins: Style.space(2)
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
                sourceSize.width: 80
                sourceSize.height: 80
                source: itemArtDataUrl
                visible: itemArtDataUrl !== ""
              }

              Text {
                anchors.centerIn: parent
                visible: itemArtDataUrl === ""
                textFormat: Text.PlainText
                text: "󰐑"
                color: root.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.icon
              }
            }

            Column {
              anchors.left: queueArtwork.right
              anchors.leftMargin: Style.space(9)
              anchors.right: queueHandle.left
              anchors.rightMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                width: parent.width
                text: item.title || "Untitled"
                textFormat: Text.PlainText
                color: isCurrent ? Color.accent : root.bar.foreground
                font.bold: isCurrent
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.body
                elide: Text.ElideRight
              }

              Text {
                width: parent.width
                text: SoundCloudModel.queueRowLabel(item) || String(item.artist || "")
                textFormat: Text.PlainText
                color: root.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            // Drag to reorder: the row follows the pointer and, on release, moves by
            // the whole rows it was dragged over (queue-move). The playing row has
            // no handle, and nothing can go above it.
            Item {
              id: queueHandle
              anchors.right: queueRemove.left
              anchors.rightMargin: Style.space(2)
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(22)
              height: Style.space(32)
              visible: !isCurrent

              Text {
                anchors.centerIn: parent
                textFormat: Text.PlainText
                text: "󰍜"
                color: handleMouse.pressed || handleMouse.containsMouse ? root.bar.foreground : root.dim
                font.family: root.bar.fontFamily
                font.pixelSize: Style.font.icon
              }

              MouseArea {
                id: handleMouse
                property real startY: 0
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                function listY(mouse) { return mapToItem(queueList.contentItem, mouse.x, mouse.y).y }
                onPressed: function(mouse) { startY = listY(mouse); dragging = true }
                onPositionChanged: function(mouse) {
                  if (pressed) dragDy = listY(mouse) - startY
                }
                onReleased: function(mouse) {
                  var at = SoundCloudModel.queueIndexOf(root.queueRows, item.playbackId)
                  root.moveQueueRow(item.playbackId, SoundCloudModel.queueDragOffset(
                    dragDy, rowStep, at, root.queueRows.length))
                  dragging = false
                  dragDy = 0
                }
                onCanceled: { dragging = false; dragDy = 0 }
              }
            }

            // The playing track stays; the rest can be taken out of the queue.
            Button {
              id: queueRemove
              anchors.right: parent.right
              anchors.rightMargin: Style.space(4)
              anchors.verticalCenter: parent.verticalCenter
              visible: !isCurrent
              iconText: "󰅖"
              foreground: root.bar.foreground
              tooltipText: "Remove from queue"
              onClicked: root.sendUi(SoundCloudModel.queueRemoveCommand(item.playbackId))
            }
          }
        }

        Text {
          anchors.centerIn: queueList
          visible: queueList.count === 0
          textFormat: Text.PlainText
          text: "The queue is empty"
          color: root.dim
          font.family: root.bar.fontFamily
          font.pixelSize: Style.font.body
        }
      }

      Text {
        width: parent.width
        visible: root.running && !root.loggedIn
        textFormat: Text.PlainText
        text: "A SoundCloud window opens. Sign in there once, then close it. Playback keeps running and you stay signed in."
        color: root.dim
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignHCenter
        wrapMode: Text.WordWrap
      }

      Button {
        anchors.horizontalCenter: parent.horizontalCenter
        visible: !root.running || !root.loggedIn
        text: root.running ? "Show SoundCloud sign in" : "Connect"
        foreground: root.bar.foreground
        enabled: !root.actionBusy
        onClicked: root.runAction(root.running ? "show" : "ensure")
      }

      Text {
        id: errorText
        width: parent.width
        visible: root.lastError !== ""
          && !SoundCloudModel.errorShownOnRow(root.tracks, root.playbackId, root.playbackState)
        textFormat: Text.PlainText
        text: root.lastError
        color: root.bar.urgent
        font.family: root.bar.fontFamily
        font.pixelSize: Style.font.caption
        wrapMode: Text.WordWrap
      }

    }

    }
  }

  Timer {
    id: volumeHideTimer
    interval: 1000
    onTriggered: root.volumeLinger = false
  }

  // A status that was already on its way when the drag ended is older than the
  // level just set; ignore it for a moment.
  Timer {
    id: volumeSettleTimer
    interval: 500
    onTriggered: root.volumeSettling = false
  }

  Timer {
    id: trackReloadTimer
    interval: 100
    repeat: false
    onTriggered: root.refreshTracks()
  }

  // After a volume or queue command, read back the state it changed.
  Timer {
    id: uiRefreshTimer
    interval: 150
    onTriggered: {
      root.sendCommand("status", "status")
      root.refreshQueue()
    }
  }

  Timer {
    interval: 30000
    repeat: true
    running: root.backendConnected
    onTriggered: root.sendCommand("status", "status")
  }

  Component {
    id: socketComponent

    Socket {
      path: root.socketPath
      connected: root.backendWanted && root.socketPath !== ""
      parser: SplitParser {
        splitMarker: ""
        onRead: function(chunk) { root.handleSocketChunk(chunk) }
      }
      onConnectionStateChanged: {
        if (connected) root.initializeConnection()
        else {
          root.resetConnectionState()
          // Drop the last track so a dead backend never looks like it is playing.
          root.applyStatus({ running: false, playerPresent: false })
        }
      }
    }
  }

  Loader {
    id: socketLoader
    active: false
    sourceComponent: socketComponent
  }

  property int reconnectAttempt: 0

  Timer {
    id: reconnectTimer
    interval: Math.min(1500, 100 + root.reconnectAttempt * 100)
    repeat: root.launchingBackend
    triggeredOnStart: true
    running: root.backendWanted && !root.backendConnected
      && (root.socketProbePending || root.launchingBackend)
    onTriggered: {
      root.socketProbePending = false
      root.reconnectAttempt = Math.min(14, root.reconnectAttempt + 1)
      socketLoader.active = false
      socketLoader.active = true
    }
  }

  Process {
    id: launcherProcess
    running: false
    command: []
    stdout: SplitParser {
      onRead: function(line) {
        var error = SoundCloudModel.launchError(line)
        if (error) root.lastError = error
      }
    }
    onStarted: root.lastError = ""
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.actionBusy = false
        root.launchingBackend = false
        if (!root.lastError) root.lastError = "Could not start SoundCloud"
      } else {
        root.socketProbePending = true
      }
      reconnectTimer.restart()
    }
  }
}
