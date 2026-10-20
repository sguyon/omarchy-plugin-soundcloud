import QtQuick
import "SoundCloudModel.js" as SoundCloudModel

// The message line at the bottom of the popup. One policy for every message
// (see SoundCloudModel.toastDelay): an info message stays 4 s, an error 8 s; a
// new message replaces the old one and restarts the time, the same text pushed
// again by a status update does not; the time is paused while the pointer is
// over the line and 2 s are left once it leaves; a click dismisses it; it fades
// out over 150 ms. No Quickshell imports so it can be tested with Qt Quick Test.
Text {
  id: root

  property color infoColor: "gray"
  property color errorColor: "red"
  // Set by the owner to hide the line without ending its time (an error that
  // is already shown on a track row).
  property bool suppressed: false
  // Durations in milliseconds; tests shorten them.
  property int infoMs: SoundCloudModel.toastDelay("info")
  property int errorMs: SoundCloudModel.toastDelay("error")
  property int leaveMs: SoundCloudModel.toastLeaveDelay()

  // "info", "error" or "" (nothing to show); message is the current text.
  property string kind: ""
  property string message: ""
  readonly property bool hovered: area.containsMouse
  readonly property bool active: message !== ""
  // Emitted when a message ends (time, click or clear()), with what it was.
  signal ended(string kind, string message)

  // Kept while fading out, after the message itself is gone.
  property string shownText: ""
  textFormat: Text.PlainText
  text: message !== "" ? message : shownText
  color: kind === "error" || (kind === "" && lastKind === "error") ? errorColor : infoColor
  property string lastKind: ""
  wrapMode: Text.WordWrap
  opacity: active && !suppressed ? 1 : 0
  visible: opacity > 0
  Behavior on opacity { NumberAnimation { duration: 150 } }

  function show(newKind, newText, fromStatus) {
    newText = String(newText || "")
    if (newText === "") return
    var restarts = SoundCloudModel.toastRestarts(kind, message, newKind, newText, fromStatus === true)
    kind = newKind
    lastKind = newKind
    message = newText
    shownText = newText
    if (restarts || !timer.running) arm(newKind === "error" ? errorMs : infoMs)
  }

  // Ends the current message at once (a new user action, a click).
  function clear() {
    if (message === "") return
    var oldKind = kind
    var oldMessage = message
    timer.stop()
    kind = ""
    message = ""
    ended(oldKind, oldMessage)
  }

  function arm(ms) {
    timer.interval = ms
    if (area.containsMouse) timer.stop()
    else timer.restart()
  }

  onHoveredChanged: {
    if (!active) return
    if (hovered) timer.stop()
    else arm(leaveMs)
  }

  Timer {
    id: timer
    onTriggered: root.clear()
  }

  Interactive {
    id: area
    anchors.fill: parent
    clickable: root.visible
    onClicked: root.clear()
  }
}
