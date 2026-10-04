import QtQuick

// A modal sheet over the screen (the app's own dialogs: edit a note, "forget this?"). Esc closes it.
FocusScope {
    id: root
    property string title: ""
    property real sheetWidth: 560
    property real sheetHeight: 360
    default property alias body: holder.data
    signal closed()
    anchors.fill: parent
    visible: opacity > 0
    opacity: 0
    z: 50
    function open() { opacity = 1; forceActiveFocus() }
    function close() { opacity = 0; closed() }
    Behavior on opacity { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
    Keys.onEscapePressed: close()

    Rectangle { anchors.fill: parent; color: Qt.rgba(0.04, 0.045, 0.05, 0.78)
                MouseArea { anchors.fill: parent; onClicked: {} } }
    Panel {
        id: sheet
        width: Math.min(root.sheetWidth, root.width - 48)
        height: Math.min(root.sheetHeight, root.height - 48)
        anchors.centerIn: parent
        anchors.verticalCenterOffset: (1 - root.opacity) * 16
        padding: 24
        SectionLabel { id: head; text: root.title }
        Item { id: holder; anchors { top: head.bottom; topMargin: 16; left: parent.left; right: parent.right; bottom: parent.bottom } }
    }
}
