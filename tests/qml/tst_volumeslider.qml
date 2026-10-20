import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "VolumeSlider"
  width: 200
  height: 100
  visible: true
  when: windowShown

  property var emitted: []
  property int commits: 0

  Component {
    id: factory
    VolumeSlider {
      width: 100  // handle centre runs from x 6 to x 94, 88 px
      height: 32
      throttleMs: 0
      onMoved: function(value) { testCase.emitted.push(value) }
      onCommitted: testCase.commits++
    }
  }

  function make(extra) {
    emitted = []
    commits = 0
    var item = createTemporaryObject(factory, testCase, extra || {})
    verify(item !== null)
    return item
  }

  function test_click_jumps_to_the_point() {
    var item = make()
    mouseClick(item, 50, 16)
    compare(emitted, [0.5])
    compare(commits, 1)
  }

  function test_drag_emits_in_order_and_ends_with_the_release_value() {
    var item = make()
    mousePress(item, 6, 16)
    mouseMove(item, 28, 16)
    mouseMove(item, 50, 16)
    mouseMove(item, 72, 16)
    mouseRelease(item, 72, 16)
    compare(emitted, [0, 0.25, 0.5, 0.75])
    compare(commits, 1)
  }

  function test_clamps_at_zero_and_one() {
    var item = make()
    mousePress(item, 50, 16)
    mouseMove(item, -40, 16)
    mouseMove(item, 300, 16)
    mouseRelease(item, 300, 16)
    compare(emitted[emitted.length - 1], 1)
    verify(emitted.indexOf(0) >= 0)
    verify(Math.min.apply(null, emitted) >= 0 && Math.max.apply(null, emitted) <= 1)
  }

  function test_status_update_is_ignored_while_dragging() {
    var item = make({ value: 0.2 })
    compare(item.shownValue, 0.2)
    mousePress(item, 50, 16)
    compare(item.shownValue, 0.5)
    item.value = 0.9  // a status arrives mid-drag
    compare(item.shownValue, 0.5)
    mouseRelease(item, 50, 16)
    compare(item.shownValue, 0.9)  // follows the status again afterwards
  }

  function test_handle_follows_value_when_idle() {
    var item = make({ value: 0 })
    item.value = 1
    compare(item.shownValue, 1)
    item.value = 7
    compare(item.shownValue, 1)
    item.value = -3
    compare(item.shownValue, 0)
  }

  function test_throttle_drops_fast_moves_but_keeps_the_last() {
    var item = make({ throttleMs: 1000 })
    mousePress(item, 6, 16)
    mouseMove(item, 28, 16)
    mouseMove(item, 50, 16)
    mouseRelease(item, 72, 16)
    compare(emitted, [0, 0.75])  // press, then only the release inside the window
    compare(commits, 1)
  }
}
