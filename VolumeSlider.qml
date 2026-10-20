import QtQuick
import "SoundCloudModel.js" as SoundCloudModel

// A thin horizontal volume bar in Omarchy's PanelSlider look (rounded track,
// filled part, round handle), for the popup's volume icon. A click anywhere on
// it jumps there; dragging emits moved(value) at most once per throttleMs, plus
// one last time on release, then committed(). While dragging the handle follows
// the pointer and ignores `value` (the status would fight it); otherwise it
// follows `value`. No Quickshell
// imports so it can be tested with Qt Quick Test.
Item {
  id: root

  property real value: 0          // 0..1, from the status (0 when muted)
  property int throttleMs: SoundCloudModel.sliderThrottleMs
  property color trackColor: Qt.rgba(1, 1, 1, 0.25)
  property color fillColor: "#4aa3ff"
  property color knobColor: "#ffffff"
  property real trackHeight: 4
  property real knobSize: 12

  readonly property bool dragging: area.pressed
  property real dragValue: 0
  readonly property real shownValue: dragging ? dragValue : Math.max(0, Math.min(1, value))

  property real lastEmitted: -1
  property real lastEmitMs: 0

  signal moved(real value)
  signal committed()

  implicitWidth: 88
  implicitHeight: 32

  // The handle centre runs from knobSize / 2 to width - knobSize / 2.
  readonly property real span: Math.max(1, width - knobSize)

  function emitMoved(level, force) {
    if (level === lastEmitted) return
    var now = Date.now()
    if (!force && !SoundCloudModel.sliderEmitDue(lastEmitMs, now, throttleMs)) return
    lastEmitted = level
    lastEmitMs = now
    moved(level)
  }

  Rectangle {
    id: track
    anchors.verticalCenter: parent.verticalCenter
    anchors.left: parent.left
    anchors.right: parent.right
    height: root.trackHeight
    radius: height / 2
    color: root.trackColor
  }

  Rectangle {
    anchors.verticalCenter: track.verticalCenter
    anchors.left: track.left
    height: track.height
    radius: track.radius
    color: root.fillColor
    width: knob.x + knob.width / 2
  }

  Rectangle {
    id: knob
    anchors.verticalCenter: track.verticalCenter
    width: root.knobSize
    height: root.knobSize
    radius: width / 2
    color: root.knobColor
    x: root.shownValue * root.span
    scale: area.pressed ? 1.15 : (area.containsMouse ? 1.08 : 1)

    Behavior on x {
      enabled: !root.dragging
      NumberAnimation { duration: 100; easing.type: Easing.OutCubic }
    }
    Behavior on scale {
      NumberAnimation { duration: 90; easing.type: Easing.OutCubic }
    }
  }

  Interactive {
    id: area
    anchors.fill: parent
    preventStealing: true

    function level(x) {
      return SoundCloudModel.sliderValueFromX(x, root.knobSize / 2, root.span)
    }

    onPressed: function(mouse) {
      root.dragValue = level(mouse.x)
      root.emitMoved(root.dragValue, true)
    }
    onPositionChanged: function(mouse) {
      if (!pressed) return
      root.dragValue = level(mouse.x)
      root.emitMoved(root.dragValue, false)
    }
    onReleased: function(mouse) {
      root.dragValue = level(mouse.x)
      root.emitMoved(root.dragValue, true)
      root.lastEmitted = -1
      root.committed()
    }
  }
}
