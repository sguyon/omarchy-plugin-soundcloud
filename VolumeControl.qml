import QtQuick
import "SoundCloudModel.js" as SoundCloudModel

// Turns wheel and touchpad swipes over its area into volume steps. It is a
// transparent overlay: place it on top of the icon, clicks still reach what is
// below. Events add up (a touchpad sends many small ones, a mouse notch is 120
// units) and every full notch emits stepped(+1 or -1); the total is dropped
// after a short idle. It uses a MouseArea wheel handler, the same path the
// bar icon uses, because a WheelHandler inside the popup never fired for the
// owner's touchpad. No Quickshell imports so it can be tested with Qt Quick Test.
MouseArea {
  id: root

  signal stepped(int direction)

  property real wheelUnits: 0
  property int idleMs: 300

  acceptedButtons: Qt.NoButton

  onWheel: function(wheel) {
    var result = SoundCloudModel.wheelSteps(wheelUnits, wheel.angleDelta.y)
    wheelUnits = result.accumulator
    idle.restart()
    for (var i = 0; i < Math.abs(result.steps); i++) stepped(result.steps > 0 ? 1 : -1)
  }

  Timer {
    id: idle
    interval: root.idleMs
    onTriggered: root.wheelUnits = 0
  }
}
