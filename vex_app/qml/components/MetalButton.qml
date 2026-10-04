import QtQuick

// A machined button: gradient #2a2e34 -> #1e2126, 1px top highlight, outline #33373e. `chrome: true` = the primary chrome button
// (#f1f3f6 -> #b8bec7, dark text). Keyboard: Tab to focus (visible ring), Space/Enter to press. Min 44 px tall.
Item {
    id: root
    property string text: ""
    property url icon: ""
    property bool chrome: false
    property bool round: false
    property bool checked: false
    property bool enabledLook: enabled
    property bool mono: false
    property int iconSize: 18
    property string accessibleName: text
    signal clicked()

    implicitHeight: Theme.touch
    implicitWidth: round ? Theme.touch : Math.max(Theme.touch, row.implicitWidth + 32)
    activeFocusOnTab: true
    opacity: enabled ? 1 : 0.45
    Accessible.role: Accessible.Button
    Accessible.name: accessibleName
    Keys.onSpacePressed: root.clicked()
    Keys.onReturnPressed: root.clicked()
    Keys.onEnterPressed: root.clicked()

    Rectangle {
        id: face
        anchors.fill: parent
        radius: root.round ? height / 2 : Theme.buttonRadius
        border.width: 1
        border.color: root.chrome ? "#d6dae0" : Theme.buttonOutline
        gradient: Gradient {
            GradientStop { position: 0; color: root.chrome ? (mouse.pressed ? "#d9dde3" : Theme.chromeTop)
                                                        : (mouse.pressed ? "#23272c" : (mouse.containsMouse || root.checked ? "#30343b" : Theme.buttonTop)) }
            GradientStop { position: 1; color: root.chrome ? (mouse.pressed ? "#a7adb6" : Theme.chromeBottom)
                                                        : (mouse.pressed ? "#191b1f" : Theme.buttonBottom) }
        }
        Behavior on opacity { NumberAnimation { duration: Theme.motion; easing.type: Theme.easing } }
        Rectangle {                                      // 1px top highlight
            x: face.radius * 0.6; width: parent.width - 2 * x; y: 1; height: 1
            color: root.chrome ? Qt.rgba(1, 1, 1, 0.8) : Qt.rgba(1, 1, 1, 0.07)
        }
    }
    Row {
        id: row
        anchors.centerIn: parent
        spacing: 8
        Image {
            visible: root.icon != ""
            source: root.icon
            width: root.iconSize; height: root.iconSize
            sourceSize: Qt.size(root.iconSize * 2, root.iconSize * 2)
            anchors.verticalCenter: parent.verticalCenter
            opacity: root.chrome ? 0.0 : 0.95
        }
        SText {
            visible: root.text !== ""
            text: root.text
            mono: root.mono
            color: root.chrome ? Theme.chromeText : Theme.text
            font.pixelSize: 14
            font.weight: root.chrome ? Font.DemiBold : Font.Medium
            anchors.verticalCenter: parent.verticalCenter
        }
    }
    Rectangle {                                          // visible focus ring
        anchors.fill: parent
        anchors.margins: -3
        radius: face.radius + 3
        color: "transparent"
        border.width: 2
        border.color: Theme.focus
        opacity: root.activeFocus ? 0.75 : 0
        Behavior on opacity { NumberAnimation { duration: 140 } }
    }
    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: { root.forceActiveFocus(); root.clicked() }
    }
}
