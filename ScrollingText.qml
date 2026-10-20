import QtQuick

// One line of text that scrolls slowly back and forth when it is wider than
// its area, pausing at each end. It stops while the pointer is over it and
// can be dragged by hand; the next step continues from where it was left.
// It has no Quickshell imports so it can be tested with Qt Quick Test.
Item {
  id: root

  property string text: ""
  property color color: "white"
  property alias font: label.font
  // Pixels per second, and how long each end holds, in milliseconds.
  property real speed: 28
  property int pauseMs: 1500

  readonly property bool overflowing: label.implicitWidth > width + 0.5
  readonly property real maxScroll: Math.max(0, label.implicitWidth - width)
  readonly property bool hovered: hover.hovered
  readonly property alias cursorShape: hover.cursorShape
  readonly property bool dragging: flick.dragging || flick.flicking
  readonly property alias scrollX: flick.contentX
  // 1 while moving toward the end, -1 while moving back.
  property int direction: 1
  property int holdMs: pauseMs

  implicitHeight: label.implicitHeight
  clip: true

  Flickable {
    id: flick
    anchors.fill: parent
    contentWidth: Math.max(label.implicitWidth, root.width)
    contentHeight: height
    interactive: root.overflowing
    flickableDirection: Flickable.HorizontalFlick
    boundsBehavior: Flickable.StopAtBounds

    Text {
      id: label
      textFormat: Text.PlainText
      text: root.text
      color: root.color
      wrapMode: Text.NoWrap
    }
  }

  // The pointing hand (as on the buttons) while there is something to drag; the
  // plain arrow otherwise.
  HoverHandler {
    id: hover
    cursorShape: root.overflowing ? Qt.PointingHandCursor : Qt.ArrowCursor
  }

  // Advances the scroll by the time that passed; pure steps so tests can drive it.
  function advance(elapsedMs) {
    if (!overflowing || hovered || dragging) return
    if (holdMs > 0) {
      holdMs = Math.max(0, holdMs - elapsedMs)
      return
    }
    var next = flick.contentX + direction * speed * elapsedMs / 1000
    if (next >= maxScroll) {
      flick.contentX = maxScroll
      direction = -1
      holdMs = pauseMs
    } else if (next <= 0) {
      flick.contentX = 0
      direction = 1
      holdMs = pauseMs
    } else {
      flick.contentX = next
    }
  }

  function restart() {
    flick.contentX = 0
    direction = 1
    holdMs = pauseMs
  }

  onTextChanged: restart()
  onWidthChanged: if (!overflowing) restart()

  Timer {
    interval: 40
    repeat: true
    running: root.overflowing && root.visible
    onTriggered: root.advance(interval)
  }
}
