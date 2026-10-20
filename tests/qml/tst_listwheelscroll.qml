import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "ListWheelScroll"
  width: 200
  height: 300
  visible: true
  when: windowShown

  property int settledCount: 0

  Component {
    id: factory
    Item {
      width: 200
      height: 300
      property alias list: list
      property alias scroll: scroll
      ListView {
        id: list
        anchors.fill: parent
        boundsBehavior: Flickable.StopAtBounds
        model: 100
        delegate: Item { width: list.width; height: 50 }
      }
      ListWheelScroll {
        id: scroll
        anchors.fill: parent
        view: list
        endMs: 30
        onSettled: testCase.settledCount++
      }
    }
  }

  function make() {
    settledCount = 0
    var item = createTemporaryObject(factory, testCase)
    verify(item !== null)
    return item
  }

  function test_mouse_notch_moves_three_times_a_notch() {
    var item = make()
    mouseWheel(item, 100, 100, 0, -120)   // wheel down one notch
    compare(item.list.contentY, 180)
    mouseWheel(item, 100, 100, 0, 120)    // and back up
    compare(item.list.contentY, 0)
  }

  function test_stops_at_the_top_and_bottom() {
    var item = make()
    mouseWheel(item, 100, 100, 0, 120)
    compare(item.list.contentY, 0)
    for (var i = 0; i < 100; i++) mouseWheel(item, 100, 100, 0, -120)
    compare(item.list.contentY, 100 * 50 - 300)
    verify(!item.list.flicking)
  }

  function test_settled_fires_after_a_swipe_without_coasting() {
    var item = make()
    mouseWheel(item, 100, 100, 0, -120)
    tryCompare(testCase, "settledCount", 1)
  }

  function test_keyboard_scroll_to_index_still_works() {
    var item = make()
    item.list.positionViewAtIndex(50, ListView.Contain)
    verify(item.list.contentY > 0)
    mouseWheel(item, 100, 100, 0, 120)
    verify(item.list.contentY < 50 * 50)
  }

  // Touchpad swipe: pixel deltas 10 ms apart (Qt Test's mouseWheel sends angle deltas only).
  function test_touchpad_swipe_coasts_after_the_last_event() {
    var item = make()
    for (var i = 0; i < 8; i++) item.scroll.moveBy(-20, -24, 1000 + i * 10)
    var atRelease = item.list.contentY
    compare(atRelease, 8 * 60)   // 8 events of 20 px, three times as far
    tryVerify(function() { return item.list.flicking })
    tryVerify(function() { return item.list.contentY > atRelease + 20 })
    tryVerify(function() { return !item.list.flicking }, 8000)
    verify(item.list.contentY <= 100 * 50 - 300)
    compare(testCase.settledCount, 0)  // it coasted, so no settled()
  }

  function test_slow_swipe_does_not_coast() {
    var item = make()
    item.scroll.moveBy(-2, -2, 1000)
    item.scroll.moveBy(-2, -2, 1090)
    compare(item.list.contentY, 12)
    tryCompare(testCase, "settledCount", 1)
    verify(!item.list.flicking)
    compare(item.list.contentY, 12)
  }

  function test_coasting_stops_at_the_bottom() {
    var item = make()
    item.list.contentY = 100 * 50 - 300 - 100
    for (var i = 0; i < 8; i++) item.scroll.moveBy(-40, -48, 1000 + i * 10)
    tryVerify(function() { return !item.list.flicking }, 8000)
    compare(item.list.contentY, 100 * 50 - 300)
  }
}
