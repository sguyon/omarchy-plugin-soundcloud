import QtQuick

// A speaker with 0 to 3 sound waves, or a cross when muted. Drawn here because
// no icon font has a speaker with one, two and three waves, so each volume
// level can show its own number of them.
Canvas {
  id: root

  // 0 to 3 waves; muted draws a cross instead of waves.
  property int waves: 3
  property bool muted: false
  property color color: "white"
  // The height in pixels; the width follows (the drawing is 18 units wide, 16 high).
  property real size: 16

  implicitWidth: size * 18 / 16
  implicitHeight: size
  width: implicitWidth
  height: implicitHeight
  antialiasing: true

  onWavesChanged: requestPaint()
  onMutedChanged: requestPaint()
  onColorChanged: requestPaint()
  onWidthChanged: requestPaint()
  onHeightChanged: requestPaint()

  onPaint: {
    var ctx = getContext("2d")
    ctx.reset()
    var u = height / 16
    ctx.fillStyle = root.color
    ctx.strokeStyle = root.color

    // The speaker: a small box and the cone.
    ctx.beginPath()
    ctx.moveTo(1 * u, 6 * u)
    ctx.lineTo(4.5 * u, 6 * u)
    ctx.lineTo(8.5 * u, 2.5 * u)
    ctx.lineTo(8.5 * u, 13.5 * u)
    ctx.lineTo(4.5 * u, 10 * u)
    ctx.lineTo(1 * u, 10 * u)
    ctx.closePath()
    ctx.fill()

    ctx.lineWidth = 1.4 * u
    ctx.lineCap = "round"
    if (root.muted) {
      ctx.beginPath()
      ctx.moveTo(11 * u, 5.5 * u)
      ctx.lineTo(16 * u, 10.5 * u)
      ctx.moveTo(16 * u, 5.5 * u)
      ctx.lineTo(11 * u, 10.5 * u)
      ctx.stroke()
      return
    }
    var count = Math.max(0, Math.min(3, Math.floor(root.waves)))
    for (var i = 1; i <= count; i++) {
      ctx.beginPath()
      ctx.arc(8.5 * u, 8 * u, (2 + 2.2 * i) * u, -0.62, 0.62, false)
      ctx.stroke()
    }
  }
}
