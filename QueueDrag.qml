import QtQml
import "SoundCloudModel.js" as SoundCloudModel

// The state of dragging one queue row to reorder it, kept apart from the popup so
// it can be tested (no Quickshell imports). The list can scroll under a still
// pointer (the wheel), so positions are kept as the pointer's place in the content,
// not in the view: begin() when the handle is pressed, move() on every pointer move
// and on every scroll, end() when it is released.
QtObject {
  id: root

  // From the list: the distance between two rows, how many rows there are, and the
  // height of the visible list and of a row.
  property real rowStep: 43
  property int count: 0
  property real listHeight: 0
  property real rowHeight: 40

  // The index of the row being dragged (-1 for none), how many rows it has moved
  // so far (signed), and how far it has been dragged in the content (pixels).
  property int fromIndex: -1
  property int places: 0
  property real dy: 0
  // Where the pointer is in the visible list, and where in the row it took hold.
  property real pointerViewY: 0
  property real grab: 0
  property real startContentY: 0

  readonly property bool active: fromIndex >= 0

  // Where the copy of the held row is drawn in the visible list: at the pointer,
  // as far from the row's top as the pointer was when it was grabbed, and never
  // outside the list.
  readonly property real ghostY: Math.max(0, Math.min(listHeight - rowHeight, pointerViewY - grab))

  // `rowContentY` is the row's top in the list's content.
  function begin(index, pointerY, contentY, rowContentY) {
    pointerViewY = pointerY
    startContentY = pointerY + contentY
    grab = startContentY - rowContentY
    dy = 0
    places = 0
    fromIndex = index
  }

  function move(pointerY, contentY) {
    if (!active) return
    pointerViewY = pointerY
    dy = pointerY + contentY - startContentY
    places = SoundCloudModel.queueDragOffset(dy, rowStep, fromIndex, count)
  }

  // The places the row has been dragged by, for a row now at `index` (the list may
  // have changed while it was held); the drag is over.
  function end(index) {
    var moved = SoundCloudModel.queueDragOffset(dy, rowStep, index, count)
    cancel()
    return moved
  }

  function cancel() {
    fromIndex = -1
    places = 0
    dy = 0
  }
}
