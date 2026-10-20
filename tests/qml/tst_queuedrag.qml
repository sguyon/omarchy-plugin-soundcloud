import QtQuick
import QtTest
import "../.."

// Dragging a queue row (QueueDrag): where the held row's copy is drawn and how far
// the row has moved, with the list scrolling under a still pointer. The last test
// holds a row on a real scrolling list, the way BarWidget does.
TestCase {
  id: testCase
  name: "QueueDrag"
  width: 300
  height: 300
  visible: true
  when: windowShown

  Component {
    id: sessionFactory
    QueueDrag {
      rowStep: 43
      rowHeight: 40
      count: 40
      listHeight: 200
    }
  }

  function test_the_copy_starts_where_the_row_is() {
    var s = createTemporaryObject(sessionFactory, testCase)
    // The row is the third (content top 86) in a list scrolled by 30: it is drawn at
    // 56 in the view, and the pointer took hold 12 pixels below its top.
    s.begin(2, 56 + 12, 30, 86)
    compare(s.active, true)
    compare(s.grab, 12)
    compare(s.ghostY, 56)
    compare(s.places, 0)
  }

  function test_the_copy_follows_the_pointer() {
    var s = createTemporaryObject(sessionFactory, testCase)
    s.begin(2, 68, 30, 86)
    s.move(100, 30)
    compare(s.ghostY, 100 - 12)
    compare(s.dy, 32)
    compare(s.places, 1) // 32 px is about one row (43)
  }

  function test_scrolling_moves_the_row_with_a_still_pointer() {
    var s = createTemporaryObject(sessionFactory, testCase)
    s.begin(2, 68, 30, 86)
    s.move(68, 30 + 172) // the wheel scrolled four rows, the pointer did not move
    compare(s.ghostY, 56)
    compare(s.dy, 172)
    compare(s.places, 4)
  }

  function test_the_copy_stays_inside_the_list() {
    var s = createTemporaryObject(sessionFactory, testCase)
    s.begin(2, 68, 30, 86)
    s.move(-50, 30)
    compare(s.ghostY, 0)
    s.move(900, 30)
    compare(s.ghostY, 160) // list height 200 minus row height 40
  }

  function test_the_first_row_after_the_playing_one_cannot_go_up() {
    var s = createTemporaryObject(sessionFactory, testCase)
    s.begin(1, 60, 0, 43)
    s.move(-100, 0)
    compare(s.places, 0) // nothing goes above the playing row
  }

  function test_end_returns_the_places_and_resets() {
    var s = createTemporaryObject(sessionFactory, testCase)
    s.begin(2, 68, 0, 86)
    s.move(68 + 90, 0)
    compare(s.end(2), 2)
    compare(s.active, false)
    compare(s.places, 0)
    compare(s.dy, 0)
    // moves after the end are ignored
    s.move(500, 0)
    compare(s.dy, 0)
  }

  // A real list: hold a row on its handle, scroll with the wheel, and the row
  // follows and is not dropped.
  Component {
    id: listFactory
    Item {
      id: host
      width: 300
      height: 300
      property alias list: list
      property alias session: session

      QueueDrag {
        id: session
        count: 60
        listHeight: 300
      }

      ListView {
        id: list
        anchors.fill: parent
        clip: true
        spacing: 3
        boundsBehavior: Flickable.StopAtBounds
        cacheBuffer: session.active ? 44 * 60 : 0
        model: 60

        delegate: Item {
          id: row
          required property int index
          width: list.width
          height: 40

          Interactive {
            id: handle
            width: 30
            height: parent.height
            anchors.right: parent.right
            preventStealing: true
            function view(mouse) { return mapToItem(list, mouse.x, mouse.y).y }
            onPressed: function(mouse) { session.begin(row.index, view(mouse), list.contentY, row.y) }
            onPositionChanged: function(mouse) { if (pressed) session.move(view(mouse), list.contentY) }
            onReleased: session.cancel()
            Connections {
              target: list
              function onContentYChanged() { if (handle.pressed) session.move(session.pointerViewY, list.contentY) }
            }
          }
        }
      }

      ListWheelScroll {
        anchors.fill: list
        view: list
        endMs: 30
      }
    }
  }

  function test_a_held_row_follows_a_wheel_scroll_on_a_list() {
    var host = createTemporaryObject(listFactory, testCase)
    verify(host)
    waitForRendering(host)
    var list = host.list
    var row = list.itemAtIndex(2)
    verify(row)
    var x = row.width - 10
    var y = row.y + row.height / 2
    mousePress(host, x, y)
    mouseMove(host, x, y + 12)
    compare(host.session.active, true)
    var before = list.contentY
    for (var i = 0; i < 12; i++) {
      mouseWheel(host, x, y + 12, 0, -120)
      wait(20)
    }
    wait(80)
    verify(list.contentY > before)
    // The pointer has not moved, so the copy has not either; the row moved with the list.
    fuzzyCompare(host.session.ghostY, y + 12 - host.session.grab, 1)
    fuzzyCompare(host.session.dy, 12 + (list.contentY - before), 1)
    verify(list.itemAtIndex(2) !== null) // the held row's delegate is still there
    mouseRelease(host, x, y + 12)
    compare(host.session.active, false)
  }
}
