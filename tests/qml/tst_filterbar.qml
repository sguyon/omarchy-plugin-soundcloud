import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "FilterBar"
  width: 300
  height: 100
  visible: true
  when: windowShown

  property int closes: 0
  property int lists: 0

  // Same shape as the popup: a key catcher that is blocked while the field has
  // focus (it must then leave every key to the field).
  Component {
    id: factory
    Item {
      id: catcher
      property alias bar: bar
      property int caught: 0
      width: 300
      height: 40
      focus: true
      Keys.priority: Keys.BeforeItem
      Keys.onPressed: function(event) {
        if (bar.fieldFocused) return
        catcher.caught++
        event.accepted = true
      }
      FilterBar {
        id: bar
        anchors.fill: parent
        matchCount: 2
        totalCount: 24
        onCloseRequested: testCase.closes++
        onListRequested: { testCase.lists++; catcher.forceActiveFocus() }
      }
    }
  }

  function make() {
    closes = 0
    lists = 0
    var item = createTemporaryObject(factory, testCase)
    verify(item !== null)
    item.bar.focusField()
    verify(item.bar.fieldFocused)
    return item
  }

  function test_typing_and_count() {
    var item = make()
    compare(item.bar.countText, "24")
    keyClick("a")
    keyClick("B")
    keyClick("/")
    compare(item.bar.text, "aB/")
    compare(item.bar.countText, "2 of 24")
    compare(item.caught, 0)
    item.bar.matchCount = 0
    compare(item.bar.countText, "0 of 24")
  }

  function test_escape_clears_then_closes() {
    var item = make()
    keyClick("a")
    keyClick("b")
    keyClick(Qt.Key_Escape)
    compare(item.bar.text, "")
    compare(closes, 0)
    verify(item.bar.fieldFocused)
    keyClick(Qt.Key_Escape)
    compare(closes, 1)
    compare(item.caught, 0)
  }

  function test_escape_on_an_empty_field_closes() {
    var item = make()
    keyClick(Qt.Key_Escape)
    compare(closes, 1)
  }

  function test_down_tab_and_enter_ask_for_the_list() {
    var item = make()
    keyClick("a")
    keyClick(Qt.Key_Down)
    compare(lists, 1)
    verify(!item.bar.fieldFocused)
    item.bar.focusField()
    keyClick(Qt.Key_Tab)
    compare(lists, 2)
    item.bar.focusField()
    keyClick(Qt.Key_Backtab, Qt.ShiftModifier)
    compare(lists, 2)
    verify(item.bar.fieldFocused)
    keyClick(Qt.Key_Return)
    compare(lists, 3)
    compare(item.bar.text, "a")
    // Every key went to the field or to nothing: the catcher saw none while focused.
    compare(item.caught, 0)
  }

  function test_held_enter_asks_once_and_only_on_release() {
    var item = make()
    keyPress(Qt.Key_Return)
    compare(lists, 0)
    keyPress(Qt.Key_Return, Qt.NoModifier, 50) // not a real repeat in Qt Test, but must not double up
    keyRelease(Qt.Key_Return)
    compare(lists, 1)
    verify(!item.bar.fieldFocused)
  }

  // Ctrl+V is the field's own paste here, not the track-link paste.
  Component {
    id: source
    TextEdit { text: "pasted words" }
  }

  function test_ctrl_v_pastes_into_the_field() {
    var clip = createTemporaryObject(source, testCase)
    clip.selectAll()
    clip.copy()
    var item = make()
    keyClick(Qt.Key_V, Qt.ControlModifier)
    compare(item.bar.text, "pasted words")
    compare(item.caught, 0)
  }

  function test_query_is_capped() {
    var item = make()
    item.bar.text = "x".repeat(150)
    compare(item.bar.text.length, 100)
  }

  function test_clear_button() {
    var item = make()
    keyClick("a")
    mouseClick(item.bar, item.bar.width - 10, item.bar.height / 2)
    compare(item.bar.text, "")
    compare(closes, 0)
    mouseClick(item.bar, item.bar.width - 10, item.bar.height / 2)
    compare(closes, 1)
  }
}
