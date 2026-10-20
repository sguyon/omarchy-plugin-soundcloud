import QtQuick
import QtQuick.Controls
import "SoundCloudModel.js" as SoundCloudModel

// The filter field over the track list: a magnifier, the text, the result count
// ("2 of 24") and a clear button. It is transparent; the owner draws Omarchy's
// text field surface behind it (and its border insets around it) and the
// tooltip on countItem. Keys while it has focus: Esc clears the text, and on an
// empty field closes it (closeRequested); Down, Tab and Enter ask for the list
// (listRequested), Enter on release so a held key cannot go on to play a row;
// everything else, Ctrl+V included, is the text field's own. No Quickshell
// imports so it can be tested with Qt Quick Test.
Item {
  id: root

  property alias text: field.text
  property int matchCount: 0
  property int totalCount: 0
  property string placeholderText: "Filter tracks"
  property string fontFamily: "sans"
  property real fontSize: 13
  property real captionSize: 10
  property real iconSize: 14
  property real horizontalPadding: 10
  property real verticalPadding: 7
  property color foreground: "white"
  property color dimColor: "gray"
  property color selectionColor: "#664488ff"
  property color hoverColor: "#33ffffff"
  property color pressedColor: "#55ffffff"

  readonly property bool fieldFocused: field.activeFocus
  readonly property alias countItem: countLabel
  readonly property bool countHovered: countHover.hovered
  readonly property string countText: SoundCloudModel.filterCountText(field.text, matchCount, totalCount)

  signal closeRequested()
  signal listRequested()

  implicitHeight: Math.ceil(field.contentHeight) + verticalPadding * 2

  function focusField() {
    field.forceActiveFocus()
    field.selectAll()
  }

  // What Esc (and the clear button) does: clear first, close when already empty.
  function clearOrClose() {
    if (field.text !== "") field.text = ""
    else closeRequested()
  }

  Text {
    id: magnifier
    anchors.left: parent.left
    anchors.leftMargin: root.horizontalPadding
    anchors.verticalCenter: parent.verticalCenter
    textFormat: Text.PlainText
    text: "󰍉"
    color: root.dimColor
    font.family: root.fontFamily
    font.pixelSize: root.iconSize
  }

  Text {
    id: countLabel
    anchors.right: clearButton.left
    anchors.rightMargin: 6
    anchors.verticalCenter: parent.verticalCenter
    textFormat: Text.PlainText
    text: root.countText
    color: root.dimColor
    font.family: root.fontFamily
    font.pixelSize: root.captionSize

    HoverHandler { id: countHover }
  }

  Item {
    id: clearButton
    anchors.right: parent.right
    anchors.rightMargin: Math.max(2, root.horizontalPadding / 2)
    anchors.verticalCenter: parent.verticalCenter
    width: Math.max(root.iconSize + 8, root.height - 6)
    height: width

    Rectangle {
      anchors.fill: parent
      radius: 3
      color: clearArea.held ? root.pressedColor : root.hoverColor
      visible: clearArea.hot
    }

    Text {
      anchors.centerIn: parent
      textFormat: Text.PlainText
      text: "󰅖"
      color: root.foreground
      font.family: root.fontFamily
      font.pixelSize: root.iconSize
    }

    Interactive {
      id: clearArea
      anchors.fill: parent
      onClicked: {
        var wasEmpty = field.text === ""
        root.clearOrClose()
        if (!wasEmpty) field.forceActiveFocus()
      }
    }
  }

  TextField {
    id: field
    anchors.left: magnifier.right
    anchors.leftMargin: 6
    anchors.right: countLabel.left
    anchors.rightMargin: 6
    anchors.verticalCenter: parent.verticalCenter
    leftPadding: 0
    rightPadding: 0
    topPadding: 0
    bottomPadding: 0
    background: Item {}
    placeholderText: root.placeholderText
    placeholderTextColor: root.dimColor
    color: root.foreground
    selectionColor: root.selectionColor
    selectedTextColor: root.foreground
    font.family: root.fontFamily
    font.pixelSize: root.fontSize
    maximumLength: SoundCloudModel.filterMaxChars
    selectByMouse: true

    property bool enterDown: false

    Keys.onPressed: function(event) {
      if (event.key === Qt.Key_Escape) {
        root.clearOrClose()
        event.accepted = true
      } else if (event.key === Qt.Key_Down || event.key === Qt.Key_Tab) {
        root.listRequested()
        event.accepted = true
      } else if (event.key === Qt.Key_Backtab) {
        event.accepted = true
      } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
        enterDown = true
        event.accepted = true
      }
    }

    Keys.onReleased: function(event) {
      if ((event.key === Qt.Key_Return || event.key === Qt.Key_Enter) && !event.isAutoRepeat && enterDown) {
        enterDown = false
        root.listRequested()
        event.accepted = true
      }
    }
  }
}
