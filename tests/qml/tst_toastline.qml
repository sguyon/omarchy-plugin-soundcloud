import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "ToastLine"
  width: 300
  height: 100
  visible: true
  when: windowShown

  Component {
    id: factory
    ToastLine {
      x: 50
      y: 20
      width: 200
      font.pixelSize: 12
      infoMs: 200
      errorMs: 400
      leaveMs: 100
    }
  }

  function make() {
    mouseMove(testCase, 280, 90)      // away from where the line will be
    var item = createTemporaryObject(factory, testCase)
    verify(item !== null)
    return item
  }

  function test_info_ends_after_its_time_and_fades() {
    var item = make()
    item.show("info", "Queued: x")
    compare(item.active, true)
    compare(item.text, "Queued: x")
    tryCompare(item, "active", false, 600)
    // The text stays while it fades out, then the line is gone.
    tryCompare(item, "visible", false, 600)
  }

  function test_error_lasts_longer_than_info() {
    var item = make()
    item.show("error", "boom")
    wait(300)
    compare(item.active, true)
    tryCompare(item, "active", false, 600)
  }

  function test_new_message_replaces_and_restarts() {
    var item = make()
    item.show("info", "one")
    wait(150)
    item.show("info", "two")
    wait(150)
    compare(item.message, "two")      // 300 ms after the first, still shown
    tryCompare(item, "active", false, 600)
  }

  function test_same_status_text_does_not_restart() {
    var item = make()
    item.show("error", "boom", true)
    wait(250)
    item.show("error", "boom", true)  // a status push: no restart
    wait(250)                         // 500 ms in, past the 400 ms limit
    compare(item.active, false)
  }

  function test_click_dismisses_at_once() {
    var item = make()
    var ended = []
    item.ended.connect(function(kind, message) { ended.push(kind + ":" + message) })
    item.show("error", "boom")
    wait(50)
    mouseClick(item, 10, 5)
    compare(item.active, false)
    compare(ended[0], "error:boom")
  }

  function test_hover_pauses_and_leaving_leaves_a_short_time() {
    var item = make()
    item.show("info", "hold")
    mouseMove(item, 9, 4)
    mouseMove(item, 10, 5)
    tryVerify(function() { return item.hovered })
    wait(500)                         // far past 200 ms, but paused
    compare(item.active, true)
    mouseMove(testCase, 280, 90)
    tryVerify(function() { return !item.hovered })
    wait(40)
    compare(item.active, true)
    tryCompare(item, "active", false, 400)
  }

  function test_clear_ends_the_message_first() {
    var item = make()
    item.show("info", "a")
    item.clear()
    compare(item.active, false)
    item.show("error", "b")
    compare(item.message, "b")
  }
}
