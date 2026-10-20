import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "ListScrollBar"
  width: 200
  height: 200
  visible: true
  when: windowShown

  Component {
    id: factory
    Item {
      width: 200
      height: 100
      property alias list: list
      property alias bar: bar
      Flickable {
        id: list
        width: 190
        height: 100
        contentHeight: 1000
      }
      ListScrollBar { id: bar; view: list }
    }
  }

  function test_hidden_when_everything_fits() {
    var item = createTemporaryObject(factory, testCase)
    item.list.contentHeight = 80
    compare(item.bar.visible, false)
  }

  function test_shown_and_follows_the_scroll_position() {
    var item = createTemporaryObject(factory, testCase)
    compare(item.bar.visible, true)
    var thumb = item.bar.children[0]
    compare(thumb.y, 0)
    item.list.contentY = 900   // the end
    compare(thumb.y, item.bar.height - thumb.height)
    verify(thumb.height >= item.bar.minThumb)
  }
}
