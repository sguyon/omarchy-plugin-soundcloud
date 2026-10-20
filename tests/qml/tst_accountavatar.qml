import QtQuick
import QtTest
import "../.."

TestCase {
  id: testCase
  name: "AccountAvatar"
  width: 200
  height: 100
  visible: true
  when: windowShown

  // A 4x4 red png.
  readonly property string png: "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAEElEQVR4nGP4z8AARwzEcQCukw/x0F8jngAAAABJRU5ErkJggg=="

  Component {
    id: factory
    AccountAvatar {
      width: 28
      height: 28
    }
  }

  function make(props) {
    var item = createTemporaryObject(factory, testCase, props || ({}))
    verify(item !== null)
    return item
  }

  function test_without_an_image_it_shows_the_glyph() {
    var item = make()
    compare(item.hasImage, false)
    compare(item.glyph, "\uDB80\uDC04")
  }

  function test_a_usable_data_url_loads() {
    var item = make({ source: png })
    tryCompare(item, "hasImage", true)
  }

  function test_other_sources_fall_back_to_the_glyph() {
    var item = make({ source: "https://example.com/a.png" })
    wait(100)
    compare(item.hasImage, false)
    item.source = png
    tryCompare(item, "hasImage", true)
    item.source = ""
    tryCompare(item, "hasImage", false)
  }

  function test_click_emits_clicked() {
    var item = make()
    var spy = createTemporaryObject(spyFactory, testCase, { target: item, signalName: "clicked" })
    mouseClick(item)
    compare(spy.count, 1)
  }

  function test_hover_is_reported_while_the_pointer_is_on_it() {
    var item = make()
    item.x = 20
    item.y = 20
    mouseMove(testCase, 150, 80)
    compare(item.hovered, false)
    mouseMove(item, 14, 14)
    tryCompare(item, "hovered", true)
    mouseMove(testCase, 150, 80)
    tryCompare(item, "hovered", false)
  }

  Component {
    id: spyFactory
    SignalSpy {}
  }
}
