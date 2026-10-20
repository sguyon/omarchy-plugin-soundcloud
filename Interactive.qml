import QtQuick

// The one pointer behaviour for every clickable thing in the popup, matching
// Omarchy's controls (Button, PanelActionButton): the pointing hand while it
// can be clicked, the plain arrow while it cannot, and hover and pressed state
// for the owner to draw (hot / held). Omarchy dims or tints with its Style
// helpers; this file only holds the state so it can be tested with Qt Quick
// Test (no Quickshell imports), the owner draws it with Style.hoverFillFor and
// Style.pressedFillFor.
//
// `clickable` replaces `enabled`: the area stays enabled so a disabled thing
// still shows the arrow (and takes the hover, so nothing under it reacts).
// For something dragged, set `dragCursor: true` to get the open hand, and the
// closed hand while pressed.
MouseArea {
  id: root

  property bool clickable: true
  property bool dragCursor: false
  // Hover and pressed state, false while not clickable.
  readonly property bool hot: containsMouse && clickable
  readonly property bool held: pressed && clickable

  hoverEnabled: true
  acceptedButtons: clickable ? Qt.LeftButton : Qt.NoButton
  cursorShape: !clickable ? Qt.ArrowCursor
    : (dragCursor ? (pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor) : Qt.PointingHandCursor)
}
