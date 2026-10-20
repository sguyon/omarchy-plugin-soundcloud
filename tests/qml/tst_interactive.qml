import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "Interactive"
  width: 200
  height: 100
  visible: true
  when: windowShown

  property int clicks: 0

  Component {
    id: factory
    Interactive {
      x: 20
      y: 20
      width: 60
      height: 30
      onClicked: testCase.clicks++
    }
  }

  function make() {
    clicks = 0
    return createTemporaryObject(factory, testCase)
  }

  function test_clickable_shows_pointing_hand() {
    var item = make()
    compare(item.cursorShape, Qt.PointingHandCursor)
    compare(item.hot, false)
  }

  function test_hover_sets_hot_and_leave_clears_it() {
    var item = make()
    mouseMove(testCase, 150, 80)
    compare(item.hot, false)
    mouseMove(item, 10, 10)
    tryCompare(item, "hot", true)
    mouseMove(testCase, 150, 80)
    tryCompare(item, "hot", false)
  }

  function test_pressed_state_follows_the_button() {
    var item = make()
    mouseMove(item, 10, 10)
    mousePress(item, 10, 10)
    compare(item.held, true)
    mouseRelease(item, 10, 10)
    compare(item.held, false)
    compare(clicks, 1)
  }

  function test_not_clickable_shows_arrow_ignores_clicks_and_never_looks_hot() {
    var item = make()
    item.clickable = false
    compare(item.cursorShape, Qt.ArrowCursor)
    mouseMove(item, 10, 10)
    mouseClick(item, 10, 10)
    compare(item.containsMouse, true)
    compare(item.hot, false)
    compare(item.held, false)
    compare(clicks, 0)
    item.clickable = true
    compare(item.cursorShape, Qt.PointingHandCursor)
    tryCompare(item, "hot", true)
  }

  function test_drag_cursor_is_open_then_closed_hand() {
    var item = make()
    item.dragCursor = true
    compare(item.cursorShape, Qt.OpenHandCursor)
    mouseMove(item, 10, 10)
    mousePress(item, 10, 10)
    compare(item.cursorShape, Qt.ClosedHandCursor)
    mouseRelease(item, 10, 10)
    compare(item.cursorShape, Qt.OpenHandCursor)
    item.clickable = false
    compare(item.cursorShape, Qt.ArrowCursor)
  }
}
