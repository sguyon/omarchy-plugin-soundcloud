import QtQuick
import "SoundCloudModel.js" as SoundCloudModel

// The popup's own shortcuts (see SoundCloudModel.keyAction): next/previous,
// volume, like, repeat, shuffle, pasted links and queue moves. It sits first in
// the key catcher's Keys.forwardTo, which runs before the catcher's own handler,
// so keys the catcher would turn into moves (l, h, Shift+arrows) are taken here.
// A recognised key is accepted; held keys repeat only for the volume and queue
// moves. No Quickshell imports so it can be tested with Qt Quick Test.
Item {
  id: root

  // False while a text field has focus.
  property bool active: true

  signal triggered(string action)

  Keys.onPressed: function(event) {
    event.accepted = false
    if (!active) return
    var action = SoundCloudModel.keyAction(event.key, event.modifiers, event.text)
    if (action === "") return
    event.accepted = true
    if (action === "ignore") return
    if (event.isAutoRepeat && !SoundCloudModel.actionRepeats(action)) return
    triggered(action)
  }
}
