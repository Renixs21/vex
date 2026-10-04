import QtQuick

// A note-type pill: neutral metal with a small dot in the type's graph colour (the only colour is the dot).
Rectangle {
    id: root
    property string label: ""
    property color dot: "#b9bec6"
    implicitWidth: row.implicitWidth + 22
    implicitHeight: 26
    radius: height / 2
    color: "#15171a"
    border.width: 1
    border.color: Theme.buttonOutline
    Row {
        id: row
        anchors.centerIn: parent
        spacing: 7
        Rectangle { width: 8; height: 8; radius: 4; color: root.dot; anchors.verticalCenter: parent.verticalCenter }
        Text { text: root.label; color: Theme.secondary; font.family: Theme.sans; font.pixelSize: 12; font.weight: Font.Medium
               font.capitalization: Font.AllUppercase; font.letterSpacing: 1.2; renderType: Text.NativeRendering }
    }
}
