import QtQuick
import "SoundCloudModel.js" as SoundCloudModel

// Faster list scrolling with a two-finger swipe, with momentum. A transparent
// overlay for a Flickable (placed on top of it, clicks pass through): each wheel
// event moves contentY by SoundCloudModel.listScrollFactor times what the toolkit
// would, kept inside the list, and when the swipe ends the list coasts on with
// the swipe's speed through the Flickable's own deceleration. A mouse wheel
// moves the list the same way but does not coast. settled() fires when a swipe
// ended without coasting, so the caller can load more rows. No Quickshell
// imports so it can be tested with Qt Quick Test.
MouseArea {
  id: root

  required property Flickable view

  signal settled()

  // Speed of the swipe in contentY px/s; 0 for a mouse wheel or a new swipe.
  property real velocity: 0
  property double lastAt: 0
  property int endMs: 70
  property int swipeGapMs: 100

  acceptedButtons: Qt.NoButton

  onWheel: function(wheel) { moveBy(wheel.pixelDelta.y, wheel.angleDelta.y, Date.now()) }

  // One wheel event; the time is a parameter so a test can feed a swipe.
  function moveBy(pixelDeltaY, angleDeltaY, now) {
    var change = SoundCloudModel.scrollChange(pixelDeltaY, angleDeltaY)
    if (change === 0) return
    var gap = now - lastAt
    var touchpad = pixelDeltaY !== 0
    if (touchpad && lastAt > 0 && gap > 0 && gap <= swipeGapMs)
      velocity = SoundCloudModel.swipeVelocity(velocity, change, gap)
    else
      velocity = 0
    lastAt = now
    view.cancelFlick()
    view.contentY = SoundCloudModel.scrolledContentY(
      view.contentY, change, view.originY, view.contentHeight, view.height)
    end.restart()
  }

  Timer {
    id: end
    interval: root.endMs
    onTriggered: {
      var start = SoundCloudModel.flickStartVelocity(root.velocity)
      root.velocity = 0
      root.lastAt = 0
      // flick() takes the direction the content moves: down is positive.
      if (start !== 0) root.view.flick(0, -start)
      if (!root.view.flicking) root.settled()
    }
  }
}
