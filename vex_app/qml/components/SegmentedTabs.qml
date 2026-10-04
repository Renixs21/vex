import QtQuick

// Machined segmented tabs. Left/Right arrows move between tabs when focused.
Rectangle {
    id: root
    property var tabs: []
    property int current: 0
    property int minTab: 88
    signal picked(int index)
    implicitHeight: Theme.touch
    implicitWidth: row.implicitWidth + 8
    radius: Theme.buttonRadius + 2
    color: "#121417"
    border.width: 1
    border.color: Theme.outline
    activeFocusOnTab: true
    Accessible.role: Accessible.PageTabList
    Keys.onLeftPressed: if (current > 0) picked(current - 1)
    Keys.onRightPressed: if (current < tabs.length - 1) picked(current + 1)

    Row {
        id: row
        anchors.centerIn: parent
        spacing: 2
        Repeater {
            model: root.tabs
            delegate: Rectangle {
                required property var modelData
                required property int index
                readonly property bool on: index === root.current
                width: Math.max(root.minTab, t.implicitWidth + 28)
                height: Theme.touch - 8
                radius: Theme.buttonRadius
                border.width: on ? 1 : 0
                border.color: Theme.buttonOutline
                gradient: Gradient {
                    GradientStop { position: 0; color: on ? Theme.buttonTop : "transparent" }
                    GradientStop { position: 1; color: on ? Theme.buttonBottom : "transparent" }
                }
                Accessible.role: Accessible.PageTab
                Accessible.name: modelData
                Text { id: t; anchors.centerIn: parent; text: modelData; font.family: Theme.sans; font.pixelSize: 14
                       font.weight: on ? Font.Medium : Font.Normal; color: on ? Theme.text : Theme.secondary
                       renderType: Text.NativeRendering }
                MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: { root.forceActiveFocus(); root.picked(index) } }
            }
        }
    }
    Rectangle { anchors.fill: parent; anchors.margins: -3; radius: parent.radius + 3; color: "transparent"; border.width: 2
                border.color: Theme.focus; opacity: root.activeFocus ? 0.75 : 0 }
}
