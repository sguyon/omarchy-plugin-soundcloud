import QtQuick
import QtQuick.Effects
import "SoundCloudModel.js" as SoundCloudModel

// The signed-in account's picture as a small circle. Without a usable image
// (not loaded yet, or the account has none) it shows an account glyph in the
// same circle. The picture only comes from a png/jpeg data URL (see
// SoundCloudModel.accountAvatarSource). Clicks emit clicked(); the caller opens
// the Library. The circle edge is antialiased (a multisampled mask with a soft
// threshold) and the thin ring is its own Rectangle on top, not part of the
// clipped picture. No Quickshell imports so it can be tested with Qt Quick Test.
Item {
  id: root

  property string source: ""
  property string glyph: "󰀄"
  property string fontFamily: ""
  property color foreground: "white"
  property color fill: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.18)
  property string tooltipText: ""
  // Drawn over the picture while the pointer is on it, and darker while pressed.
  property color hoverFill: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.18)
  property color pressedFill: Qt.rgba(foreground.r, foreground.g, foreground.b, 0.3)
  readonly property bool hasImage: image.status === Image.Ready
  readonly property alias hovered: mouse.hot

  signal clicked()

  implicitWidth: 28
  implicitHeight: 28

  // Circular mask for the picture, multisampled so its edge is smooth.
  Rectangle {
    id: mask
    anchors.fill: parent
    radius: width / 2
    visible: false
    layer.enabled: true
    layer.smooth: true
    layer.samples: 4
  }

  Rectangle {
    anchors.fill: parent
    radius: width / 2
    color: root.fill
    visible: !root.hasImage

    Text {
      anchors.centerIn: parent
      textFormat: Text.PlainText
      text: root.glyph
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Math.round(root.height * 0.7)
    }
  }

  Image {
    id: image
    anchors.fill: parent
    fillMode: Image.PreserveAspectCrop
    asynchronous: true
    smooth: true
    mipmap: true
    // Decoded at four times the shown size (never less than 256) and scaled down.
    sourceSize.width: Math.max(256, Math.round(root.width * 4))
    sourceSize.height: Math.max(256, Math.round(root.height * 4))
    source: SoundCloudModel.accountAvatarSource(root.source)
    visible: false
  }

  MultiEffect {
    anchors.fill: parent
    source: image
    visible: root.hasImage
    maskEnabled: true
    maskSource: mask
    maskThresholdMin: 0.5
    maskSpreadAtMin: 1.0
  }

  // The contour: drawn above the picture, antialiased by itself.
  Rectangle {
    anchors.fill: parent
    radius: width / 2
    color: "transparent"
    antialiasing: true
    border.width: 1
    border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.35)
  }

  // Hover and pressed tint, inside the contour.
  Rectangle {
    anchors.fill: parent
    radius: width / 2
    color: mouse.held ? root.pressedFill : root.hoverFill
    visible: mouse.hot
  }

  Interactive {
    id: mouse
    anchors.fill: parent
    onClicked: root.clicked()
  }
}
