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
