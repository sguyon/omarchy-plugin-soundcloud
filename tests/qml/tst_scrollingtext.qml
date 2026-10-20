import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "ScrollingText"
  width: 400
  height: 100
  visible: true
  when: windowShown

  Component {
    id: factory
    ScrollingText {
      width: 100
      font.pixelSize: 14
      speed: 100
      pauseMs: 200
    }
  }

  function make(text) {
    var item = createTemporaryObject(factory, testCase, { text: text })
    verify(item !== null)
    return item
  }

  function test_short_text_does_not_scroll() {
    var item = make("Hi")
    compare(item.overflowing, false)
    item.advance(1000)
    compare(item.scrollX, 0)
  }

  function test_long_text_holds_then_scrolls_to_the_end_and_back() {
    var item = make("A very long track title that cannot fit in one hundred pixels")
    verify(item.overflowing)
    verify(item.maxScroll > 0)
    item.advance(100)
    compare(item.scrollX, 0)          // still holding at the start
    item.advance(100)
    item.advance(100)                 // hold over, moves 10 px
    verify(item.scrollX > 0)
    item.advance(100000)              // far past the end
    compare(item.scrollX, item.maxScroll)
    compare(item.direction, -1)
    item.advance(200)                 // holds at the end
    item.advance(100)
    verify(item.scrollX < item.maxScroll)
    item.advance(100000)
    compare(item.scrollX, 0)
    compare(item.direction, 1)
  }

  function test_new_text_restarts_at_the_beginning() {
    var item = make("A very long track title that cannot fit in one hundred pixels")
    item.advance(200)
    item.advance(500)
    verify(item.scrollX > 0)
    item.text = "Another very long track title that cannot fit in one hundred pixels"
    compare(item.scrollX, 0)
  }

  // The artist line: smaller, not bold, in a wider area, same rules as the title.
  Component {
    id: artistFactory
    ScrollingText {
      width: 200
      font.pixelSize: 11
      color: "gray"
    }
  }

  function test_short_artist_looks_like_plain_text() {
    var item = createTemporaryObject(artistFactory, testCase, { text: "Burial" })
    verify(item !== null)
    compare(item.overflowing, false)
    compare(item.cursorShape, Qt.ArrowCursor)
    compare(item.scrollX, 0)
    compare(item.implicitHeight, item.children[0].contentItem.children[0].implicitHeight)
    item.advance(100000)
    compare(item.scrollX, 0)
  }

  function test_long_artist_scrolls_and_shows_a_hand() {
    var item = createTemporaryObject(artistFactory, testCase,
      { text: "An extremely long collective name, featuring many guests and their friends" })
    verify(item !== null)
    verify(item.overflowing)
    compare(item.cursorShape, Qt.PointingHandCursor)
    item.advance(1500)
    item.advance(1000)
    verify(item.scrollX > 0)
  }
}
