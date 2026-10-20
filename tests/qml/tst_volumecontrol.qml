import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "VolumeControl"
  width: 200
  height: 100
  visible: true
  when: windowShown

  property var steps: []
  property int clicks: 0

  Component {
    id: factory
    Item {
      width: 100
      height: 100
      property alias control: volume
      property alias area: below
      MouseArea {
        id: below
        anchors.fill: parent
        onClicked: testCase.clicks++
      }
      VolumeControl {
        id: volume
        anchors.fill: parent
        idleMs: 100
        onStepped: function(direction) { testCase.steps.push(direction) }
      }
    }
  }

  // The popup layout: a full-size VolumeControl underlay (z: -1) below the
  // content, a plain click target (a button) and a list with ListWheelScroll.
  Component {
    id: popupFactory
    Item {
      width: 200
      height: 200
      property alias list: list
      property alias scroll: scroll
      VolumeControl {
        anchors.fill: parent
        z: -1
        onStepped: function(direction) { testCase.steps.push(direction) }
      }
      Column {
        anchors.fill: parent
        MouseArea {
          width: 200
          height: 40
          onClicked: testCase.clicks++
        }
        Item {
          width: 200
          height: 100
          Flickable {
            id: list
            anchors.fill: parent
            contentHeight: 100
            clip: true
          }
          ListWheelScroll {
            id: scroll
            anchors.fill: list
            view: list
          }
        }
      }
    }
  }

  function makePopup(listContentHeight) {
    steps = []
    clicks = 0
    var item = createTemporaryObject(popupFactory, testCase)
    verify(item !== null)
    item.list.contentHeight = listContentHeight
    return item
  }

  function test_underlay_wheel_over_empty_area_steps_the_volume() {
    var item = makePopup(1000)
    mouseWheel(item, 100, 170, 0, 120)
    compare(steps, [1])
    mouseWheel(item, 100, 170, 0, -120)
    compare(steps, [1, -1])
  }

  function test_underlay_wheel_over_a_click_target_steps_the_volume() {
    var item = makePopup(1000)
    mouseWheel(item, 100, 20, 0, 120)
    compare(steps, [1])
    mouseClick(item, 100, 20)
    compare(clicks, 1)
  }

  function test_underlay_wheel_over_a_list_scrolls_it_and_not_the_volume() {
    var item = makePopup(1000)
    mouseWheel(item, 100, 90, 0, -120)
    compare(steps.length, 0)
    verify(item.list.contentY > 0)
  }

  function test_underlay_list_at_its_end_keeps_the_wheel() {
    var item = makePopup(1000)
    // At the top, scrolling up has nowhere to go; the volume must not move.
    mouseWheel(item, 100, 90, 0, 120)
    compare(item.list.contentY, 0)
    compare(steps.length, 0)
    // At the bottom, scrolling down likewise.
    item.list.contentY = item.list.contentHeight - item.list.height
    mouseWheel(item, 100, 90, 0, -120)
    compare(steps.length, 0)
  }

  function test_underlay_list_that_does_not_scroll_keeps_the_wheel() {
    var item = makePopup(50)
    mouseWheel(item, 100, 90, 0, 120)
    mouseWheel(item, 100, 90, 0, -120)
    compare(steps.length, 0)
  }

  function make() {
    steps = []
    clicks = 0
    var item = createTemporaryObject(factory, testCase)
    verify(item !== null)
    return item
  }

  function test_mouse_notch_is_one_step() {
    var item = make()
    mouseWheel(item, 50, 50, 0, 120)
    compare(steps, [1])
    mouseWheel(item, 50, 50, 0, -120)
    compare(steps, [1, -1])
  }

  function test_touchpad_events_add_up() {
    var item = make()
    for (var i = 0; i < 40; i++) mouseWheel(item, 50, 50, 0, 6)
    compare(steps.length, 2)  // 240 units = two notches
    compare(steps[0], 1)
  }

  function test_small_events_alone_do_nothing() {
    var item = make()
    mouseWheel(item, 50, 50, 0, 30)
    mouseWheel(item, 50, 50, 0, 30)
    compare(steps.length, 0)
  }

  function test_total_is_dropped_after_idle() {
    var item = make()
    mouseWheel(item, 50, 50, 0, 80)
    wait(200)
    mouseWheel(item, 50, 50, 0, 80)
    compare(steps.length, 0)
    compare(item.control.wheelUnits, 80)
  }

  function test_direction_change_drops_the_total() {
    var item = make()
    mouseWheel(item, 50, 50, 0, 100)
    mouseWheel(item, 50, 50, 0, -50)
    compare(steps.length, 0)
    compare(item.control.wheelUnits, -50)
  }

  function test_clicks_reach_the_item_below() {
    var item = make()
    mouseClick(item, 50, 50)
    compare(clicks, 1)
  }
}
