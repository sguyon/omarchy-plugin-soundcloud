import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "AppKeys"
  width: 200
  height: 100
  visible: true
  when: windowShown

  property var actions: []
  property var catcherKeys: []

  // Same shape as the popup: a catcher that handles keys before its children
  // (and turns l, h, Shift+arrows into moves), with AppKeys in its forwardTo.
  Component {
    id: factory
    Item {
      id: catcher
      property alias keys: keys
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.forwardTo: [keys]
      Keys.onPressed: function(event) {
        // The modifier keys' own events (Shift, Ctrl, Meta, Alt) are not shortcuts.
        if (event.key < Qt.Key_Shift || event.key > Qt.Key_Alt) testCase.catcherKeys.push(event.key)
        event.accepted = true
      }
      AppKeys {
        id: keys
        onTriggered: function(action) { testCase.actions.push(action) }
      }
    }
  }

  function make() {
    actions = []
    catcherKeys = []
    var item = createTemporaryObject(factory, testCase)
    verify(item !== null)
    item.forceActiveFocus()
    return item
  }

  function test_keys_are_taken_before_the_catcher() {
    make()
    keyClick("l")
    keyClick(Qt.Key_Right, Qt.ShiftModifier)
    keyClick(Qt.Key_PageDown)
    keyClick(Qt.Key_Up, Qt.ShiftModifier)
    keyClick(Qt.Key_Down, Qt.ShiftModifier)
    keyClick("L", Qt.ShiftModifier)
    keyClick("S", Qt.ShiftModifier)
    compare(actions, ["like", "next", "next", "volume-up", "volume-down", "repeat", "shuffle"])
    compare(catcherKeys, [])
  }

  function test_alt_up_and_down_move_the_queue_row() {
    make()
    keyClick(Qt.Key_Up, Qt.AltModifier)
    keyClick(Qt.Key_Down, Qt.AltModifier)
    compare(actions, ["queue-up", "queue-down"])
    compare(catcherKeys, [])
  }

  function test_h_is_swallowed_without_an_action() {
    make()
    keyClick("h")
    compare(actions, [])
    compare(catcherKeys, [])
  }

  function test_other_keys_reach_the_catcher() {
    make()
    keyClick("j")
    keyClick(Qt.Key_Right)
    keyClick(Qt.Key_Down)
    compare(actions, [])
    compare(catcherKeys.length, 3)
  }

  function test_inactive_leaves_every_key_alone() {
    var item = make()
    item.keys.active = false
    keyClick("l")
    keyClick(Qt.Key_PageDown)
    compare(actions, [])
    compare(catcherKeys.length, 2)
  }
}
