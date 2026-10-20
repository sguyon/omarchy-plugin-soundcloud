import QtQuick
import "SoundCloudModel.js" as SoundCloudModel

// Counts the key-repeat events of a held seek key (Left, Right) for the
// website's "max(5, n) seconds" rule. It never accepts a key, so it can sit in
// another item's Keys.forwardTo, which runs before that item's own handler:
// the count is current when the handler's signal arrives. A fresh press or
// the release of the key resets it. No Quickshell imports so it can be tested
// with Qt Quick Test.
Item {
  id: root

  property int repeats: 0

  Keys.onPressed: function(event) {
    if (SoundCloudModel.isSeekKey(event.key))
      repeats = SoundCloudModel.seekRepeatCount(repeats, event.isAutoRepeat)
    event.accepted = false
  }

  Keys.onReleased: function(event) {
    if (!event.isAutoRepeat && SoundCloudModel.isSeekKey(event.key)) repeats = 0
    event.accepted = false
  }
}
