import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "SeekKeys"
  width: 200
  height: 100
  visible: true
  when: windowShown

  property var moves: []

  // Same shape as the key catcher in the popup: it handles keys before its
  // children and accepts them; SeekKeys sits in its Keys.forwardTo.
  Component {
    id: factory
    Item {
      id: catcher
      property alias seek: seek
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.forwardTo: [seek]
      Keys.onPressed: function(event) {
        if (event.key === Qt.Key_Right) { testCase.moves.push(seek.repeats); event.accepted = true }
        else if (event.text === "l") { testCase.moves.push(seek.repeats); event.accepted = true }
      }
      SeekKeys { id: seek }
    }
  }

  function make() {
    moves = []
    var item = createTemporaryObject(factory, testCase)
    verify(item !== null)
    item.forceActiveFocus()
    return item
  }

  function test_fresh_presses_count_zero() {
    var item = make()
    keyClick(Qt.Key_Right)
    keyClick(Qt.Key_Right)
    compare(moves, [0, 0])
  }

  function test_count_is_updated_before_the_catcher_handles_the_key() {
    var item = make()
    // Auto-repeat events cannot be sent from QML, so set the count the way Keys would
    // after a few repeats: a fresh press must reset it before the catcher reads it.
    item.seek.repeats = 3
    keyPress(Qt.Key_Right)
    compare(moves, [0])
    item.seek.repeats = 3
    keyRelease(Qt.Key_Right)
    compare(item.seek.repeats, 0)
  }

  function test_letter_keys_are_not_seek_aliases() {
    var item = make()
    item.seek.repeats = 4
    keyClick("l")
    compare(item.seek.repeats, 4)
  }

  function test_other_keys_do_not_reset_the_count() {
    var item = make()
    item.seek.repeats = 4
    keyClick(Qt.Key_Down)
    compare(item.seek.repeats, 4)
  }
}
