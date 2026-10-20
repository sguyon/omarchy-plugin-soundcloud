import QtQuick

// A thin scroll indicator for a ListView or Flickable, drawn in the gap on its
// right. It is a sibling of the list (anchor it to the list's right edge, e.g.
// `anchors.left: list.right`), shows only while the content does not fit, and
// follows contentY however it moves (touch, wheel, ListWheelScroll, keys). It
// does not take mouse events. Both track lists and the queue list use it so
// they look the same. No Quickshell imports so it can be tested with Qt Quick Test.
Item {
  id: root

  required property Flickable view
  property color color: "white"
  property real thumbOpacity: 0.35
  property int minThumb: 24

  width: 3
  visible: view.visibleArea.heightRatio < 1
  anchors.top: view.top
  anchors.bottom: view.bottom
  anchors.left: view.right
  anchors.leftMargin: 2

  Rectangle {
    width: parent.width
    radius: width / 2
    color: root.color
    opacity: root.thumbOpacity
    height: Math.max(root.minThumb, root.height * root.view.visibleArea.heightRatio)
    y: Math.max(0, Math.min(root.height - height,
      root.height * root.view.visibleArea.yPosition))
  }
}
